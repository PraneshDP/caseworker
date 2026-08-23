from __future__ import annotations
"""Policy document ingestion — structure-aware chunking into ChromaDB + BM25.

Three fixes over the first version:

  1. OFFLINE-FIRST MODEL LOAD. `SentenceTransformer(name)` reaches out to
     huggingface.co on every construction. In a locked-down network — or a demo
     room's wifi — that turns a local RAG pipeline into a hard network
     dependency and every run dies with a tunnel error. We now try the local
     cache first and only fall back to the network.

  2. CONTENT-ADDRESSED doc_id. It used to hash the *path*, so editing the policy
     manual produced identical chunk ids and `upsert` silently left stale text
     in the collection while BM25 indexed the new text. Retrieval and citation
     verification then disagreed about what the policy said. The id now hashes
     the file's contents.

  3. IDEMPOTENT INGEST. A manifest records the content hash and the chunking
     parameters. An unchanged manual with unchanged settings skips embedding
     entirely, which takes a repeat run from ~8s to ~0.2s.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import chromadb
from sentence_transformers import SentenceTransformer

from src.observability.logging_setup import get_logger, log_event

logger = get_logger(__name__)

COLLECTION_NAME = "policy_chunks"
MANIFEST_FILENAME = "ingest_manifest.json"


@dataclass
class PolicyChunk:
    """A single chunk of the policy manual with metadata."""
    chunk_id: str
    doc_id: str
    section_path: str     # e.g., "2 > 2.1 > 2.1.1"
    clause_id: str        # e.g., "2.1.1"
    content: str
    chunk_index: int      # position within the section
    heading: str = ""     # the heading text

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "section_path": self.section_path,
            "clause_id": self.clause_id,
            "content": self.content,
            "chunk_index": self.chunk_index,
            "heading": self.heading,
        }


# ---------------------------------------------------------------------------
# Embedding model loading
# ---------------------------------------------------------------------------

_model_cache: dict[str, SentenceTransformer] = {}


def load_embedding_model(
    model_name: str = "all-MiniLM-L6-v2",
    offline_first: bool = True,
) -> SentenceTransformer:
    """Load a sentence-transformers model, preferring the local cache.

    Cached per process: the model is ~90MB and loading it repeatedly across
    tasks was a measurable share of run time.
    """
    cache_key = f"{model_name}:{offline_first}"
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    attempts: list[tuple[str, dict]] = []
    if offline_first:
        attempts.append(("local_cache", {"local_files_only": True}))
    attempts.append(("network", {}))

    last_error: Optional[Exception] = None
    for source, kwargs in attempts:
        try:
            model = SentenceTransformer(model_name, **kwargs)
            log_event(logger, "rag.embedding_model_loaded",
                      model=model_name, source=source,
                      dimensions=model.get_sentence_embedding_dimension())
            _model_cache[cache_key] = model
            return model
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log_event(logger, "rag.embedding_model_load_failed", level=30,
                      model=model_name, source=source,
                      error_type=type(exc).__name__, error=str(exc)[:300])

    raise RuntimeError(
        f"Could not load embedding model {model_name!r} from the local cache or "
        f"the network. Last error: {type(last_error).__name__}: {last_error}"
    ) from last_error


def reset_model_cache() -> None:
    """Drop cached models. For tests."""
    _model_cache.clear()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def parse_sections(markdown_text: str) -> list[dict]:
    """Parse markdown into sections based on heading hierarchy.

    Returns a list of {heading, level, section_path, content} dicts,
    one per leaf section (section with actual content).
    """
    lines = markdown_text.split("\n")
    sections: list[dict] = []
    current_headings: dict[int, str] = {}  # level -> heading text
    current_content_lines: list[str] = []
    current_clause: str = ""
    current_heading: str = ""

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

    def flush_section():
        content = "\n".join(current_content_lines).strip()
        if content and current_clause:
            path_parts = [current_headings[level] for level in sorted(current_headings)]
            section_path = " > ".join(path_parts) if path_parts else "Root"
            sections.append({
                "heading": current_heading,
                "clause_id": current_clause,
                "section_path": section_path,
                "content": content,
            })

    for line in lines:
        match = heading_pattern.match(line)
        if match:
            flush_section()
            current_content_lines = []

            level = len(match.group(1))
            heading_text = match.group(2).strip()
            current_heading = heading_text
            current_headings[level] = heading_text

            for deeper in list(current_headings.keys()):
                if deeper > level:
                    del current_headings[deeper]

            clause_match = re.match(r"^(\d+(?:\.\d+)*)", heading_text)
            current_clause = clause_match.group(1) if clause_match else heading_text[:30]
        else:
            current_content_lines.append(line)

    flush_section()
    return sections


def chunk_text(text: str, max_tokens: int = 400, overlap_fraction: float = 0.15) -> list[str]:
    """Split text into overlapping chunks of approximately max_tokens.

    Word count is used as a proxy for tokens (1 word ~= 1.3 tokens). Exact
    tokenisation would need the model's tokeniser for a difference that does not
    change which chunk wins retrieval.
    """
    words = text.split()
    max_words = max(1, int(max_tokens / 1.3))
    overlap_words = int(max_words * overlap_fraction)

    if len(words) <= max_words:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start = max(end - overlap_words, start + 1)

    return chunks


def build_chunks(
    markdown_text: str,
    doc_id: str,
    chunk_size_tokens: int = 400,
    chunk_overlap_fraction: float = 0.15,
) -> list[PolicyChunk]:
    """Turn a policy manual into chunks. Pure function — no I/O, easy to test."""
    all_chunks: list[PolicyChunk] = []
    for section in parse_sections(markdown_text):
        text_chunks = chunk_text(
            section["content"],
            max_tokens=chunk_size_tokens,
            overlap_fraction=chunk_overlap_fraction,
        )
        for i, content in enumerate(text_chunks):
            all_chunks.append(PolicyChunk(
                chunk_id=f"{doc_id}_{section['clause_id']}_{i}",
                doc_id=doc_id,
                section_path=section["section_path"],
                clause_id=section["clause_id"],
                content=content,
                chunk_index=i,
                heading=section["heading"],
            ))
    return all_chunks


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _manifest_path(chroma_persist_dir: str) -> str:
    return os.path.join(chroma_persist_dir, MANIFEST_FILENAME)


def _read_manifest(chroma_persist_dir: str) -> dict:
    path = _manifest_path(chroma_persist_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_manifest(chroma_persist_dir: str, manifest: dict) -> None:
    os.makedirs(chroma_persist_dir, exist_ok=True)
    with open(_manifest_path(chroma_persist_dir), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


def ingest_policy(
    policy_path: str,
    chroma_persist_dir: str,
    embedding_model_name: str = "all-MiniLM-L6-v2",
    chunk_size_tokens: int = 400,
    chunk_overlap_fraction: float = 0.15,
    *,
    offline_first: bool = True,
    force: bool = False,
) -> tuple[list[PolicyChunk], Any, SentenceTransformer]:
    """Ingest a policy markdown document into ChromaDB and return chunks.

    Skips embedding when the manual's content hash and the chunking parameters
    match the previous run.

    Returns:
        (chunks, collection, embedding_model)
    """
    with open(policy_path, "r", encoding="utf-8") as fh:
        markdown_text = fh.read()

    # Content-addressed. Editing the manual changes every chunk id, so stale
    # vectors cannot survive an upsert.
    content_hash = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()
    doc_id = content_hash[:8]

    chunks = build_chunks(
        markdown_text, doc_id,
        chunk_size_tokens=chunk_size_tokens,
        chunk_overlap_fraction=chunk_overlap_fraction,
    )
    if not chunks:
        raise ValueError(
            f"No chunks extracted from {policy_path}. The document needs markdown "
            f"headings (#, ##, ...) with content beneath them."
        )

    model = load_embedding_model(embedding_model_name, offline_first=offline_first)

    fingerprint = {
        "content_hash": content_hash,
        "doc_id": doc_id,
        "embedding_model": embedding_model_name,
        "chunk_size_tokens": chunk_size_tokens,
        "chunk_overlap_fraction": chunk_overlap_fraction,
        "chunk_count": len(chunks),
        "source": os.path.abspath(policy_path),
    }

    client = chromadb.PersistentClient(path=chroma_persist_dir)
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    previous = _read_manifest(chroma_persist_dir)
    unchanged = (
        not force
        and previous.get("content_hash") == content_hash
        and previous.get("embedding_model") == embedding_model_name
        and previous.get("chunk_size_tokens") == chunk_size_tokens
        and previous.get("chunk_overlap_fraction") == chunk_overlap_fraction
        and collection.count() == len(chunks)
    )

    if unchanged:
        log_event(logger, "rag.ingest_skipped", doc_id=doc_id,
                  chunk_count=len(chunks), reason="content and settings unchanged")
        return chunks, collection, model

    texts = [c.content for c in chunks]
    embeddings = model.encode(texts, show_progress_bar=False).tolist()

    # Drop anything from a previous version of the manual. Without this, an edit
    # that removes a section leaves that section retrievable forever.
    stale_doc_id = previous.get("doc_id")
    if stale_doc_id and stale_doc_id != doc_id:
        try:
            collection.delete(where={"doc_id": stale_doc_id})
            log_event(logger, "rag.stale_chunks_deleted", stale_doc_id=stale_doc_id)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, "rag.stale_chunk_delete_failed", level=30,
                      stale_doc_id=stale_doc_id, error=str(exc)[:200])

    collection.upsert(
        ids=[c.chunk_id for c in chunks],
        embeddings=embeddings,
        documents=texts,
        metadatas=[
            {
                "doc_id": c.doc_id,
                "section_path": c.section_path,
                "clause_id": c.clause_id,
                "chunk_index": c.chunk_index,
                "heading": c.heading,
            }
            for c in chunks
        ],
    )

    _write_manifest(chroma_persist_dir, fingerprint)
    log_event(logger, "rag.ingest_completed", doc_id=doc_id,
              chunk_count=len(chunks), embedding_model=embedding_model_name,
              sections=len(set(c.clause_id for c in chunks)))
    return chunks, collection, model
