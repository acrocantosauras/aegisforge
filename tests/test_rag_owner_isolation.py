"""WS8 P0 REGRESSION — cross-user RAG retrieval within a shared organization.

Attack that found the P0 (proven LIVE against the Docker stack):

    User A ingests a private document (chunk bound to default-org only).
    User B — same shared default-org, zero knowledge of A's content —
    fishes with broad topic terms ("vault access").  The org-only predicate
    matched A's chunks and B retrieved A's private content at scores far
    above the default threshold.

Fix: retrieval is now org AND owner scoped (owner_id column on
vector_embeddings, user_id threaded through RetrievalQuery →
RetrievalService → vector stores → RAGAgent), mirroring the REST
authorization model (documents/requests are owner-scoped).

These tests attack the boundary — they are not "RAG returns results" tests:

- B's semantic search over A's chunks must return nothing (memory + pgvector
  semantics) even with a 0.0 threshold and B's query taken verbatim from a
  leaked snippet.
- B's lexical search must return nothing.
- B's count/delete calls must not touch or reveal A's chunks.
- Unscoped queries (empty org or empty owner) must read nothing (fail-closed).
- RAGAgent (the agent-mediated path) must apply the same scoping via
  RetrievalQuery.user_id.
- RetrievalService/hybrid adapter must propagate the user scope to the
  underlying store (scope-stripping regression guard).

Evidence type: ADVERSARIAL (UNIT-level execution against the real classes).
"""
from __future__ import annotations

import pytest

from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.rag.vector_store import InMemoryVectorStore, VectorStoreEntry

ORG = "default-org"
OWNER_A = "user-a-1111"
OWNER_B = "user-b-2222"
SECRET_SNIPPET = "Project vault access code is BANANA-FALCON-9931"


def _seed_store(store) -> None:  # type: ignore[no-untyped-def]
    """Seed exactly like the live attack: A ingests via /documents."""
    provider = DeterministicEmbeddingProvider(dimension=64)
    vec = provider.embed_text(SECRET_SNIPPET)
    store.add(
        [
            VectorStoreEntry(
                id="chunk-a-secret",
                content=SECRET_SNIPPET,
                embedding=vec,
                metadata={"document_id": "doc-a", "source": "vault-a.txt"},
            )
        ],
        organization_id=ORG,
        owner_id=OWNER_A,
    )


class TestInMemoryStoreOwnerIsolation:
    """InMemoryVectorStore must enforce org AND owner scope."""

    def setup_method(self) -> None:
        self.provider = DeterministicEmbeddingProvider(dimension=64)
        self.store = InMemoryVectorStore()
        _seed_store(self.store)

    def _vec(self, text: str) -> list[float]:
        return self.provider.embed_text(text)

    def test_b_semantic_search_reads_nothing(self) -> None:
        # B queries with the EXACT leaked phrase (worst case for the defender)
        hits = self.store.search(
            query_embedding=self._vec(SECRET_SNIPPET),
            top_k=5,
            organization_id=ORG,
            owner_id=OWNER_B,
            similarity_threshold=0.0,
        )
        assert hits == []

    def test_owner_retrieves_own_chunk(self) -> None:
        hits = self.store.search(
            query_embedding=self._vec(SECRET_SNIPPET),
            top_k=5,
            organization_id=ORG,
            owner_id=OWNER_A,
            similarity_threshold=0.0,
        )
        assert len(hits) == 1 and SECRET_SNIPPET in hits[0].content

    def test_lexical_search_reads_nothing_for_b(self) -> None:
        assert self.store.lexical_search("vault access", organization_id=ORG, owner_id=OWNER_B) == []
        assert self.store.lexical_search("vault", organization_id=ORG, owner_id=OWNER_A)

    def test_count_cannot_reveal_a_volume_to_b(self) -> None:
        assert self.store.count(ORG, OWNER_B) == 0
        assert self.store.count(ORG, OWNER_A) == 1

    def test_delete_scoped_to_owner(self) -> None:
        assert self.store.delete_by_document("doc-a", ORG, OWNER_B) == 0
        assert len(self.store.search(self._vec("vault"), organization_id=ORG, owner_id=OWNER_A, similarity_threshold=0.0)) == 1
        assert self.store.delete_by_document("doc-a", ORG, OWNER_A) == 1
        assert self.store.count(ORG, OWNER_A) == 0

    def test_unscoped_queries_fail_closed(self) -> None:
        # Empty owner scope must NOT behave like a wildcard.
        assert self.store.search(self._vec("vault"), organization_id=ORG, owner_id="", similarity_threshold=0.0) == []
        # Empty org scope must NOT behave like a wildcard either.
        assert self.store.search(self._vec("vault"), organization_id="", owner_id=OWNER_A, similarity_threshold=0.0) == []

    def test_legacy_org_only_call_still_scopes(self) -> None:
        """Callers not yet passing owner_id must not silently regain access."""
        assert self.store.search(self._vec("vault"), organization_id=ORG, similarity_threshold=0.0) == []


