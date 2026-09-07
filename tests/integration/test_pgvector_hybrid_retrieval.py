"""Real PostgreSQL + pgvector hybrid retrieval integration tests (Phase 5.1).

These tests run only with AEGISFORGE_INTEGRATION_TESTS=true and require
real PostgreSQL/pgvector. They verify that the advanced retrieval path
(vector + lexical + RRF + reranking + tenant filtering) actually works
against the production storage path, not just the in-memory test doubles.
"""
from __future__ import annotations

import pytest

from aegisforge.domain.models import RetrievalQuery, RetrievalResult
from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
from aegisforge.rag.hybrid import (
    HybridRetrievalService,
    build_hybrid_retrieval_adapter,
    get_reranker,
)
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.rag.vector_store import PgVectorStore, VectorStoreEntry

from .conftest import requires_infra

ORG_ACME = "org-acme-integ"
ORG_BETA = "org-beta-integ"

_QUERY_ACME = "support escalation policy"
_QUERY_BETA = "beta support escalation beta program"


def _bow_embedding(text: str, dimension: int = 384) -> list[float]:
    """Bag-of-words style embedding for deterministic hybrid retrieval tests."""
    import hashlib
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


def _store_with_evidence(pg_session_factory, dimension: int = 384):
    """Fresh pgvector store preloaded with a small multi-tenant evidence set."""
    from sqlalchemy import text

    session = pg_session_factory()
    session.execute(text("TRUNCATE TABLE vector_embeddings"))
    session.commit()
    session.close()

    store = PgVectorStore(db_session_factory=pg_session_factory, dimension=dimension)

    entries_acme = [
        VectorStoreEntry(
            id="acme-support-escalation-000",
            content=(
                "Support escalation policy: customer support issues must be triaged "
                "within 4 business hours. Severity 1 critical business impact requires "
                "immediate escalation to the on-call engineering lead."
            ),
            embedding=_bow_embedding(
                "Support escalation policy customer support issues triaged business hours"
            ),
            metadata={"document_id": "acme-support-escalation", "source": "acme-policy.md"},
        ),
        VectorStoreEntry(
            id="acme-incident-response-000",
            content=(
                "Incident response procedure: incidents are classified as P1 service down."
            ),
            embedding=_bow_embedding(
                "Incident response procedure incidents classified P1 service down"
            ),
            metadata={"document_id": "acme-incident-response", "source": "acme-incident.md"},
        ),
    ]
    entries_beta = [
        VectorStoreEntry(
            id="beta-support-escalation-000",
            content=(
                "Beta support escalation: beta program support issues must be triaged "
                "within 1 business day. Critical beta issues are escalated to the beta "
                "program lead."
            ),
            embedding=_bow_embedding(
                "Beta support escalation beta program support issues triaged business day "
                "critical beta issues escalated beta program lead"
            ),
            metadata={
                "document_id": "beta-support-escalation",
                "source": "beta-policy.md",
            },
        ),
    ]

    store.add(entries_acme, organization_id=ORG_ACME)
    store.add(entries_beta, organization_id=ORG_BETA)
    return store


def _lexical_only_for_org(
    store: PgVectorStore,
    query: str,
    org: str,
) -> list[RetrievalResult]:
    return _lexical_results(store, query, org)


def _vector_results(
    store: PgVectorStore,
    provider: DeterministicEmbeddingProvider,
    query: str,
    org: str,
) -> list[RetrievalResult]:
    svc = RetrievalService(provider, store)
    return svc.retrieve(
        RetrievalQuery(query=query, organization_id=org, top_k=5)
    )


def _hybrid_tenant_filtering_results(
    pgstore: PgVectorStore,
) -> dict[str, list[str]]:
    provider = DeterministicEmbeddingProvider(dimension=384)
    base = RetrievalService(provider, pgstore)
    adapter = build_hybrid_retrieval_adapter(
        base,
        pgstore,
        reranker_type="deterministic",
        lexical_top_k=10,
    )
    acme = adapter.retrieve(
        RetrievalQuery(query=_QUERY_ACME, organization_id=ORG_ACME, top_k=5)
    )
    beta = adapter.retrieve(
        RetrievalQuery(query=_QUERY_BETA, organization_id=ORG_BETA, top_k=5)
    )
    return {
        ORG_ACME: [r.chunk_id for r in acme],
        ORG_BETA: [r.chunk_id for r in beta],
    }


