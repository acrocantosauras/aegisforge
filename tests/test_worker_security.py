from __future__ import annotations

import pytest

from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.models import RequestModel, UserModel
from aegisforge.db.session import get_engine, get_session_factory
from aegisforge.domain.models import ExecutionJob
from aegisforge.worker import _create_job_handler


def _setup_database(tmp_path):
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'worker-security.db'}",
        embedding_provider="deterministic",
        llm_provider="deterministic",
    )
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    session = get_session_factory(settings)()
    session.add(
        UserModel(
            id="user-1",
            organization_id="org-b",
            email="worker-security@example.com",
            password_hash="hash",
            full_name="Worker Security",
        )
    )
    session.add(
        RequestModel(
            id="req-b",
            organization_id="org-b",
            requested_by="user-1",
            intent="test request",
        )
    )
    session.commit()
    session.close()
    return settings


def test_worker_rejects_cross_tenant_request(tmp_path) -> None:
    settings = _setup_database(tmp_path)
    handler = _create_job_handler(settings)
    job = ExecutionJob(
        job_id="job-cross-tenant",
        request_id="req-b",
        workflow_id="wf-cross-tenant",
        organization_id="org-a",
    )

    with pytest.raises(ValueError, match="not found for organization org-a"):
        handler(job)


def test_worker_accepts_same_tenant_request(tmp_path, monkeypatch) -> None:
    settings = _setup_database(tmp_path)
    monkeypatch.setattr(
        "aegisforge.workflows.langgraph_workflow.execute_workflow",
        lambda **kwargs: {
            "status": "failed",
            "errors": ["test"],
            "final_result": {},
        },
    )
    handler = _create_job_handler(settings)
    job = ExecutionJob(
        job_id="job-same-tenant",
        request_id="req-b",
        workflow_id="wf-same-tenant",
        organization_id="org-b",
    )

    result = handler(job)

    assert result["request_id"] == "req-b"
    assert result["status"] == "failed"
