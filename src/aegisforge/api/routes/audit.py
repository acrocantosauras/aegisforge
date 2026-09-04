from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aegisforge.db.models import AuditEventModel, UserModel
from aegisforge.db.session import get_db
from aegisforge.domain.schemas import AuditEventRead
from aegisforge.services.auth_service import get_current_user

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=dict)
def list_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> dict:
    """List audit events for the caller's organization (tenant-isolated)."""
    events = (
        db.query(AuditEventModel)
        .filter(AuditEventModel.organization_id == user.organization_id)
        .order_by(AuditEventModel.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"events": [AuditEventRead.model_validate(e) for e in events]}


@router.get("/events", response_model=list[AuditEventRead])
def list_audit_events_legacy(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> list[AuditEventModel]:
    """List audit events for the caller's organization (legacy path)."""
    events = (
        db.query(AuditEventModel)
        .filter(AuditEventModel.organization_id == user.organization_id)
        .order_by(AuditEventModel.created_at.desc())
        .limit(limit)
        .all()
    )
    return events