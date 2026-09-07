"""MCP lifecycle management: connect, discover, health, reconnect, shutdown.

Phase 5 requirement: a failed MCP server must never bring down unrelated
workflows.  The lifecycle manager isolates per-server failures, exposes
health through the catalog + Prometheus metrics, and performs bounded
reconnects with backoff.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aegisforge.domain.models import MCPToolDefinition
from aegisforge.mcp.adapter import MCPToolAdapter
from aegisforge.mcp.catalog import MCPHealthState, MCPServerCatalog, build_catalog_from_settings
from aegisforge.mcp.client import MCPClient, StdioMCPClient
from aegisforge.observability.metrics import record_mcp_server_status
from aegisforge.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class MCPLifecycleError(Exception):
    """Raised when a server cannot be used for a workflow-critical operation."""


class MCPLifecycleManager:
    """Owns connection lifecycle + health for every catalog server.

    Responsibilities:
    - connect / disconnect / reconnect (bounded, with small backoff),
    - health checks that update catalog state and metrics,
    - tool discovery → catalog update → adapter registration into a ToolRegistry,
    - graceful shutdown of all servers.
    """

    def __init__(
        self,
        catalog: MCPServerCatalog,
        client: MCPClient,
        health_check_interval_seconds: int = 60,
        max_reconnect_attempts: int = 2,
    ) -> None:
        self._catalog = catalog
        self._client = client
        self._interval = health_check_interval_seconds
        self._max_reconnects = max_reconnect_attempts
        self._last_health_check: dict[str, float] = {}
        self._registry = ToolRegistry()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self, server_id: str) -> bool:
        entry = self._catalog.get(server_id)
        if entry is None:
            logger.error("Cannot connect: unknown MCP server %s", server_id)
            return False
        config = entry.config
        if not config.enabled:
            entry.health.state = MCPHealthState.DISABLED
            self._set_status(server_id, "disabled")
            return False

        entry.health.state = MCPHealthState.CONNECTING
        try:
            ok = self._client.connect(config)
        except Exception as exc:
            logger.exception("Connect to MCP server %s failed", server_id)
            self._catalog.set_health(server_id, MCPHealthState.UNHEALTHY, str(exc), connected=False)
            self._set_status(server_id, "unhealthy")
            return False

        if ok:
            entry.health.connected = True
            entry.health.state = MCPHealthState.HEALTHY
            self._set_status(server_id, "connected")
            logger.info("MCP server %s connected", server_id)
            return True

        self._catalog.set_health(server_id, MCPHealthState.UNHEALTHY, "connect returned failure", connected=False)
        self._set_status(server_id, "unhealthy")
        return False

    def connect_all(self) -> dict[str, bool]:
        return {sid: self.connect(sid) for sid in [e.config.server_id for e in self._catalog.list_servers()]}

    def disconnect(self, server_id: str) -> None:
        entry = self._catalog.get(server_id)
        if entry is None:
            return
        try:
            self._client.disconnect(server_id)
        except Exception as exc:
            logger.warning("Disconnect MCP server %s failed: %s", server_id, exc)
        entry.health.connected = False
        entry.health.state = MCPHealthState.UNKNOWN
        self._set_status(server_id, "disconnected")

    def shutdown(self) -> None:
        """Graceful shutdown of every connected server."""
        for entry in self._catalog.list_servers():
            if entry.health.connected:
                self.disconnect(entry.config.server_id)

    def reconnect(self, server_id: str) -> bool:
        """Attempt a bounded reconnect with backoff."""
        entry = self._catalog.get(server_id)
        if entry is None:
            return False
        self.disconnect(server_id)
        delay = 0.25
        for attempt in range(1, self._max_reconnects + 1):
            logger.info(
                "Reconnecting MCP server %s (attempt %d/%d)",
                server_id,
                attempt,
                self._max_reconnects,
            )
            if self.connect(server_id):
                self._set_status(server_id, "reconnected")
                return True
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
        return False

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    def health_check(self, server_id: str, force: bool = False) -> bool:
        """Check a server's liveness and update catalog health state."""
        entry = self._catalog.get(server_id)
        if entry is None:
            return False
        if not entry.config.enabled:
            entry.health.state = MCPHealthState.DISABLED
            return False

        now = time.time()
        if not force and now - self._last_health_check.get(server_id, 0) < self._interval:
            return entry.health.state == MCPHealthState.HEALTHY
        self._last_health_check[server_id] = now

        try:
            healthy = self._client.health_check(server_id)
        except Exception as exc:
            healthy = False
            self._catalog.set_health(server_id, MCPHealthState.UNHEALTHY, f"health check error: {exc}", connected=False)
            self._set_status(server_id, "unhealthy")
            return False

        if healthy:
            self._catalog.set_health(server_id, MCPHealthState.HEALTHY, "", connected=True)
            self._set_status(server_id, "healthy")
        else:
            self._catalog.set_health(server_id, MCPHealthState.UNHEALTHY, "health check failed", connected=False)
            self._set_status(server_id, "unhealthy")
        return healthy

    def check_all(self, force: bool = False) -> dict[str, bool]:
        return {
            e.config.server_id: self.health_check(e.config.server_id, force=force)
            for e in self._catalog.list_servers()
            if e.config.enabled
        }

    # ------------------------------------------------------------------
    # Tool discovery → catalog → registry
    # ------------------------------------------------------------------

    def discover(self, server_id: str) -> list[MCPToolDefinition]:
        """Discover tools from a server and update the catalog."""
        entry = self._catalog.get(server_id)
        if entry is None:
            return []
        if not entry.health.connected:
            logger.warning("Cannot discover tools: MCP server %s not connected", server_id)
            return []

        try:
            tools = self._client.discover_tools(server_id)
        except Exception as exc:
            self._catalog.set_health(server_id, MCPHealthState.UNHEALTHY, f"discovery error: {exc}", connected=False)
            self._set_status(server_id, "unhealthy")
            logger.warning("Tool discovery failed for %s: %s", server_id, exc)
            return []

        allowed = set(entry.config.allowed_tools)
        filtered = [t for t in tools if not allowed or t.name in allowed]
        self._catalog.set_tools(server_id, filtered)
        self._catalog.set_health(server_id, MCPHealthState.HEALTHY, "", connected=True)
        return filtered

    def register_tools(self, server_id: str, registry: ToolRegistry | None = None) -> int:
        """Discover + register adapters for a server into a ToolRegistry.

        Returns the number of adapters registered.  Tool risk/approval
        metadata comes from the server config (operator-controlled); the
        planner can never change it.
        """
        target = registry or self._registry
        tools = self.discover(server_id)
        if not tools:
            return 0
        entry = self._catalog.get(server_id)
        assert entry is not None
        config = entry.config
        registered = 0
        for tool in tools:
            name = f"mcp.{config.server_id}.{tool.name}"
            try:
                if target.has_tool(name):
                    continue
                adapter = MCPToolAdapter(
                    mcp_tool=tool,
                    mcp_client=self._client,
                    server_config=config,
                )
                # Server-controlled risk metadata flows into the adapter's
                # ToolDefinition at construction time (never editable later).
                adapter.definition.risk_level = config.risk_level
                adapter.definition.read_only = config.read_only_default
                adapter.definition.external_side_effect = config.risk_level in ("high", "critical")
                target.register(adapter)
                registered += 1
            except Exception as exc:
                logger.warning("Could not register MCP tool %s: %s", name, exc)
        return registered

    def register_all(self, registry: ToolRegistry | None = None) -> dict[str, int]:
        return {
            e.config.server_id: self.register_tools(e.config.server_id, registry=registry)
            for e in self._catalog.list_servers()
            if e.config.enabled and e.health.connected
        }

    def execute_tool_safely(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        granted_permissions: list[str] | None = None,
    ):
        """Invoke a tool with health guarding; failed servers never crash callers.

        Raises MCPLifecycleError only when the server is unreachable so the
        caller can decide (approval/policy) how to proceed.
        """
        if not self.health_check(server_id):
            raise MCPLifecycleError(f"MCP server {server_id} is not healthy")
        return self._client.invoke_tool(
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
            timeout_seconds=self._catalog.get(server_id).config.timeout_seconds,  # type: ignore[union-attr]
        )

    def _set_status(self, server_id: str, status: str) -> None:
        try:
            record_mcp_server_status(server_id, status)
        except Exception:  # noqa: S110 - observability must never break lifecycle
            pass


def configure_mcp_registry(
    settings: Any,
    registry: ToolRegistry | None = None,
    catalog: MCPServerCatalog | None = None,
) -> tuple[ToolRegistry, MCPLifecycleManager | None]:
    """Connect configured stdio servers and register their approved tools."""
    target = registry or ToolRegistry()
    if not getattr(settings, "mcp_enabled", False):
        return target, None

    catalog = catalog or build_catalog_from_settings(settings)
    lifecycle = MCPLifecycleManager(
        catalog,
        StdioMCPClient(),
        health_check_interval_seconds=settings.mcp_health_check_interval_seconds,
    )
    for entry in catalog.list_servers():
        if not entry.config.enabled:
            continue
        if entry.config.transport != "stdio":
            logger.warning(
                "MCP server %s uses unsupported transport %s; leaving it unavailable",
                entry.config.server_id,
                entry.config.transport,
            )
            continue
        if lifecycle.connect(entry.config.server_id):
            lifecycle.register_tools(entry.config.server_id, registry=target)
    return target, lifecycle
