"""Prometheus instrumentation verification (Phase 4.2 Part 3 + 5).

Every test exercises a REAL code path (HTTP middleware, workflow,
agents, tools, LLM providers, RAG retrieval, approvals, job queue) and
asserts the corresponding Prometheus metric moved. No values are
fabricated.
"""
from __future__ import annotations

import re

from aegisforge.approval.service import ApprovalService
from aegisforge.domain.models import RiskLevel


def _metric_value(metrics_text: str, name: str, labels: dict[str, str]) -> float:
    """Parse a metric value from the /metrics text format for exact labels."""
    escaped = re.escape(name)
    if labels:
        label_str = ",".join(f'{k}="{re.escape(v)}"' for k, v in sorted(labels.items()))
        pattern = rf"^{escaped}\{{{label_str}\}} (\S+)"
    else:
        pattern = rf"^{escaped} (\S+)"
    for line in metrics_text.splitlines():
        m = re.match(pattern, line)
        if m:
            return float(m.group(1))
    return 0.0


def _make_client():
    from fastapi.testclient import TestClient

    from aegisforge.app import create_app
    from aegisforge.config import Settings
    from aegisforge.db.base import Base
    from aegisforge.db.session import get_engine

    get_engine.cache_clear()
    settings = Settings(
        database_url="sqlite:///:memory:",
        secret_key="test-secret",
        environment="test",
    )
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    app = create_app(settings=settings)
    return TestClient(app), settings


class TestHTTPMetrics:
    def test_http_metrics_change_after_request(self):
        client, _ = _make_client()
        before = client.get("/metrics").text
        client.get("/api/v1/health")
        after = client.get("/metrics").text

        assert _metric_value(after, "http_requests_total", {"method": "GET", "endpoint": "/api/v1/health", "status": "200"}) > \
            _metric_value(before, "http_requests_total", {"method": "GET", "endpoint": "/api/v1/health", "status": "200"})


class TestWorkflowMetrics:
    def test_workflow_metrics_change_after_execution(self):
        client, settings = _make_client()
        token = _register(client, settings)

        before = client.get("/metrics").text
        resp = client.post(
            "/api/v1/requests",
            json={"intent": "Research AI frameworks"},
            headers={"Authorization": f"Bearer {token}"},
        )
        request_id = resp.json()["id"]
        client.post(
            f"/api/v1/execution/requests/{request_id}/execute",
            headers={"Authorization": f"Bearer {token}"},
        )
        after = client.get("/metrics").text

        assert _metric_value(after, "workflow_runs_total", {"status": "completed"}) > \
            _metric_value(before, "workflow_runs_total", {"status": "completed"})
        assert _metric_value(after, "agent_executions_total", {"agent_type": "planner", "status": "completed"}) > \
            _metric_value(before, "agent_executions_total", {"agent_type": "planner", "status": "completed"})
        assert _metric_value(after, "agent_executions_total", {"agent_type": "research", "status": "completed"}) > \
            _metric_value(before, "agent_executions_total", {"agent_type": "research", "status": "completed"})
        # The research agent calls the knowledge.search tool
        assert _metric_value(after, "tool_executions_total", {"tool_name": "knowledge.search", "status": "success"}) > \
            _metric_value(before, "tool_executions_total", {"tool_name": "knowledge.search", "status": "success"})


class TestLLMMetrics:
    def test_llm_metrics_change_after_provider_call(self):
        from aegisforge.llm.providers import DeterministicModelProvider

        provider = DeterministicModelProvider()
        from aegisforge.observability.metrics import get_metrics

        before = get_metrics().decode()
        provider.generate([{"role": "user", "content": "Hello world"}])
        after = get_metrics().decode()

        assert _metric_value(after, "llm_requests_total", {"provider": "deterministic", "model": "deterministic-fake", "status": "success"}) > \
            _metric_value(before, "llm_requests_total", {"provider": "deterministic", "model": "deterministic-fake", "status": "success"})
        # Token usage is genuinely reported by the deterministic provider
        assert _metric_value(after, "llm_tokens_total", {"provider": "deterministic", "model": "deterministic-fake", "type": "total"}) > 0

    def test_llm_metrics_change_inside_workflow(self):
        from aegisforge.llm.providers import DeterministicModelProvider
        from aegisforge.observability.metrics import get_metrics
        from aegisforge.workflows.langgraph_workflow import execute_workflow

        provider = DeterministicModelProvider()
        before = get_metrics().decode()
        execute_workflow(
            request_id="req-llm-metrics",
            intent="Research LLM observability",
            user_id="user-1",
            organization_id="org-1",
            model_provider=provider,
        )
        after = get_metrics().decode()
        assert _metric_value(after, "llm_requests_total", {"provider": "deterministic", "model": "deterministic-fake", "status": "success"}) > \
            _metric_value(before, "llm_requests_total", {"provider": "deterministic", "model": "deterministic-fake", "status": "success"})


