"""Tests for the RAG retrieval pipeline.

Verifies that:
1. Policy chunks are ingested correctly
2. Hybrid retrieval returns relevant results
3. Known queries return expected sections
"""

import pytest

from src.rag.ingest import ingest_policy, parse_sections, chunk_text


class TestParseSections:
    """Test markdown section parsing."""

    def test_parses_headings(self):
        md = "# Title\n\nSome content\n\n## Section 1.1\n\nMore content\n"
        sections = parse_sections(md)
        assert len(sections) >= 1

    def test_extracts_clause_ids(self):
        md = "## 2.1 Eligibility\n\nContent about eligibility.\n"
        sections = parse_sections(md)
        assert any(s["clause_id"] == "2.1" for s in sections)

    def test_handles_nested_headings(self):
        md = (
            "# 1. General\n\nTop level\n\n"
            "## 1.1 Sub\n\nSub content\n\n"
            "### 1.1.1 Detail\n\nDetail content\n"
        )
        sections = parse_sections(md)
        assert len(sections) >= 2

    def test_empty_sections_skipped(self):
        md = "## 1.1 Empty\n\n## 1.2 Has Content\n\nActual content here.\n"
        sections = parse_sections(md)
        # Only the section with content should be returned
        assert all(s["content"].strip() for s in sections)


class TestChunking:
    """Test text chunking."""

    def test_short_text_single_chunk(self):
        text = "This is a short text that fits in one chunk."
        chunks = chunk_text(text, max_tokens=400)
        assert len(chunks) == 1

    def test_long_text_multiple_chunks(self):
        text = " ".join(["word"] * 1000)  # ~1000 tokens
        chunks = chunk_text(text, max_tokens=400)
        assert len(chunks) > 1

    def test_chunks_overlap(self):
        text = " ".join([f"word{i}" for i in range(500)])
        chunks = chunk_text(text, max_tokens=200, overlap_fraction=0.15)
        if len(chunks) > 1:
            # Check that consecutive chunks share some words
            words_0 = set(chunks[0].split()[-30:])
            words_1 = set(chunks[1].split()[:30])
            overlap = words_0 & words_1
            assert len(overlap) > 0


class TestIngestion:
    """Test full ingestion pipeline."""

    def test_ingest_policy_manual(self, tmp_path):
        """Ingest the seed policy manual and verify chunks are created."""
        chunks, collection, model = ingest_policy(
            policy_path="data/seed/policy_manual.md",
            chroma_persist_dir=str(tmp_path / "chroma"),
            embedding_model_name="all-MiniLM-L6-v2",
        )
        assert len(chunks) > 0
        assert collection.count() == len(chunks)

    def test_chunks_have_metadata(self, tmp_path):
        """Every chunk should have required metadata fields."""
        chunks, _, _ = ingest_policy(
            policy_path="data/seed/policy_manual.md",
            chroma_persist_dir=str(tmp_path / "chroma"),
        )
        for chunk in chunks:
            assert chunk.chunk_id
            assert chunk.doc_id
            assert chunk.section_path
            assert chunk.clause_id
            assert chunk.content


class TestRetrieval:
    """Test hybrid retrieval (requires ingestion first)."""

    def test_snap_eligibility_query(self, tmp_path):
        """Query about SNAP eligibility should return SNAP sections."""
        from src.rag.retrieve import HybridRetriever

        chunks, collection, model = ingest_policy(
            policy_path="data/seed/policy_manual.md",
            chroma_persist_dir=str(tmp_path / "chroma"),
        )
        retriever = HybridRetriever(
            chunks=chunks, collection=collection, model=model,
            top_k_per_retriever=10, final_top_k=5,
        )

        results = retriever.retrieve("SNAP eligibility income threshold")
        assert len(results) > 0
        # At least one result should be from the SNAP section
        snap_results = [r for r in results if "SNAP" in r.chunk.section_path or "2" in r.chunk.clause_id]
        assert len(snap_results) > 0

    def test_tanf_time_limit_query(self, tmp_path):
        """Query about TANF time limits should return relevant section."""
        from src.rag.retrieve import HybridRetriever

        chunks, collection, model = ingest_policy(
            policy_path="data/seed/policy_manual.md",
            chroma_persist_dir=str(tmp_path / "chroma"),
        )
        retriever = HybridRetriever(
            chunks=chunks, collection=collection, model=model,
            top_k_per_retriever=10, final_top_k=5,
        )

        results = retriever.retrieve("TANF 60 month time limit approaching")
        assert len(results) > 0
        # Should find content about time limits
        content_joined = " ".join(r.chunk.content for r in results)
        assert "60" in content_joined or "time" in content_joined.lower()
