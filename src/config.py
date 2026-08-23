from __future__ import annotations
"""Configuration loaded from .env with pydantic-settings.

Every tunable lives here. Nothing in the guardrail path reads a magic number from
its own module — the threshold and all weights are auditable in one place and
overridable per environment.

One deliberate exception: the authority boundary itself is NOT configurable.
Which actions require a supervisor comes from `data/policy/authority-rules.json`,
projected from the Department's own policy document, and the paths below point at
those files rather than restating what they contain. A weight in this file can
change how loudly the system worries about something; it cannot make a section 3
action performable.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings — loaded from the .env file at project root."""

    # --- LLM & NLP ---------------------------------------------------------
    groq_api_key: str = Field(default="", description="Groq API key for ultra-fast multilingual NLP inference")
    groq_model: str = Field("openai/gpt-oss-120b", description="Groq model name")
    gemini_api_key: str = Field(default="", description="Google Gemini API key")
    gemini_model: str = Field("gemini-3.6-flash", description="Gemini model name")
    llm_timeout_seconds: float = Field(30.0, description="Per-request LLM timeout")
    llm_max_retries: int = Field(2, description="Retries after the first attempt")
    llm_retry_backoff_seconds: float = Field(1.5, description="Base for exponential backoff")
    llm_temperature: float = Field(
        0.2,
        description="Low by default: the model writes a caseworker-facing narrative, "
                    "not creative prose.",
    )

    # --- RAG ---------------------------------------------------------------
    rag_enabled: bool = Field(
        True,
        description="Ground policy claims via retrieval. Falls back to a stdlib "
                    "lexical index if chromadb/sentence-transformers are absent, so a "
                    "clean clone still runs.",
    )
    chroma_persist_dir: str = Field("data/chromadb", description="ChromaDB persistence directory")
    embedding_model: str = Field("all-MiniLM-L6-v2", description="Sentence-transformers model")
    embedding_offline_first: bool = Field(
        True,
        description="Try the local HuggingFace cache before the network. Removes a "
                    "hard network dependency from every run.",
    )
    chunk_size_tokens: int = Field(400, description="Target chunk size in tokens")
    chunk_overlap_fraction: float = Field(0.15, description="Chunk overlap as fraction")
    retrieval_top_k: int = Field(20, description="Top-K per retriever before fusion")
    final_top_k: int = Field(5, description="Top-K after RRF fusion")
    rrf_k: int = Field(60, description="RRF constant k")

    # --- Risk: threshold ---------------------------------------------------
    risk_threshold: float = Field(0.4, description="Risk score threshold tau for the HITL gate")

    # --- Risk: deterministic base weights (never model-influenced) --------
    weight_reversibility: float = Field(0.35)
    weight_scope: float = Field(0.25)
    weight_financial: float = Field(0.25)
    # Applied to (1 - confidence). Raise-only: high confidence contributes 0.
    weight_confidence: float = Field(0.15)

    # --- Risk: per-action signal weights (raise-only) ---------------------
    signal_weight_irreversible: float = Field(0.30)
    signal_weight_adverse: float = Field(0.30)
    signal_weight_eligibility: float = Field(0.15)
    signal_weight_authority_restricted: float = Field(
        0.40,
        description="A section 3 action is gated by the authority layer before the "
                    "score is consulted; this weight only affects how a *related* "
                    "restricted matter colours a permitted action's score.",
    )
    signal_weight_data_incomplete: float = Field(0.20)
    signal_weight_unverified_citation: float = Field(0.25)
    signal_weight_injection: float = Field(0.35)
    signal_weight_escalation: float = Field(0.30)
    signal_weight_financial_moderate: float = Field(0.10)
    signal_weight_financial_high: float = Field(0.25)
    financial_moderate_amount: float = Field(500.0, description="£ for a moderate bump")
    financial_high_amount: float = Field(2_000.0, description="£ for a high bump")

    # --- Citation ----------------------------------------------------------
    citation_similarity_threshold: float = Field(
        0.5, description="Min cosine similarity for a citation to count as verified",
    )

    # --- Paths: policy (the authority boundary) ----------------------------
    policy_rules_path: str = Field(
        "data/policy/authority-rules.json",
        description="Machine-readable projection of the authority policy. The rules "
                    "engine refuses to load if a rule's quote is not verbatim in the "
                    "source document below.",
    )
    policy_document_path: str = Field(
        "data/policy/authority-policy.md",
        description="The Department's policy document, committed verbatim.",
    )

    # --- Paths: inputs -----------------------------------------------------
    referral_queue_path: str = Field("data/referrals/referral-queue.json")

    # --- Paths: outputs ----------------------------------------------------
    runs_dir: str = Field("data/runs")
    log_dir: str = Field("data/logs")
    artifacts_dir: str = Field("data/artifacts", description="Drafted notes, per run")
    escalations_dir: str = Field(
        "data/escalations", description="Section 4 escalation packets, per run",
    )
    triage_record_path: str = Field(
        "data/logs/triage-record.jsonl",
        description="s.2.5 — that a referral was read and triaged. Not a case change.",
    )
    flag_path: str = Field(
        "data/logs/flags.jsonl", description="s.2.6 — flags raised for human attention",
    )

    # --- Resident history service -----------------------------------------
    history_api_url: str = Field(
        "http://127.0.0.1:8083",
        description="services/history_service.py. Start it before a run; if it is "
                    "down the run still completes on the local snapshot.",
    )
    history_timeout_seconds: float = Field(5.0)
    history_retries: int = Field(2, description="Retries after the first attempt")
    history_retry_backoff_seconds: float = Field(0.25)
    history_snapshot_path: str = Field(
        "services/_history_data.json",
        description="Fallback snapshot. Used only when the API is unreachable, and "
                    "every note says so — a degraded source is declared, not hidden.",
    )
    history_allow_snapshot_fallback: bool = Field(
        True,
        description="Set false to make an unreachable history service a hard failure "
                    "instead of a declared degradation.",
    )

    # --- Observability -----------------------------------------------------
    log_level_file: str = Field("DEBUG")
    log_level_console: str = Field("WARNING")
    audit_hash_chain: bool = Field(
        True, description="Chain each audit entry to the previous one's hash (tamper evidence)",
    )

    # --- Server ------------------------------------------------------------
    host: str = Field("127.0.0.1", description="Bind address for the web console")
    port: int = Field(8000, description="Port for the web console")

    @field_validator("risk_threshold")
    @classmethod
    def _threshold_in_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError(f"risk_threshold must be in (0, 1]; got {v}")
        return v

    @field_validator("history_retries")
    @classmethod
    def _retries_not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"history_retries must be >= 0; got {v}")
        return v

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings from the .env file. Cached after the first call."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. For tests that mutate the environment."""
    get_settings.cache_clear()
