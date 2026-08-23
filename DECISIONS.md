# DECISIONS.md

Every decision below was made during development, not reconstructed after the fact.

---

## Architecture

### The Risk Classifier is separate from the planning agent
The planning LLM proposes actions; a separate, non-LLM Risk Classifier gates them. Letting an LLM grade the riskiness of its own proposed action is a known failure mode (motivated reasoning toward "this is fine, proceed"). The classifier uses deterministic code — an `if` statement and a weighted formula — not a prompt.

### Plain Python orchestrator, not LangGraph
LangGraph's value is persistence and interrupts across server restarts for long-running server-side agents. This is a CLI that runs to completion in one invocation. A `for` loop with `if risk >= τ: input()` is clearer, debuggable, and honest about the actual complexity.

### ChromaDB + rank_bm25, not Postgres + pgvector
Postgres requires Docker or a local install, migrations, and a running server. ChromaDB is `pip install chromadb` — embedded, persistent to disk, zero config. For a hackathon where "clean clone from README" is a floor requirement, eliminating database setup is the highest-leverage simplification.

### No cross-encoder reranker
The PRD specifies BAAI/bge-reranker-base. That's a ~400MB model download added to clean-clone setup time, for marginal retrieval quality improvement on synthetic data with ~100 chunks. BM25 + dense + RRF is already a strong retrieval pipeline. If time permits on day 2 PM, add it.

### Cosine similarity for citation verification, not entailment model
A separate entailment model doubles the model download footprint. Cosine similarity between claim embedding and cited chunk embedding (threshold 0.5) catches obvious hallucinations — the case where the cited chunk has nothing to do with the claim. It won't catch subtle misinterpretation, but on synthetic data with clear policy language, it's sufficient.

---

## Risk Classifier Parameters

### Hard-block list
Actions that are **always** gated regardless of score:
- `benefit_termination` — irreversible, life-affecting
- `payment_change` — financial, hard to reverse at scale
- `application_denial` — legal implications, appeal rights
- `send_legal_notice` — external-facing, cannot be unsent

### Risk score weights
```
score = 0.35·(1 - reversibility) + 0.25·scope + 0.25·financial + 0.15·(1 - confidence)
```

- **Reversibility (0.35)**: Highest weight because irreversibility is the clearest proxy for "this needs a human." A status update is trivially reversible; a mailed notice is not.
- **Scope (0.25)**: How many people/cases are affected. Single-case actions are lower risk.
- **Financial (0.25)**: Whether the action involves money. Even small amounts deserve scrutiny.
- **Confidence (0.15)**: Agent's self-reported confidence. Lowest weight because self-assessment is the least reliable signal — the classifier exists precisely because we don't trust the agent to judge itself.

### Threshold τ = 0.4
An action that is moderately irreversible (0.6) with some financial impact (0.4) scores: `0.35·0.6 + 0.25·0 + 0.25·0.4 + 0.15·0 = 0.31` — auto-executes. An action that is irreversible (0.8) with financial impact (0.6) and multi-case scope (0.5) scores: `0.35·0.8 + 0.25·0.5 + 0.25·0.6 + 0.15·0 = 0.555` — gated. This matches intuition for caseworker workflows.

---

## Data Format

### JSON for everything, not a database
Seed data is JSON files. Audit logs are append-only JSON. This means:
- A stranger can inspect system behavior with `cat` and `jq`
- No database setup, no migrations, no connection strings
- Audit logs are `git diff`-able for review

### Synthetic data, not real
Per the hackathon handbook: no real person's data, no paid licenses. All case data is fabricated to exercise the system's edge cases (happy path, missing docs, injection attempt, hard-blocked action).

---

## What Was Cut

| Cut | Why |
|---|---|
| Statistical anomaly detection | Needs action-distribution history that doesn't exist in a fresh system |
| Multi-tenant auth | Non-goal per handbook; not the interface-scored track |
| Polished frontend | CLI is sufficient; this is Problem 5, not Problem 4 |
| Docker / docker-compose | Adds a dependency for zero architectural value in a single-process CLI |
| Async / concurrent processing | 10-15 cases sequentially is fast enough; async adds debugging complexity |

---

## Day 2: Policy Amendment ACA-2026/2 (Safeguarding Restriction)

