"""
MCP lifecycle, health, timeout, failure-mode, and security-boundary tests.

These complement the existing MCP tests by focusing on the parts of the
Phase 5 MCP ecosystem that matter most in production:

* server lifecycle (connect / discover / disconnect / shutdown)
* health state transitions
* unhealthy-server behavior
* timeout propagation
* disabled-server deny-by-default
* bounded reconnect behavior
* MCPToolManager registration path
* catalog tool selection / allow-list semantics
* no-secrets-in-catalog invariant
"""

from __future__ import annotations

import pytest

from aegisforge.domain.models import (
    MCPServerConfig,
    MCPToolDefinition,
)
from aegisforge.mcp.adapter import MCPToolAdapter, MCPToolManager
from aegisforge.mcp.catalog import MCPHealthState, MCPServerCatalog
from aegisforge.mcp.client import MCPToolResult, MockMCPClient
from aegisforge.mcp.lifecycle import (
    MCPLifecycleError,
    MCPLifecycleManager,
    configure_mcp_registry,
)
from aegisforge.tools.base import TStatus
from aegisforge.tools.registry import ToolRegistry


def _server(
    *
    ,
    server_id: str = "test-server",
    name: str = "Test MCP Server",
    command: str = "echo",
    allowed_tools: list[str] | None = None,
    risk_level: str = "low",
    enabled: bool = True,
    timeout_seconds: int = 5,
) -> MCPServerConfig:
    return MCPServerConfig(
        server_id=server_id,
        name=name,
        command=command,
        args=[],
        url="",
        allowed_tools=allowed_tools or [],
        timeout_seconds=timeout_seconds,
        enabled=enabled,
        transport="stdio",
        version="1.0",
        risk_level=risk_level,
        read_only_default=True,
        description="",
    )


def _server(
    *,
    server_id: str = "test-server",
    name: str = "Test MCP Server",
    command: str = "echo",
    allowed_tools: list[str] | None = None,
    risk_level: str = "low",
    enabled: bool = True,
    timeout_seconds: int = 5,
) -> MCPServerConfig:
    return MCPServerConfig(
        server_id=server_id,
        name=name,
        command=command,
        args=[],
        url="",
        allowed_tools=allowed_tools or [],
        timeout_seconds=timeout_seconds,
        enabled=enabled,
        transport="stdio",
        version="1.0",
        risk_level=risk_level,
        read_only_default=True,
        description="",
    )


# ---------------------------------------------------------------------------
# Catalog basics
# ---------------------------------------------------------------------------


def test_catalog_register_and_list() -> None:
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1"))
    catalog.register(_server(server_id="s2", name="Second"))

    assert len(catalog.list_servers()) == 2
    assert {e.config.server_id for e in catalog.list_servers()} == {"s1", "s2"}


def test_catalog_duplicate_id_updates_config() -> None:
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1", risk_level="low"))
    catalog.register(_server(server_id="s1", risk_level="high"))

    entry = catalog.get("s1")
    assert entry is not None
    assert entry.config.risk_level == "high"


def test_catalog_disabling_sets_health_state() -> None:
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1", enabled=True))
    catalog.register(_server(server_id="s2", enabled=False))

    assert catalog.get("s1").health.state == MCPHealthState.UNKNOWN
    assert catalog.get("s2").health.state == MCPHealthState.DISABLED


def test_catalog_public_dict_excludes_secrets() -> None:
    catalog = MCPServerCatalog()
    catalog.register(
        MCPServerConfig(
            server_id="s1",
            name="Test",
            command="echo",
            url="http://internal.example/secrets",
            enabled=True,
        )
    )
    public = catalog.get("s1").public_dict()
    assert "url" not in public
    assert "command" not in public
    assert public["server_id"] == "s1"


def test_catalog_list_tools_skips_disabled_server() -> None:
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1", enabled=True))
    catalog.register(_server(server_id="s2", enabled=False))
    catalog.set_tools(
        "s1",
        [MCPToolDefinition(name="t1", description="T1", server_id="s1")],
    )
    catalog.set_tools(
        "s2",
        [MCPToolDefinition(name="t2", description="T2", server_id="s2")],
    )

    tools = catalog.list_tools()
    assert len(tools) == 1
    assert tools[0]["server_id"] == "s1"


def test_catalog_list_tools_respects_allowed_tools() -> None:
    catalog = MCPServerCatalog()
    catalog.register(
        _server(server_id="s1", allowed_tools=["allowed"])
    )
    catalog.set_tools(
        "s1",
        [
            MCPToolDefinition(name="allowed", description="A", server_id="s1"),
            MCPToolDefinition(name="blocked", description="B", server_id="s1"),
        ],
    )

    tools = catalog.list_tools(server_id="s1")
    assert len(tools) == 1
    assert tools[0]["tool_name"] == "allowed"


