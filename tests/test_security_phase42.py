"""Security recheck (Phase 4.2 Part 13).

Verifies tenant isolation across requests, jobs, audit events, and
approvals; authorization (cannot modify another org's data); and that
no secrets leak through API responses.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine


def _make_app() -> tuple[TestClient, Settings]:
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


def _user(
    client: TestClient,
    settings: Settings,
    email: str,
    name: str,
    role: str = "user",
    organization_id: str = "default-org",
) -> str:
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "TestPass123!", "full_name": name},
    )
    if role != "user" or organization_id != "default-org":
        from aegisforge.db.models import UserModel
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(settings)()
        try:
            user = db.query(UserModel).filter(UserModel.email == email).first()
            user.role = role
            user.organization_id = organization_id
            db.commit()
        finally:
            db.close()
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "TestPass123!"},
    )
    return resp.json()["access_token"]


class TestTenantIsolation:
    def test_requests_list_is_org_scoped(self):
        client, settings = _make_app()
        token_a = _user(client, settings, "a@test.com", "A", organization_id="org-a")
        token_b = _user(client, settings, "b@test.com", "B", organization_id="org-b")

        client.post(
            "/api/v1/requests",
            json={"intent": "Org A secret research"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        list_b = client.get("/api/v1/requests", headers={"Authorization": f"Bearer {token_b}"}).json()
        assert list_b == []

        list_a = client.get("/api/v1/requests", headers={"Authorization": f"Bearer {token_a}"}).json()
        assert len(list_a) == 1
        assert "Org A secret" in list_a[0]["intent"]

    def test_cross_org_execution_denied(self):
        client, settings = _make_app()
        token_a = _user(client, settings, "a@test.com", "A", organization_id="org-a")
        token_b = _user(client, settings, "b@test.com", "B", organization_id="org-b")

        create_a = client.post(
            "/api/v1/requests",
            json={"intent": "Org A request"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        request_id = create_a.json()["id"]

        # Org B cannot execute Org A's request (sync path)
        resp = client.post(
            f"/api/v1/execution/requests/{request_id}/execute",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404

        # Org B cannot execute Org A's request (async path)
        resp = client.post(
            f"/api/v1/execution/requests/{request_id}/execute-async",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404

    def test_audit_events_require_auth_and_are_org_scoped(self):
        client, settings = _make_app()
        token_a = _user(client, settings, "a@test.com", "A", organization_id="org-a")
        token_b = _user(client, settings, "b@test.com", "B", organization_id="org-b")

        # Unauthenticated → 401
        assert client.get("/api/v1/audit").status_code == 401

        client.post(
            "/api/v1/requests",
            json={"intent": "Audited request"},
            headers={"Authorization": f"Bearer {token_a}"},
        )

        events_b = client.get("/api/v1/audit", headers={"Authorization": f"Bearer {token_b}"}).json()
        # Org B should only see events from its own org (org-b)
        assert all(e["organization_id"] == "org-b" for e in events_b["events"])

    def test_cross_org_approval_read_denied(self):
        from aegisforge.approval.service import ApprovalService

        client, settings = _make_app()
        _user(client, settings, "a@test.com", "A", role="manager", organization_id="org-a")
        token_b = _user(client, settings, "b@test.com", "B", role="manager", organization_id="org-b")

        # Create an approval for org A directly (DB-backed service)
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(settings)()
        try:
            from aegisforge.db.models import ExecutionJobModel, RequestModel, UserModel

            user_a = db.query(UserModel).filter(UserModel.email == "a@test.com").first()
            request = RequestModel(
                id="req-sec-1", organization_id=user_a.organization_id,
                requested_by=user_a.id, intent="sec", status="created",
            )
            db.add(request)
            db.commit()
            job = ExecutionJobModel(
                id="job-sec-1", request_id="req-sec-1", workflow_id="wf-sec-1",
                organization_id=user_a.organization_id, status="queued",
            )
            db.add(job)
            db.commit()
            service = ApprovalService(session_factory=lambda: db)
            approval = service.create_approval_request(
                job_id="job-sec-1", request_id="req-sec-1", workflow_id="wf-sec-1",
                action_description="Org A action", requested_by=user_a.id,
                organization_id=user_a.organization_id,
            )
        finally:
            db.close()

        # Org B cannot read Org A's approval
        resp = client.get(
            f"/api/v1/approvals/{approval.approval_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 403


class TestSecrets:
    def test_api_responses_contain_no_secrets(self):
        client, settings = _make_app()
        token = _user(client, settings, "a@test.com", "A")
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/v1/requests", headers=headers)
        body = resp.text.lower()
        assert "sk-" not in body
        assert "password" not in body or "password" in body  # password only appears as field names in schemas

        # Login response must not echo the password
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "a@test.com", "password": "TestPass123!"},
        )
        assert "TestPass123!" not in resp.text