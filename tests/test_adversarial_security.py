"""Adversarial security tests — AUDIT → ATTACK → FIX regression suite.

Covers the authorization fixes from the production hardening sprint:

- Anonymous rejection on operational and tool-executing endpoints
  (/system, /workers, /agents, /agents/research) that were previously
  reachable without a token.
- Same-shared-organization IDOR: self-registration places every user in
  ``default-org``, so an organization filter alone exposes every user's
  data.  Requests, execution, and documents must be owner-scoped too.
- No-existence-oracle policy: foreign resources answer 404 (never 403),
  evaluation metrics answer ``null`` identically for unknown/foreign/
  unauthenticated lookups.
- Malformed identifiers must fail closed (404/null), never 500 with a
  stack trace or SQL fragment in the body.
- Token attacks: missing, garbage, tampered-signature, expired, and
  wrong-key tokens are all rejected with 401 and no body leak.
- Identity spoofing: /agents/research derives user/organization from the
  verified bearer token, never from client-supplied body fields.

Evidence type: ADVERSARIAL (UNIT-level execution against the real app).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from jose import jwt

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine

PASSWORD = "AdversarialPass123!"


def _make_app() -> tuple[TestClient, Settings]:
    get_engine.cache_clear()
    settings = Settings(
        database_url="sqlite:///:memory:",
        secret_key="adversarial-test-secret",
        environment="test",
        llm_provider="deterministic",
        embedding_provider="deterministic",
    )
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    return TestClient(create_app(settings=settings)), settings


def _register_and_login(
    client: TestClient, settings: Settings, email: str
) -> tuple[str, dict[str, str]]:
    """Register + login; return (user_id, headers)."""
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Adv"},
    )
    assert reg.status_code in {201, 409}, reg.text
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    from aegisforge.db.models import UserModel
    from aegisforge.db.session import get_session_factory

    db = get_session_factory(settings)()
    try:
        user = db.query(UserModel).filter(UserModel.email == email).first()
        assert user is not None
        user_id = user.id
    finally:
        db.close()
    return user_id, {"Authorization": f"Bearer {token}"}


def _make_request(client: TestClient, headers: dict[str, str], intent: str) -> str:
    resp = client.post("/api/v1/requests", json={"intent": intent}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _no_stack_trace(resp) -> None:  # type: ignore[no-untyped-def]
    """A response body must never carry framework/DB internals."""
    body = resp.text.lower()
    for marker in ("traceback", "sqlalchemy", "stack trace", "file \"", "password_hash"):
        assert marker not in body, f"response leaked {marker!r}: {resp.text[:300]}"


class TestAnonymousRejected:
    """Operational/tool-executing endpoints must reject anonymous callers."""

    def test_operational_endpoints_require_auth(self) -> None:
        client, _ = _make_app()
        probes = [
            ("GET", "/api/v1/system", None),
            ("GET", "/api/v1/workers", None),
            ("GET", "/api/v1/requests", None),
            ("GET", "/api/v1/requests/req-x", None),
            ("GET", "/api/v1/documents", None),
            ("GET", "/api/v1/approvals", None),
            ("GET", "/api/v1/approvals/ap-x", None),
            ("GET", "/api/v1/agents", None),
            ("GET", "/api/v1/evaluation/metrics", None),
            ("GET", "/api/v1/evaluation/metrics/req-x", None),
            ("POST", "/api/v1/agents/research", {"query": "x"}),
            ("POST", "/api/v1/execution/requests/req-x/execute", None),
            ("POST", "/api/v1/execution/requests/req-x/execute-async", None),
        ]
        for method, path, body in probes:
            resp = client.request(
                method, path, json=body if body is not None else None
            )
            assert resp.status_code == 401, (
                f"{method} {path} anonymous => {resp.status_code}, body={resp.text[:200]}"
            )
            _no_stack_trace(resp)

    def test_liveness_and_readiness_stay_public(self) -> None:
        """Healthcheck probes must remain reachable without a token."""
        client, _ = _make_app()
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code in {200, 503}


class TestCrossUserIDOR:
    """User B attacking user A's resources inside the shared default org."""

    def setup_method(self) -> None:
        self.client, self.settings = _make_app()
        self.a_id, self.a_headers = _register_and_login(
            self.client, self.settings, "victim@example.com"
        )
        self.b_id, self.b_headers = _register_and_login(
            self.client, self.settings, "attacker@example.com"
        )
        self.request_id = _make_request(
            self.client, self.a_headers, "Analyze confidential revenue figures"
        )

    def test_requests_list_is_owner_scoped(self) -> None:
        b_list = self.client.get("/api/v1/requests", headers=self.b_headers)
        assert b_list.status_code == 200
        b_ids = {item["id"] for item in b_list.json()}
        assert self.request_id not in b_ids, "shared-org leak: B sees A's request"

        a_list = self.client.get("/api/v1/requests", headers=self.a_headers)
        assert {item["id"] for item in a_list.json()} == {self.request_id}

    def test_get_foreign_request_is_404(self) -> None:
        resp = self.client.get(
            f"/api/v1/requests/{self.request_id}", headers=self.b_headers
        )
        assert resp.status_code == 404
        _no_stack_trace(resp)
        # Owner is unaffected
        own = self.client.get(
            f"/api/v1/requests/{self.request_id}", headers=self.a_headers
        )
        assert own.status_code == 200

    def test_patch_foreign_request_is_404_and_state_unchanged(self) -> None:
        resp = self.client.patch(
            f"/api/v1/requests/{self.request_id}/status",
            json={"status": "failed"},
            headers=self.b_headers,
        )
        assert resp.status_code == 404
        state = self.client.get(
            f"/api/v1/requests/{self.request_id}", headers=self.a_headers
        ).json()
        assert state["status"] != "failed", "attacker mutated A's request state"

    def test_execute_foreign_request_is_404_sync_and_async(self) -> None:
        sync = self.client.post(
            f"/api/v1/execution/requests/{self.request_id}/execute",
            headers=self.b_headers,
        )
        assert sync.status_code == 404
        async_resp = self.client.post(
            f"/api/v1/execution/requests/{self.request_id}/execute-async",
            headers=self.b_headers,
        )
        assert async_resp.status_code == 404
        _no_stack_trace(async_resp)

    def test_evaluation_metrics_foreign_request_is_null(self) -> None:
        foreign = self.client.get(
            f"/api/v1/evaluation/metrics/{self.request_id}", headers=self.b_headers
        )
        unknown = self.client.get(
            "/api/v1/evaluation/metrics/req-nope", headers=self.b_headers
        )
        assert foreign.status_code == 200 and foreign.json() is None
        assert unknown.status_code == 200 and unknown.json() is None
        # Indistinguishable — not an existence oracle
        assert foreign.text == unknown.text

    def test_documents_are_owner_scoped(self) -> None:
        up = self.client.post(
            "/api/v1/documents",
            files={"file": ("salary.txt", b"Alice salary is 120k", "text/plain")},
            headers=self.a_headers,
        )
        assert up.status_code == 201, up.text
        doc_id = up.json()["document_id"]

        # B's list must not include A's document
        b_list = self.client.get("/api/v1/documents", headers=self.b_headers)
        assert b_list.status_code == 200
        assert doc_id not in {d["id"] for d in b_list.json()["documents"]}

        # B cannot read it
        b_get = self.client.get(f"/api/v1/documents/{doc_id}", headers=self.b_headers)
        assert b_get.status_code == 404
        _no_stack_trace(b_get)

        # B cannot delete it (and the attempt must not destroy A's data)
        b_del = self.client.delete(f"/api/v1/documents/{doc_id}", headers=self.b_headers)
        assert b_del.status_code == 404
        a_get = self.client.get(f"/api/v1/documents/{doc_id}", headers=self.a_headers)
        assert a_get.status_code == 200, "A's document was destroyed by B's delete"

        # A can still delete their own document
        a_del = self.client.delete(f"/api/v1/documents/{doc_id}", headers=self.a_headers)
        assert a_del.status_code == 204

    def test_audit_scoped_to_caller(self) -> None:
        resp = self.client.get("/api/v1/audit", headers=self.b_headers)
        assert resp.status_code == 200
        _no_stack_trace(resp)


