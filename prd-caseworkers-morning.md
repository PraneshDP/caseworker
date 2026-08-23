# PRD — The Caseworker's Morning
### Agentic AI Assistant with Risk-Gated Guardrails · Brite Spark 2026 · Problem 5

> **Important assumption flag:** I don't have your individual Problem 5 document or data pack — only the one-paragraph summary from the participant handbook. This PRD is built on reasonable interpretation of that summary, structured so you can drop in the real specifics (exact systems, exact click-sequence, exact "floor" list, exact data pack schema) without restructuring anything. Treat every bracketed `[ASSUMED]` as a slot to fill once you've read the real doc.

---

## 1. Problem framing

A caseworker at a benefits/public-service office starts every day with ~40 minutes of repetitive, low-judgment navigation across systems: pulling the day's caseload, checking flags, cross-referencing eligibility rules, drafting routine communications, updating case status. `[ASSUMED]` The exact sequence and systems will be defined in your problem doc's data pack.

**The core design tension, not the plumbing, is what's being judged:** an agent that does real work autonomously, but that has a well-reasoned, structurally enforced line it will not cross without a human. Everything below is built around making that line legible, testable, and *not solely trusted to the LLM's own judgment.*

---

## 2. Goals

- **G1** — Fully automate the reversible, low-judgment 80% of the morning routine.
- **G2** — Any action that is irreversible, high-impact, or low-confidence is intercepted **before execution** and routed to the caseworker for approval, with a plain-language reason.
- **G3** — Every eligibility- or policy-relevant claim the agent makes is grounded in a cited source clause — not asserted from model memory.
- **G4** — The system survives an unknown day-two requirement change by *extension*, not rewrite.
- **G5** — The system is resilient to adversarial or malformed inputs, including prompt injection embedded in case data.

## 3. Non-goals `[ASSUMED — replace with your doc's "Not required" section]`

