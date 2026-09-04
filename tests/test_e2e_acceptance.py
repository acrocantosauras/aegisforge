"""End-to-end acceptance test for the full execution pipeline.

Tests the complete flow:
    Authenticated User → Create Request → Planner → LangGraph Workflow →
    Research Agent → Permission Check → Approved Tool → Tool Result →
    Agent Result → Evaluation → Completion → Persisted Execution → Audit Event
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine


def _make_test_app():
    settings = Settings(
        app_name="aegisforge-e2e-test",
        environment="test",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="e2e-test-secret",
        access_token_expire_minutes=60,
    )
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    app = create_app(settings=settings)
    return app, settings


def test_e2e_full_execution_pipeline() -> None:
    """Complete end-to-end test of the execution pipeline."""
    app, _settings = _make_test_app()
    client = TestClient(app)

    # Step 1: Register a user
    register_resp = client.post(
        "/api/v1/auth/register",
        json={"email": "e2e@example.com", "password": "SecurePass123!", "full_name": "E2E User"},
    )
    assert register_resp.status_code == 201, register_resp.text

    # Step 2: Login and get token
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "e2e@example.com", "password": "SecurePass123!"},
    )
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Step 3: Create a request
    create_resp = client.post(
        "/api/v1/requests",
        json={"intent": "Find recent guidance on support escalation policies"},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    request_id = create_resp.json()["id"]
    assert create_resp.json()["intent"] == "Find recent guidance on support escalation policies"

    # Step 4: Execute the full workflow
    exec_resp = client.post(
        f"/api/v1/execution/requests/{request_id}/execute",
        headers=headers,
    )
    assert exec_resp.status_code == 200, exec_resp.text
    exec_result = exec_resp.json()

    # Step 5: Verify execution result
    assert exec_result["status"] == "completed"
    assert exec_result["request_id"] == request_id
    assert exec_result["workflow_id"]
    assert not exec_result["errors"]

    # Step 6: Verify the final result contains research data
    final = exec_result["final_result"]
    assert final["status"] == "completed"
    assert final["result"]["answer"]  # Should have research content
    assert final["result"]["source"]  # Should have a source
    assert final["tool_calls"]  # Should have made tool calls

    # Step 7: Verify request status was updated
    detail_resp = client.get(f"/api/v1/requests/{request_id}", headers=headers)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["status"] == "completed"

    # Step 8: Verify audit events were recorded
    audit_resp = client.get("/api/v1/audit/events", headers=headers)
    assert audit_resp.status_code == 200
    events = audit_resp.json()
    actions = [e["action"] for e in events]
    assert "workflow.started" in actions
    assert "workflow.completed" in actions


def test_e2e_api_contract_validation() -> None:
    """Verify API contracts for the execution endpoint."""
    app, _settings = _make_test_app()
    client = TestClient(app)

    # Register and login
    client.post(
        "/api/v1/auth/register",
        json={"email": "contract@example.com", "password": "SecurePass123!", "full_name": "Contract User"},
    )
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "contract@example.com", "password": "SecurePass123!"},
    )
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Try to execute without auth
    exec_resp = client.post("/api/v1/execution/requests/fake-id/execute")
    assert exec_resp.status_code in (401, 403)

    # Try to execute non-existent request
    exec_resp = client.post(
        "/api/v1/execution/requests/nonexistent/execute",
        headers=headers,
    )
    assert exec_resp.status_code == 404
