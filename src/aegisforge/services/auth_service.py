from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from aegisforge.auth.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from aegisforge.config import Settings, get_settings
from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UserModel:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        payload = decode_token(credentials.credentials, settings)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def create_user(db: Session, email: str, password: str, full_name: str, organization_id: str = "default-org") -> UserModel:
    if db.query(UserModel).filter(UserModel.email == email.lower()).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    user = UserModel(
        id=str(uuid.uuid4()),
        organization_id=organization_id,
        email=email.lower(),
        password_hash=hash_password(password),
        full_name=full_name,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str, settings: Settings) -> tuple[UserModel, str]:
    user = db.query(UserModel).filter(UserModel.email == email.lower()).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(subject=user.id, settings=settings)
    return user, token
