from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from aegisforge.db.models import AuditEventModel


def record_audit_event(
    db: Session,
    *,
    organization_id: str,
    actor_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str,
    outcome: str,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditEventModel:
    event = AuditEventModel(
        id=str(uuid.uuid4()),
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        event_metadata=json.dumps(metadata or {}),
        request_id=request_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
