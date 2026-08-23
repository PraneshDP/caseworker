# CaseWorkers — Automated Caseworker Assistant with Risk-Gated Guardrails

> **Core Invariant:** The Risk Classifier and Effect Registry are structurally separate from the planning agent. The AI proposes actions; deterministic policy code and human-in-the-loop gates govern them. The system never trusts an LLM to grade the riskiness of its own proposed actions.

---

## 🚀 Quick Start (Clean Clone → Running Demo)

```bash
# 1. Clone
git clone <repo-url> && cd CaseWorkers

# 2. Create virtual environment
python3 -m venv .venv && source .venv/bin/activate

# 3. Install
pip install -e ".[dev]"

# 4. Configure
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY (optional: system has deterministic offline fallbacks)

# 5. Start the Resident History API (Background Service)
python services/history_service.py --port 8083 &

# 6. Run the Morning Workflow
python -m src.main run-morning
```

*Zero external database or Docker containers required. Runs entirely on standard Python 3.9+.*

---

## 🕹️ CLI Commands

```bash
# 1. Run morning sequence in interactive HITL mode (CLI approval prompts)
python -m src.main run-morning

# 2. Run morning sequence in automated demo mode (auto-approves scored gates)
python -m src.main run-morning --auto-approve

# 3. Launch the Caseworker Web Console Dashboard (UI)
python -m src.main serve --host 127.0.0.1 --port 8000

# 4. Verify cryptographic tamper evidence of the audit ledger
python -m src.main verify-chain data/runs/run_*.jsonl

# 5. Search policy knowledge base via hybrid BM25 + dense retrieval
python -m src.main search "SNAP eligibility income"

# 6. Verify structural guardrails and task reachability
python -m src.main verify-guardrails

# 7. Print the authoritative assistant capability statement
python -m src.main capability

# 8. List registered tasks in execution order
python -m src.main list-tasks

# 9. Run full automated test suite (51 tests)
pytest tests/ -v
```

---

## 🏛️ System Architecture

```
  Overnight Queue (12 referrals) 
               │
               ▼
      [ Injection Screening ]  ── (Quarantines instruction-override attempts)
               │
               ▼
      [ Sequential Task Pipeline ]
        1. Read Referral (s.2.1)
        2. Retrieve Resident History (s.2.2) (from API :8083)
        3. Categorise Referral & Routing (s.2.3)
        4. Draft Triage Note (s.2.4) OR Hand-off to Caseworker (s.3.9 / ACA-2026/2)
        5. Flag for Human Attention (s.2.6)
        6. Escalate Out-of-Authority Requests to Supervisor (s.2.7 / Section 4)
        7. Record Referral Triaged (s.2.5)
               │
               ▼
      [ Deterministic 4-Layer Risk Classifier ]
        • Layer 1: Unknown Action Kind (fails closed)
        • Layer 2: Authority Policy ACA-2026/1 Restriction (s.3.1-s.3.9)
        • Layer 3: Mandatory Review Signals (prompt injection, unverified citation)
        • Layer 4: Weighted Risk Score (τ = 0.40)
               │
        ┌──────┴──────┐
        ▼             ▼
  [ Clear (< 0.4) ] [ Gated (HITL Review Gate) ]
        │             │
        │             ├──► Approve (Caseworker accepts)
        │             ├──► Edit (Caseworker amends payload)
        │             └──► Reject (Caseworker rejects, recorded in ledger)
        ▼
  [ Effect Registry ] ── (Strictly refuses to bind callables to Section 3 restricted kinds)
        │
        ▼
  [ SHA-256 Chained Audit Ledger ] ── (Cryptographically verifiable append-only log)
```

---

## 🛡️ Authority Policy & Guardrails

1. **Default-Deny Model (Section 6.1)**: If it is unclear whether an action falls within Section 3, it is treated as though it does.
2. **Restricted Actions (Section 3)**:
   - `s.3.1`: Change entitlement / award amount / eligibility status
   - `s.3.2`: Suspend, terminate, or reinstate an award
   - `s.3.3`: Initiate, alter, or cancel a payment
   - `s.3.4`: Change payment details / bank info
   - `s.3.5`: Send communications to residents / third parties
   - `s.3.6`: Disclose resident data externally
   - `s.3.7`: Assert findings of fact on fraud / conduct
   - `s.3.8`: Irreversible actions
   - `s.3.9` *(ACA-2026/2)*: Drafting triage notes for households with minors (<18).
3. **Safeguarding Hand-offs (ACA-2026/2)**: Cases involving minors bypass draft note generation and create structured caseworker hand-off packets in `data/handoffs/<run_id>/index.md`.
4. **Supervisor Escalations (Section 4)**: Out-of-authority requests (e.g. `RF-2026-0415` suspension) are declined, logged, and compiled into `data/escalations/<run_id>/index.md`.

---

## 📂 Key Files & Structure

| File / Directory | Purpose |
|---|---|
| `src/orchestrator.py` | Morning routine coordinator & task execution loop |
| `src/policy/authority.py` | Dynamic Authority Policy engine & quote verifier |
| `src/risk/classifier.py` | Multi-layer deterministic risk gate (τ = 0.40) |
| `src/effects/registry.py` | Effect registry with strict restriction enforcement |
| `src/tasks/` | Pluggable tasks: `read`, `history`, `assess`, `draft`, `handoff`, `flag`, `escalate`, `record` |
| `src/security/injection.py` | Regex & heuristic prompt injection screening |
| `src/rag/` | Structure-aware chunker, BM25 + dense hybrid retriever, and citation verifier |
| `src/audit/log.py` | Append-only ledger with SHA-256 hash chaining |
| `src/api/` & `web/` | Web Console dashboard, SSE event streaming, and HITL gate UI |
| `services/history_service.py` | Mock Resident History API service (port 8083) |
| `data/policy/` | Policy markdown (`authority-policy.md`) and rules data |
| `data/referrals/` | 12 overnight referrals queue (`referral-queue.json`) |
| `DECISIONS.md` | Architectural decision records, rationale, and Day 2 amendment response |
| `AI-USAGE.md` | AI tooling disclosure |

---

## 🧪 Testing

Run the full test suite covering unit, integration, RAG, injection defense, risk gating, and audit chaining:

```bash
pytest tests/ -v
```