class TestRetrievalServiceScopePropagation:
    """RetrievalService must carry user_id into the store (no stripping)."""

    def test_retrieve_passes_owner_scope(self) -> None:
        from aegisforge.domain.models import RetrievalQuery

        store = InMemoryVectorStore()
        _seed_store(store)
        service = RetrievalService(
            embedding_provider=DeterministicEmbeddingProvider(dimension=64),
            vector_store=store,
        )

        b_query = RetrievalQuery(query="vault access", organization_id=ORG, user_id=OWNER_B, similarity_threshold=0.0)
        assert service.retrieve(b_query) == []

        a_query = RetrievalQuery(query="vault access", organization_id=ORG, user_id=OWNER_A, similarity_threshold=0.0)
        assert len(service.retrieve(a_query)) == 1

    def test_hybrid_adapter_preserves_owner_scope(self) -> None:
        """The hybrid adapter re-queries internally (build_context); it must
        re-attach the user scope, not drop it."""
        from aegisforge.rag.hybrid import build_hybrid_retrieval_adapter

        provider = DeterministicEmbeddingProvider(dimension=64)
        store = InMemoryVectorStore()
        _seed_store(store)
        service = RetrievalService(embedding_provider=provider, vector_store=store)
        adapter = build_hybrid_retrieval_adapter(service, store)

        from aegisforge.domain.models import RetrievalQuery

        b_query = RetrievalQuery(query="vault access", organization_id=ORG, user_id=OWNER_B, similarity_threshold=0.0)
        assert adapter.retrieve(b_query) == []
        # build_context re-runs retrieval with remembered parameters — the
        # owner scope must survive that round-trip.
        assert adapter.build_context() == "" or "BANANA" not in adapter.build_context()


class TestRAGAgentOwnerScoping:
    """Agent-mediated retrieval must scope by context.user_id."""

    @staticmethod
    def _agent() -> object:  # type: ignore[no-untyped-def]
        from aegisforge.agents.rag_agent import RAGAgent

        store = InMemoryVectorStore()
        _seed_store(store)
        return RAGAgent(
            retrieval_service=RetrievalService(
                embedding_provider=DeterministicEmbeddingProvider(dimension=64),
                vector_store=store,
            )
        )

    @staticmethod
    def _context(user_id: str) -> AgentExecutionContext:
        return AgentExecutionContext(
            request_id="req-x",
            user_id=user_id,
            organization_id=ORG,
            permissions=[PermissionSpec(name="knowledge.search", allow=True)],
        )

    def test_b_cannot_retrieve_a_content_via_agent(self) -> None:
        agent = self._agent()
        result = agent.execute(  # type: ignore[attr-defined]
            {"query": "vault access code", "top_k": 5, "similarity_threshold": 0.0},
            self._context(OWNER_B),
        )
        blob = str(result.model_dump())
        assert "BANANA-FALCON-9931" not in blob
        assert result.result.get("context_available") is False  # type: ignore[attr-defined]

    def test_owner_still_gets_own_content(self) -> None:
        agent = self._agent()
        result = agent.execute(  # type: ignore[attr-defined]
            {"query": "vault access code", "top_k": 5, "similarity_threshold": 0.0},
            self._context(OWNER_A),
        )
        assert result.result.get("context_available") is True  # type: ignore[attr-defined]


@pytest.mark.integration
class TestPgVectorOwnerIsolation:
    """Same boundary against real PostgreSQL + pgvector.

    Runs only when AEGISFORGE_INTEGRATION_TESTS=true and Postgres is up
    (the Docker stack qualifies).
    """

    def _store(self):  # type: ignore[no-untyped-def]
        from aegisforge.db.session import get_engine, get_session_factory
        from aegisforge.rag.vector_store import PgVectorStore

        url = os.environ.get("AEGISFORGE_TEST_DATABASE_URL", "")
        if not url:
            pytest.skip("AEGISFORGE_TEST_DATABASE_URL not set")
        get_engine.cache_clear()
        factory = get_session_factory(_settings_for(url))
        return PgVectorStore(db_session_factory=factory, dimension=384)

    def test_owner_isolation(self) -> None:
        store = self._store()
        doc_id = f"doc-it-{uuid4().hex[:8]}"
        # 384 dims: matches the production vector_embeddings table shared
        # with the Docker stack (table already exists at that width).
        provider = DeterministicEmbeddingProvider(dimension=384)
        vec = provider.embed_text(SECRET_SNIPPET)
        store.add(
            [VectorStoreEntry(id=f"{doc_id}-c0", content=SECRET_SNIPPET, embedding=vec, metadata={"document_id": doc_id, "source": "x.txt"})],
            organization_id=ORG,
            owner_id=OWNER_A,
        )
        try:
            b_hits = store.search(vec, top_k=5, organization_id=ORG, owner_id=OWNER_B, similarity_threshold=0.0)
            assert b_hits == []
            a_hits = store.search(vec, top_k=5, organization_id=ORG, owner_id=OWNER_A, similarity_threshold=0.0)
            assert len(a_hits) == 1
            assert store.count(ORG, OWNER_B) == 0
            assert store.count(ORG, OWNER_A) >= 1
        finally:
            store.delete_by_document(doc_id, ORG, OWNER_A)


import os
from uuid import uuid4


def _settings_for(url: str):  # type: ignore[no-untyped-def]
    from aegisforge.config import Settings

    return Settings(database_url=url, environment="test", secret_key="owner-iso-test-secret")
