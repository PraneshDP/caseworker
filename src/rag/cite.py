from __future__ import annotations
"""Citation verification — does the cited clause actually support the claim?

THE BUG THIS FIXES
------------------
The first version took the planner's claim, searched every retrieved chunk for
the one with the highest cosine similarity, and recorded that as the citation.
The clause the model *said* it was citing was discarded.

That check cannot fail. There is always a best match, so `verified` was a
statement about "some retrieved text is topically similar to this sentence" —
not about whether the planner's policy reference was real. A model that
confidently cited a nonexistent "Section 9.9" got a green tick pointing at
Section 2.1.

Now there are two independent checks:

  1. CLAUSE RESOLUTION. The clause the model named is looked up in the policy
     manual. If it does not exist, `clause_matched=False` — a fabricated
     reference, regardless of how similar anything else is.
  2. SEMANTIC SUPPORT. The claim is compared against the resolved clause's text.
     Below threshold means the real clause does not support the claim.

A citation is `verified` only if both hold. Failing either sets the
`unverified_citation` signal, which the classifier treats as mandatory human
review.

WHY COSINE SIMILARITY AND NOT ENTAILMENT
----------------------------------------
Similarity is a weaker check than natural-language inference: it detects
topical mismatch, not logical contradiction. A proper NLI cross-encoder is the
right tool and is a known, documented limitation rather than an oversight — it
would add a second model download and roughly 200ms per claim. The mitigation
is that the human reviewer sees the clause text next to the claim and makes the
final call, which is the whole point of the gate.
"""

from typing import Any, Callable, Optional, Sequence, Union

import numpy as np
from sentence_transformers import SentenceTransformer

from src.observability.logging_setup import get_logger, log_event
from src.rag.retrieve import RetrievalResult, _normalise_clause
from src.tasks.base import Citation

logger = get_logger(__name__)

# A claim is either a bare string or {"claim": ..., "policy_section": ...}.
ClaimInput = Union[str, dict]
ClauseLookup = Callable[[str], Sequence[Any]]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def verify_citation(
    claim: str,
    chunk_content: str,
    model: SentenceTransformer,
    threshold: float = 0.5,
) -> float:
    """Cosine similarity between a claim and a chunk of policy text.

    `threshold` is accepted but not applied here — the caller decides. Keeping
    the comparison out of this function stops two callers from disagreeing about
    what "verified" means.
    """
    if not claim or not chunk_content:
        return 0.0
    embeddings = model.encode([claim, chunk_content], show_progress_bar=False)
    return cosine_similarity(embeddings[0], embeddings[1])


def _split_claim(item: ClaimInput) -> tuple[str, str]:
    """Return (claim_text, requested_clause)."""
    if isinstance(item, dict):
        claim = str(item.get("claim") or item.get("text") or "").strip()
        clause = str(
            item.get("policy_section")
            or item.get("clause")
            or item.get("clause_id")
            or item.get("section")
            or ""
        ).strip()
        return claim, clause
    return str(item or "").strip(), ""


def _resolve_requested_clause(
    requested: str,
    retrieval_results: Sequence[RetrievalResult],
    clause_lookup: Optional[ClauseLookup],
) -> tuple[Optional[Any], bool]:
    """Find the chunk for the clause the planner named.

    Looks in the retrieved set first (cheap, and it is what the planner was
    shown), then in the whole manual via `clause_lookup` — a planner may cite a
    real clause that retrieval did not surface, and that is not a fabrication.

    Returns (chunk, found).
    """
    needle = _normalise_clause(requested)
    if not needle:
        return None, False

    for result in retrieval_results:
        chunk = result.chunk
        if _normalise_clause(chunk.clause_id) == needle:
            return chunk, True

    for result in retrieval_results:
        chunk = result.chunk
        if needle in _normalise_clause(chunk.section_path) or \
           needle in _normalise_clause(chunk.heading):
            return chunk, True

    if clause_lookup is not None:
        matches = clause_lookup(requested)
        if matches:
            return matches[0], True

    return None, False


