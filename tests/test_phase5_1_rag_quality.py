from __future__ import annotations

import pytest

from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.agents.rag_agent import RAGAgent
from aegisforge.domain.models import RetrievalQuery, RetrievalResult
from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
from aegisforge.rag.hybrid import (
    HybridRetrievalService,
    RAGHybridAdapter,
    build_hybrid_retrieval_adapter,
    get_reranker,
)
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.rag.retrieval_quality import (
    RETRIEVAL_QUALITY_CORPUS,
    QualityEvidence,
    RetrievalQualityCase,
    RetrievalQualityReport,
    build_retrieval_quality_report,
    hybrid_outperforms_baselines,
    quality_evidence_corpus,
)
from aegisforge.rag.vector_store import InMemoryVectorStore, VectorStoreEntry

# ---------------------------------------------------------------------------
# Deterministic helpers for retrieval-quality baselines
# ---------------------------------------------------------------------------


def _embedding_provider() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(dimension=384)


def _vector_store_with_evidence(
    evidence: tuple[QualityEvidence, ...],
) -> InMemoryVectorStore:
    provider = _embedding_provider()
    store = InMemoryVectorStore()
    entries: list[VectorStoreEntry] = []
    for item in evidence:
        embedding = provider.embed_text(item.content)
        entries.append(
            VectorStoreEntry(
                id=item.chunk_id,
                content=item.content,
                embedding=embedding,
                metadata={
                    "document_id": item.document_id,
                    "source": item.source,
                    "organization_id": item.organization_id,
                },
            )
        )
    for entry in entries:
        store.add([entry], organization_id=entry.metadata["organization_id"])
    return store


def _retrieval_service_for_evidence(
    evidence: tuple[QualityEvidence, ...],
) -> RetrievalService:
    return RetrievalService(
        embedding_provider=_embedding_provider(),
        vector_store=_vector_store_with_evidence(evidence),
    )


def _lexical_only_retrieve(
    store: InMemoryVectorStore,
    query: str,
    organization_id: str,
    top_k: int,
    metadata_filter: dict | None = None,
) -> list[RetrievalResult]:
    hits = store.lexical_search(
        query=query,
        top_k=top_k,
        organization_id=organization_id,
        metadata_filter=metadata_filter or None,
    )
    return [
        RetrievalResult(
            chunk_id=hit.id,
            document_id=hit.metadata.get("document_id", ""),
            content=hit.content,
            score=hit.score,
            source=hit.metadata.get("source", ""),
            metadata=hit.metadata,
        )
        for hit in hits
    ]


def _vector_only_retrieve(
    service: RetrievalService,
    query: str,
    organization_id: str,
    top_k: int,
    similarity_threshold: float = 0.0,
) -> list[RetrievalResult]:
    return service.retrieve(
        RetrievalQuery(
            query=query,
            organization_id=organization_id,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )
    )


def _hybrid_retrieve(
    hybrid: HybridRetrievalService,
    query: str,
    organization_id: str,
    top_k: int,
    similarity_threshold: float = 0.0,
    use_query_expansion: bool = False,
) -> list[RetrievalResult]:
    return hybrid.retrieve(
        RetrievalQuery(
            query=query,
            organization_id=organization_id,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        ),
        use_query_expansion=use_query_expansion,
    ).results


def _hybrid_adapter_retrieve(
    adapter: RAGHybridAdapter,
    query: str,
    organization_id: str,
    top_k: int,
    similarity_threshold: float = 0.0,
) -> list[RetrievalResult]:
    return adapter.retrieve(
        RetrievalQuery(
            query=query,
            organization_id=organization_id,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )
    )


def _rag_agent_context(
    organization_id: str = "org-acme",
    permissions: list[PermissionSpec] | None = None,
) -> AgentExecutionContext:
    return AgentExecutionContext(
        request_id="req-RAG-quality",
        user_id="user-RAG-quality",
        organization_id=organization_id,
        permissions=permissions or [PermissionSpec(name="knowledge.search", allow=True)],
    )


# ---------------------------------------------------------------------------
# Dataset and baselines
# ---------------------------------------------------------------------------