def test_catalog_select_tools_resolves_qualified_names() -> None:
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1", allowed_tools=["search"]))
    catalog.set_tools(
        "s1",
        [
            MCPToolDefinition(name="search", description="Search", server_id="s1"),
            MCPToolDefinition(name="write", description="Write", server_id="s1"),
        ],
    )

    selected = catalog.select_tools(["mcp.s1.search", "mcp.s1.write", "unknown"])
    assert len(selected) == 1
    server_id, tool_name, qualified = selected[0]
    assert server_id == "s1"
    assert tool_name == "search"
    assert qualified == "mcp.s1.search"


def test_catalog_select_tools_ignores_disabled_server() -> None:
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1", enabled=False))
    catalog.set_tools(
        "s1",
        [MCPToolDefinition(name="search", description="Search", server_id="s1")],
    )
    assert catalog.select_tools(["mcp.s1.search"]) == []


# ---------------------------------------------------------------------------
# Lifecycle manager
# ---------------------------------------------------------------------------


def test_lifecycle_connect_unblocks_tool_discovery() -> None:
    client = MockMCPClient()
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1"))
    client.register_tools(
        "s1",
        [MCPToolDefinition(name="search", description="Search", server_id="s1")],
    )

    lifecycle = MCPLifecycleManager(catalog, client)
    assert lifecycle.connect("s1") is True
    assert lifecycle.health_check("s1", force=True) is True

    tools = lifecycle.discover("s1")
    assert len(tools) == 1
    assert tools[0].name == "search"


def test_lifecycle_health_transitions_on_failure() -> None:
    client = MockMCPClient()
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1"))
    lifecycle = MCPLifecycleManager(catalog, client, max_reconnect_attempts=1)

    client.set_health("s1", healthy=False)

    assert lifecycle.health_check("s1", force=True) is False
    entry = catalog.get("s1")
    assert entry is not None
    assert entry.health.state == MCPHealthState.UNHEALTHY


def test_lifecycle_reconnect_bounded_recovers_and_then_fails() -> None:
    client = MockMCPClient()
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1"))
    lifecycle = MCPLifecycleManager(catalog, client, max_reconnect_attempts=2)

    assert lifecycle.connect("s1") is True
    client.set_health("s1", healthy=False)

    # First reconnect recovers the server; subsequent forced-failure path is unhealthy.
    assert lifecycle.reconnect("s1") is True
    assert lifecycle.health_check("s1", force=True) is True

    client.set_health("s1", healthy=False)
    assert lifecycle.health_check("s1", force=True) is False
    entry = catalog.get("s1")
    assert entry is not None
    assert entry.health.state == MCPHealthState.UNHEALTHY


def test_lifecycle_disconnect_and_shutdown() -> None:
    client = MockMCPClient()
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1"))
    catalog.register(_server(server_id="s2"))
    client.register_tools(
        "s1",
        [MCPToolDefinition(name="t1", description="T1", server_id="s1")],
    )
    lifecycle = MCPLifecycleManager(catalog, client)

    lifecycle.connect("s1")
    lifecycle.connect("s2")

    lifecycle.disconnect("s1")
    assert client.is_connected("s1") is False
    assert client.is_connected("s2") is True

    lifecycle.shutdown()
    assert client.is_connected("s1") is False
    assert client.is_connected("s2") is False


def test_lifecycle_tool_registration_keeps_server_controlled_risk() -> None:
    client = MockMCPClient()
    catalog = MCPServerCatalog()
    catalog.register(
        _server(
            server_id="s1",
            risk_level="high",
            allowed_tools=["dangerous"],
        )
    )
    client.register_tools(
        "s1",
        [MCPToolDefinition(name="dangerous", description="D", server_id="s1")],
    )
    lifecycle = MCPLifecycleManager(catalog, client)
    lifecycle.connect("s1")
    registry = ToolRegistry()
    count = lifecycle.register_tools("s1", registry=registry)

    assert count == 1
    tool = registry.get("mcp.s1.dangerous")
    assert tool is not None
    assert tool.definition.risk_level == "high"
    assert tool.definition.external_side_effect is True


def test_lifecycle_execute_tool_safely_raises_when_unhealthy() -> None:
    client = MockMCPClient()
    catalog = MCPServerCatalog()
    catalog.register(_server(server_id="s1"))
    lifecycle = MCPLifecycleManager(catalog, client)

    with pytest.raises(MCPLifecycleError, match="not healthy"):
        lifecycle.execute_tool_safely("s1", "t1", {})


# ---------------------------------------------------------------------------
# MCPToolManager
# ---------------------------------------------------------------------------


def test_manager_register_in_registry_after_discovery() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    manager.configure_server(_server(server_id="s1"))
    manager.connect_server("s1")
    client.register_tools(
        "s1",
        [
            MCPToolDefinition(name="search", description="Search", server_id="s1"),
            MCPToolDefinition(name="write", description="Write", server_id="s1"),
        ],
    )

    registry = ToolRegistry()
    adapters = manager.discover_and_create_adapters("s1")
    for adapter in adapters:
        registry.register(adapter)

    assert registry.has_tool("mcp.s1.search")
    assert registry.has_tool("mcp.s1.write")


