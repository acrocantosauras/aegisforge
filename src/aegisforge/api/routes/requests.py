from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aegisforge.db.models import RequestModel, UserModel
from aegisforge.db.session import get_db
from aegisforge.domain.schemas import RequestCreate, RequestRead, RequestStatusUpdate
from aegisforge.services.auth_service import get_current_user
from aegisforge.services.request_service import create_request, get_request, update_request_status

router = APIRouter(prefix="/requests", tags=["requests"])


@router.post("", response_model=RequestRead, status_code=status.HTTP_201_CREATED)
def create_request_route(
    payload: RequestCreate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> RequestModel:
    request = create_request(db, user.id, user.organization_id, payload.intent, payload.context)
    return request


@router.get("", response_model=list[RequestRead])
def list_requests_route(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> list[RequestModel]:
    """List the caller's own requests (org + owner scoped).

    SECURITY: registration currently places all self-registered users in a
    shared default organization, so an org-only filter would expose every
    user's intents/context to every other user.  Owner scoping keeps the
    history view private per user; the organization filter remains as the
    hard tenant boundary.
    """
    requests = (
        db.query(RequestModel)
        .filter(
            RequestModel.organization_id == user.organization_id,
            RequestModel.requested_by == user.id,
        )
        .order_by(RequestModel.created_at.desc())
        .limit(100)
        .all()
    )
    return requests


@router.get("/{request_id}", response_model=RequestRead)
def get_request_route(
    request_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> RequestModel:
    request = get_request(db, request_id)
    if request.organization_id != user.organization_id or request.requested_by != user.id:
        # 404 (never 403): must not confirm the existence of another user's request.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return request


@router.patch("/{request_id}/status", response_model=RequestRead)
def update_request_status_route(
    request_id: str,
    payload: RequestStatusUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> RequestModel:
    request = get_request(db, request_id)
    if request.organization_id != user.organization_id or request.requested_by != user.id:
        # 404 (never 403): must not confirm the existence of another user's request.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return update_request_status(db, request_id, payload.status)
