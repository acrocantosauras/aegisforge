"""Tool + MCP ecosystem read endpoints (Phase 5).

GET /tools        → registered AegisForge tools (incl. risk metadata).
GET /mcp/servers  → configured MCP servers from the catalog (no secrets).
GET /mcp/tools    → discovered MCP tools (permission/allow-list filtered).

All require authentication; catalog views are tenant-neutral metadata (MCP
servers are operator-configured platform infrastructure).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_settings
from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db
from aegisforge.mcp.catalog import MCPServerCatalog, build_catalog_from_settings
from aegisforge.mcp.lifecycle import configure_mcp_registry
from aegisforge.services.auth_service import get_current_user
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry

router = APIRouter(tags=["catalog"])


class ToolRead(BaseModel):
    name: str
    description: str
    version: str
    permission_requirements: list[str] = Field(default_factory=list)
    timeout_seconds: int
    requires_approval: bool = False
    risk_level: str = "low"
    read_only: bool = True
    external_side_effect: bool = False
    data_sensitivity: str = "internal"


class ToolListRead(BaseModel):
    tools: list[ToolRead] = Field(default_factory=list)
    total: int = 0


class MCPServerRead(BaseModel):
    server_id: str
    name: str
    description: str = ""
    version: str = "1.0"
    transport: str
    enabled: bool
    allowed_tools: list[str] = Field(default_factory=list)
    risk_level: str
    read_only_default: bool = True
    timeout_seconds: int
    health: dict = Field(default_factory=dict)
    tool_count: int = 0


class MCPToolRead(BaseModel):
    name: str
    server_id: str
    tool_name: str
    description: str
    risk_level: str
    read_only: bool


class MCPListRead(BaseModel):
    servers: list[MCPServerRead] = Field(default_factory=list)
    total: int = 0


class MCPToolListRead(BaseModel):
    tools: list[MCPToolRead] = Field(default_factory=list)
    total: int = 0


def _default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    try:
        registry.register(KnowledgeSearchTool())
    except ValueError:
        pass
    return registry


@router.get("/tools", response_model=ToolListRead)
def list_tools(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ToolListRead:
    """List tools available to agents with risk classification metadata."""
    registry = _default_registry()
    registry, lifecycle = configure_mcp_registry(settings, registry)
    try:
        tools = []
        for definition in registry.list_tools():
            tools.append(
                ToolRead(
                    name=definition.name,
                    description=definition.description,
                    version=definition.version,
                    permission_requirements=list(definition.permission_requirements),
                    timeout_seconds=definition.timeout_seconds,
                    requires_approval=definition.requires_approval,
                    risk_level=definition.risk_level,
                    read_only=definition.read_only,
                    external_side_effect=definition.external_side_effect,
                    data_sensitivity=definition.data_sensitivity,
                )
            )
        return ToolListRead(tools=tools, total=len(tools))
    finally:
        if lifecycle is not None:
            lifecycle.shutdown()


def _catalog(settings: Settings) -> MCPServerCatalog:
    return build_catalog_from_settings(settings)


@router.get("/mcp/servers", response_model=MCPListRead)
def list_mcp_servers(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> MCPListRead:
    """List configured MCP servers (catalog metadata + health, no secrets)."""
    servers = [
        MCPServerRead(**entry.public_dict())
        for entry in _catalog(settings).list_servers()
    ]
    return MCPListRead(servers=servers, total=len(servers))


@router.get("/mcp/tools", response_model=MCPToolListRead)
def list_mcp_tools(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> MCPToolListRead:
    """List discovered MCP tools (respecting server allow-lists)."""
    catalog = _catalog(settings)
    _, lifecycle = configure_mcp_registry(settings, catalog=catalog)
    try:
        tools = [MCPToolRead(**t) for t in catalog.list_tools()]
    finally:
        if lifecycle is not None:
            lifecycle.shutdown()
    return MCPToolListRead(tools=tools, total=len(tools))
