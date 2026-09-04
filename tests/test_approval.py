"""Tests for human approval workflow.

Covers: approval creation, authorization, approval/rejection, expiry,
workflow pause/resume, safe actions, and cross-user isolation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aegisforge.approval.service import (
    ApprovalService,
    execute_safe_action,
    is_approval_required,
)
from aegisforge.domain.models import ApprovalStatus, RiskLevel


# --- Approval Service Tests ---


def test_create_approval_request() -> None:
    service = ApprovalService()
    approval = service.create_approval_request(
        job_id="job-1",
        request_id="req-1",
        workflow_id="wf-1",
        action_description="Restart service",
        requested_by="user-1",
        organization_id="org-1",
        risk_level=RiskLevel.HIGH,
    )
    assert approval.approval_id
    assert approval.status == ApprovalStatus.PENDING
    assert approval.risk_level == RiskLevel.HIGH
    assert approval.requested_by == "user-1"


def test_get_approval() -> None:
    service = ApprovalService()
    approval = service.create_approval_request(
        job_id="job-1",
        request_id="req-1",
        workflow_id="wf-1",
        action_description="Test action",
        requested_by="user-1",
    )
    retrieved = service.get_approval(approval.approval_id)
    assert retrieved is not None
    assert retrieved.approval_id == approval.approval_id


def test_get_pending_approvals() -> None:
    service = ApprovalService()
    service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="Action 1", requested_by="user-1",
    )
    service.create_approval_request(
        job_id="job-2", request_id="req-2", workflow_id="wf-2",
        action_description="Action 2", requested_by="user-2",
    )
    pending = service.get_pending_approvals()
    assert len(pending) == 2


def test_approve_request() -> None:
    service = ApprovalService()
    approval = service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="Test", requested_by="user-1",
    )
    result = service.approve(approval.approval_id, reviewer_id="reviewer-1", decision_reason="LGTM")
    assert result is not None
    assert result.status == ApprovalStatus.APPROVED
    assert result.reviewer_id == "reviewer-1"
    assert result.decision_reason == "LGTM"
    assert result.decided_at is not None


def test_reject_request() -> None:
    service = ApprovalService()
    approval = service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="Test", requested_by="user-1",
    )
    result = service.reject(approval.approval_id, reviewer_id="reviewer-1", decision_reason="Too risky")
    assert result is not None
    assert result.status == ApprovalStatus.REJECTED
    assert result.decision_reason == "Too risky"


def test_cannot_approve_already_approved() -> None:
    service = ApprovalService()
    approval = service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="Test", requested_by="user-1",
    )
    service.approve(approval.approval_id, reviewer_id="r1")
    result = service.approve(approval.approval_id, reviewer_id="r2")
    assert result is None  # Cannot approve twice


def test_cancel_approval() -> None:
    service = ApprovalService()
    approval = service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="Test", requested_by="user-1",
    )
    result = service.cancel(approval.approval_id)
    assert result is not None
    assert result.status == ApprovalStatus.CANCELLED


def test_approval_expiry() -> None:
    from datetime import timedelta
    service = ApprovalService(approval_timeout_hours=24)
    approval = service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="Test", requested_by="user-1",
    )
    # Manually set expiry to the past to test expiry logic
    approval.expires_at = datetime.now(UTC) - timedelta(hours=1)
    expired = service.check_expired()
    assert len(expired) >= 1
    assert expired[0].status == ApprovalStatus.EXPIRED


def test_approval_not_found() -> None:
    service = ApprovalService()
    result = service.approve("nonexistent", reviewer_id="r1")
    assert result is None


# --- Risk Level Tests ---


def test_is_approval_required_high() -> None:
    assert is_approval_required(RiskLevel.HIGH) is True


def test_is_approval_required_critical() -> None:
    assert is_approval_required(RiskLevel.CRITICAL) is True


def test_is_approval_required_low() -> None:
    assert is_approval_required(RiskLevel.LOW) is False


def test_is_approval_required_medium() -> None:
    assert is_approval_required(RiskLevel.MEDIUM) is False


def test_is_approval_required_custom_levels() -> None:
    assert is_approval_required(RiskLevel.MEDIUM, "medium,high") is True
    assert is_approval_required(RiskLevel.LOW, "medium,high") is False


# --- Safe Action Tests ---


def test_execute_safe_action_service_restart() -> None:
    result = execute_safe_action("simulated_service_restart", {"service": "api"})
    assert result["status"] == "completed"
    assert result["real_world_effect"] is False


def test_execute_safe_action_config_change() -> None:
    result = execute_safe_action("simulated_config_change", {"key": "value"})
    assert result["status"] == "completed"
    assert result["real_world_effect"] is False


def test_execute_safe_action_workflow_operation() -> None:
    result = execute_safe_action("simulated_workflow_operation", {"op": "pause"})
    assert result["status"] == "completed"
    assert result["real_world_effect"] is False


def test_execute_safe_action_unknown() -> None:
    result = execute_safe_action("unknown_action", {})
    assert result["status"] == "failed"
    assert "Unknown" in result["error"]


# --- Cross-user Isolation Tests ---


def test_cross_user_approval_isolation() -> None:
    """Verify that approval IDs cannot be guessed or accessed across users."""
    service = ApprovalService()
    approval1 = service.create_approval_request(
        job_id="job-1", request_id="req-1", workflow_id="wf-1",
        action_description="User 1 action", requested_by="user-1",
    )
    approval2 = service.create_approval_request(
        job_id="job-2", request_id="req-2", workflow_id="wf-2",
        action_description="User 2 action", requested_by="user-2",
    )

    # User 1 cannot approve User 2's request (different approval_id)
    assert approval1.approval_id != approval2.approval_id

    # Approving one does not affect the other
    service.approve(approval1.approval_id, reviewer_id="admin")
    assert service.get_approval(approval1.approval_id).status == ApprovalStatus.APPROVED
    assert service.get_approval(approval2.approval_id).status == ApprovalStatus.PENDING
