"""Approval management API endpoints for AegisForge.

Provides: list pending, approve, reject with authorization.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db
from aegisforge.approval.service import ApprovalService, is_approval_required
from aegisforge.domain.models import ApprovalStatus, RiskLevel
from aegisforge.services.auth_service import get_current_user

router = APIRouter(prefix="/approvals", tags=["approvals"])

# In-memory approval service (Phase 4 TODO: persist to DB)
_approval_service = ApprovalService()


class ApprovalRead(BaseModel):
    approval_id: str
    job_id: str
    request_id: str
    workflow_id: str
    action_description: str
    requested_by: str
    reviewer_id: str | None = None
    risk_level: str
    status: str
    reason: str = ""
    decision_reason: str = ""
    created_at: datetime | None = None
    decided_at: datetime | None = None

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    decision_reason: str = Field(default="", max_length=2000)


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalRead]
    total: int


@router.get("", response_model=ApprovalListResponse)
def list_pending_approvals(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> ApprovalListResponse:
    """List pending approval requests for the user's organization."""
    pending = _approval_service.get_pending_approvals(
        organization_id=user.organization_id,
    )
    return ApprovalListResponse(
        approvals=[
            ApprovalRead(
                approval_id=a.approval_id,
                job_id=a.job_id,
                request_id=a.request_id,
                workflow_id=a.workflow_id,
                action_description=a.action_description,
                requested_by=a.requested_by,
                risk_level=a.risk_level.value,
                status=a.status.value,
                reason=a.reason,
                created_at=a.created_at,
            )
            for a in pending
        ],
        total=len(pending),
    )


@router.get("/{approval_id}", response_model=ApprovalRead)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> ApprovalRead:
    """Get an approval request by ID."""
    approval = _approval_service.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    # F14: Tenant isolation check
    if approval.organization_id and approval.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return ApprovalRead(
        approval_id=approval.approval_id,
        job_id=approval.job_id,
        request_id=approval.request_id,
        workflow_id=approval.workflow_id,
        action_description=approval.action_description,
        requested_by=approval.requested_by,
        reviewer_id=approval.reviewer_id,
        risk_level=approval.risk_level.value,
        status=approval.status.value,
        reason=approval.reason,
        decision_reason=approval.decision_reason,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )


@router.post("/{approval_id}/approve", response_model=ApprovalRead)
def approve_request(
    approval_id: str,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> ApprovalRead:
    """Approve an approval request. Requires authorization."""
    # Only admin/manager can approve
    if user.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or manager roles can approve requests",
        )
    # F14: Tenant isolation check
    existing = _approval_service.get_approval(approval_id)
    if existing and existing.organization_id and existing.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = _approval_service.approve(
        approval_id,
        reviewer_id=user.id,
        decision_reason=decision.decision_reason,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Approval not found or not pending")

    return ApprovalRead(
        approval_id=result.approval_id,
        job_id=result.job_id,
        request_id=result.request_id,
        workflow_id=result.workflow_id,
        action_description=result.action_description,
        requested_by=result.requested_by,
        reviewer_id=result.reviewer_id,
        risk_level=result.risk_level.value,
        status=result.status.value,
        reason=result.reason,
        decision_reason=result.decision_reason,
        created_at=result.created_at,
        decided_at=result.decided_at,
    )


@router.post("/{approval_id}/reject", response_model=ApprovalRead)
def reject_request(
    approval_id: str,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> ApprovalRead:
    """Reject an approval request. Requires authorization."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or manager roles can reject requests",
        )
    # F14: Tenant isolation check
    existing = _approval_service.get_approval(approval_id)
    if existing and existing.organization_id and existing.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = _approval_service.reject(
        approval_id,
        reviewer_id=user.id,
        decision_reason=decision.decision_reason,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Approval not found or not pending")

    return ApprovalRead(
        approval_id=result.approval_id,
        job_id=result.job_id,
        request_id=result.request_id,
        workflow_id=result.workflow_id,
        action_description=result.action_description,
        requested_by=result.requested_by,
        reviewer_id=result.reviewer_id,
        risk_level=result.risk_level.value,
        status=result.status.value,
        reason=result.reason,
        decision_reason=result.decision_reason,
        created_at=result.created_at,
        decided_at=result.decided_at,
    )