def build_citations(
    claims: Sequence[ClaimInput],
    retrieval_results: Sequence[RetrievalResult],
    model: SentenceTransformer,
    threshold: float = 0.5,
    clause_lookup: Optional[ClauseLookup] = None,
) -> list[Citation]:
    """Build verified citations, one per claim.

    Args:
        claims: Claim strings, or dicts carrying the clause the model cited.
        retrieval_results: What retrieval showed the planner.
        model: Embedding model for the semantic check.
        threshold: Minimum cosine similarity to count as supported.
        clause_lookup: Resolves a clause reference against the whole manual —
            pass `HybridRetriever.find_by_clause`.

    Returns:
        Citations. Unverified ones are RETAINED, not dropped: the reviewer needs
        to see that a claim was unsupported, and dropping it would make the
        action look better grounded than it is.
    """
    citations: list[Citation] = []

    for item in claims:
        claim_text, requested = _split_claim(item)
        if not claim_text:
            continue

        resolved_chunk, clause_found = _resolve_requested_clause(
            requested, retrieval_results, clause_lookup
        )
        clause_matched = clause_found if requested else True

        if resolved_chunk is not None:
            # Verify against the clause the planner actually named.
            similarity = verify_citation(claim_text, resolved_chunk.content, model, threshold)
            citation = Citation(
                chunk_id=resolved_chunk.chunk_id,
                section_path=resolved_chunk.section_path,
                clause_id=resolved_chunk.clause_id,
                content_snippet=resolved_chunk.content[:200],
                similarity_score=similarity,
                claim=claim_text,
                verified=bool(similarity >= threshold),
                requested_clause=requested,
                clause_matched=True,
            )
        elif retrieval_results:
            # Either the planner named no clause, or it named one that does not
            # exist. Show the closest real policy text so the reviewer can judge,
            # but never mark it verified when the named clause was fabricated.
            best_chunk = retrieval_results[0].chunk
            best_sim = -1.0
            for result in retrieval_results:
                sim = verify_citation(claim_text, result.chunk.content, model, threshold)
                if sim > best_sim:
                    best_sim, best_chunk = sim, result.chunk

            citation = Citation(
                chunk_id=best_chunk.chunk_id,
                section_path=best_chunk.section_path,
                clause_id=best_chunk.clause_id,
                content_snippet=best_chunk.content[:200],
                similarity_score=best_sim,
                claim=claim_text,
                verified=bool(best_sim >= threshold and clause_matched),
                requested_clause=requested,
                clause_matched=clause_matched,
            )
            if requested and not clause_found:
                log_event(logger, "rag.citation_clause_not_found", level=30,
                          requested_clause=requested, claim=claim_text[:160],
                          nearest_clause=best_chunk.clause_id)
        else:
            # Nothing retrieved at all. similarity_score stays None — "not
            # checked" is a different fact from "checked and scored zero".
            citation = Citation(
                chunk_id="",
                section_path="",
                clause_id=requested,
                content_snippet="",
                similarity_score=None,
                claim=claim_text,
                verified=False,
                requested_clause=requested,
                clause_matched=False,
            )
            log_event(logger, "rag.citation_unresolvable", level=30,
                      requested_clause=requested, claim=claim_text[:160],
                      reason="no policy chunks were retrieved")

        citations.append(citation)

    return citations


def is_citation_valid(citation: Citation, threshold: float = 0.5) -> bool:
    """Whether a citation counts as supporting its claim.

    An unchecked citation (`similarity_score is None`) is NOT valid. Treating
    "we could not check" as "it passed" is how unverifiable claims reach a
    caseworker with a green tick.
    """
    if citation.similarity_score is None:
        return False
    if not citation.clause_matched:
        return False
    return citation.similarity_score >= threshold


def any_unverified(citations: Sequence[Citation], threshold: float = 0.5) -> bool:
    """True if any citation fails verification. Drives the risk signal."""
    return any(not is_citation_valid(c, threshold) for c in citations)


def citation_summary(citations: Sequence[Citation], threshold: float = 0.5) -> dict[str, Any]:
    """Counts for the audit entry and the web console."""
    total = len(citations)
    verified = sum(1 for c in citations if is_citation_valid(c, threshold))
    fabricated = sum(1 for c in citations if c.requested_clause and not c.clause_matched)
    unchecked = sum(1 for c in citations if c.similarity_score is None)
    return {
        "total": total,
        "verified": verified,
        "unverified": total - verified,
        "fabricated_references": fabricated,
        "unchecked": unchecked,
    }
