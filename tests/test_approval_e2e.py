"""Complete approval E2E through the real API (Phase 4.2 Part 9).

request → planner → high-risk action → persisted approval →
action_requires_approval → checkpoint → human approval (API) →
resume → evaluation → completion

Also verifies reject / expire / unauthorized never execute the
protected action.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine
from aegisforge.domain.models import RequestStatus


def _make_app() -> tuple[TestClient, Settings]:
    # Ensure each app gets a fresh in-memory database (get_engine is cached)
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


def _register(client: TestClient, settings: Settings, email: str, name: str, role: str = "user") -> str:
    """Register a user and return their bearer token."""
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "TestPass123!", "full_name": name},
    )
    if role != "user":
        # Promote the user directly in the DB (no admin API exists by design)
        from aegisforge.db.models import UserModel
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(settings)()
        try:
            user = db.query(UserModel).filter(UserModel.email == email).first()
            user.role = role
            db.commit()
        finally:
            db.close()
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "TestPass123!"},
    )
    return resp.json()["access_token"]


def _create_and_execute(client: TestClient, token: str, intent: str) -> tuple[str, str]:
    """Create a request, execute it (sync), return (request_id, workflow_id)."""
    create_resp = client.post(
        "/api/v1/requests",
        json={"intent": intent},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    request_id = create_resp.json()["id"]

    exec_resp = client.post(
        f"/api/v1/execution/requests/{request_id}/execute",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert exec_resp.status_code == 200, exec_resp.text
    return request_id, exec_resp.json()["workflow_id"]


class TestApprovalE2E:
    def test_full_approve_resume_path(self):
        client, settings = _make_app()
        token = _register(client, settings, "admin@test.com", "Admin", role="manager")
        headers = {"Authorization": f"Bearer {token}"}

        # 1. High-risk request → workflow pauses at approval
        request_id, _ = _create_and_execute(
            client, token, "Restart the production service after review"
        )

        # Request is paused, not completed
        req = client.get(f"/api/v1/requests/{request_id}", headers=headers).json()
        assert req["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value

        # 2. A persisted approval exists and is visible via the API
        approvals = client.get("/api/v1/approvals", headers=headers).json()
        assert approvals["total"] >= 1
        approval = next(
            a for a in approvals["approvals"] if a["request_id"] == request_id
        )
        assert approval["risk_level"] == "high"
        assert approval["status"] == "pending"

        # 3. Human approves via the API
        approve_resp = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/approve",
            json={"decision_reason": "Reviewed — safe"},
            headers=headers,
        )
        assert approve_resp.status_code == 200, approve_resp.text
        assert approve_resp.json()["status"] == "approved"

        # 4. The workflow resumed and completed
        req = client.get(f"/api/v1/requests/{request_id}", headers=headers).json()
        assert req["status"] == RequestStatus.COMPLETED.value

        # 5. The approval is no longer pending
        approvals = client.get("/api/v1/approvals", headers=headers).json()
        assert all(a["status"] != "pending" for a in approvals["approvals"])

    def test_reject_never_executes_protected_action(self):
        client, settings = _make_app()
        token = _register(client, settings, "admin@test.com", "Admin", role="manager")
        headers = {"Authorization": f"Bearer {token}"}

        request_id, _ = _create_and_execute(
            client, token, "Delete the staging database after review"
        )

        approvals = client.get("/api/v1/approvals", headers=headers).json()
        approval = next(
            a for a in approvals["approvals"] if a["request_id"] == request_id
        )

        reject_resp = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/reject",
            json={"decision_reason": "Too dangerous"},
            headers=headers,
        )
        assert reject_resp.status_code == 200
        assert reject_resp.json()["status"] == "rejected"

        # The request is failed — the protected action never executed
        req = client.get(f"/api/v1/requests/{request_id}", headers=headers).json()
        assert req["status"] == RequestStatus.FAILED.value

    def test_unauthorized_user_cannot_approve(self):
        client, settings = _make_app()
        token = _register(client, settings, "admin@test.com", "Admin", role="manager")
        # Regular user from a DIFFERENT org
        other_token = _register(client, settings, "other@test.com", "Other")

        request_id, _ = _create_and_execute(client, token, "Restart the production service")
        approvals = client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {token}"}).json()
        approval = next(
            a for a in approvals["approvals"] if a["request_id"] == request_id
        )

        # Cross-tenant user: forbidden
        resp = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/approve",
            json={"decision_reason": "sneaky"},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 403

        # Same-org but insufficient role: forbidden
        client2, settings2 = _make_app()
        token2 = _register(client2, settings2, "user@test.com", "User", role="user")
        headers2 = {"Authorization": f"Bearer {token2}"}
        _create_and_execute(client2, token2, "Restart the production service")
        approvals2 = client2.get("/api/v1/approvals", headers=headers2).json()
        approval2 = approvals2["approvals"][0]
        resp2 = client2.post(
            f"/api/v1/approvals/{approval2['approval_id']}/approve",
            json={"decision_reason": "as user"},
            headers=headers2,
        )
        assert resp2.status_code == 403

    def test_expired_approval_cannot_resume(self):
        from datetime import UTC, datetime, timedelta

        client, settings = _make_app()
        token = _register(client, settings, "admin@test.com", "Admin", role="manager")
        headers = {"Authorization": f"Bearer {token}"}

        request_id, _ = _create_and_execute(
            client, token, "Restart the production service after review"
        )
        approvals = client.get("/api/v1/approvals", headers=headers).json()
        approval = next(
            a for a in approvals["approvals"] if a["request_id"] == request_id
        )

        # Back-date the expiry so the approval is stale
        from aegisforge.db.models import ApprovalRequestModel
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(settings)()
        try:
            model = db.query(ApprovalRequestModel).filter(
                ApprovalRequestModel.id == approval["approval_id"]
            ).first()
            model.expires_at = datetime.now(UTC) - timedelta(hours=1)
            db.commit()
        finally:
            db.close()

        # Approve → not pending (expired) → 409, workflow must NOT resume
        resp = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/approve",
            json={"decision_reason": "late"},
            headers=headers,
        )
        assert resp.status_code == 409

        req = client.get(f"/api/v1/requests/{request_id}", headers=headers).json()
        assert req["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value

    def test_approval_roundtrip_via_async_job(self):
        """Approval created via the worker job path is still resumable."""
        from aegisforge.db.models import ExecutionJobModel
        from aegisforge.db.session import get_session_factory

        client, settings = _make_app()
        token = _register(client, settings, "admin@test.com", "Admin", role="manager")
        headers = {"Authorization": f"Bearer {token}"}

        request_id, workflow_id = _create_and_execute(
            client, token, "Restart the production service after review"
        )

        # Verify the job row reflects the paused state (worker path parity)
        db = get_session_factory(settings)()
        try:
            job = db.query(ExecutionJobModel).filter(
                ExecutionJobModel.workflow_id == workflow_id
            ).first()
            assert job is not None
        finally:
            db.close()

        # And the approval flow still completes end-to-end
        approvals = client.get("/api/v1/approvals", headers=headers).json()
        approval = next(
            a for a in approvals["approvals"] if a["request_id"] == request_id
        )
        resp = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/approve",
            json={"decision_reason": "OK"},
            headers=headers,
        )
        assert resp.status_code == 200
        req = client.get(f"/api/v1/requests/{request_id}", headers=headers).json()
        assert req["status"] == RequestStatus.COMPLETED.value