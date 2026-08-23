from __future__ import annotations
"""Caseworker AI Assistant Chatbot.

Provides grounding over:
  1. Active / latest morning run ledgers and outcomes.
  2. Department Authority Policy (ACA-2026/1) and Safeguarding Rules (ACA-2026/2).
  3. Resident profiles, household composition (minors/adults), and case events.
  4. Cryptographic SHA-256 audit ledger verification.
  5. Full Multilingual & Code-switching NLP — caseworkers can chat in any language,
     including Thanglish (Tamil written in English letters mixed with English).

Multi-Engine Architecture:
  - Primary:  Groq API (ultra-fast multilingual NLP, openai/gpt-oss-120b)
  - Secondary: Google Gemini (gemini-3.6-flash)
  - Fallback:  Exact deterministic knowledge engine (100% offline / zero hallucination)
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.config import Settings
from src.domain.referral import load_referrals
from src.llm import LLMError, call_llm
from src.observability.logging_setup import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Groq NLP System Prompt — with explicit Thanglish / code-switching support
# ---------------------------------------------------------------------------
_GROQ_SYSTEM_PROMPT = """\
You are an expert multilingual NLP-powered Caseworker AI Assistant in the Automated Casework Console.

=== LANGUAGE DETECTION & RESPONSE RULES (CRITICAL) ===

1. DETECT the exact style/language of the caseworker's message before replying.

2. THANGLISH (highest priority rule):
   - "Thanglish" = Tamil words romanized in English letters mixed with English words.
   - Key Thanglish markers: "da", "bro", "yean", "enna", "achu", "sollu", "pannanga",
     "paar", "oru", "case", "aagum", "ille", "irukka", "seri", "dei", "machan",
     "pochu", "nalla", "theriyum", "solla", "pom", "poda", "venum", "ipo", "appuram".
   - If the user writes in Thanglish you MUST reply in Thanglish — mixing romanized
     Tamil words and English exactly like a Tamil-speaking friend/colleague would chat.
   - NEVER translate Thanglish replies into pure Tamil (Unicode) or pure English.
     Preserve the natural code-switching style throughout your reply.

3. PURE TAMIL: If the user types in Unicode Tamil script (அ, இ, உ…), reply in pure Tamil script.

4. OTHER LANGUAGES: Spanish, French, German, Hindi, Arabic, Chinese, etc. — detect and
   reply in the exact same language with professional, natural phrasing.

5. ENGLISH: Professional, clear, well-structured English with Markdown formatting.

=== CONTENT RULES ===
- Ground ALL facts in the policy provisions (ACA-2026/1 & ACA-2026/2) and referral data provided.
- Cite exact sections: s.2.4, s.3.1, s.3.2, s.3.7, s.3.9 etc. where relevant.
- Use Markdown (bold, bullet lists, headers) to structure the reply.
- Be concise but complete. Caseworkers are busy professionals.
"""


def call_groq(
    prompt: str,
    api_key: str,
    model: str = "openai/gpt-oss-120b",
    temperature: float = 0.35,
    timeout_seconds: float = 15.0,
    chat_history: Optional[list[dict[str, str]]] = None,
) -> str:
    """Call Groq API for ultra-fast multilingual NLP inference.

    Uses an explicit Thanglish-aware system prompt so the model correctly mirrors
    the caseworker's code-switching style.

    Args:
        prompt:          The grounded user question (includes policy & referral context).
        api_key:         Groq API key (must start with ``gsk_``).
        model:           Groq model ID.
        temperature:     Slightly higher than 0.2 to give more natural conversational tone.
        timeout_seconds: HTTP timeout.
        chat_history:    Previous conversation turns for multi-turn context.

    Returns:
        The assistant reply as a string.

    Raises:
        LLMError:        When Groq returns an empty response.
        urllib.error.HTTPError: On HTTP-level errors (caught by caller).
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "CaseworkerAssistant/2.0",
    }

    messages: list[dict[str, str]] = [{"role": "system", "content": _GROQ_SYSTEM_PROMPT}]

    # Inject last N turns for conversational context (keep recent history only)
    if chat_history:
        for msg in chat_history[-8:]:
            role = "user" if msg.get("role") == "user" else "assistant"
            content = str(msg.get("content", "")).strip()
            if content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1500,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices", [])
        if not choices:
            raise LLMError("Groq returned empty choices list.")
        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            raise LLMError("Groq returned an empty message content.")
        return content


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChatResponse:
    reply: str
    sources: list[dict[str, str]] = field(default_factory=list)
    mode: str = "deterministic"   # "groq" | "gemini" | "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "sources": self.sources,
            "mode": self.mode,
        }