class TestRAGMetrics:
    def test_rag_metrics_change_after_retrieval(self):
        from aegisforge.domain.models import RetrievalQuery
        from aegisforge.observability.metrics import get_metrics
        from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
        from aegisforge.rag.retrieval import RetrievalService
        from aegisforge.rag.vector_store import InMemoryVectorStore, VectorStoreEntry

        provider = DeterministicEmbeddingProvider(dimension=8)
        store = InMemoryVectorStore()
        emb = provider.embed_text("AI agents are autonomous software entities")
        store.add(
            [VectorStoreEntry(id="c1", content="AI agents are autonomous software entities", embedding=emb, metadata={"document_id": "d1"})],
            organization_id="org-1",
        )
        service = RetrievalService(embedding_provider=provider, vector_store=store)

        before = get_metrics().decode()
        results = service.retrieve(RetrievalQuery(query="AI agents", organization_id="org-1", top_k=3, similarity_threshold=-1.0))
        after = get_metrics().decode()

        assert len(results) == 1
        assert _metric_value(after, "rag_retrieval_total", {"status": "success"}) > \
            _metric_value(before, "rag_retrieval_total", {"status": "success"})

    def test_rag_insufficient_context_metric(self):
        from aegisforge.domain.models import RetrievalQuery
        from aegisforge.observability.metrics import get_metrics
        from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
        from aegisforge.rag.retrieval import RetrievalService
        from aegisforge.rag.vector_store import InMemoryVectorStore

        provider = DeterministicEmbeddingProvider(dimension=8)
        service = RetrievalService(embedding_provider=provider, vector_store=InMemoryVectorStore())

        before = get_metrics().decode()
        service.retrieve(RetrievalQuery(query="nothing relevant here", organization_id="org-1"))
        after = get_metrics().decode()

        assert _metric_value(after, "rag_insufficient_context_total", {}) > \
            _metric_value(before, "rag_insufficient_context_total", {})


class TestApprovalMetrics:
    def test_approval_metrics_change_after_decision(self):
        from aegisforge.observability.metrics import get_metrics

        service = ApprovalService()
        before = get_metrics().decode()
        approval = service.create_approval_request(
            job_id="j1", request_id="r1", workflow_id="w1",
            action_description="Deploy", requested_by="u1",
            risk_level=RiskLevel.HIGH,
        )
        service.approve(approval.approval_id, "admin-1")
        after = get_metrics().decode()

        assert _metric_value(after, "approval_requests_total", {"risk_level": "high", "status": "requested"}) > \
            _metric_value(before, "approval_requests_total", {"risk_level": "high", "status": "requested"})
        assert _metric_value(after, "approval_requests_total", {"risk_level": "high", "status": "approved"}) > \
            _metric_value(before, "approval_requests_total", {"risk_level": "high", "status": "approved"})


class TestQueueMetrics:
    def test_queue_metrics_change_after_worker_run(self):
        from aegisforge.async_execution.jobs import InMemoryJobQueue, JobManager, JobWorker
        from aegisforge.observability.metrics import get_metrics

        queue = InMemoryJobQueue()
        manager = JobManager(queue)
        worker = JobWorker(manager, lambda job: {"done": True})

        before = get_metrics().decode()
        manager.submit_job(request_id="r1", workflow_id="w1", organization_id="org-1")
        worker.process_next_job()
        after = get_metrics().decode()

        assert _metric_value(after, "queue_jobs_total", {"status": "queued"}) > \
            _metric_value(before, "queue_jobs_total", {"status": "queued"})
        assert _metric_value(after, "queue_jobs_total", {"status": "completed"}) > \
            _metric_value(before, "queue_jobs_total", {"status": "completed"})


def _register(client, settings):  # type: ignore[no-untyped-def]
    """Register + login a test user."""
    client.post(
        "/api/v1/auth/register",
        json={"email": "metrics@test.com", "password": "TestPass123!", "full_name": "Metrics"},
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "metrics@test.com", "password": "TestPass123!"},
    )
    return resp.json()["access_token"]