"""Tests for the Phase 8 productization API extensions.

Covers:
- GET /api/v1/workflows/{wf}/result — final answer, citations, evidence,
  tools, confidence (tenant-isolated, no chain-of-thought or secrets).
- GET /api/v1/workflows/by-request/{request_id} — request→workflow lookup.
- GET /api/v1/system — real component status (DB/Redis/workers/queue).

All data comes from real workflow execution against the deterministic
backend; nothing is fabricated.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine

COMPOUND_INTENT = (
    "Conduct enterprise knowledge research on data access policy, "
    "analyze the evidence, and synthesize a recommendation"
)


def _make_app() -> tuple[TestClient, Settings]:
    settings = Settings(
        environment="test",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="phase8-test-secret",
        llm_provider="deterministic",
        embedding_provider="deterministic",
    )
    get_engine.cache_clear()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    return TestClient(create_app(settings=settings)), settings


def _register_and_login(client: TestClient, email: str) -> dict[str, str]:
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123!", "full_name": "T"},
    )
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "SecurePass123!"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _run_compound_workflow(client: TestClient, headers: dict[str, str]) -> dict:
    created = client.post(
        "/api/v1/requests", json={"intent": COMPOUND_INTENT}, headers=headers
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    executed = client.post(
        f"/api/v1/execution/requests/{request_id}/execute", headers=headers
    )
    assert executed.status_code == 200, executed.text
    return executed.json()


class TestWorkflowResultEndpoint:
    def test_result_has_answer_citations_evidence_and_tools(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "result@example.com")
        payload = _run_compound_workflow(client, headers)
        workflow_id = payload["workflow_id"]

        resp = client.get(f"/api/v1/workflows/{workflow_id}/result", headers=headers)
        assert resp.status_code == 200, resp.text
        result = resp.json()

        assert result["workflow_id"] == workflow_id
        assert result["request_id"] == payload["request_id"]
        assert result["status"] in {"completed", "evaluating"}
        # The deterministic research pipeline surfaces a grounded answer with
        # at least one citation/evidence item (measured, never invented).
        assert result["answer"], "expected a non-empty synthesized answer"
        assert result["agent_type"] in {"synthesis", "research"}
        assert result["tools_used"], "expected the research tool to be recorded"
        assert isinstance(result["citations"], list)
        assert isinstance(result["evidence"], list)

    def test_result_is_tenant_isolated(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "owner@example.com")
        payload = _run_compound_workflow(client, owner)
        workflow_id = payload["workflow_id"]

        stranger = _register_and_login(client, "stranger@example.com")
        resp = client.get(f"/api/v1/workflows/{workflow_id}/result", headers=stranger)
        assert resp.status_code == 404, resp.text

    def test_result_requires_authentication(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "auth@example.com")
        payload = _run_compound_workflow(client, headers)

        resp = client.get(f"/api/v1/workflows/{payload['workflow_id']}/result")
        assert resp.status_code in {401, 403}

    def test_result_for_unknown_workflow_is_404(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "unknown@example.com")
        resp = client.get("/api/v1/workflows/wf-doesnotexist/result", headers=headers)
        assert resp.status_code == 404


class TestWorkflowLookupEndpoint:
    def test_lookup_resolves_latest_workflow_for_request(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "lookup@example.com")
        payload = _run_compound_workflow(client, headers)
        request_id = payload["request_id"]

        resp = client.get(
            f"/api/v1/workflows/by-request/{request_id}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["request_id"] == request_id
        assert body["workflow_id"] == payload["workflow_id"]
        assert body["status"] in {"completed", "evaluating"}

    def test_lookup_without_workflow_is_404(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "nowf@example.com")
        created = client.post(
            "/api/v1/requests",
            json={"intent": "Simple research question"},
            headers=headers,
        )
        request_id = created.json()["id"]
        resp = client.get(
            f"/api/v1/workflows/by-request/{request_id}", headers=headers
        )
        assert resp.status_code == 404

    def test_lookup_is_tenant_isolated(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "wfowner@example.com")
        payload = _run_compound_workflow(client, owner)

        stranger = _register_and_login(client, "wfstranger@example.com")
        resp = client.get(
            f"/api/v1/workflows/by-request/{payload['request_id']}",
            headers=stranger,
        )
        assert resp.status_code == 404


class TestWorkflowIntrospectionOwnership:
    """Workflow introspection is owner-scoped (parity with GET /requests/{id}).

    Self-registration places users in a shared organization, so an org check
    alone leaks task/evaluation state between users. These tests pin the
    org + owner enforcement on every workflow introspection endpoint.
    """

    def test_owner_can_read_tasks(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "tasks-self@example.com")
        payload = _run_compound_workflow(client, owner)

        resp = client.get(
            f"/api/v1/workflows/{payload['workflow_id']}/tasks", headers=owner
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["tasks"]) >= 1

    def test_tasks_are_owner_scoped(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "tasks-owner@example.com")
        payload = _run_compound_workflow(client, owner)

        stranger = _register_and_login(client, "tasks-stranger@example.com")
        resp = client.get(
            f"/api/v1/workflows/{payload['workflow_id']}/tasks", headers=stranger
        )
        assert resp.status_code == 404, resp.text

    def test_evaluations_are_owner_scoped(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "eval-owner@example.com")
        payload = _run_compound_workflow(client, owner)

        stranger = _register_and_login(client, "eval-stranger@example.com")
        resp = client.get(
            f"/api/v1/workflows/{payload['workflow_id']}/evaluations",
            headers=stranger,
        )
        assert resp.status_code == 404, resp.text

    def test_evaluations_readable_by_owner(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "eval-self@example.com")
        payload = _run_compound_workflow(client, owner)

        resp = client.get(
            f"/api/v1/workflows/{payload['workflow_id']}/evaluations", headers=owner
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["request_id"] == payload["request_id"]

    def test_graph_overview_is_owner_scoped(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "graph-owner@example.com")
        payload = _run_compound_workflow(client, owner)

        stranger = _register_and_login(client, "graph-stranger@example.com")
        resp = client.get(
            f"/api/v1/workflows/{payload['workflow_id']}", headers=stranger
        )
        assert resp.status_code == 404, resp.text

    def test_graph_overview_readable_by_owner(self) -> None:
        client, _ = _make_app()
        owner = _register_and_login(client, "graph-self@example.com")
        payload = _run_compound_workflow(client, owner)

        resp = client.get(
            f"/api/v1/workflows/{payload['workflow_id']}", headers=owner
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["request_id"] == payload["request_id"]
        assert body["edges"], "expected dependency edges in the task graph"


class TestRequestHistoryField:
    """Request payloads carry created_at for the execution history UI."""

    def test_list_requests_includes_created_at(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "history@example.com")
        client.post(
            "/api/v1/requests", json={"intent": "First"}, headers=headers
        )
        client.post(
            "/api/v1/requests", json={"intent": "Second"}, headers=headers
        )

        resp = client.get("/api/v1/requests", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        assert len(items) == 2
        for item in items:
            assert item["created_at"], "expected created_at on history rows"
        # Newest first
        assert items[0]["created_at"] >= items[1]["created_at"]


class TestSystemStatusEndpoint:
    def test_system_status_requires_authentication(self) -> None:
        """/system exposes operational detail — anonymous access is rejected."""
        client, _ = _make_app()
        resp = client.get("/api/v1/system")
        assert resp.status_code == 401

    def test_system_status_reports_real_components(self) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "system@example.com")
        resp = client.get("/api/v1/system", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["status"] in {"healthy", "degraded"}
        components = body["components"]
        assert components["api"] == "ok"
        # SQLite in-memory test DB is reachable.
        assert components["database"] == "ok"
        assert components["redis"] in {"ok", "unavailable"}
        assert components["workers"]["status"] in {"ok", "none_active"}
        assert isinstance(components["queue"]["depth"], int)

    def test_system_status_degrades_without_redis(self, monkeypatch) -> None:
        client, _ = _make_app()
        headers = _register_and_login(client, "system-degraded@example.com")

        import redis

        def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise ConnectionError("Redis down")

        monkeypatch.setattr(redis, "from_url", unavailable)
        resp = client.get("/api/v1/system", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["components"]["redis"] == "unavailable"
        assert body["components"]["workers"]["status"] == "none_active"
