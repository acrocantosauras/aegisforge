"""MCP Server Catalog for AegisForge (Phase 5).

Turns MCP from a basic integration into a manageable tool ecosystem.  The
catalog is the operator-controlled registry of configured MCP servers: id,
name, version, transport, enablement, allowed tools, permissions/risk
metadata, health state, and configuration metadata.

The catalog NEVER stores secrets (API keys, tokens).  Secrets belong in
environment variables / secure configuration mechanisms referenced by the
transport configuration.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import MCPServerConfig as ServerConfig
from aegisforge.domain.models import MCPToolDefinition

logger = logging.getLogger(__name__)


class MCPHealthState(str):
    """Health states for an MCP server (string values, JSON friendly)."""

    UNKNOWN = "unknown"
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


@dataclass
class MCPServerHealth:
    """Health/lifecycle state for one catalog server."""

    state: str = MCPHealthState.UNKNOWN
    connected: bool = False
    tool_count: int = 0
    last_checked_at: float | None = None
    last_error: str = ""
    failures_since_last_success: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.connected,
            "tool_count": self.tool_count,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "failures_since_last_success": self.failures_since_last_success,
        }


@dataclass
class MCPServerEntry:
    """A catalog entry: operator config + live health + discovered tools."""

    config: ServerConfig
    health: MCPServerHealth = field(default_factory=MCPServerHealth)
    tools: list[MCPToolDefinition] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        """Catalog view — explicitly excludes any secret-bearing fields."""
        return {
            "server_id": self.config.server_id,
            "name": self.config.name,
            "description": self.config.description,
            "version": self.config.version,
            "transport": self.config.transport,
            "enabled": self.config.enabled,
            "allowed_tools": list(self.config.allowed_tools),
            "risk_level": self.config.risk_level,
            "read_only_default": self.config.read_only_default,
            "timeout_seconds": self.config.timeout_seconds,
            "health": self.health.to_dict(),
            "tool_count": len(self.tools),
        }


class MCPServerCatalog:
    """Registry of configured MCP servers with tool + health metadata.

    Thread-safe enough for read paths (dict reads); lifecycle updates happen
    through the lifecycle manager.
    """

    def __init__(self) -> None:
        self._entries: dict[str, MCPServerEntry] = {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def register(self, config: ServerConfig) -> MCPServerEntry:
        """Register (or update) a server configuration."""
        if not config.server_id:
            raise ValueError("MCP server config must have a server_id")
        entry = self._entries.get(config.server_id)
        if entry is None:
            entry = MCPServerEntry(config=config)
            self._entries[config.server_id] = entry
        else:
            entry.config = config
        if not config.enabled:
            entry.health.state = MCPHealthState.DISABLED
        logger.info(
            "Registered MCP server %s (%s v%s, risk=%s)",
            config.name,
            config.server_id,
            config.version,
            config.risk_level,
        )
        return entry

    def register_many(self, configs: list[ServerConfig]) -> list[MCPServerEntry]:
        return [self.register(c) for c in configs]

    def register_from_json(self, payload: str) -> list[MCPServerEntry]:
        """Register servers from a JSON array of config dicts (no secrets)."""
        raw = json.loads(payload) if payload else []
        if not isinstance(raw, list):
            raise TypeError("MCP catalog JSON must be an array of server configs")
        configs = [ServerConfig.model_validate(item) for item in raw]
        return self.register_many(configs)

    def unregister(self, server_id: str) -> None:
        self._entries.pop(server_id, None)

    def get(self, server_id: str) -> MCPServerEntry | None:
        return self._entries.get(server_id)

    def list_servers(self) -> list[MCPServerEntry]:
        return list(self._entries.values())

    def list_public(self) -> list[dict[str, Any]]:
        return [e.public_dict() for e in self._entries.values()]

    def get_config(self, server_id: str) -> ServerConfig | None:
        entry = self._entries.get(server_id)
        return entry.config if entry else None

    # ------------------------------------------------------------------
    # Health updates (driven by the lifecycle manager / health monitor)
    # ------------------------------------------------------------------

    def set_health(self, server_id: str, state: str, error: str = "", connected: bool | None = None) -> None:
        entry = self._entries.get(server_id)
        if entry is None:
            return
        entry.health.state = state
        entry.health.last_checked_at = time.time()
        entry.health.last_error = error[:500]
        if connected is not None:
            entry.health.connected = connected
        if state == MCPHealthState.HEALTHY:
            entry.health.failures_since_last_success = 0
        elif state in (MCPHealthState.UNHEALTHY, MCPHealthState.DEGRADED):
            entry.health.failures_since_last_success += 1

    def set_tools(self, server_id: str, tools: list[MCPToolDefinition]) -> None:
        entry = self._entries.get(server_id)
        if entry is None:
            return
        entry.tools = list(tools)
        entry.health.tool_count = len(tools)

    # ------------------------------------------------------------------
    # Tool filtering (permission/risk boundaries)
    # ------------------------------------------------------------------

    def list_tools(
        self,
        server_id: str = "",
        tool_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List discovered tools, optionally filtered by server/names.

        Returns public tool metadata only (no schemas with secrets, no
        arguments beyond the tool contract).
        """
        entries = [self._entries[s] for s in (server_id.split(",") if server_id else list(self._entries)) if s in self._entries]
        results: list[dict[str, Any]] = []
        for entry in entries:
            if not entry.config.enabled:
                continue
            allowed = set(entry.config.allowed_tools)
            for tool in entry.tools:
                if tool_names and tool.name not in tool_names:
                    continue
                if allowed and tool.name not in allowed:
                    continue
                results.append(
                    {
                        "name": f"mcp.{entry.config.server_id}.{tool.name}",
                        "server_id": entry.config.server_id,
                        "tool_name": tool.name,
                        "description": tool.description,
                        "risk_level": entry.config.risk_level,
                        "read_only": entry.config.read_only_default,
                    }
                )
        return results

    def select_tools(self, requested: list[str]) -> list[tuple[str, str, str]]:
        """Resolve *requested* fully-qualified tool names against the catalog.

        ``requested`` may be fully qualified (``mcp.<server>.<tool>``) or a
        bare tool name.  Returns ``(server_id, tool_name, qualified)`` tuples
        only for tools that exist AND are in the server allow-list.  This is
        the ONLY way planner/agents may reference MCP tools: arbitrary names
        never resolve.
        """
        selected: list[tuple[str, str, str]] = []
        for req in requested:
            parts = req.split(".")
            if len(parts) >= 3 and parts[0] == "mcp":
                server_id, tool_name = parts[1], ".".join(parts[2:])
            else:
                server_id, tool_name = "", req
            for entry in self._entries.values():
                if not entry.config.enabled:
                    continue
                if server_id and entry.config.server_id != server_id:
                    continue
                allowed = set(entry.config.allowed_tools)
                if allowed and tool_name not in allowed:
                    continue
                if any(t.name == tool_name for t in entry.tools):
                    selected.append(
                        (entry.config.server_id, tool_name, f"mcp.{entry.config.server_id}.{tool_name}")
                    )
                    break
        return selected


def build_catalog_from_settings(settings: Any) -> MCPServerCatalog:
    """Create + populate a catalog from Settings (empty catalog when unset)."""
    catalog = MCPServerCatalog()
    if getattr(settings, "mcp_catalog_json", ""):
        try:
            catalog.register_from_json(settings.mcp_catalog_json)
        except Exception as exc:
            logger.warning("Could not load MCP catalog from settings: %s", exc)
    return catalog
