from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine


def _new_app() -> TestClient:
    settings = Settings(
        environment="test",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="serial-introspection-secret",
        llm_provider="deterministic",
        embedding_provider="deterministic",
    )
    get_engine.cache_clear()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    return TestClient(create_app(settings=settings))


def _register_and_login(client: TestClient, email: str) -> dict[str, str]:
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Qa25Passw0rd!x", "full_name": "QA Serial"},
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Qa25Passw0rd!x"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _run_serial_workflow(client: TestClient, headers: dict[str, str], expect_completed: bool = True) -> dict[str, str]:
    created = client.post(
        "/api/v1/requests",
        json={"intent": "Summarize the enterprise onboarding security controls."},
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
    assert result["status"] in ("completed", "failed")
    workflow_id = result["workflow_id"]

    wf = client.get(f"/api/v1/workflows/by-request/{request_id}", headers=headers)
    assert wf.status_code == 200, wf.text
    assert wf.json()["status"] == ("completed" if expect_completed else "failed")
    return {"request_id": request_id, "workflow_id": workflow_id, "status": wf.json()["status"]}


def _introspect(client: TestClient, headers: dict[str, str], payload: dict[str, str]) -> dict[str, object]:
    workflow_id = payload["workflow_id"]
    tasks = client.get(f"/api/v1/workflows/{workflow_id}/tasks", headers=headers)
    assert tasks.status_code == 200, tasks.text
    graph = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert graph.status_code == 200, graph.text
    evaluation = client.get(f"/api/v1/workflows/{workflow_id}/evaluations", headers=headers)
    assert evaluation.status_code == 200, evaluation.text
    result = client.get(f"/api/v1/workflows/{workflow_id}/result", headers=headers)
    assert result.status_code == 200, result.text
    return {
        "tasks": tasks.json()["tasks"],
        "graph_nodes": graph.json()["nodes"],
        "evaluation": evaluation.json()["evaluation"],
        "result": result.json(),
    }


def test_serial_path_reports_task_status_in_workspace_introspection() -> None:
    client = _new_app()
    headers = _register_and_login(client, "serial-intro-1@example.com")
    payload = _run_serial_workflow(client, headers, expect_completed=True)
    intro = _introspect(client, headers, payload)
    tasks = intro["tasks"]

    # A single-task legacy run now exposes one task record with the task's real
    # status, not the default ``pending`` every task inherits when task_records
    # is empty.
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_id"]
    assert task["status"] in ("completed", "failed")
    assert task["description"]
    assert task["agent_type"]

    nodes = intro["graph_nodes"]
    node = next(n for n in nodes if n["task_id"] == task["task_id"])
    assert node["status"] == task["status"]

    if payload["status"] == "completed":
        assert intro["result"]["answer"]
        assert intro["result"]["status"] == "completed"
        # The workflow evaluation must score the real final answer instead of
        # reporting produced=False / 0.00 for a serial run with no synthesis
        # record.
        final_response = intro["evaluation"].get("final_response") or {}
        assert final_response.get("produced") is True
        assert final_response.get("score", 0.0) > 0


def test_serial_path_failure_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deterministic evaluator normally passes this intent, so force a
    # failing verdict through the real evaluate -> retry_or_complete path.
    # The LLM critic may promote FAILED to RETRY until max_retries is spent,
    # but it can never turn it into PASSED, so the run ends terminal-failed.
    from aegisforge.domain.models import EvaluationResult, EvaluationVerdict
    from aegisforge.workflows import langgraph_workflow as lw

    class _AlwaysFailEvaluator:
        def evaluate(self, agent_result: object, expected_fields: list[str] | None = None) -> EvaluationResult:
            return EvaluationResult(
                verdict=EvaluationVerdict.FAILED,
                score=0.0,
                reasons=["forced failure for failure-visibility coverage"],
                retryable=False,
            )

    monkeypatch.setattr(lw, "ResultEvaluator", _AlwaysFailEvaluator)

    client = _new_app()
    headers = _register_and_login(client, "serial-intro-2@example.com")
    payload = _run_serial_workflow(client, headers, expect_completed=False)
    intro = _introspect(client, headers, payload)
    tasks = intro["tasks"]

    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"
    # Deterministic evaluator still runs on the final terminal state.
    assert intro["evaluation"]


def test_serial_path_task_records_are_owner_scoped() -> None:
    owner_client = _new_app()
    owner_headers = _register_and_login(owner_client, "serial-owner@example.com")
    payload = _run_serial_workflow(owner_client, owner_headers, expect_completed=True)

    stranger_client = _new_app()
    stranger_headers = _register_and_login(stranger_client, "serial-stranger@example.com")

    resp = stranger_client.get(
        f"/api/v1/workflows/{payload['workflow_id']}/tasks",
        headers=stranger_headers,
    )
    assert resp.status_code == 404, resp.text

    resp = stranger_client.get(
        f"/api/v1/workflows/{payload['workflow_id']}/result",
        headers=stranger_headers,
    )
    assert resp.status_code == 404, resp.text


def test_serial_path_retry_records_respect_max_retries() -> None:
    settings = Settings(
        environment="test",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="serial-retry-secret",
        llm_provider="deterministic",
        embedding_provider="deterministic",
        max_retries=1,
    )
    get_engine.cache_clear()
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    client = TestClient(create_app(settings=settings))
    headers = _register_and_login(client, "serial-retry@example.com")

    created = client.post(
        "/api/v1/requests",
        json={"intent": "Research the security onboarding controls."},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    executed = client.post(
        f"/api/v1/execution/requests/{request_id}/execute",
        headers=headers,
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] in ("completed", "failed")

    wf = client.get(
        f"/api/v1/workflows/by-request/{request_id}", headers=headers
    )
    assert wf.status_code == 200, wf.text
    assert wf.json()["status"] in ("completed", "failed")

    tasks = client.get(
        f"/api/v1/workflows/{wf.json()['workflow_id']}/tasks", headers=headers
    )
    assert tasks.status_code == 200, tasks.text
    assert len(tasks.json()["tasks"]) == 1
    assert tasks.json()["tasks"][0]["status"] in ("completed", "failed")
