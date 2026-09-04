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
    """List requests for the user's organization (tenant-isolated)."""
    requests = (
        db.query(RequestModel)
        .filter(RequestModel.organization_id == user.organization_id)
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
    if request.requested_by != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    return request


@router.patch("/{request_id}/status", response_model=RequestRead)
def update_request_status_route(
    request_id: str,
    payload: RequestStatusUpdate,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> RequestModel:
    request = get_request(db, request_id)
    if request.requested_by != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    return update_request_status(db, request_id, payload.status)