class TestMalformedIdentifiers:
    """Malformed ids must fail closed — never a 500 or an internal leak."""

    def setup_method(self) -> None:
        self.client, self.settings = _make_app()
        _, self.headers = _register_and_login(
            self.client, self.settings, "malformed@example.com"
        )

    def test_malformed_ids_do_not_500(self) -> None:
        probes = [
            ("GET", "/api/v1/requests/not-a-uuid", None),
            (
                "PATCH",
                "/api/v1/requests/not-a-uuid/status",
                {"status": "completed"},
            ),
            ("GET", "/api/v1/documents/not-a-uuid", None),
            ("DELETE", "/api/v1/documents/not-a-uuid", None),
            ("GET", "/api/v1/approvals/not-a-uuid", None),
            ("POST", "/api/v1/approvals/not-a-uuid/approve", {"decision_reason": "x"}),
            ("GET", "/api/v1/workflows/not-a-uuid/result", None),
            ("GET", "/api/v1/evaluation/metrics/%2e%2e%2fetc", None),
            ("GET", "/api/v1/requests/'; DROP TABLE requests;--", None),
        ]
        for method, path, body in probes:
            resp = self.client.request(
                method, path, json=body if body is not None else None,
                headers=self.headers,
            )
            assert resp.status_code in {404, 422}, (
                f"{method} {path} => {resp.status_code}, body={resp.text[:300]}"
            )
            _no_stack_trace(resp)

    def test_metrics_malformed_id_returns_null(self) -> None:
        resp = self.client.get(
            "/api/v1/evaluation/metrics/not-a-uuid", headers=self.headers
        )
        assert resp.status_code == 200
        assert resp.json() is None
        _no_stack_trace(resp)