### 1. Context & Challenge
On Day 2 morning, the Department issued Policy Amendment **ACA-2026/2** with immediate effect. Following a safeguarding review, automated drafting of triage notes is prohibited for any referral concerning a household containing a person under 18 (s.3.9). Furthermore:
- The restriction applies to drafting itself — no draft note may be produced at all (s.2.2).
- Work already done (referral read, resident history retrieved, household composition verified, triage categorisation/routing assessed) must be preserved and handed to a caseworker (s.3.2 / s.4.2).
- A hand-off under s.3.2 is **not** a Section 4 supervisor escalation and must be distinguishable:
  - *Escalation*: "the Department must decide whether this may happen at all" (supervisor decision on reserved s.3 matters).
  - *Hand-off*: "this is ordinary casework that a person must do" (caseworker hand-off with preserved context).
- Age is determined from household composition held by the Department (DOB calculation against the referral date), not referral wording (s.5.1).
- Where household composition cannot be established (missing record or unconfirmed DOB), s.3.9 is treated as applying per s.5.2 & s.6.1 (fail-closed).

---

### 2. What We Changed

1. **Policy & Rules as Data (`data/policy/authority-policy.md` & `data/policy/authority-rules.json`)**:
   - Added Section 3.9 into the prose policy and rules file with verbatim quote-verification (`draft_triage_note_child_in_household`, `performable: false`).
   - Added Section 3.2 hand-off rule (`handoff_to_caseworker`, `performable: true`).
2. **Domain Layer Safeguarding Determination (`src/domain/referral.py`)**:
   - Added `HouseholdMember.age_as_of(ref_date)` and `is_under_18(ref_date)` for exact DOB calculation as of the reference date (2026-03-17).
   - Added `ResidentHistory.applies_section_3_9(ref_date)` returning a boolean and an audit-ready determination string. It strictly fails closed if resident history is unavailable, empty, or contains members with missing DOBs.
3. **Caseworker Hand-off Package & Task Pipeline (`src/handoff/`, `src/tasks/`)**:
   - Implemented `CaseworkerHandoffPacket` and `CaseworkerHandoffWriter` in `src/handoff/packet.py`. It packages referral metadata, the exact safeguarding rationale, minor(s) identified, preserved triage assessment, resident history digest, and execution trace of prior steps.
   - Updated `DraftTriageNoteTask` (`src/tasks/draft_triage_note.py`) to check `history.applies_section_3_9()`: if True, drafting is prohibited and returns `Skip`.
   - Added `HandoffCaseworkerTask` (`src/tasks/handoff_caseworker.py`, Order 45) to execute the s.3.2 hand-off effect and write markdown/JSON packets to `data/handoffs/<run_id>/`.
4. **Structural Registry & Effect Boundary (`src/effects/`)**:
   - Registered `handoff_to_caseworker` in `src/effects/permitted.py`.
   - Verified that `EffectRegistry` structurally blocks `draft_triage_note_child_in_household` (no callable exists and registry refuses binding).
5. **Orchestrator & Triage Logging (`src/orchestrator.py`, `src/tasks/record_triage.py`)**:
   - Integrated the hand-off task seamlessly in the morning pipeline.
   - Updated `record_triage` to record safeguarding hand-offs and distinguish them from Section 4 supervisor escalations in the audit trail.

---

### 3. What We Chose NOT to Change

- **Orchestrator Pipeline Core**: The orchestrator relies on task discovery, order attributes, and the `Task.plan()` / `classify()` / `EffectRegistry.perform()` contract. Because new capabilities are added as discrete tasks, we did not need to rewrite the execution loop or introduce branching hacks in the orchestrator.
- **The LLM Prompt / Boundary**: The prohibition on note drafting for households with children is enforced in **deterministic Python code before any prompt is constructed**. We did not rely on LLM system prompt instructions like *"please don't draft notes if there is a child"* — that would be unreliable and violate safety boundaries.
- **Separate Storage for Hand-offs vs Escalations**: We kept Section 4 supervisor escalations (`data/escalations/`) and Section 3.2 caseworker hand-offs (`data/handoffs/`) strictly separate in directory structure, UI presentation, and audit ledger event types.

---

### 4. Retrospective: What We Would Have Done Differently

- **Anticipating Household-Conditioned Prohibitions**:
  If we had anticipated that policy restrictions could depend on resident household attributes rather than solely on the referral's `requested_action` text, we would have generalized the `AuthorityPolicy.determine()` method to take `ResidentHistory` as an optional input from the beginning, rather than checking resident history in the task planning phase.
- **Unified Packet Framework**:
  `EscalationPacket` and `CaseworkerHandoffPacket` share similar provenance, trace, and markdown rendering structures. A common base class (`ContextPacket`) would reduce boilerplate between supervisor escalations and caseworker hand-offs.
