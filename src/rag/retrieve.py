from __future__ import annotations
"""Hybrid retrieval — BM25 + dense + Reciprocal Rank Fusion.

Two retrievers (sparse and dense) run in parallel over the same chunk store.
Results are fused using RRF, which handles incomparable score scales correctly.
"""

import re
from dataclasses import dataclass

import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from src.rag.ingest import PolicyChunk


def _normalise_clause(text: str) -> str:
    """Normalise a clause reference for comparison.

    "Section 4.2.1", "sec 4.2.1", "4.2.1" and "§4.2.1" all mean the same clause
    to a caseworker, so they must mean the same clause to the verifier.
    """
    if not text:
        return ""
    cleaned = str(text).strip().lower()
    cleaned = re.sub(r"^(section|sec\.?|clause|policy|§)\s*", "", cleaned)
    cleaned = re.sub(r"[\s]+", " ", cleaned).strip(" .:,;")
    return cleaned


@dataclass
class RetrievalResult:
    """A single retrieval result with its chunk and score."""
    chunk: PolicyChunk
    rrf_score: float
    bm25_rank: int | None = None
    dense_rank: int | None = None


class HybridRetriever:
    """Hybrid BM25 + dense retriever with Reciprocal Rank Fusion."""

    def __init__(
        self,
        chunks: list[PolicyChunk],
        collection,  # ChromaDB collection
        model: SentenceTransformer,
        top_k_per_retriever: int = 20,
        final_top_k: int = 5,
        rrf_k: int = 60,
    ):
        self.chunks = chunks
        self.collection = collection
        self.model = model
        self.top_k_per_retriever = top_k_per_retriever
        self.final_top_k = final_top_k
        self.rrf_k = rrf_k

        # Build BM25 index
        tokenized_corpus = [c.content.lower().split() for c in chunks]
        self.bm25 = BM25Okapi(tokenized_corpus)

        # Build chunk_id -> chunk lookup
        self._chunk_by_id: dict[str, PolicyChunk] = {c.chunk_id: c for c in chunks}

    def retrieve(self, query: str) -> list[RetrievalResult]:
        """Retrieve the most relevant policy chunks for a query.

        Runs BM25 (sparse) and ChromaDB (dense) in parallel,
        then fuses with Reciprocal Rank Fusion.
        """
        # BM25 retrieval
        bm25_results = self._bm25_retrieve(query)

        # Dense retrieval
        dense_results = self._dense_retrieve(query)

        # Reciprocal Rank Fusion
        fused = self._rrf_fuse(bm25_results, dense_results)

        return fused[:self.final_top_k]

    def find_by_clause(self, clause_ref: str) -> list[PolicyChunk]:
        """Look up chunks by the clause id or heading a planner claims to cite.

        Citation verification needs this: without it, the only way to "verify" a
        citation is to find the chunk that best matches the claim, which cannot
        fail and therefore verifies nothing. Resolving the clause the model
        actually named is what makes a fabricated section number detectable.
        """
        needle = _normalise_clause(clause_ref)
        if not needle:
            return []

        exact = [c for c in self.chunks if _normalise_clause(c.clause_id) == needle]
        if exact:
            return exact

        prefix = [
            c for c in self.chunks
            if _normalise_clause(c.clause_id).startswith(needle + ".")
        ]
        if prefix:
            return prefix

        haystack_hits = [
            c for c in self.chunks
            if needle in _normalise_clause(c.heading)
            or needle in _normalise_clause(c.section_path)
        ]
        return haystack_hits

    def _bm25_retrieve(self, query: str) -> list[tuple[str, int]]:
        """Retrieve via BM25. Returns [(chunk_id, rank), ...]."""
        tokenized_query = query.lower().split()
        scores = self.bm25.get_scores(tokenized_query)

        # Get top-k indices
        top_indices = np.argsort(scores)[::-1][:self.top_k_per_retriever]

        results = []
        for rank, idx in enumerate(top_indices):
            if scores[idx] > 0:  # Only include non-zero scores
                chunk = self.chunks[idx]
                results.append((chunk.chunk_id, rank))

        return results

    def _dense_retrieve(self, query: str) -> list[tuple[str, int]]:
        """Retrieve via ChromaDB dense embeddings. Returns [(chunk_id, rank), ...]."""
        query_embedding = self.model.encode([query], show_progress_bar=False).tolist()

        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=self.top_k_per_retriever,
        )

        ranked = []
        if results and results["ids"]:
            for rank, chunk_id in enumerate(results["ids"][0]):
                ranked.append((chunk_id, rank))

        return ranked

    def _rrf_fuse(
        self,
        bm25_results: list[tuple[str, int]],
        dense_results: list[tuple[str, int]],
    ) -> list[RetrievalResult]:
        """Reciprocal Rank Fusion.

        RRF(d) = Σ_r 1/(k + rank_r(d))

        where k = 60 (standard default). This correctly handles incomparable
        score scales between BM25 and dense retrievers.
        """
        rrf_scores: dict[str, float] = {}
        bm25_ranks: dict[str, int] = {}
        dense_ranks: dict[str, int] = {}

        for chunk_id, rank in bm25_results:
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0) + 1.0 / (self.rrf_k + rank)
            bm25_ranks[chunk_id] = rank

        for chunk_id, rank in dense_results:
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0) + 1.0 / (self.rrf_k + rank)
            dense_ranks[chunk_id] = rank

        # Sort by RRF score descending
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)

        results = []
        for chunk_id in sorted_ids:
            chunk = self._chunk_by_id.get(chunk_id)
            if chunk:
                results.append(RetrievalResult(
                    chunk=chunk,
                    rrf_score=rrf_scores[chunk_id],
                    bm25_rank=bm25_ranks.get(chunk_id),
                    dense_rank=dense_ranks.get(chunk_id),
                ))

        return results