def test_manager_uses_server_timeout_in_adapter() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    manager.configure_server(_server(server_id="s1", timeout_seconds=17))
    manager.connect_server("s1")
    client.register_tools(
        "s1",
        [MCPToolDefinition(name="slow", description="Slow", server_id="s1")],
    )

    adapters = manager.discover_and_create_adapters("s1")
    adapter = adapters[0]
    assert adapter.definition.timeout_seconds == 17


# ---------------------------------------------------------------------------
# Timeout / failure-mode propagation
# ---------------------------------------------------------------------------


def test_adapter_timeout_becomes_tool_timeout_status() -> None:
    client = MockMCPClient()
    config = _server(server_id="s1", timeout_seconds=2)
    client.connect(config)
    client.set_tool_response(
        "s1",
        "slow",
        MCPToolResult(
            status="timeout",
            error="MCP tool slow timed out after 2s",
            tool_name="slow",
            server_id="s1",
        ),
    )

    mcp_tool = MCPToolDefinition(name="slow", description="Slow", server_id="s1")
    adapter = MCPToolAdapter(mcp_tool=mcp_tool, mcp_client=client, server_config=config)
    result = adapter.execute({}, granted_permissions=[f"mcp.{config.server_id}"])

    assert result.status == TStatus.TIMEOUT
    assert "timed out" in (result.error or "").lower()
    assert result.duration_ms >= 0


def test_adapter_failed_mcp_response_becomes_failed_status() -> None:
    client = MockMCPClient()
    config = _server(server_id="s1")
    client.connect(config)
    client.set_tool_response(
        "s1",
        "bad",
        MCPToolResult(
            status="failed",
            error="Remote server returned an error",
            tool_name="bad",
            server_id="s1",
        ),
    )

    mcp_tool = MCPToolDefinition(name="bad", description="Bad", server_id="s1")
    adapter = MCPToolAdapter(mcp_tool=mcp_tool, mcp_client=client, server_config=config)
    result = adapter.execute({}, granted_permissions=[f"mcp.{config.server_id}"])

    assert result.status == TStatus.FAILED
    assert result.error == "Remote server returned an error"


def test_adapter_unconnected_server_is_a_clear_failure() -> None:
    client = MockMCPClient()
    config = _server(server_id="s1")
    mcp_tool = MCPToolDefinition(name="t1", description="T1", server_id="s1")
    adapter = MCPToolAdapter(mcp_tool=mcp_tool, mcp_client=client, server_config=config)

    result = adapter.execute({}, granted_permissions=[f"mcp.{config.server_id}"])
    assert result.status == TStatus.FAILED
    assert config.server_id in (result.error or "")


def test_tool_registry_denies_disabled_tool() -> None:
    registry = ToolRegistry()
    tool = MCPToolAdapter(
        mcp_tool=MCPToolDefinition(name="hidden", description="Hidden", server_id="s1"),
        mcp_client=MockMCPClient(),
        server_config=_server(server_id="s1"),
    )
    tool.definition.enabled = False  # type: ignore[attr-defined]
    registry.register(tool)

    result = registry.execute("mcp.s1.hidden", {}, granted_permissions=["mcp.s1"])
    assert result.status == TStatus.DENIED
    assert "disabled" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Configure registry flow (settings → catalog → lifecycle)
# ---------------------------------------------------------------------------


def test_configure_mcp_registry_disabled_returns_no_lifecycle() -> None:
    settings = type("Settings", (), {"mcp_enabled": False})()  # type: ignore[attr-defined]
    registry, lifecycle = configure_mcp_registry(settings)
    assert lifecycle is None
    assert isinstance(registry, ToolRegistry)


def test_configure_mcp_registry_uses_catalog_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MockMCPClient()
    client.register_tools(
        "s1",
        [
            MCPToolDefinition(name="allowed", description="A", server_id="s1"),
            MCPToolDefinition(name="blocked", description="B", server_id="s1"),
        ],
    )
    monkeypatch.setattr("aegisforge.mcp.lifecycle.StdioMCPClient", lambda: client)
    settings = type(  # type: ignore[attr-defined]
        "Settings",
        (),
        {
            "mcp_enabled": True,
            "mcp_catalog_json": (
                '[{"server_id":"s1","name":"Test","allowed_tools":["allowed"],'
                '"command":"echo","transport":"stdio","timeout_seconds":5,'
                '"risk_level":"low","read_only_default":true}]'
            ),
            "mcp_health_check_interval_seconds": 60,
            "environment": "test",
        },
    )()
    registry, lifecycle = configure_mcp_registry(settings)

    assert lifecycle is not None
    assert registry.has_tool("mcp.s1.allowed")
    assert not registry.has_tool("mcp.s1.blocked")
    lifecycle.shutdown()
