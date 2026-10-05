"""Phase 10 flagship demo tests.

Behavioural, not string-matching: these tests validate that the demo corpus is
real and retrievable, that seeding is idempotent and owner-scoped, that the
canonical request decomposes into the intended dependency graph, that the real
orchestration engine produces grounded evidence and a real evaluation, and that
the demo never leaks across users.

Nothing here asserts a hard-coded demo answer: the assertions are about
measured structure, provenance, and authorization.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.agents.planner import PlannerAgent
from aegisforge.agents.base import AgentExecutionContext
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.models import DocumentModel
from aegisforge.db.session import get_engine, get_session_factory
from aegisforge.demo.scenario import (
    DEMO_DOCUMENTS,
    FLAGSHIP_DEMO_REQUEST,
    DemoDocument,
    document_id_for,
    read_demo_document,
)
from aegisforge.demo.seed import delete_demo_documents, seed_demo_documents
from aegisforge.domain.models import RequestStatus, RetrievalQuery
from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.rag.vector_store import InMemoryVectorStore
from aegisforge.workflows.langgraph_workflow import execute_workflow


def _settings() -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="phase10-demo-secret",
        llm_provider="deterministic",
        embedding_provider="deterministic",
        embedding_dimension=384,
    )


@pytest.fixture()
def settings() -> Settings:
    return _settings()


@pytest.fixture()
def store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture()
def seeded(settings: Settings, store: InMemoryVectorStore):
    """A seeded owner with a fresh in-memory vector store + retrieval service."""
    db = _new_db(settings)
    try:
        outcome = seed_demo_documents(
            db,
            organization_id="acme-org",
            owner_id="user-a",
            settings=settings,
            vector_store=store,
        )
        retrieval = RetrievalService(DeterministicEmbeddingProvider(dimension=384), store)
        yield db, outcome, retrieval, store
    finally:
        db.close()


def _new_db(settings: Settings) -> Any:
    get_engine.cache_clear()
    Base.metadata.create_all(bind=get_engine(settings.database_url))
    return get_session_factory(settings)()


# ---------------------------------------------------------------------------
# 1. The corpus is real, non-trivial, and internally contradictory
# ---------------------------------------------------------------------------


class TestDemoCorpus:
    def test_every_manifest_document_exists_and_is_substantial(self) -> None:
        assert len(DEMO_DOCUMENTS) >= 5
        for document in DEMO_DOCUMENTS:
            content = read_demo_document(document).decode("utf-8")
            assert len(content) > 800, f"{document.slug} is too thin to retrieve over"
            assert content.lstrip().startswith("# "), f"{document.slug} has no title"
            assert document.doc_title.split("(")[0].strip() in content

    def test_corpus_spans_requirements_policies_and_product_material(self) -> None:
        categories = {d.category for d in DEMO_DOCUMENTS}
        assert {"company-requirements", "policies", "architecture", "product"} <= categories

    def test_corpus_contains_real_contradictions(self) -> None:
        """Vendor claims must genuinely conflict with internal requirements.

        A corpus without conflicts would make the analysis/conflict machinery
        decorative, so this is asserted explicitly rather than assumed.
        """
        all_text = " ".join(
            read_demo_document(d).decode("utf-8").lower() for d in DEMO_DOCUMENTS
        )
        # Internal prohibition of hosted SaaS for regulated data...
        assert "managed saas control planes are prohibited" in all_text
        # ...and a vendor that is hosted-only.
        assert "exclusively as a vendor-hosted multi-tenant" in all_text
        # Internal customer-managed key requirement vs vendor-managed keys.
        assert "customer-managed keys" in all_text
        assert "vendor-managed keys" in all_text
        # Numeric conflict: internal 250 ms budget vs vendor 240 ms p99.
        assert "250 ms" in all_text
        assert "240 ms" in all_text

    def test_manifest_ids_are_deterministic_and_namespaced(self) -> None:
        ids = [document_id_for(d.slug, "user-a") for d in DEMO_DOCUMENTS]
        assert len(set(ids)) == len(ids)
        assert all(i.startswith("demo-") for i in ids)
        assert document_id_for("abc", "user-a") == document_id_for("abc", "user-a")
        # Owner-scoped ids: two accounts must never collide on the primary key
        # (self-registration shares the default organization).
        assert document_id_for("abc", "user-a") != document_id_for("abc", "user-b")
        # Id length must fit the documents.id column.
        longest = max(document_id_for(d.slug, "0" * 36) for d in DEMO_DOCUMENTS)
        assert len(longest) <= 64, longest


# ---------------------------------------------------------------------------
# 2. Seeding is deterministic, idempotent, and reversible
# ---------------------------------------------------------------------------


class TestDemoSeeding:
    def test_seed_creates_documents_and_chunks(self, seeded) -> None:
        db, outcome, _retrieval, _store = seeded
        assert sorted(outcome.created) == sorted(d.slug for d in DEMO_DOCUMENTS)
        assert not outcome.updated and not outcome.skipped
        assert not outcome.indexing_failures
        assert all(count > 0 for count in outcome.chunk_counts.values())
        rows = db.query(DocumentModel).all()
        assert len(rows) == len(DEMO_DOCUMENTS)
        # Owner scoping is written on every row.
        assert all(r.uploaded_by == "user-a" for r in rows)
        assert all(r.organization_id == "acme-org" for r in rows)

    def test_second_seed_is_a_no_op(self, settings: Settings, store: InMemoryVectorStore) -> None:
        db = _new_db(settings)
        try:
            first = seed_demo_documents(
                db,
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                vector_store=store,
            )
            doc_count = db.query(DocumentModel).count()
            chunks_first = store.count("acme-org", "user-a")

            second = seed_demo_documents(
                db,
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                vector_store=store,
            )
            assert len(second.created) == 0
            assert len(second.updated) == 0
            assert sorted(second.skipped) == sorted(d.slug for d in DEMO_DOCUMENTS)
            assert db.query(DocumentModel).count() == doc_count
            assert store.count("acme-org", "user-a") == chunks_first
            assert second.chunk_counts == first.chunk_counts
        finally:
            db.close()

    def test_two_owners_in_one_org_can_both_seed(self, settings: Settings) -> None:
        """Regression: demo document ids must be owner-scoped.

        ``documents.id`` is the primary key and self-registration places every
        user in the same organization, so a shared id made the second account's
        seed fail with a unique-constraint violation.
        """
        db = _new_db(settings)
        try:
            store_a, store_b = InMemoryVectorStore(), InMemoryVectorStore()
            first = seed_demo_documents(
                db,
                organization_id="shared-org",
                owner_id="user-a",
                settings=settings,
                documents=[DEMO_DOCUMENTS[0]],
                vector_store=store_a,
            )
            second = seed_demo_documents(
                db,
                organization_id="shared-org",
                owner_id="user-b",
                settings=settings,
                documents=[DEMO_DOCUMENTS[0]],
                vector_store=store_b,
            )
            assert first.created == [DEMO_DOCUMENTS[0].slug]
            assert second.created == [DEMO_DOCUMENTS[0].slug]
            ids = {d.id for d in db.query(DocumentModel).all()}
            assert len(ids) == 2
            assert all(r.uploaded_by in {"user-a", "user-b"} for r in db.query(DocumentModel).all())

            # Each account sees only its own copy.
            assert store_a.count("shared-org", "user-a") > 0
            assert store_a.count("shared-org", "user-b") == 0
            assert store_b.count("shared-org", "user-b") > 0
            assert store_b.count("shared-org", "user-a") == 0

            # Resetting one account must not touch the other.
            delete_demo_documents(
                db,
                organization_id="shared-org",
                owner_id="user-a",
                settings=settings,
                vector_store=store_a,
            )
            remaining = {d.uploaded_by for d in db.query(DocumentModel).all()}
            assert remaining == {"user-b"}
        finally:
            db.close()

    def test_changed_corpus_content_reingests_under_same_id(
        self, settings: Settings, store: InMemoryVectorStore
    ) -> None:
        db = _new_db(settings)
        try:
            document = DemoDocument(
                slug="temp-doc",
                relative_path=DEMO_DOCUMENTS[0].relative_path,
                title="Temporary",
                category="temp",
                doc_title="Temporary",
            )
            seed_demo_documents(
                db,
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                documents=[document],
                vector_store=store,
            )
            temp_id = document_id_for("temp-doc", "user-a")
            stored = db.query(DocumentModel).filter_by(id=temp_id).one()
            original_hash = stored.content_hash
            # Corrupt the stored hash to simulate corpus drift, then re-seed.
            stored.content_hash = "drifted"
            db.commit()

            outcome = seed_demo_documents(
                db,
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                documents=[document],
                vector_store=store,
            )
            assert outcome.updated == ["temp-doc"]
            assert outcome.created == []
            assert db.query(DocumentModel).filter_by(id=temp_id).one().content_hash == original_hash
        finally:
            db.close()

    def test_reset_removes_only_demo_documents(
        self, settings: Settings, store: InMemoryVectorStore
    ) -> None:
        db = _new_db(settings)
        try:
            seed_demo_documents(
                db,
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                vector_store=store,
            )
            # A non-demo document belonging to the same user must survive.
            from aegisforge.services.document_service import ingest_and_store_document

            ingest_and_store_document(
                db,
                content=b"# Personal notes\nunrelated user content",
                title="Personal notes",
                content_type="text/markdown",
                source="personal-notes.md",
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                document_id="user-doc-1",
                vector_store=store,
            )
            assert db.query(DocumentModel).count() == len(DEMO_DOCUMENTS) + 1

            removed = delete_demo_documents(
                db,
                organization_id="acme-org",
                owner_id="user-a",
                settings=settings,
                vector_store=store,
            )
            assert removed == len(DEMO_DOCUMENTS)
            remaining = [d.id for d in db.query(DocumentModel).all()]
            assert remaining == ["user-doc-1"]
            # No orphaned demo embeddings remain for the reset scope.
            assert store.count("acme-org", "user-a") > 0
            assert (
                store.count("acme-org", "user-a")
                == len(
                    [
                        e
                        for e in store._entries  # noqa: SLF001 - asserting cleanup
                        if e.metadata.get("document_id") == "user-doc-1"
                    ]
                )
            )
        finally:
            db.close()

    def test_seed_refuses_without_owner_scope(self, settings: Settings) -> None:
        db = _new_db(settings)
        try:
            with pytest.raises(ValueError):
                seed_demo_documents(
                    db,
                    organization_id="acme-org",
                    owner_id="",
                    settings=settings,
                    vector_store=InMemoryVectorStore(),
                )
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 3. Retrieval over the demo corpus is real and tenant-isolated
# ---------------------------------------------------------------------------


class TestDemoRetrieval:
    def test_flagship_query_retrieves_multiple_demo_documents(self, seeded) -> None:
        _db, _outcome, retrieval, _store = seeded
        results = retrieval.retrieve(
            RetrievalQuery(
                query=FLAGSHIP_DEMO_REQUEST,
                organization_id="acme-org",
                user_id="user-a",
                top_k=8,
                similarity_threshold=0.0,
            )
        )
        assert results, "the flagship query must retrieve something"
        sources = {r.metadata.get("demo_slug") or r.source for r in results}
        # Retrieval must span more than one document for the comparison to be real.
        assert len(sources) >= 2, f"expected cross-document retrieval, got {sources}"

    def test_retrieval_is_owner_scoped(self, seeded) -> None:
        _db, _outcome, retrieval, _store = seeded
        query = RetrievalQuery(
            query=FLAGSHIP_DEMO_REQUEST,
            organization_id="acme-org",
            user_id="user-a",
            top_k=5,
            similarity_threshold=0.0,
        )
        assert retrieval.retrieve(query)
        # Same organization, different user: nothing is visible.
        query.user_id = "user-b"
        assert retrieval.retrieve(query) == []
        # Different organization, same owner: nothing is visible.
        query.user_id = "user-a"
        query.organization_id = "other-org"
        assert retrieval.retrieve(query) == []

    def test_retrieval_requires_all_scopes_fail_closed(self, seeded) -> None:
        _db, _outcome, retrieval, _store = seeded
        # A missing owner scope must never widen visibility.
        assert (
            retrieval.retrieve(
                RetrievalQuery(
                    query=FLAGSHIP_DEMO_REQUEST,
                    organization_id="acme-org",
                    user_id="",
                    top_k=5,
                    similarity_threshold=0.0,
                )
            )
            == []
        )

    def test_vendor_and_requirement_documents_are_both_reachable(self, seeded) -> None:
        """The comparison needs both sides: vendor claims and internal rules."""
        _db, _outcome, retrieval, _store = seeded
        vendor = retrieval.retrieve(
            RetrievalQuery(
                query="vendor hosted deployment model pricing support",
                organization_id="acme-org",
                user_id="user-a",
                top_k=8,
                similarity_threshold=0.0,
            )
        )
        requirement = retrieval.retrieve(
            RetrievalQuery(
                query="on-premises deployment required customer managed encryption keys",
                organization_id="acme-org",
                user_id="user-a",
                top_k=8,
                similarity_threshold=0.0,
            )
        )
        assert vendor and requirement
        assert {r.source for r in vendor} != {r.source for r in requirement}


# ---------------------------------------------------------------------------
# 4. The canonical request produces the intended dependency graph
# ---------------------------------------------------------------------------


class TestFlagshipPlan:
    def _plan(self) -> Any:
        agent = PlannerAgent()
        result = agent.execute(
            {"intent": FLAGSHIP_DEMO_REQUEST},
            AgentExecutionContext(request_id="req-demo", organization_id="acme-org", user_id="user-a"),
        )
        assert result.status.value == "completed", result.errors
        return result.result

    def test_plan_is_multi_agent_with_parallel_entry(self) -> None:
        plan = self._plan()
        tasks = plan["tasks"]
        agent_types = [t["assigned_agent_type"] for t in tasks]
        assert agent_types == ["research", "rag", "analysis", "synthesis"]

        by_id = {t["task_id"]: t for t in tasks}
        research, rag, analysis, synthesis = tasks
        # Genuine parallelism: research and RAG both start at wave 0.
        assert research["dependencies"] == []
        assert rag["dependencies"] == []
        # Analysis consumes both evidence sources.
        assert set(analysis["dependencies"]) == {research["task_id"], rag["task_id"]}
        assert analysis["input_data"]["evidence_from"] == [
            research["task_id"],
            rag["task_id"],
        ]
        # Synthesis depends on analysis and reads its upstream outputs.
        assert analysis["task_id"] in synthesis["dependencies"]
        assert analysis["task_id"] in synthesis["input_data"]["agent_outputs_from"]
        # Every dependency reference points at a real task.
        for task in tasks:
            for dep in task["dependencies"]:
                assert dep in by_id

    def test_plan_uses_a_tool_and_is_low_risk_for_the_happy_path(self) -> None:
        plan = self._plan()
        research = plan["tasks"][0]
        assert research["input_data"]["tool_name"] == "knowledge.search"
        assert research["tool_permissions_required"] == ["knowledge.search"]
        # A recommendation request performs no action, so it must not gate.
        assert all(task["risk_level"] == "low" for task in plan["tasks"])

    def test_plan_survives_serialization_round_trip(self) -> None:
        from aegisforge.domain.models import ExecutionPlan

        plan = ExecutionPlan(**self._plan())
        assert len(plan.tasks) == 4


# ---------------------------------------------------------------------------
# 5. The real engine runs the demo and produces grounded, evaluated output
# ---------------------------------------------------------------------------


class TestFlagshipEngineRun:
    def _run(self, seeded, *, user_id: str = "user-a", org: str = "acme-org") -> dict[str, Any]:
        _db, _outcome, retrieval, _store = seeded
        return execute_workflow(
            request_id="req-flagship",
            intent=FLAGSHIP_DEMO_REQUEST,
            user_id=user_id,
            organization_id=org,
            retrieval_service=retrieval,
        )

    def test_run_completes_with_a_real_task_graph(self, seeded) -> None:
        final = self._run(seeded)
        assert final["status"] == RequestStatus.COMPLETED.value, final.get("errors")
        records = final["task_records"]
        assert len(records) == 4
        assert {r["agent_type"] for r in records.values()} == {
            "research",
            "rag",
            "analysis",
            "synthesis",
        }
        assert all(r["status"] == "completed" for r in records.values())
        # Evidence actually moved between agents.
        assert records  # non-empty
        analysis = next(r for r in records.values() if r["agent_type"] == "analysis")
        assert analysis["output"]["evidence_reviewed"] >= 2

    def test_result_is_grounded_in_retrieved_citations(self, seeded) -> None:
        final = self._run(seeded)
        result = final["agent_result"]["result"]
        answer = result["answer"]
        assert answer.strip()

        # Citations must correspond to real retrieved chunks, not invented ones.
        citations = result.get("citations") or []
        assert citations, "the flagship run must produce citations"
        rag_record = next(
            r for r in final["task_records"].values() if r["agent_type"] == "rag"
        )
        rag_chunk_ids = {e["chunk_id"] for e in rag_record["evidence"]}
        assert rag_chunk_ids, "the RAG task must have retrieved chunks"
        assert all(
            c.get("chunk_id") in rag_chunk_ids
            for c in citations
            if c.get("chunk_id")
        )

    def test_tool_usage_is_recorded_from_a_real_tool_execution(self, seeded) -> None:
        final = self._run(seeded)
        research = next(
            r for r in final["task_records"].values() if r["agent_type"] == "research"
        )
        assert research["tool_calls"], "the research task must record its tool call"
        tool_call = research["tool_calls"][0]
        assert tool_call["tool_name"] == "knowledge.search"
        assert tool_call["status"] == "completed"

    def test_evaluation_is_real_and_persisted_in_state(self, seeded) -> None:
        final = self._run(seeded)
        wf_eval = final.get("workflow_evaluation")
        assert wf_eval, "workflow-level evaluation must be computed"
        assert 0.0 <= wf_eval["overall_score"] <= 1.0
        # Groundedness is measured from citations the run actually produced.
        assert wf_eval["final_response"]["produced"] is True
        assert wf_eval["final_response"]["grounded"] is True
        assert wf_eval["final_response"]["citation_count"] > 0
        # Collaboration domain reflects real inter-agent references.
        assert wf_eval["collaboration"]["references_resolved"] > 0
        assert wf_eval["collaboration"]["references_failed"] == 0

    def test_synthesis_reports_coverage_and_confidence(self, seeded) -> None:
        final = self._run(seeded)
        synthesis = next(
            r for r in final["task_records"].values() if r["agent_type"] == "synthesis"
        )
        assert synthesis["output"]["complete"] is True
        assert synthesis["output"]["failed_upstream"] == []
        assert 0.0 <= synthesis["output"]["confidence"] <= 1.0
        assert synthesis["output"]["evidence_assessment"]["total_items"] > 0

    def test_run_is_repeatable(self, seeded) -> None:
        first = self._run(seeded)
        second = self._run(seeded)
        assert first["status"] == second["status"] == RequestStatus.COMPLETED.value
        assert {r["agent_type"] for r in first["task_records"].values()} == {
            r["agent_type"] for r in second["task_records"].values()
        }
        # Deterministic embeddings + deterministic planner ⇒ same citation count.
        assert (
            len(first["agent_result"]["result"].get("citations") or [])
            == len(second["agent_result"]["result"].get("citations") or [])
        )
        assert (
            first["workflow_evaluation"]["overall_score"]
            == second["workflow_evaluation"]["overall_score"]
        )

    def test_other_tenants_retrieve_nothing(self, seeded) -> None:
        """A second user running the same demo must get no demo evidence.

        Retrieval is owner-scoped, so the RAG leg sees nothing and the run
        reports insufficient context instead of borrowing another tenant's
        documents.
        """
        _db, _outcome, retrieval, _store = seeded
        final = execute_workflow(
            request_id="req-other",
            intent=FLAGSHIP_DEMO_REQUEST,
            user_id="user-b",
            organization_id="acme-org",
            retrieval_service=retrieval,
        )
        assert final["status"] == RequestStatus.COMPLETED.value
        rag = next(r for r in final["task_records"].values() if r["agent_type"] == "rag")
        assert rag["evidence"] == []
        assert rag["output"]["citations"] == []
        assert rag["output"]["context_available"] is False
        assert rag["output"]["retrieval_count"] == 0
        # The final answer carries no citation drawn from another owner's corpus.
        citations = final["agent_result"]["result"].get("citations") or []
        assert all(
            not str(c.get("source", "")).startswith("demo-") for c in citations
        ), citations


# ---------------------------------------------------------------------------
# 6. The whole demo path through the real API
# ---------------------------------------------------------------------------


def _api_client() -> TestClient:
    settings = _settings()
    get_engine.cache_clear()
    Base.metadata.create_all(bind=get_engine(settings.database_url))
    return TestClient(create_app(settings=settings))


def _register(client: TestClient, email: str) -> dict[str, str]:
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123!", "full_name": "Demo"},
    )
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "SecurePass123!"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestFlagshipApiPath:
    def test_full_demo_path_over_http(self, settings: Settings, store: InMemoryVectorStore) -> None:
        client = _api_client()
        headers = _register(client, "demo@example.com")

        # Seed through the real ingestion service against the same DB the API uses.
        db = get_session_factory(settings)()
        try:
            from aegisforge.db.models import UserModel

            user = db.query(UserModel).filter_by(email="demo@example.com").one()
            outcome = seed_demo_documents(
                db,
                organization_id=user.organization_id,
                owner_id=user.id,
                settings=settings,
                vector_store=store,
            )
            assert len(outcome.created) == len(DEMO_DOCUMENTS)
        finally:
            db.close()

        created = client.post(
            "/api/v1/requests", json={"intent": FLAGSHIP_DEMO_REQUEST}, headers=headers
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]

        lookup = client.get(f"/api/v1/workflows/by-request/{request_id}", headers=headers)
        assert lookup.status_code == 404, "no workflow before execution"

        executed = client.post(
            f"/api/v1/execution/requests/{request_id}/execute", headers=headers
        )
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["status"] == "completed"
        workflow_id = payload["workflow_id"]

        graph = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
        assert graph.status_code == 200
        nodes = graph.json()["nodes"]
        assert {n["agent_type"] for n in nodes} == {
            "research",
            "rag",
            "analysis",
            "synthesis",
        }
        assert len(graph.json()["edges"]) >= 3

        tasks = client.get(f"/api/v1/workflows/{workflow_id}/tasks", headers=headers).json()
        assert len(tasks["tasks"]) == 4
        assert all(t["status"] == "completed" for t in tasks["tasks"])
        research_task = next(t for t in tasks["tasks"] if t["agent_type"] == "research")
        assert research_task["tools_used"] == ["knowledge.search"]

        result = client.get(f"/api/v1/workflows/{workflow_id}/result", headers=headers).json()
        assert result["answer"].strip()
        assert result["citations"], "workspace needs real citations"
        assert result["tools_used"] == ["knowledge.search"]
        assert result["confidence"] is not None

        # Reconciliation: the workspace result exposes a *synthesis* confidence that is
        # distinct from the *workflow-level* evaluation. These are two separate concepts
        # and must not be conflated in docs or UI copy.
        assert isinstance(result["confidence"], (int, float)), "result.confidence must be numeric"

        evaluation = client.get(
            f"/api/v1/workflows/{workflow_id}/evaluations", headers=headers
        ).json()["evaluation"]
        assert evaluation["final_response"]["grounded"] is True
        assert evaluation["overall_score"] > 0

        # The workspace renders the workflow-level evaluation grid, so the evaluations
        # payload must carry the four workflow-level fields (planning, collaboration,
        # final_response, overall_score), not just a single verdict/score.
        assert set(evaluation) == {"planning", "collaboration", "final_response", "overall_score"}
        assert evaluation["planning"]["overall_score"] is not None
        assert evaluation["collaboration"]["score"] is not None
        assert evaluation["final_response"]["score"] is not None
        assert evaluation["final_response"]["citation_count"] == len(result["citations"])
        # Owner isolation for the demo workflow.
        stranger = _register(client, "stranger@example.com")
        for path in (
            f"/api/v1/workflows/{workflow_id}",
            f"/api/v1/workflows/{workflow_id}/tasks",
            f"/api/v1/workflows/{workflow_id}/result",
            f"/api/v1/workflows/{workflow_id}/evaluations",
        ):
            assert client.get(path, headers=stranger).status_code == 404, path

        # History: the run is listed for its owner only.
        history = client.get("/api/v1/requests", headers=headers).json()
        assert any(r["id"] == request_id for r in history)
        assert not any(
            r["id"] == request_id
            for r in client.get("/api/v1/requests", headers=stranger).json()
        )
        # The stranger's account still works (isolation, not breakage).
        assert client.get("/api/v1/requests", headers=stranger).status_code == 200

    def test_demo_endpoints_require_authentication(self) -> None:
        client = _api_client()
        assert client.get("/api/v1/requests").status_code == 401
        assert client.get("/api/v1/documents").status_code == 401