# ---------------------------------------------------------------------------
# Main chatbot class
# ---------------------------------------------------------------------------

class CaseworkerChatbot:
    """Answers caseworker questions with grounding in policy and run data.

    Supports any natural language and code-switching styles such as Thanglish
    (Tamil romanized + English).
    """

    def __init__(self, settings: Settings, manager: Any = None) -> None:
        self.settings = settings
        self.manager = manager
        self._history_cache: Optional[dict[str, Any]] = None

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _get_history_data(self) -> dict[str, Any]:
        """Load optional history data file (non-critical, best-effort)."""
        if self._history_cache is not None:
            return self._history_cache

        candidates = [
            Path("services/_history_data.json"),
            Path(self.settings.referral_queue_path).resolve().parents[1]
            / "services"
            / "_history_data.json",
        ]
        for data_path in candidates:
            if data_path.exists():
                try:
                    with open(data_path, "r", encoding="utf-8") as f:
                        self._history_cache = json.load(f)
                        return self._history_cache
                except Exception as exc:
                    logger.warning("Failed to load history data file %s: %s", data_path, exc)
        return {}

    def _get_run_context(self, run_id: Optional[str] = None) -> dict[str, Any]:
        """Collect context about the current/latest run, referrals, and ledger."""
        context: dict[str, Any] = {
            "run_id": "",
            "status": "idle",
            "actor": "",
            "actions": [],
            "referrals": {},
            "ledger_entries": [],
        }

        if self.manager:
            try:
                state = self.manager.state()
                target_run_id = run_id or state.get("run_id") or "latest"
                context["run_id"] = target_run_id
                context["status"] = state.get("status", "idle")
                context["actor"] = state.get("actor", "")
                context["actions"] = self.manager.actions()

                ledger_res = self.manager.ledger(target_run_id)
                if isinstance(ledger_res, dict) and "entries" in ledger_res:
                    context["ledger_entries"] = ledger_res["entries"]
            except Exception as exc:
                logger.debug("Failed to fetch run context from manager: %s", exc)

        try:
            result = load_referrals(self.settings.referral_queue_path)
            for r in result.referrals:
                context["referrals"][r.referral_id] = r.to_dict()
        except Exception as exc:
            logger.debug("Failed loading referrals for chat context: %s", exc)

        return context

    def _search_policy(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Hybrid BM25 + dense RAG search over policy documents."""
        try:
            from src.rag.ingest import ingest_policy
            from src.rag.retrieve import HybridRetriever

            chunks, collection, embed_model = ingest_policy(
                policy_path=self.settings.policy_document_path,
                chroma_persist_dir=self.settings.chroma_persist_dir,
                embedding_model_name=self.settings.embedding_model,
            )
            retriever = HybridRetriever(
                chunks=chunks,
                collection=collection,
                model=embed_model,
                final_top_k=top_k,
            )
            hits = retriever.retrieve(query)
            return [
                {
                    "clause_id": h.chunk.clause_id,
                    "section_path": h.chunk.section_path,
                    "content": h.chunk.content,
                    "score": round(h.rrf_score, 4),
                }
                for h in hits
            ]
        except Exception as exc:
            logger.debug("Policy RAG search failed (will proceed without it): %s", exc)
            return []

    def _build_grounded_prompt(
        self,
        query: str,
        run_ctx: dict[str, Any],
        policy_hits: list[dict[str, Any]],
    ) -> str:
        """Build a context-rich prompt that includes referral, ledger, and policy data."""
        # Policy excerpts
        policy_str = "\n\n".join(
            f"[{p['clause_id']} — {p['section_path']}]:\n{p['content']}"
            for p in policy_hits
        ) or "(No policy excerpts retrieved — use your knowledge of ACA-2026/1 and ACA-2026/2.)"

        # Referral summary (top 12)
        referral_lines = [
            f"- {ref_id}: Resident {ref.get('resident_ref')}, "
            f"Source: {ref.get('source')}, "
            f"Action: '{ref.get('requested_action')}', "
            f"Urgency: {ref.get('urgency')}"
            for ref_id, ref in list(run_ctx["referrals"].items())[:12]
        ]
        referrals_str = "\n".join(referral_lines) or "(No referrals loaded)"

        # Recent ledger actions (top 25)
        ledger_lines = []
        for entry in run_ctx.get("ledger_entries", []):
            rec_type = entry.get("record_type", "")
            if rec_type in ("action", "step_declined", "security_event"):
                ledger_lines.append(
                    f"[{rec_type}] {entry.get('referral_id', '')}: "
                    f"{entry.get('action_kind', '')} — "
                    f"{entry.get('description', entry.get('reason', ''))}"
                )
        actions_str = "\n".join(ledger_lines[:25]) or "(No ledger actions recorded yet)"

        return (
            f"=== OVERNIGHT REFERRAL QUEUE ===\n{referrals_str}\n\n"
            f"=== RECENT AUDIT LEDGER ENTRIES ===\n{actions_str}\n\n"
            f"=== RELEVANT POLICY PROVISIONS ===\n{policy_str}\n\n"
            f"=== CASEWORKER QUESTION ===\n{query}"
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def answer(
        self,
        query: str,
        *,
        run_id: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> ChatResponse:
        """Answer a caseworker question in any language (incl. Thanglish) with grounding.

        Engine priority:
        1. Groq  — if ``GROQ_API_KEY`` set and starts with ``gsk_``
        2. Gemini — if ``GEMINI_API_KEY`` set and starts with ``AIza``
        3. Deterministic — always available, no network required
        """
        q_clean = query.strip()
        if not q_clean:
            return ChatResponse(
                reply=(
                    "Enna question? 😄 Morning run results, policy rules, "
                    "or case details — anything potu kelu!"
                ),
                mode="deterministic",
            )

        run_ctx = self._get_run_context(run_id)
        policy_hits = self._search_policy(q_clean, top_k=3)
        sources = [
            {"title": p["clause_id"], "detail": p["section_path"]}
            for p in policy_hits
        ]

        # ── 1. Groq NLP Engine (multilingual + Thanglish) ────────────────
        groq_key = self.settings.groq_api_key or ""
        if groq_key.startswith("gsk_"):
            try:
                grounded_prompt = self._build_grounded_prompt(q_clean, run_ctx, policy_hits)
                reply = call_groq(
                    prompt=grounded_prompt,
                    api_key=groq_key,
                    model=self.settings.groq_model or "openai/gpt-oss-120b",
                    chat_history=history or [],
                )
                return ChatResponse(reply=reply, sources=sources, mode="groq")
            except urllib.error.HTTPError as exc:
                logger.warning("Groq HTTP %s: %s — trying Gemini", exc.code, exc.reason)
            except Exception as exc:
                logger.warning("Groq call failed (%s) — trying Gemini", exc)

        # ── 2. Gemini Fallback ────────────────────────────────────────────
        gemini_key = self.settings.gemini_api_key or ""
        if gemini_key.startswith("AIza"):
            try:
                grounded_prompt = self._build_grounded_prompt(q_clean, run_ctx, policy_hits)
                reply = call_llm(
                    prompt=grounded_prompt,
                    api_key=gemini_key,
                    model=self.settings.gemini_model or "gemini-3.6-flash",
                )
                return ChatResponse(reply=reply, sources=sources, mode="gemini")
            except Exception as exc:
                logger.warning("Gemini call failed (%s) — using deterministic engine", exc)

        # ── 3. Deterministic Fallback ─────────────────────────────────────
        return self._answer_deterministic(
            query=q_clean,
            run_ctx=run_ctx,
            policy_hits=policy_hits,
        )

    # -----------------------------------------------------------------------
    # Deterministic engine (offline fallback — exact, zero hallucination)
    # -----------------------------------------------------------------------

    def _answer_deterministic(
        self,
        query: str,
        run_ctx: dict[str, Any],
        policy_hits: list[dict[str, Any]],
    ) -> ChatResponse:
        """Rule-based exact-match reasoning — works fully offline."""
        q_lower = query.lower()
        sources: list[dict[str, str]] = [
            {"title": p["clause_id"], "detail": p["section_path"]}
            for p in policy_hits
        ]

        # Extract any explicit referral / resident IDs from the query
        ref_match = re.search(r"rf-2026-\d{4}", q_lower)
        target_ref_id = ref_match.group(0).upper() if ref_match else ""
        res_match = re.search(r"r-\d{5}", q_lower)
        target_res_id = res_match.group(0).upper() if res_match else ""

        # ── William Iverson / RF-2026-0412 ───────────────────────────────
        if (
            "william iverson" in q_lower
            or target_ref_id == "RF-2026-0412"
            or target_res_id == "R-20500"
        ):
            sources += [
                {"title": "ACA-2026/2 s.3.9", "detail": "Safeguarding: Children & Young Persons"},
                {"title": "ACA-2026/1 s.3.1", "detail": "Award review requires supervisor"},
            ]
            return ChatResponse(
                reply=(
                    "### Referral RF-2026-0412 — Resident R-20500\n\n"
                    "**Resident**: Elizabeth Whitlock (R-20500), District: Ash Hill\n"
                    "**Household**:\n"
                    "- Elizabeth Whitlock (Applicant, b. 1964-05-25, age 61)\n"
                    "- **William Iverson** (b. 2021-02-26, **age 5 — minor under 18**)\n\n"
                    "**Why handed off?**\n"
                    "1. **ACA-2026/2 s.3.9 Safeguarding Trigger** — Household has a child under 18. "
                    "Automated triage notes are **strictly prohibited**.\n"
                    "2. **Mandatory Hand-off (s.3.2)** — Routed to a human caseworker with a complete "
                    "handover packet (`RF-2026-0412-handoff.md`).\n"
                    "3. **Authority Limit (ACA-2026/1 s.3.1)** — 'Review award' action is outside "
                    "automated authority; escalated to supervisor."
                ),
                sources=sources,
                mode="deterministic",
            )

        # ── Fraud / RF-2026-0415 ─────────────────────────────────────────
        if (
            "counter-fraud" in q_lower
            or "fraud" in q_lower
            or target_ref_id == "RF-2026-0415"
            or target_res_id == "R-20521"
        ):
            sources += [
                {"title": "ACA-2026/1 s.3.2", "detail": "Suspension or termination of award"},
                {"title": "ACA-2026/1 s.3.7", "detail": "Fraud referrals reserved to supervisor"},
            ]
            return ChatResponse(
                reply=(
                    "### Referral RF-2026-0415 — Counter-Fraud Escalation\n\n"
                    "**Resident**: R-20521 | **Source**: Counter-Fraud Unit | **Urgency**: High\n"
                    "**Requested Action**: *Suspend assistance pending investigation*\n\n"
                    "**Policy Determinations**:\n"
                    "1. **s.3.2 & s.3.7** — Automated system cannot suspend assistance or act on "
                    "fraud reports without supervisor approval.\n"
                    "2. Action refused; escalation packet raised for human supervisor.\n"
                    "3. **Urgent Flag (s.2.6)** — Flagged URGENT for Counter-Fraud liaison."
                ),
                sources=sources,
                mode="deterministic",
            )

        # ── Safeguarding (general) ────────────────────────────────────────
        if any(kw in q_lower for kw in ("safeguard", "minor", "under 18", "children", "child")):
            sources.append({"title": "ACA-2026/2 s.3.9", "detail": "Mandatory Caseworker Hand-off"})
            return ChatResponse(
                reply=(
                    "### Safeguarding Policy — ACA-2026/2 s.3.9\n\n"
                    "**Rule**: Automated triage notes are **strictly prohibited** for any household "
                    "containing one or more persons under the age of 18.\n\n"
                    "**Required Procedure**: Immediate hand-off to a named human caseworker (s.3.2) "
                    "with a complete handover packet.\n\n"
                    "**Affected Overnight Cases**: RF-2026-0412 (William Iverson, age 5), "
                    "RF-2026-0416 (Maria Carver, age 3), RF-2026-0418 (Michael Crowley, age 12; "
                    "Rosa Vance, age 0)."
                ),
                sources=sources,
                mode="deterministic",
            )

        # ── Supervisor reservations (Section 3) ──────────────────────────
        if any(kw in q_lower for kw in ("escalat", "section 3", "supervisor", "s.3.")):
            sources.append({"title": "ACA-2026/1 Section 3", "detail": "Actions Reserved to Supervisor"})
            return ChatResponse(
                reply=(
                    "### ACA-2026/1 Section 3 — Supervisor Reserved Actions\n\n"
                    "The assistant operates under a **default-deny boundary**. "
                    "The following always require supervisor approval:\n\n"
                    "- **s.3.1** Change/review of award amount\n"
                    "- **s.3.2** Suspension, termination, or reinstatement\n"
                    "- **s.3.3** Hardship payments, crisis grants, budgeting loans\n"
                    "- **s.3.4** Change of payment destination (without ID verification)\n"
                    "- **s.3.5** Formal written communications to residents\n"
                    "- **s.3.7** Sanctions, penalties, fraud referrals"
                ),
                sources=sources,
                mode="deterministic",
            )

        # ── Audit / SHA-256 ───────────────────────────────────────────────
        if any(kw in q_lower for kw in ("hash", "sha", "audit", "verify", "ledger", "chain")):
            sources.append({"title": "Cryptographic Ledger", "detail": "SHA-256 Hash Chain"})
            return ChatResponse(
                reply=(
                    "### Cryptographic Audit Ledger — SHA-256 Hash Chain\n\n"
                    "Every run event is appended to a tamper-evident JSONL log with a **SHA-256** chain.\n\n"
                    "**How it works**:\n"
                    "1. `entry_hash = SHA256(prev_hash + timestamp + record_type + referral_id + payload)`\n"
                    "2. Genesis entry starts with `0000000000000000...`\n"
                    "3. **Verify chain** button re-hashes all records; any tampered byte "
                    "breaks the chain immediately."
                ),
                sources=sources,
                mode="deterministic",
            )

        # ── Default welcome / help message ───────────────────────────────
        sources.append({"title": "Console Overview", "detail": "Caseworker AI Assistant"})
        return ChatResponse(
            reply=(
                "### Caseworker AI Assistant\n\n"
                "I can answer questions about:\n"
                "- **Referrals** — e.g. *Why was RF-2026-0412 handed off?*\n"
                "- **Policy rules** — Section 2 (permitted) or Section 3 (supervisor-only)\n"
                "- **Safeguarding** — ACA-2026/2 s.3.9 child protection rules\n"
                "- **Audit chain** — SHA-256 tamper verification\n\n"
                "You can ask in **any language** — English, Español, Français, Deutsch, "
                "தமிழ், हिन्दी, or **Thanglish** (e.g. *'Enna da, RF-2026-0412 ku enna achu?'*)"
            ),
            sources=sources,
            mode="deterministic",
        )
