from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aegisforge.db.models import AuditEventModel
from aegisforge.db.session import get_db
from aegisforge.domain.schemas import AuditEventRead

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", response_model=list[AuditEventRead])
def list_audit_events(db: Session = Depends(get_db)) -> list[AuditEventModel]:
    events = db.query(AuditEventModel).order_by(AuditEventModel.created_at.desc()).limit(20).all()
    result = []
    for event in events:
        event_dict = {
            "id": event.id,
            "organization_id": event.organization_id,
            "actor_id": event.actor_id,
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "outcome": event.outcome,
            "event_metadata": json.loads(event.event_metadata) if isinstance(event.event_metadata, str) else event.event_metadata,
            "created_at": event.created_at,
            "request_id": event.request_id,
        }
        result.append(event_dict)
    return result