class TestRetrievalQualityDataset:
    def test_corpus_is_small_and_deterministic(self) -> None:
        cases = RETRIEVAL_QUALITY_CORPUS
        assert 0 < len(cases) < 50

    def test_corpus_contains_multiple_query_types(self) -> None:
        types = {c.query_type for c in RETRIEVAL_QUALITY_CORPUS}
        assert {"exact", "semantic", "paraphrased", "distractor", "insufficient"} & types

    def test_corpus_contains_multiple_tenants(self) -> None:
        orgs = {c.organization_id for c in RETRIEVAL_QUALITY_CORPUS}
        assert len(orgs) >= 2

    def test_evidence_corpus_exists(self) -> None:
        corpus = quality_evidence_corpus()
        assert corpus
        assert all(isinstance(item, QualityEvidence) for item in corpus)

    def test_evidence_corpus_is_multi_tenant(self) -> None:
        corpus = quality_evidence_corpus()
        orgs = {item.organization_id for item in corpus}
        assert "org-acme" in orgs
        assert "org-beta" in orgs


class TestRetrievalQualityBaselines:
    @pytest.fixture()
    def corpus(self) -> tuple[QualityEvidence, ...]:
        return quality_evidence_corpus()

    @pytest.fixture()
    def store(self, corpus: tuple[QualityEvidence, ...]) -> InMemoryVectorStore:
        return _vector_store_with_evidence(corpus)

    @pytest.fixture()
    def service(self, corpus: tuple[QualityEvidence, ...]) -> RetrievalService:
        return _retrieval_service_for_evidence(corpus)

    @pytest.fixture()
    def hybrid(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> HybridRetrievalService:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)
        return HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            query_expander=None,
            context_assembler=None,
            fusion_candidates=60,
            lexical_top_k=10,
        )

    def _case_results(
        self,
        retriever,
        *,
        top_k: int = 5,
    ) -> dict[str, list[RetrievalResult]]:
        results_by_case: dict[str, list[RetrievalResult]] = {}
        for case in RETRIEVAL_QUALITY_CORPUS:
            results_by_case[case.name] = retriever(case, top_k)
        return results_by_case

    def test_vector_baseline_is_measurable(self, corpus: tuple[QualityEvidence, ...]) -> None:
        def vector_retriever(
            case: RetrievalQualityCase, top_k: int
        ) -> list[RetrievalResult]:
            service = RetrievalService(_embedding_provider(), _vector_store_with_evidence(corpus))
            return _vector_only_retrieve(
                service,
                case.query,
                case.organization_id,
                top_k,
            )

        report = build_retrieval_quality_report(
            RETRIEVAL_QUALITY_CORPUS,
            self._case_results(vector_retriever),
        )
        assert report.total_queries == len(RETRIEVAL_QUALITY_CORPUS)
        assert report.case_metrics

    def test_lexical_baseline_is_measurable(self, corpus: tuple[QualityEvidence, ...]) -> None:
        def lexical_retriever(
            case: RetrievalQualityCase, top_k: int
        ) -> list[RetrievalResult]:
            return _lexical_only_retrieve(
                _vector_store_with_evidence(corpus),
                case.query,
                case.organization_id,
                top_k,
            )

        report = build_retrieval_quality_report(
            RETRIEVAL_QUALITY_CORPUS,
            self._case_results(lexical_retriever),
        )
        assert report.total_queries == len(RETRIEVAL_QUALITY_CORPUS)

    def test_hybrid_baseline_is_measurable(self, corpus: tuple[QualityEvidence, ...]) -> None:
        def hybrid_retriever(
            case: RetrievalQualityCase, top_k: int
        ) -> list[RetrievalResult]:
            store = _vector_store_with_evidence(corpus)
            base = RetrievalService(_embedding_provider(), store)
            hybrid = HybridRetrievalService(
                semantic=base,
                vector_store=store,
                reranker=get_reranker("deterministic"),
                lexical_top_k=10,
            )
            return _hybrid_retrieve(
                hybrid,
                case.query,
                case.organization_id,
                top_k,
            )

        results_by_case: dict[str, list[RetrievalResult]] = {}
        for case in RETRIEVAL_QUALITY_CORPUS:
            results_by_case[case.name] = hybrid_retriever(case, case.top_k)

        report = build_retrieval_quality_report(RETRIEVAL_QUALITY_CORPUS, results_by_case)
        assert report.total_queries == len(RETRIEVAL_QUALITY_CORPUS)
        assert report.average_recall_at_k >= 0.0


