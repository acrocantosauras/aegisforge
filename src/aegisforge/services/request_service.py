from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from aegisforge.db.models import RequestModel, TaskModel
from aegisforge.domain.models import RequestStatus


def create_request(db: Session, user_id: str, organization_id: str, intent: str, context: dict[str, Any] | None = None) -> RequestModel:
    request = RequestModel(
        id=str(uuid.uuid4()),
        organization_id=organization_id,
        requested_by=user_id,
        intent=intent,
        status=RequestStatus.CREATED.value,
        context=json.dumps(context or {}),
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def get_request(db: Session, request_id: str) -> RequestModel:
    request = db.query(RequestModel).filter(RequestModel.id == request_id).first()
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return request


def update_request_status(db: Session, request_id: str, status: RequestStatus) -> RequestModel:
    request = get_request(db, request_id)
    request.status = status.value
    db.commit()
    db.refresh(request)
    return request


def create_task(db: Session, request_id: str, title: str, description: str, agent_type: str, dependencies: list[str] | None = None) -> TaskModel:
    task = TaskModel(
        id=str(uuid.uuid4()),
        request_id=request_id,
        title=title,
        description=description,
        agent_type=agent_type,
        status=RequestStatus.CREATED.value,
        dependencies=json.dumps(dependencies or []),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task