- Production-grade multi-tenant auth / SSO
- A polished front end — Problem 5 is not the interface-scored track (that's Problem 4). CLI or a thin single-page status view is enough.
- Real integrations with real case-management systems — mock/synthetic APIs only, per handbook (no real person's data, no paid licenses).
- Horizontal scaling, k8s, message queues — this is a 1-person, 2-day build. Save the enterprise patterns for the actual job.

---

## 4. The floor (must be true before anything else earns credit)

| ID | Requirement |
|---|---|
| F1 | Agent executes the full morning sequence end-to-end against synthetic case data, unattended, for the reversible steps |
| F2 | At least one class of action is classified as irreversible and **structurally blocked** pending human approval — not just prompted to "ask if unsure" |
| F3 | Every policy/eligibility claim in agent output carries a citation to a specific clause in the knowledge base |
| F4 | System runs from a clean clone following only the README |
| F5 | Full audit trail: every action the agent proposed, whether it executed or was gated, and why |
| F6 | DECISIONS.md and AI-USAGE.md started day one, updated as you go |

Nothing above this line is optional. Nothing below it counts until this line is fully met.

---

## 5. System architecture

```
                         ┌─────────────────────────┐
                         │   Morning Trigger        │
                         │  (CLI: "run morning")    │
                         └────────────┬─────────────┘
                                      ▼
                      ┌───────────────────────────────┐
                      │   Orchestrator (LangGraph)     │
                      │   loads Task Registry           │
                      └───────────────┬───────────────┘
                                      ▼
            ┌─────────────────────────────────────────────────┐
            │  For each case in queue → Task pipeline:          │
            │                                                    │
            │   [Task 1] → [Task 2] → [Task N]  (pluggable)     │
            │      each Task = {plan, risk_score, execute}      │
            └───────────────┬───────────────────────────────────┘
                            ▼
                 ┌─────────────────────┐
                 │  Injection Screen    │  ← untrusted content quarantine
                 └──────────┬───────────┘
                            ▼
                 ┌─────────────────────┐
                 │  Policy RAG Tool      │  ← hybrid retrieval + rerank
                 │  (grounds any claim)  │
                 └──────────┬───────────┘
                            ▼
                 ┌─────────────────────┐
                 │  Risk Classifier      │  ← separate from planning agent
                 │  (deterministic +     │     (no self-grading)
                 │   rules-based floor)  │
                 └──────────┬───────────┘
                     risk < τ │  risk ≥ τ  OR  in hard-block list
                            ▼             ▼
                   ┌────────────┐   ┌──────────────────┐
                   │ Auto-Execute│   │  HITL Approval Gate│
                   │ + log        │   │  (CLI prompt)      │
                   └────────────┘   └──────────┬─────────┘
                                                ▼
                                     approve / reject / edit
                                                ▼
                                     ┌────────────────────┐
                                     │  Audit Log (append-  │
                                     │  only) + Briefing    │
                                     └────────────────────┘
```

**Why this shape wins on the rubric:** the Task Registry is the single point where a day-two requirement change lands — a new/modified Task, not a rewritten orchestrator. The Risk Classifier is deliberately a *separate* component from the planning agent, because letting an LLM grade the riskiness of its own proposed action is a known failure mode (motivated reasoning toward "this is fine, proceed").

---

## 6. Functional requirements

### FR1 — Case Queue & Triage
- Load the day's caseload from synthetic seed data (JSON/CSV — matches your data pack format once known).
- Sort/flag by staleness, missing documents, upcoming deadlines.

### FR2 — Task Registry (extensibility backbone)
- Each morning-routine step is a self-contained `Task`:
  ```
  Task {
    id: str
    description: str
    input_schema: Schema
    plan(context) -> ProposedAction
    risk_profile: RiskProfile        # static hints, not the final say
    execute(action) -> Result
  }
  ```
- Orchestrator discovers tasks from a registry (`tasks/registry.py`), not a hardcoded script.
- **Day-two change response plan:** whatever the twist is, the response is "add or edit one Task file" — not touch the orchestrator, the risk classifier, or other tasks. State this explicitly in DECISIONS.md when the change lands; that sentence is worth real rubric points.

### FR3 — Policy Knowledge Base (RAG)
- Ingests a policy/procedure manual (synthetic) as the grounding source for any eligibility or procedural claim the agent makes.
- **Chunking:** structure-aware, not fixed-window. Parse heading hierarchy (e.g., `3.2.1 Verification Requirements`) via regex/markdown structure detection. Chunk within leaf sections at ~350–450 tokens, 15% overlap. Attach metadata: `{doc_id, section_path, clause_id, effective_date, chunk_index}`.
- **Hybrid retrieval:** BM25 (sparse, via Postgres `tsvector`/`ts_rank` or `rank_bm25`) + dense vector (pgvector, cosine) run in parallel over the same chunk store.
- **Fusion:** Reciprocal Rank Fusion, not naive score averaging (different retrievers have incomparable score scales):
  ```
  RRF(d) = Σ_r  1 / (k + rank_r(d))     where k = 60 (standard default)
  ```
- **Rerank:** take top ~20–30 fused candidates → score with an open-source cross-encoder (`BAAI/bge-reranker-base` or `cross-encoder/ms-marco-MiniLM-L-6-v2`, both free/local) → keep top 3–5.
- **Citation verification:** before a claim is emitted, run a lightweight entailment check — does the cited chunk actually support the claim? (embedding similarity threshold or a small verifier LLM call). If it fails, the agent must say "I don't know, here's who to ask" rather than assert. This is the difference between "has RAG" and "doesn't hallucinate with RAG."

### FR4 — Risk Classification (separate from the planning agent)
- Two layers, deliberately redundant:
  1. **Deterministic hard-block list** — action types that are *always* gated regardless of model output (e.g., benefit termination, payment amount change, application denial, any outbound legal/official notice). This is a static allow/deny list, not a prompt instruction — it cannot be talked out of it.
  2. **Scored risk model** for everything else:
     ```
     risk_score = w1·reversibility + w2·scope_of_impact
                + w3·financial_impact + w4·(1 − agent_confidence)
     ```
     Threshold `τ` (e.g., 0.4) routes to HITL. Weights and τ live in DECISIONS.md as an explicit, justified choice — judges reward "here's why 0.4."

### FR5 — HITL Approval Gate
- Blocking checkpoint: proposed action, plain-language reason, supporting citation (if policy-relevant), and an approve/reject/edit prompt.
- Rejections and edits are logged with the caseworker's stated reason — this becomes training signal you can point to even if you don't build the feedback loop.

### FR6 — Injection Screening (see §8) — runs on all case data, uploaded documents, and applicant-authored text *before* it reaches planning context.

### FR7 — Audit Log & Morning Briefing
- Append-only log of every proposed action, its risk classification, and its resolution.
- End-of-run output: a short plain-language briefing ("6 cases processed, 4 auto-completed, 2 awaiting your review, 1 flagged for possible injected content").

### FR8 — Interface `[low priority — not scored on this track]`
- CLI is fully sufficient. If time remains on day two only, a thin read-only status page is a "nice to have," not a target.

---

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR1 | No paid software licenses. Postgres+pgvector, open-source rerankers/embeddings, `rank_bm25` — all free. LLM API calls are explicitly allowed per the handbook's "use your keys" clause; that's a separate rule from the licensing one. |
| NFR2 | All action-execution paths go through typed, schema-validated tool calls — never raw text-to-action. |
| NFR3 | Retrieved/tool/case content is never concatenated into the system prompt as instructions — always wrapped and tagged as data (see §8). |
| NFR4 | Full clean-clone reproducibility: seed data + migrations + one README command sequence. |
| NFR5 | Every non-happy-path input (empty case, malformed field, conflicting policy versions) degrades to a logged, explainable state — never a silent failure or an unguarded exception that halts the whole run. |

---

## 8. Security & prompt-injection defense (layered)

Case notes, uploaded documents, and applicant-authored messages are the highest-risk input surface — they're attacker-controlled text that flows into agent context. Defense in depth:

1. **Trust-boundary tagging.** All external content wrapped in explicit delimiters (`<untrusted_case_data>...</untrusted_case_data>`) with a standing system instruction that content inside these tags is data, never instructions, and cannot override system/developer instructions — reinforced right before each injection, not just once at the top.
2. **Injection heuristic screen.** Lightweight regex + classifier pass over incoming case text before it enters planning context (patterns like "ignore previous instructions," role-reassignment phrasing, embedded system-prompt-style text). Flagged content is quarantined — surfaced to the caseworker in the briefing, not silently fed to the planner.
3. **Structured tool calling only.** The agent cannot execute free text as an action. Every action must validate against the Task Registry's typed schema; anything else is rejected and logged, not attempted.
4. **Least-privilege scoping.** Each tool call is scoped to only the single case in context — no ambient access to the full case store.
5. **Statistical anomaly check.** Compare each proposed action against the expected action distribution for its Task type. Wildly out-of-distribution proposals force HITL regardless of the self-reported risk score — a second, independent check that doesn't rely on the model correctly flagging itself.
6. **Forensic logging.** Raw untrusted input is logged alongside the agent's derived action, so any incident is replayable end-to-end.

The throughline worth stating explicitly in your write-up: **the system never trusts a single layer to catch everything, and never trusts the agent to grade its own risk.**

---

## 9. Data model (Postgres + pgvector)

```
caseworkers(id, name, role)
cases(id, applicant_ref, program_type, status, last_activity_at)
case_events(id, case_id, event_type, payload jsonb, created_at)        -- timeline

policy_documents(id, title, version, effective_date)
policy_chunks(id, doc_id, section_path, clause_id, content, 
              embedding vector(384), tsv tsvector, chunk_index, active bool)

agent_runs(id, caseworker_id, run_date, status, summary)
proposed_actions(id, run_id, case_id, action_type, payload jsonb,
                  risk_score float, hard_blocked bool, status text,
                  reasoning text, citations jsonb, created_at, resolved_at)

injection_flags(id, run_id, source, content_snippet, detector, created_at)
```

---

## 10. Two-day build sequencing

**Day 1 AM** — repo scaffold, DB + migrations, synthetic seed data, Task Registry skeleton with 2–3 trivial tasks, orchestrator loop with no RAG yet. Get *something* running end-to-end before lunch, per the handbook's own advice.

**Day 1 PM** — RAG pipeline (chunking → hybrid retrieval → rerank → citation check), risk classifier v1 (hard-block list + scored model), HITL CLI gate, audit logging wired in.

**Day 1 EOD** — full pipeline runs clean-clone-to-briefing on synthetic data. Everything after this point is hardening, not new surface area.

**Day 2 AM** — the twist lands. Response is scoped to the Task Registry extension point by design; this is where the architecture either pays off or doesn't.

**Day 2 PM** — injection-screen hardening, edge-case sweep (empty case, conflicting policy versions, malformed input), DECISIONS.md and AI-USAGE.md finished (not reconstructed), final clean-clone test.

---

## 11. Deliverable checklist (from the handbook — don't let this slip)

- [ ] Running demo, clean clone, README-only startup
- [ ] Real commit history across both days
- [ ] DECISIONS.md — written as you go, names what was cut and why
- [ ] AI-USAGE.md — what AI tooling did what
- [ ] Every "floor" item in §4 genuinely met before touching stretch goals

---

## 12. Open questions to resolve once you have the real problem doc

- Exact click-sequence / systems the caseworker touches — reshapes the Task list in §6/FR2.
- The actual "floor" and "not required" sections — may tighten or loosen §4/§3.
- Data pack schema for cases and the policy manual — reshapes §9 field names, not the architecture.
- Whether the policy-manual/RAG component is explicitly expected or is your value-add — if the latter, flag it as innovation (10% weight) in DECISIONS.md rather than assuming it's floor-level.