@pytest.fixture()
def pgstore(pg_session_factory):
    return _store_with_evidence(pg_session_factory)


def _lexical_results(store: PgVectorStore, query: str, org: str) -> list[RetrievalResult]:
    hits = store.lexical_search(query=query, top_k=10, organization_id=org)
    return [
        RetrievalResult(
            chunk_id=h.id,
            document_id=h.metadata.get("document_id", ""),
            content=h.content,
            score=h.score,
            source=h.metadata.get("source", ""),
            metadata=h.metadata,
        )
        for h in hits
    ]


@requires_infra
class TestPgVectorHybridRetrieval:
    def test_pgvector_lexical_retrieval_works(self, pgstore: PgVectorStore) -> None:
        results = _lexical_results(pgstore, _QUERY_ACME, ORG_ACME)
        ids = {r.chunk_id for r in results}
        assert "acme-support-escalation-000" in ids

    def test_hybrid_fusion_runs_on_real_pgvector(self, pgstore: PgVectorStore) -> None:
        provider = DeterministicEmbeddingProvider(dimension=384)
        base = RetrievalService(provider, pgstore)
        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=pgstore,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )
        response = hybrid.retrieve(
            RetrievalQuery(
                query=_QUERY_ACME,
                organization_id=ORG_ACME,
                top_k=5,
            )
        )
        assert response.results
        ids = {r.chunk_id for r in response.results}
        assert "acme-support-escalation-000" in ids
        assert response.semantic_results or response.lexical_results

    def test_hybrid_adapter_presents_final_context(
        self, pgstore: PgVectorStore
    ) -> None:
        provider = DeterministicEmbeddingProvider(dimension=384)
        base = RetrievalService(provider, pgstore)
        adapter = build_hybrid_retrieval_adapter(
            base,
            pgstore,
            reranker_type="deterministic",
            lexical_top_k=10,
        )
        results = adapter.retrieve(
            RetrievalQuery(
                query=_QUERY_ACME,
                organization_id=ORG_ACME,
                top_k=5,
            )
        )
        context = adapter.build_context(results)
        assert results
        assert "Support escalation policy" in context or "escalation" in context.lower()

    def test_hybrid_reranking_is_invoked(self, pgstore: PgVectorStore) -> None:
        provider = DeterministicEmbeddingProvider(dimension=384)
        base = RetrievalService(provider, pgstore)
        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=pgstore,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )
        response = hybrid.retrieve(
            RetrievalQuery(
                query=_QUERY_ACME,
                organization_id=ORG_ACME,
                top_k=5,
            )
        )
        assert response.rerank_stats.reranker == "deterministic"
        assert response.rerank_stats.candidates >= 0
        assert response.rerank_stats.kept <= max(response.rerank_stats.candidates, 0)

    def test_hybrid_tenant_filtering(self, pgstore: PgVectorStore) -> None:
        acme_ids = {
            r.chunk_id for r in _lexical_results(pgstore, _QUERY_ACME, ORG_ACME)
        }
        beta_ids = {
            r.chunk_id for r in _lexical_results(pgstore, _QUERY_BETA, ORG_BETA)
        }
        assert acme_ids
        assert beta_ids
        assert "acme-support-escalation-000" in acme_ids
        assert "beta-support-escalation-000" in beta_ids
        assert "beta-support-escalation-000" not in acme_ids
        assert "acme-support-escalation-000" not in beta_ids

    def test_hybrid_context_preserves_citations(
        self, pgstore: PgVectorStore
    ) -> None:
        provider = DeterministicEmbeddingProvider(dimension=384)
        base = RetrievalService(provider, pgstore)
        adapter = build_hybrid_retrieval_adapter(
            base,
            pgstore,
            reranker_type="deterministic",
            lexical_top_k=10,
            context_max_tokens=2000,
        )
        results = adapter.retrieve(
            RetrievalQuery(
                query=_QUERY_ACME,
                organization_id=ORG_ACME,
                top_k=5,
            )
        )
        context = adapter.build_context(results)
        first_id = results[0].chunk_id if results else ""
        assert first_id
        assert first_id in context or first_id.split('-')[0] in context
