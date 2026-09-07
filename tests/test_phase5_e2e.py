from __future__ import annotations

from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine


def _client(environment: str = "test") -> TestClient:
    settings = Settings(
        environment=environment,
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="phase5-e2e-secret",
        llm_provider="deterministic",
        embedding_provider="deterministic",
    )
    get_engine.cache_clear()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    return TestClient(create_app(settings=settings))


def _authenticated_client() -> tuple[TestClient, dict[str, str]]:
    client = _client()
    client.post(
        "/api/v1/auth/register",
        json={
            "email": "phase5-e2e@example.com",
            "password": "SecurePass123!",
            "full_name": "Phase 5 E2E",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "phase5-e2e@example.com", "password": "SecurePass123!"},
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_phase5_api_multi_agent_execution_and_introspection() -> None:
    client, headers = _authenticated_client()
    created = client.post(
        "/api/v1/requests",
        json={
            "intent": (
                "Conduct enterprise knowledge research on support escalation policy, "
                "analyze the evidence, and synthesize a recommendation"
            )
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    executed = client.post(
        f"/api/v1/execution/requests/{request_id}/execute",
        headers=headers,
    )
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["status"] == "completed"

    workflow_id = result["workflow_id"]
    graph = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert graph.status_code == 200, graph.text
    graph_payload = graph.json()
    agent_types = {node["agent_type"] for node in graph_payload["nodes"]}
    assert {"research", "rag", "analysis", "synthesis"} <= agent_types
    assert any(edge["from"] for edge in graph_payload["edges"])

    tasks = client.get(f"/api/v1/workflows/{workflow_id}/tasks", headers=headers)
    assert tasks.status_code == 200, tasks.text
    assert all(task["status"] == "completed" for task in tasks.json()["tasks"])

    evaluation = client.get(
        f"/api/v1/workflows/{workflow_id}/evaluations",
        headers=headers,
    )
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["evaluation"]


def test_production_async_execution_fails_closed_without_redis(monkeypatch) -> None:
    client, headers = _authenticated_client()
    created = client.post(
        "/api/v1/requests",
        json={"intent": "Research support policy"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    import redis

    def unavailable(*args, **kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(redis, "from_url", unavailable)
    settings = Settings(
        environment="production",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="phase5-production-test-secret",
    )
    get_engine.cache_clear()
    Base.metadata.create_all(bind=get_engine(settings.database_url))
    production_client = TestClient(create_app(settings=settings))
    production_client.post(
        "/api/v1/auth/register",
        json={
            "email": "production-redis@example.com",
            "password": "SecurePass123!",
            "full_name": "Production Redis",
        },
    )
    login = production_client.post(
        "/api/v1/auth/login",
        json={"email": "production-redis@example.com", "password": "SecurePass123!"},
    )
    production_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    request = production_client.post(
        "/api/v1/requests",
        json={"intent": "Research support policy"},
        headers=production_headers,
    )
    response = production_client.post(
        f"/api/v1/execution/requests/{request.json()['id']}/execute-async",
        headers=production_headers,
    )
    assert response.status_code == 503
