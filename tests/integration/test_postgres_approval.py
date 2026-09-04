"""Real PostgreSQL approval + checkpoint integration tests (Phase 4.2 Parts 2/8).

On real Postgres the FK constraints are enforced — approvals must
reference real jobs/requests — which the unit tests cannot exercise.
Verifies persistence across service instances ("process restart") and
tenant isolation with the database as source of truth.
"""
from __future__ import annotations

import uuid

from aegisforge.approval.service import ApprovalService
from aegisforge.db.models import ExecutionJobModel, RequestModel, UserModel
from aegisforge.domain.models import ApprovalStatus, RiskLevel
from aegisforge.workflows.checkpoint import (
    DbCheckpointStore,
    WorkflowCheckpoint,
    WorkflowCheckpointer,
)

from .conftest import requires_infra


def _seed_request_and_job(pg_session_factory) -> tuple[str, str, str]:
    """Create a real user/request/job so FKs are satisfied."""
    db = pg_session_factory()
    try:
        user = UserModel(
            id=str(uuid.uuid4()),
            organization_id="org-pg-1",
            email=f"user-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="x",
            full_name="PG User",
            role="manager",
        )
        db.add(user)
        db.commit()
        request = RequestModel(
            id=f"req-pg-{uuid.uuid4().hex[:8]}",
            organization_id="org-pg-1",
            requested_by=user.id,
            intent="Restart the service",
            status="action_requires_approval",
        )
        db.add(request)
        db.commit()
        job = ExecutionJobModel(
            id=f"job-pg-{uuid.uuid4().hex[:8]}",
            request_id=request.id,
            workflow_id=f"wf-pg-{uuid.uuid4().hex[:8]}",
            organization_id="org-pg-1",
            status="waiting_for_approval",
        )
        db.add(job)
        db.commit()
        return user.id, request.id, job.id
    finally:
        db.close()


@requires_infra
class TestPostgresApproval:
    def test_approval_persists_with_fk_constraints(self, pg_session_factory):
        user_id, request_id, job_id = _seed_request_and_job(pg_session_factory)
        service = ApprovalService(session_factory=pg_session_factory)

        approval = service.create_approval_request(
            job_id=job_id,
            request_id=request_id,
            workflow_id="wf-pg-x",
            action_description="Restart production",
            requested_by=user_id,
            organization_id="org-pg-1",
            risk_level=RiskLevel.HIGH,
        )
        assert approval.status == ApprovalStatus.PENDING

        # Read back through a fresh service instance ("restart")
        service2 = ApprovalService(session_factory=pg_session_factory)
        fetched = service2.get_approval(approval.approval_id)
        assert fetched is not None
        assert fetched.organization_id == "org-pg-1"
        assert fetched.risk_level == RiskLevel.HIGH

        # Approve through the new instance; the DB is the source of truth
        result = service2.approve(approval.approval_id, reviewer_id=user_id, decision_reason="OK")
        assert result is not None
        assert result.status == ApprovalStatus.APPROVED

        service3 = ApprovalService(session_factory=pg_session_factory)
        assert service3.get_approval(approval.approval_id).status == ApprovalStatus.APPROVED
        # No double approval
        assert service3.approve(approval.approval_id, "other") is None

    def test_tenant_isolation_on_real_postgres(self, pg_session_factory):
        user_id, request_id, job_id = _seed_request_and_job(pg_session_factory)
        service = ApprovalService(session_factory=pg_session_factory)
        service.create_approval_request(
            job_id=job_id,
            request_id=request_id,
            workflow_id="wf-pg-y",
            action_description="Org1 action",
            requested_by=user_id,
            organization_id="org-pg-1",
        )
        # Another org cannot see it as pending
        other = ApprovalService(session_factory=pg_session_factory)
        assert other.get_pending_approvals(organization_id="org-pg-999") == []

    def test_expiry_survives_restart(self, pg_session_factory):
        from datetime import UTC, datetime, timedelta

        user_id, request_id, job_id = _seed_request_and_job(pg_session_factory)
        service = ApprovalService(session_factory=pg_session_factory)
        approval = service.create_approval_request(
            job_id=job_id, request_id=request_id, workflow_id="wf-pg-z",
            action_description="Action", requested_by=user_id, organization_id="org-pg-1",
        )
        assert approval is not None

        # Back-date expiry directly in Postgres
        db = pg_session_factory()
        try:
            from aegisforge.db.models import ApprovalRequestModel

            model = db.query(ApprovalRequestModel).filter(
                ApprovalRequestModel.id == approval.approval_id
            ).first()
            model.expires_at = datetime.now(UTC) - timedelta(hours=1)
            db.commit()
        finally:
            db.close()

        service2 = ApprovalService(session_factory=pg_session_factory)
        assert service2.approve(approval.approval_id, "admin") is None
        assert service2.get_approval(approval.approval_id).status == ApprovalStatus.EXPIRED


@requires_infra
class TestPostgresCheckpoints:
    def test_checkpoint_restart_recovery(self, pg_session_factory):
        store1 = DbCheckpointStore(session_factory=pg_session_factory)
        wf_id = f"wf-pg-cp-{uuid.uuid4().hex[:8]}"
        store1.save_checkpoint(
            WorkflowCheckpoint(
                checkpoint_id=f"cp-pg-{uuid.uuid4().hex[:8]}",
                workflow_id=wf_id,
                request_id="req-pg-cp",
                node_name="retry_or_complete",
                state={"status": "action_requires_approval", "current_task_index": 0},
                organization_id="org-pg-1",
            )
        )
        del store1

        # Fresh store ("restart") reads the checkpoint from Postgres
        store2 = DbCheckpointStore(session_factory=pg_session_factory)
        latest = store2.load_latest_by_workflow(wf_id)
        assert latest is not None
        assert latest.node_name == "retry_or_complete"
        assert latest.state["status"] == "action_requires_approval"

    def test_full_approval_resume_on_postgres(self, pg_session_factory):
        """The complete pause → approve → resume cycle on real Postgres."""
        from aegisforge.domain.models import RequestStatus
        from aegisforge.workflows.langgraph_workflow import (
            execute_workflow,
            resume_workflow_after_approval,
        )

        user_id, request_id, job_id = _seed_request_and_job(pg_session_factory)
        approval_service = ApprovalService(session_factory=pg_session_factory)
        store = DbCheckpointStore(session_factory=pg_session_factory)
        wf_id = f"wf-pg-e2e-{uuid.uuid4().hex[:8]}"
        checkpointer = WorkflowCheckpointer(
            workflow_id=wf_id, request_id=request_id, organization_id="org-pg-1", store=store
        )

        result = execute_workflow(
            request_id=request_id,
            intent="Restart the production service after review",
            user_id=user_id,
            organization_id="org-pg-1",
            workflow_id=wf_id,
            checkpointer=checkpointer,
            approval_service=approval_service,
            job_id=job_id,
        )
        assert result["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value
        assert result["approval_id"]

        approval = approval_service.get_approval(result["approval_id"])
        assert approval is not None

        # Approve through a fresh service instance
        approval_service.approve(result["approval_id"], reviewer_id=user_id, decision_reason="OK")

        final = resume_workflow_after_approval(
            workflow_id=wf_id,
            approval_decision="approved",
            approval_id=result["approval_id"],
            reviewer_id=user_id,
            store=DbCheckpointStore(session_factory=pg_session_factory),
            request_id=request_id,
            organization_id="org-pg-1",
        )
        assert final["status"] == RequestStatus.COMPLETED.value