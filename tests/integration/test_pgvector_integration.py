"""Real PostgreSQL + pgvector integration tests (Phase 4.2 Part 6).

Uses the REAL pgvector store backed by PostgreSQL — nothing is mocked.
Verifies the full vector pipeline and tenant isolation, plus persistence
across store recreation ("restart").
"""
from __future__ import annotations

import hashlib

import pytest

from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
from aegisforge.rag.vector_store import PgVectorStore, VectorStoreEntry

from .conftest import requires_infra


def _bow_embedding(text: str, dimension: int = 384) -> list[float]:
    """Bag-of-words style embedding: token overlap drives cosine similarity.

    Hash-based deterministic embeddings are not semantically meaningful, so
    similarity-ordering assertions need a real (if crude) semantic signal.
    """
    import math
    import re

    vec = [0.0] * dimension
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        idx = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dimension
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


@pytest.fixture()
def store(pg_session_factory):
    """A fresh PgVectorStore against real Postgres (isolated org + ids)."""
    from sqlalchemy import text

    # Reset the shared table so re-runs never collide on primary keys.
    session = pg_session_factory()
    session.execute(text("TRUNCATE TABLE vector_embeddings"))
    session.commit()
    session.close()

    s = PgVectorStore(db_session_factory=pg_session_factory, dimension=384)
    yield s


def _truncate_vectors(pg_session_factory) -> None:
    from sqlalchemy import text

    session = pg_session_factory()
    session.execute(text("TRUNCATE TABLE vector_embeddings"))
    session.commit()
    session.close()


def _entries(doc_id: str, texts: list[str], provider) -> list[VectorStoreEntry]:
    embeddings = provider.embed_texts(texts)
    return [
        VectorStoreEntry(
            id=f"{doc_id}-c{i}",
            content=text,
            embedding=emb,
            metadata={"document_id": doc_id, "source": f"{doc_id}.md"},
        )
        for i, (text, emb) in enumerate(zip(texts, embeddings))
    ]


