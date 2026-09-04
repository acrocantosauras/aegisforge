from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from aegisforge.domain.models import RequestStatus, Role


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RequestCreate(BaseModel):
    intent: str = Field(min_length=1, max_length=2000)
    context: dict[str, Any] = Field(default_factory=dict)


class RequestRead(BaseModel):
    id: str
    organization_id: str
    requested_by: str
    intent: str
    status: RequestStatus
    context: Any = Field(default_factory=dict)
    model_config = ConfigDict(from_attributes=True)


class RequestStatusUpdate(BaseModel):
    status: RequestStatus


class TaskRead(BaseModel):
    id: str
    request_id: str
    title: str
    description: str
    status: RequestStatus
    model_config = ConfigDict(from_attributes=True)


class UserRead(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: Role
    model_config = ConfigDict(from_attributes=True)


class AuditEventRead(BaseModel):
    id: str
    organization_id: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    event_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Any
    request_id: str | None
    model_config = ConfigDict(from_attributes=True)

    @field_validator("event_metadata", mode="before")
    @classmethod
    def parse_metadata(cls, v: Any) -> dict[str, Any]:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return {}
        if isinstance(v, dict):
            return v
        return {}