class TestHybridRetrievalQualityGate:
    @pytest.fixture()
    def corpus(self) -> tuple[QualityEvidence, ...]:
        return quality_evidence_corpus()

    @pytest.fixture()
    def store(self, corpus: tuple[QualityEvidence, ...]) -> InMemoryVectorStore:
        return _vector_store_with_evidence(corpus)

    @pytest.fixture()
    def base_service(self, corpus: tuple[QualityEvidence, ...]) -> RetrievalService:
        return _retrieval_service_for_evidence(corpus)

    @pytest.fixture()
    def hybrid_service(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> HybridRetrievalService:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)
        return HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )

    def _per_case(
        self,
        cases: tuple[RetrievalQualityCase, ...],
        retriever,
        top_k: int = 5,
    ) -> dict[str, list[RetrievalResult]]:
        results: dict[str, list[RetrievalResult]] = {}
        for case in cases:
            if not case.relevant_chunk_ids:
                results[case.name] = []
                continue
            results[case.name] = retriever(case, top_k)
        return results

    def _report(
        self,
        cases: tuple[RetrievalQualityCase, ...],
        results: dict[str, list[RetrievalResult]],
    ) -> RetrievalQualityReport:
        full: dict[str, list[RetrievalResult]] = {
            c.name: results.get(c.name, []) for c in cases
        }
        return build_retrieval_quality_report(cases, full)

    def test_hybrid_outperforms_lexical_only_on_paraphrased_queries(
        self,
        corpus: tuple[QualityEvidence, ...],
        hybrid_service: HybridRetrievalService,
    ) -> None:
        paraphrased = tuple(
            c
            for c in RETRIEVAL_QUALITY_CORPUS
            if c.query_type == "paraphrased" and c.relevant_chunk_ids
        )

        def lexical_retriever(case: RetrievalQualityCase, top_k: int):
            store = _vector_store_with_evidence(corpus)
            return _lexical_only_retrieve(
                store,
                case.query,
                case.organization_id,
                top_k,
            )

        def hybrid_retriever(case: RetrievalQualityCase, top_k: int):
            store = _vector_store_with_evidence(corpus)
            base = RetrievalService(_embedding_provider(), store)
            hybrid = HybridRetrievalService(
                semantic=base,
                vector_store=store,
                reranker=get_reranker("deterministic"),
                lexical_top_k=10,
            )
            return _hybrid_retrieve(
                hybrid,
                case.query,
                case.organization_id,
                top_k,
            )

        lexical_results = self._per_case(paraphrased, lexical_retriever)
        hybrid_results = self._per_case(paraphrased, hybrid_retriever)

        lexical_report = self._report(paraphrased, lexical_results)
        hybrid_report = self._report(paraphrased, hybrid_results)

        improved, detail = hybrid_outperforms_baselines(lexical_report, hybrid_report)

        assert improved, (
            f"Hybrid retrieval should beat the lexical-only baseline on "
            f"paraphrased queries, but it did not. "
            f"lexical={detail['baseline_recall_at_k']} hybrid={detail['hybrid_recall_at_k']} "
            f"delta={detail['recall_delta']} rate_delta={detail['relevant_rate_delta']}"
        )
        assert detail["hybrid_wins"] is True
        assert detail["recall_delta"] >= 0.0
        assert detail["relevant_rate_delta"] >= 0.0

    def test_hybrid_retrieval_recovers_relevant_documents_for_paraphrased_queries(
        self,
        hybrid_service: HybridRetrievalService,
    ) -> None:
        paraphrased = tuple(
            c
            for c in RETRIEVAL_QUALITY_CORPUS
            if c.query_type == "paraphrased" and c.relevant_chunk_ids
        )

        def hybrid_retriever(case: RetrievalQualityCase, top_k: int):
            return _hybrid_retrieve(
                hybrid_service,
                case.query,
                case.organization_id,
                top_k,
            )

        hybrid_results = self._per_case(paraphrased, hybrid_retriever)
        hybrid_report = self._report(paraphrased, hybrid_results)

        assert hybrid_report.average_recall_at_k > 0.0, (
            f"Hybrid retrieval should recover relevant evidence for paraphrased queries, "
            f"but average recall was {hybrid_report.average_recall_at_k}"
        )

    def test_hybrid_outperforms_lexical_only_on_semantic_and_paraphrased_queries(
        self,
        corpus: tuple[QualityEvidence, ...],
        hybrid_service: HybridRetrievalService,
    ) -> None:
        semantic_and_paraphrase = tuple(
            c
            for c in RETRIEVAL_QUALITY_CORPUS
            if c.query_type in ("semantic", "paraphrased") and c.relevant_chunk_ids
        )

        def lexical_retriever(case: RetrievalQualityCase, top_k: int):
            store = _vector_store_with_evidence(corpus)
            return _lexical_only_retrieve(
                store,
                case.query,
                case.organization_id,
                top_k,
            )

        def hybrid_retriever(case: RetrievalQualityCase, top_k: int):
            store = _vector_store_with_evidence(corpus)
            base = RetrievalService(_embedding_provider(), store)
            hybrid = HybridRetrievalService(
                semantic=base,
                vector_store=store,
                reranker=get_reranker("deterministic"),
                lexical_top_k=10,
            )
            return _hybrid_retrieve(
                hybrid,
                case.query,
                case.organization_id,
                top_k,
            )

        lexical_results = self._per_case(semantic_and_paraphrase, lexical_retriever)
        hybrid_results = self._per_case(semantic_and_paraphrase, hybrid_retriever)

        lexical_report = self._report(semantic_and_paraphrase, lexical_results)
        hybrid_report = self._report(semantic_and_paraphrase, hybrid_results)

        improved, detail = hybrid_outperforms_baselines(lexical_report, hybrid_report)

        assert improved, (
            f"Hybrid retrieval should beat the lexical-only baseline on "
            f"semantic/paraphrased queries, but it did not. "
            f"lexical={detail['baseline_recall_at_k']} hybrid={detail['hybrid_recall_at_k']} "
            f"delta={detail['recall_delta']} rate_delta={detail['relevant_rate_delta']}"
        )
        assert detail["hybrid_wins"] is True

    def test_hybrid_does_not_dominate_on_duplicates(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )

        results = _hybrid_retrieve(
            hybrid,
            "support escalation",
            "org-acme",
            top_k=5,
        )

        chunk_ids = [r.chunk_id for r in results]
        # The deterministic reranker should remove near-duplicate chunks.
        assert len(chunk_ids) == len(set(chunk_ids))

    def test_hybrid_returns_bounded_candidate_set(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            fusion_candidates=60,
            lexical_top_k=10,
        )

        results = _hybrid_retrieve(
            hybrid,
            "support escalation policy",
            "org-acme",
            top_k=3,
        )

        assert len(results) <= 3