class TestTokenAttacks:
    """Bearer-token forgery/rotation attacks."""

    def setup_method(self) -> None:
        self.client, self.settings = _make_app()
        _, self.headers = _register_and_login(
            self.client, self.settings, "tokens@example.com"
        )

    def test_missing_token(self) -> None:
        resp = self.client.get("/api/v1/requests")
        assert resp.status_code == 401

    def test_garbage_token(self) -> None:
        resp = self.client.get(
            "/api/v1/requests", headers={"Authorization": "Bearer not.a.token"}
        )
        assert resp.status_code == 401
        _no_stack_trace(resp)

    def test_tampered_signature(self) -> None:
        token = self.headers["Authorization"].split(" ", 1)[1]
        head, body_part, sig = token.split(".")
        tampered = f"{head}.{body_part}.{sig[:-4]}{'AAAA' if not sig.endswith('AAAA') else 'BBBB'}"
        resp = self.client.get(
            "/api/v1/requests", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert resp.status_code == 401

    def test_expired_token(self) -> None:
        from aegisforge.db.models import UserModel
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(self.settings)()
        try:
            user = db.query(UserModel).filter(UserModel.email == "tokens@example.com").first()
            assert user is not None
            stale = jwt.encode(
                {
                    "sub": user.id,
                    "exp": datetime.now(UTC) - timedelta(minutes=5),
                },
                self.settings.secret_key,
                algorithm="HS256",
            )
        finally:
            db.close()
        resp = self.client.get(
            "/api/v1/requests", headers={"Authorization": f"Bearer {stale}"}
        )
        assert resp.status_code == 401

    def test_wrong_signing_key(self) -> None:
        from aegisforge.db.models import UserModel
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(self.settings)()
        try:
            user = db.query(UserModel).filter(UserModel.email == "tokens@example.com").first()
            assert user is not None
            forged = jwt.encode(
                {
                    "sub": user.id,
                    "exp": datetime.now(UTC) + timedelta(minutes=5),
                },
                "attacker-controlled-secret",
                algorithm="HS256",
            )
        finally:
            db.close()
        resp = self.client.get(
            "/api/v1/requests", headers={"Authorization": f"Bearer {forged}"}
        )
        assert resp.status_code == 401

    def test_login_has_no_user_enumeration_oracle(self) -> None:
        unknown = self.client.post(
            "/api/v1/auth/login",
            json={"email": "ghost-user@example.com", "password": PASSWORD},
        )
        wrong_pw = self.client.post(
            "/api/v1/auth/login",
            json={"email": "tokens@example.com", "password": "WrongPassword123!"},
        )
        assert unknown.status_code == wrong_pw.status_code == 401
        assert unknown.json() == wrong_pw.json(), "login responses distinguish user existence"


class TestIdentitySpoofing:
    """/agents/research must take identity from the token, not the body."""

    def test_body_identity_fields_are_ignored(self) -> None:
        client, settings = _make_app()
        victim_id, _ = _register_and_login(client, settings, "spoof-victim@example.com")
        _, attacker_headers = _register_and_login(
            client, settings, "spoof-attacker@example.com"
        )

        resp = client.post(
            "/api/v1/agents/research",
            json={
                "query": "probe",
                "user_id": victim_id,
                "organization_id": "foreign-org",
                "requested_by": victim_id,
            },
            headers=attacker_headers,
        )
        assert resp.status_code == 200, resp.text
        # Neither the victim's identity nor the foreign org may be echoed or used
        assert victim_id not in resp.text
        assert "foreign-org" not in resp.text
        _no_stack_trace(resp)

    def test_anonymous_research_rejected(self) -> None:
        client, _ = _make_app()
        resp = client.post(
            "/api/v1/agents/research",
            json={"query": "probe", "user_id": "anyone", "organization_id": "any-org"},
        )
        assert resp.status_code == 401


class TestRAGTenantIsolation:
    """Retrieval must be fail-closed on organization scope.

    Regression: vector-store search/lexical/count/delete used to skip the
    organization predicate whenever ``organization_id`` was empty, so an
    unscoped retrieval (e.g. a context missing its org) read every tenant's
    chunks.  The org predicate must now be unconditional.
    """

    def _seed_store(self):  # type: ignore[no-untyped-def]
        from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
        from aegisforge.rag.vector_store import InMemoryVectorStore, VectorStoreEntry

        store = InMemoryVectorStore()
        provider = DeterministicEmbeddingProvider(dimension=64)
        secret_vec = provider.embed_text("quarterly revenue secret figures")
        store.add(
            [
                VectorStoreEntry(
                    id="org-a-chunk",
                    content="Org A quarterly revenue was 42 million dollars",
                    embedding=secret_vec,
                    metadata={"document_id": "doc-a", "source": "a.txt"},
                )
            ],
            organization_id="org-a",
        )
        return store, provider

    def test_unscoped_semantic_search_reads_nothing(self) -> None:
        store, provider = self._seed_store()
        vec = provider.embed_text("quarterly revenue secret figures")

        # Empty organization scope must not disable the filter (the bypass).
        assert store.search(query_embedding=vec, organization_id="") == []
        # A different tenant's scope must not read org-a's chunks.
        assert store.search(query_embedding=vec, organization_id="org-b") == []
        # The owner still retrieves normally.
        owner_hits = store.search(query_embedding=vec, organization_id="org-a")
        assert len(owner_hits) == 1

    def test_unscoped_lexical_search_reads_nothing(self) -> None:
        store, _ = self._seed_store()
        assert store.lexical_search("revenue", organization_id="") == []
        assert store.lexical_search("revenue", organization_id="org-b") == []
        assert len(store.lexical_search("revenue", organization_id="org-a")) == 1

    def test_cross_org_delete_and_count_fail_closed(self) -> None:
        store, provider = self._seed_store()
        vec = provider.embed_text("quarterly revenue secret figures")

        # Unscoped/cross-org count cannot reveal the tenant's volume.
        assert store.count() == 0
        assert store.count("org-b") == 0
        assert store.count("org-a") == 1

        # A foreign tenant cannot delete org-a's chunks.
        assert store.delete_by_document("doc-a", "org-b") == 0
        assert store.delete_by_document("doc-a", "") == 0
        assert len(store.search(query_embedding=vec, organization_id="org-a")) == 1

        # The owner can.
        assert store.delete_by_document("doc-a", "org-a") == 1
        assert store.search(query_embedding=vec, organization_id="org-a") == []

    def test_retrieval_service_unscoped_query_returns_nothing(self) -> None:
        from aegisforge.domain.models import RetrievalQuery
        from aegisforge.rag.embeddings import DeterministicEmbeddingProvider
        from aegisforge.rag.retrieval import RetrievalService

        store, _ = self._seed_store()
        service = RetrievalService(
            embedding_provider=DeterministicEmbeddingProvider(dimension=64),
            vector_store=store,
        )
        # Empty-org query against org-scoped knowledge: no chunks, no citations.
        results = service.retrieve(
            RetrievalQuery(query="quarterly revenue secret figures", organization_id="")
        )
        assert results == []
        foreign = service.retrieve(
            RetrievalQuery(query="quarterly revenue secret figures", organization_id="org-b")
        )
        assert foreign == []
        owner = service.retrieve(
            RetrievalQuery(query="quarterly revenue secret figures", organization_id="org-a")
        )
        assert len(owner) == 1


class _FakeHighRiskTool:
    """Minimal registered tool with server-side HIGH risk metadata."""

    def __init__(self) -> None:
        from aegisforge.tools.base import ToolDefinition

        self.definition = ToolDefinition(
            name="deploy.production",
            description="Deploy to production",
            requires_approval=True,
            risk_level="high",
            read_only=False,
            external_side_effect=True,
        )

    @property
    def name(self) -> str:
        return self.definition.name


class _FakeLowRiskTool:
    def __init__(self) -> None:
        from aegisforge.tools.base import ToolDefinition

        self.definition = ToolDefinition(name="knowledge.search", description="search")

    @property
    def name(self) -> str:
        return self.definition.name


class _FakeApprovalService:
    """Approval service stub; optionally fails to persist."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[dict] = []

    def create_approval_request(self, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("approval store unavailable")
        self.created.append(kwargs)

        class _A:
            approval_id = "approval-fake-001"

        return _A()


def _risk_task(task_id: str = "task-1", risk: str = "low") -> dict:  # type: ignore[no-untyped-def]
    return {
        "task_id": task_id,
        "description": "Restart production",
        "risk_level": risk,
        "input_data": {"query": "restart", "tool_name": "deploy.production"},
    }


class TestApprovalGateWS6:
    """WS6: no code path executes a gated action before human approval."""

    def _set_approval_service(self, service) -> None:  # type: ignore[no-untyped-def]
        from aegisforge.workflows import langgraph_workflow as lgw

        self._prev = lgw._ctx.approval_service
        lgw._ctx.approval_service = service

    def _restore(self) -> None:
        from aegisforge.workflows import langgraph_workflow as lgw

        lgw._ctx.approval_service = self._prev

    def _registry(self, tool) -> object:  # type: ignore[no-untyped-def]
        from aegisforge.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(tool)  # type: ignore[arg-type]
        return registry

    def test_high_risk_task_pauses_before_execution(self) -> None:
        from aegisforge.workflows.langgraph_workflow import (
            RequestStatus,
            _legacy_approval_gate,
        )

        service = _FakeApprovalService()
        self._set_approval_service(service)
        try:
            state = _legacy_approval_gate(
                {"workflow_id": "wf-1", "request_id": "req-1"},
                _risk_task(risk="high"),
                None,
            )
        finally:
            self._restore()

        assert state is not None
        assert state["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value
        assert state["approval_id"] == "approval-fake-001"
        assert state["approval_task_id"] == "task-1"
        assert service.created and service.created[0]["risk_level"].value == "high"

    def test_low_risk_declaration_cannot_bypass_high_risk_tool(self) -> None:
        """Planner downgrades risk to low; registry says high → gate holds."""
        from aegisforge.workflows.langgraph_workflow import (
            RequestStatus,
            _legacy_approval_gate,
        )

        service = _FakeApprovalService()
        self._set_approval_service(service)
        try:
            state = _legacy_approval_gate(
                {"workflow_id": "wf-1", "request_id": "req-1"},
                _risk_task(risk="low"),  # planner-declared downgrade
                self._registry(_FakeHighRiskTool()),
            )
        finally:
            self._restore()

        assert state is not None, "registry HIGH-risk tool ran without approval"
        assert state["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value
        assert state["approval_risk_level"] == "high"

    def test_low_risk_tool_with_low_declaration_runs(self) -> None:
        from aegisforge.workflows.langgraph_workflow import _legacy_approval_gate

        service = _FakeApprovalService()
        self._set_approval_service(service)
        try:
            state = _legacy_approval_gate(
                {"workflow_id": "wf-1", "request_id": "req-1"},
                {**_risk_task(), "input_data": {"tool_name": "knowledge.search"}},
                self._registry(_FakeLowRiskTool()),
            )
        finally:
            self._restore()
        assert state is None

    def test_approved_task_is_not_regated(self) -> None:
        from aegisforge.workflows.langgraph_workflow import _legacy_approval_gate

        service = _FakeApprovalService()
        self._set_approval_service(service)
        try:
            state = _legacy_approval_gate(
                {
                    "workflow_id": "wf-1",
                    "request_id": "req-1",
                    "approved_task_ids": ["task-1"],
                },
                _risk_task(risk="high"),
                None,
            )
        finally:
            self._restore()
        assert state is None
        assert service.created == [], "approved task must not re-create an approval"

    def test_gate_fails_closed_when_approval_cannot_be_persisted(self) -> None:
        from aegisforge.workflows.langgraph_workflow import (
            RequestStatus,
            _legacy_approval_gate,
        )

        self._set_approval_service(_FakeApprovalService(fail=True))
        try:
            state = _legacy_approval_gate(
                {"workflow_id": "wf-1", "request_id": "req-1"},
                _risk_task(risk="high"),
                None,
            )
        finally:
            self._restore()

        assert state is not None
        assert state["status"] == RequestStatus.FAILED.value
        assert any("approval" in e.lower() for e in state["errors"])

    def test_no_approval_service_means_no_legacy_gate(self) -> None:
        """Matches multi-agent semantics: gating only exists with a service."""
        from aegisforge.workflows.langgraph_workflow import _legacy_approval_gate

        self._set_approval_service(None)
        try:
            state = _legacy_approval_gate(
                {"workflow_id": "wf-1"}, _risk_task(risk="high"), None
            )
        finally:
            self._restore()
        assert state is None

    def test_full_flow_tool_never_runs_before_approval(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """E2E: spy on the real tool choke point across pause → approve."""
        from aegisforge.tools.registry import ToolRegistry

        client, settings = _make_app()
        _, headers = _register_and_login(
            client, settings, "gate-manager@example.com"
        )
        # Promote to manager (approval decision requires admin/manager role)
        from aegisforge.db.models import UserModel
        from aegisforge.db.session import get_session_factory

        db = get_session_factory(settings)()
        try:
            user = (
                db.query(UserModel)
                .filter(UserModel.email == "gate-manager@example.com")
                .first()
            )
            user.role = "manager"
            db.commit()
        finally:
            db.close()

        calls: list[str] = []
        original = ToolRegistry.execute

        def spy(self, tool_name, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(tool_name)
            return original(self, tool_name, *args, **kwargs)

        monkeypatch.setattr(ToolRegistry, "execute", spy)

        request_id = _make_request(
            client, headers, "Restart the production service after review"
        )
        executed = client.post(
            f"/api/v1/execution/requests/{request_id}/execute", headers=headers
        )
        assert executed.status_code == 200, executed.text
        assert executed.json()["status"] == "action_requires_approval"
        assert calls == [], f"protected action executed before approval: {calls}"

        approvals = client.get("/api/v1/approvals", headers=headers).json()["approvals"]
        approval = next(a for a in approvals if a["request_id"] == request_id)

        # Reject must also never run the action
        rejected = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/reject",
            json={"decision_reason": "no"},
            headers=headers,
        )
        assert rejected.status_code == 200
        assert calls == [], "protected action executed despite REJECT"


class TestWorkflowStateIDOR:
    """Same-org peers must not read another user's workflow/checkpoint state.

    The checkpoint state contains plans, task records, summaries, tool calls,
    and evaluation results — owner scoping (not just org) is required while
    self-registration shares the default organization.
    """

    def setup_method(self) -> None:
        self.client, self.settings = _make_app()
        _, self.a_headers = _register_and_login(
            self.client, self.settings, "wf-victim@example.com"
        )
        _, self.b_headers = _register_and_login(
            self.client, self.settings, "wf-attacker@example.com"
        )
        intent = (
            "Conduct enterprise knowledge research on data access policy, "
            "analyze the evidence, and synthesize a recommendation"
        )
        self.request_id = _make_request(self.client, self.a_headers, intent)
        executed = self.client.post(
            f"/api/v1/execution/requests/{self.request_id}/execute",
            headers=self.a_headers,
        )
        assert executed.status_code == 200, executed.text
        self.workflow_id = executed.json()["workflow_id"]

    def test_workflow_endpoints_denied_for_same_org_peer(self) -> None:
        paths = [
            f"/api/v1/workflows/{self.workflow_id}",
            f"/api/v1/workflows/{self.workflow_id}/tasks",
            f"/api/v1/workflows/{self.workflow_id}/result",
            f"/api/v1/workflows/{self.workflow_id}/evaluations",
            f"/api/v1/workflows/by-request/{self.request_id}",
        ]
        for path in paths:
            foreign = self.client.get(path, headers=self.b_headers)
            assert foreign.status_code == 404, (
                f"{path} leaked to same-org peer: {foreign.status_code}"
            )
            _no_stack_trace(foreign)
            own = self.client.get(path, headers=self.a_headers)
            assert own.status_code == 200, f"{path} denied to owner: {own.text[:200]}"

    def test_workflow_endpoints_require_auth(self) -> None:
        for path in (
            f"/api/v1/workflows/{self.workflow_id}",
            f"/api/v1/workflows/{self.workflow_id}/tasks",
            f"/api/v1/workflows/{self.workflow_id}/result",
        ):
            assert self.client.get(path).status_code == 401
