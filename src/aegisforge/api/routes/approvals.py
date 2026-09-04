"""Approval management API endpoints for AegisForge.

Provides: list pending, get, approve, reject with authorization and
tenant isolation.  Approvals are persisted in PostgreSQL via
ApprovalRequestModel (DB-backed ApprovalService), never in-memory.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aegisforge.approval.service import ApprovalService
from aegisforge.config import Settings, get_settings
from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db
from aegisforge.services.auth_service import get_current_user

router = APIRouter(prefix="/approvals", tags=["approvals"])


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


def _get_approval_service(db: Session, settings: Settings) -> ApprovalService:
    """Create a DB-backed approval service bound to the request session."""
    return ApprovalService(
        session_factory=lambda: db,
        approval_timeout_hours=settings.approval_timeout_hours,
    )


def _to_read(approval) -> ApprovalRead:  # type: ignore[no-untyped-def]
    """Convert a domain ApprovalRequest to the response schema."""
    return ApprovalRead(
        approval_id=approval.approval_id,
        job_id=approval.job_id,
        request_id=approval.request_id,
        workflow_id=approval.workflow_id,
        action_description=approval.action_description,
        requested_by=approval.requested_by,
        reviewer_id=approval.reviewer_id,
        risk_level=approval.risk_level.value if hasattr(approval.risk_level, "value") else str(approval.risk_level),
        status=approval.status.value if hasattr(approval.status, "value") else str(approval.status),
        reason=approval.reason,
        decision_reason=approval.decision_reason,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )


@router.get("", response_model=ApprovalListResponse)
def list_pending_approvals(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ApprovalListResponse:
    """List pending approval requests for the user's organization."""
    service = _get_approval_service(db, settings)
    pending = service.get_pending_approvals(
        organization_id=user.organization_id,
    )
    return ApprovalListResponse(
        approvals=[_to_read(a) for a in pending],
        total=len(pending),
    )


@router.get("/{approval_id}", response_model=ApprovalRead)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ApprovalRead:
    """Get an approval request by ID (tenant-isolated)."""
    service = _get_approval_service(db, settings)
    approval = service.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    # F14: Tenant isolation check
    if approval.organization_id and approval.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return _to_read(approval)


def _authorize_and_decide(
    approval_id: str,
    decision: ApprovalDecision,
    user: UserModel,
    db: Session,
    decision_fn: str,
    settings: Settings,
) -> ApprovalRead:
    """Shared authorize → decide → resume flow for approve/reject."""
    if user.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or manager roles can approve requests",
        )

    service = _get_approval_service(db, settings)
    existing = service.get_approval(approval_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    # F14: Tenant isolation check
    if existing.organization_id and existing.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if decision_fn == "approve":
        result = service.approve(
            approval_id,
            reviewer_id=user.id,
            decision_reason=decision.decision_reason,
        )
    else:
        result = service.reject(
            approval_id,
            reviewer_id=user.id,
            decision_reason=decision.decision_reason,
        )
    if result is None:
        raise HTTPException(status_code=409, detail="Approval not found or not pending")

    # Resume (or fail) the paused workflow — protected actions only run
    # after approval; rejection terminates the workflow.
    if result.workflow_id:
        try:
            from aegisforge.services.execution_service import resume_request_after_approval

            resume_request_after_approval(
                db=db,
                request_id=result.request_id,
                workflow_id=result.workflow_id,
                approval_decision="approved" if decision_fn == "approve" else "rejected",
                approval_id=result.approval_id,
                reviewer_id=user.id,
                organization_id=result.organization_id or user.organization_id,
                settings=settings,
            )
        except Exception as exc:
            # The decision itself is persisted; resume failures must not
            # silently hide the decision. Log and surface a warning.
            import logging

            logging.getLogger(__name__).warning(
                "Approval %s decided but workflow resume failed: %s",
                approval_id,
                exc,
            )

    return _to_read(result)


@router.post("/{approval_id}/approve", response_model=ApprovalRead)
def approve_request(
    approval_id: str,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ApprovalRead:
    """Approve an approval request and resume the paused workflow."""
    return _authorize_and_decide(approval_id, decision, user, db, "approve", settings)


@router.post("/{approval_id}/reject", response_model=ApprovalRead)
def reject_request(
    approval_id: str,
    decision: ApprovalDecision,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ApprovalRead:
    """Reject an approval request and fail the paused workflow."""
    return _authorize_and_decide(approval_id, decision, user, db, "reject", settings)