@requires_infra
class TestPgVectorStore:
    def test_insert_and_count(self, store, pg_session_factory):
        org = "org-it-1"
        provider = DeterministicEmbeddingProvider(dimension=384)
        entries = _entries(
            "doc-a",
            [
                "AI agents are software entities that act autonomously.",
                "LangChain is a framework for building LLM applications.",
            ],
            provider,
        )
        store.add(entries, organization_id=org)
        assert store.count(organization_id=org) == 2
        assert store.count() >= 2

    def test_similarity_search_and_top_k(self, store, pg_session_factory):
        org = "org-it-2"
        entries = [
            VectorStoreEntry(
                id=f"doc-b-c{i}",
                content=text,
                embedding=_bow_embedding(text),
                metadata={"document_id": "doc-b", "source": "doc-b.md"},
            )
            for i, text in enumerate(
                [
                    "PostgreSQL is a relational database with vector support via pgvector.",
                    "Redis is an in-memory data structure store used as a queue.",
                    "Docker containers package applications with their dependencies.",
                ]
            )
        ]
        store.add(entries, organization_id=org)

        query_emb = _bow_embedding("PostgreSQL vector database")
        results = store.search(query_emb, top_k=2, organization_id=org, similarity_threshold=0.0)
        # top-k respected
        assert len(results) <= 2
        assert results[0].id == "doc-b-c0"

    def test_similarity_threshold_filters(self, store, pg_session_factory):
        org = "org-it-3"
        provider = DeterministicEmbeddingProvider(dimension=384)
        entries = _entries(
            "doc-c",
            ["Distinct unrelated content about weather patterns in Antarctica."],
            provider,
        )
        store.add(entries, organization_id=org)

        query_emb = provider.embed_text("weather patterns in Antarctica")
        results = store.search(
            query_emb, top_k=5, organization_id=org, similarity_threshold=0.99
        )
        # A threshold of 0.99 must filter out the unrelated entry
        assert len(results) == 0

        loose = store.search(
            query_emb, top_k=5, organization_id=org, similarity_threshold=-1.0
        )
        assert len(loose) == 1

    def test_metadata_filter(self, store, pg_session_factory):
        org = "org-it-4"
        provider = DeterministicEmbeddingProvider(dimension=384)
        store.add(
            [
                VectorStoreEntry(
                    id="doc-d-c0",
                    content="Alpha content",
                    embedding=provider.embed_text("alpha"),
                    metadata={"document_id": "doc-d", "source": "alpha.md"},
                ),
                VectorStoreEntry(
                    id="doc-d-c1",
                    content="Beta content",
                    embedding=provider.embed_text("beta"),
                    metadata={"document_id": "doc-d", "source": "beta.md"},
                ),
            ],
            organization_id=org,
        )
        query_emb = provider.embed_text("content")
        results = store.search(
            query_emb,
            top_k=5,
            organization_id=org,
            metadata_filter={"source": "alpha.md"},
            similarity_threshold=-1.0,
        )
        assert len(results) == 1
        assert results[0].metadata["source"] == "alpha.md"

    def test_organization_isolation(self, store, pg_session_factory):
        provider = DeterministicEmbeddingProvider(dimension=384)
        store.add(
            _entries("doc-secret", ["Secret org-1 confidential data"], provider),
            organization_id="org-a",
        )
        # org-b must not see org-a's vectors
        query_emb = provider.embed_text("Secret confidential data")
        results = store.search(query_emb, top_k=5, organization_id="org-b", similarity_threshold=-1.0)
        assert results == []
        # org-a sees them
        results_a = store.search(query_emb, top_k=5, organization_id="org-a", similarity_threshold=-1.0)
        assert len(results_a) == 1

    def test_delete_by_document(self, store, pg_session_factory):
        org = "org-it-5"
        provider = DeterministicEmbeddingProvider(dimension=384)
        store.add(_entries("doc-e", ["One", "Two", "Three"], provider), organization_id=org)
        assert store.count(organization_id=org) == 3
        deleted = store.delete_by_document("doc-e", organization_id=org)
        assert deleted == 3
        assert store.count(organization_id=org) == 0

    def test_persistence_after_restart(self, pg_session_factory):
        """A NEW store instance (process restart) still retrieves data."""
        _truncate_vectors(pg_session_factory)
        org = "org-it-6"
        provider = DeterministicEmbeddingProvider(dimension=384)
        store1 = PgVectorStore(db_session_factory=pg_session_factory, dimension=384)
        store1.add(
            _entries("doc-f", ["Persistent vector content that survives restarts"], provider),
            organization_id=org,
        )
        del store1

        # Simulated restart: fresh store instance, same database
        store2 = PgVectorStore(db_session_factory=pg_session_factory, dimension=384)
        query_emb = provider.embed_text("Persistent vector content")
        results = store2.search(query_emb, top_k=3, organization_id=org, similarity_threshold=-1.0)
        assert len(results) == 1
        assert results[0].id == "doc-f-c0"


@requires_infra
class TestProductionSelection:
    def test_pgvector_selected_for_postgres_config(self):
        """Production configuration must select PgVectorStore, not in-memory."""
        from aegisforge.rag.vector_store import get_vector_store

        store = get_vector_store(
            "pgvector",
            db_session_factory=lambda: None,
            dimension=384,
        )
        assert isinstance(store, PgVectorStore)

    def test_pgvector_not_silently_falls_back(self, pg_session_factory):
        """PgVectorStore must raise (fail clearly) if initialization fails."""
        # A broken session factory must raise RuntimeError, not silently pass
        store = PgVectorStore(db_session_factory=lambda: None, dimension=384)
        with pytest.raises(RuntimeError):
            store.add([VectorStoreEntry(id="x", content="x", embedding=[0.0] * 384)], organization_id="o")