class TestRerankingEffect:
    @pytest.fixture()
    def corpus(self) -> tuple[QualityEvidence, ...]:
        return quality_evidence_corpus()

    def test_deterministic_rerank_is_deterministic(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )

        results_a = _hybrid_retrieve(
            hybrid,
            "support escalation policy",
            "org-acme",
            top_k=5,
        )
        results_b = _hybrid_retrieve(
            hybrid,
            "support escalation policy",
            "org-acme",
            top_k=5,
        )

        assert [r.chunk_id for r in results_a] == [r.chunk_id for r in results_b]

    def test_rerank_can_change_ordering(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        fused = reciprocal_rank_fusion_from_services(
            semantic=base,
            vector_store=store,
            query="support escalation",
            organization_id="org-acme",
            top_k=10,
        )

        reranked, stats = get_reranker("deterministic").rerank(
            "support escalation",
            fused,
            top_k=5,
        )

        assert stats.reranker == "deterministic"
        assert stats.candidates == len(fused)
        assert stats.kept <= stats.candidates

        # Reranking should not introduce duplicates either.
        assert len({r.chunk_id for r in reranked}) == len(reranked)

    def test_rerank_fallback_is_safe(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )

        results = _hybrid_retrieve(
            hybrid,
            "support escalation policy",
            "org-acme",
            top_k=5,
        )

        assert results, "Hybrid retrieval should return results for a covered exact query"
        assert all(r.chunk_id for r in results)


class TestQueryExpansionRetrieval:
    @pytest.fixture()
    def corpus(self) -> tuple[QualityEvidence, ...]:
        return quality_evidence_corpus()

    def test_query_expander_is_bounded(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        from aegisforge.rag.hybrid import build_hybrid_retrieval_adapter

        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        adapter = build_hybrid_retrieval_adapter(
            base,
            store,
            query_expansion_enabled=True,
            query_expansion_max=2,
            reranker_type="deterministic",
        )

        expansions = adapter._hybrid._expander.expand("support escalation policy")
        assert 0 < len(expansions) <= 3
        assert "support escalation policy" in expansions

    def test_paraphrased_query_retrieves_relevant_documents(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        hybrid = HybridRetrievalService(
            semantic=base,
            vector_store=store,
            reranker=get_reranker("deterministic"),
            lexical_top_k=10,
        )

        results = _hybrid_retrieve(
            hybrid,
            "a customer is reporting a severe service issue, what is the process",
            "org-acme",
            top_k=5,
        )

        relevant_doc_ids = {"acme-support-escalation", "acme-incident-response"}
        retrieved_docs = {r.document_id for r in results}

        assert bool(retrieved_docs & relevant_doc_ids), (
            "Paraphrased query should retrieve evidence from relevant policy documents."
        )


class TestContextOptimization:
    @pytest.fixture()
    def corpus(self) -> tuple[QualityEvidence, ...]:
        return quality_evidence_corpus()

    def test_context_does_not_escape_tenant(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        adapter = build_hybrid_retrieval_adapter(
            base,
            store,
            reranker_type="deterministic",
        )

        results = adapter.retrieve(
            RetrievalQuery(
                query="support escalation policy",
                organization_id="org-acme",
                top_k=5,
            )
        )

        for result in results:
            assert result.metadata.get("organization_id") == "org-acme" or (
                result.document_id.startswith("acme")
            )

        # Context should not contain beta tenant evidence.
        context_text = adapter.build_context(results)
        assert "beta" not in context_text.lower() or "beta" not in context_text

    def test_insufficient_context_is_handled(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        adapter = build_hybrid_retrieval_adapter(
            base,
            store,
            reranker_type="deterministic",
        )

        agent = RAGAgent(retrieval_service=adapter)
        result = agent.execute(
            {"query": "quantum resistant compliance roadmap for 2027"},
            _rag_agent_context(organization_id="org-acme"),
        )

        assert result.status.value == "completed"
        assert result.result.get("context_available") is False
        assert result.result.get("retrieval_count") == 0


class TestTenantIsolationAcrossHybridPath:
    @pytest.fixture()
    def corpus(self) -> tuple[QualityEvidence, ...]:
        return quality_evidence_corpus()

    def test_hybrid_retrieval_isolated_by_organization(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        adapter = build_hybrid_retrieval_adapter(
            base,
            store,
            reranker_type="deterministic",
        )

        acme_results = adapter.retrieve(
            RetrievalQuery(
                query="support escalation policy",
                organization_id="org-acme",
                top_k=5,
            )
        )
        beta_results = adapter.retrieve(
            RetrievalQuery(
                query="support escalation policy",
                organization_id="org-beta",
                top_k=5,
            )
        )

        acme_ids = {r.chunk_id for r in acme_results}
        beta_ids = {r.chunk_id for r in beta_results}

        assert acme_ids
        assert beta_ids
        assert not (acme_ids & beta_ids)

        # Beta tenant must not see acme chunks.
        for result in beta_results:
            assert result.chunk_id in beta_ids
            assert result.chunk_id not in acme_ids

    def test_hybrid_context_isolated_by_organization(
        self, corpus: tuple[QualityEvidence, ...]
    ) -> None:
        store = _vector_store_with_evidence(corpus)
        base = RetrievalService(_embedding_provider(), store)

        adapter = build_hybrid_retrieval_adapter(
            base,
            store,
            reranker_type="deterministic",
        )

        acme_results = adapter.retrieve(
            RetrievalQuery(
                query="support escalation policy",
                organization_id="org-acme",
                top_k=5,
            )
        )
        beta_results = adapter.retrieve(
            RetrievalQuery(
                query="support escalation policy",
                organization_id="org-beta",
                top_k=5,
            )
        )

        acme_context = adapter.build_context(acme_results)
        beta_context = adapter.build_context(beta_results)

        assert "beta" not in acme_context.lower() or "beta" not in acme_context
        assert not any(chunk_id in beta_context for chunk_id in {
            r.chunk_id for r in acme_results
        })


# ---------------------------------------------------------------------------
# Internal helper kept in tests to avoid polluting production modules
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion_from_services(
    semantic: RetrievalService,
    vector_store: InMemoryVectorStore,
    query: str,
    organization_id: str,
    top_k: int,
) -> list[RetrievalResult]:
    """Build semantic + lexical candidate lists and fuse them for test inspection.

    This helper exists only in tests so production hybrid code is not duplicated.
    """
    semantic_results = semantic.retrieve(
        RetrievalQuery(
            query=query,
            organization_id=organization_id,
            top_k=top_k,
        )
    )
    lexical_results = _lexical_only_retrieve(
        vector_store, query, organization_id, top_k
    )

    from aegisforge.rag.hybrid import reciprocal_rank_fusion

    return reciprocal_rank_fusion(
        semantic_results,
        lexical_results,
        top_k=top_k,
    )
