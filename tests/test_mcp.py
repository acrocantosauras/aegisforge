"""Tests for MCP (Model Context Protocol) integration.

Covers: tool discovery, permission denial, invocation, timeout,
malformed response, unavailable server, and tool adapter.
"""
from __future__ import annotations

import pytest

from aegisforge.config import Settings
from aegisforge.domain.models import MCPServerConfig, MCPToolDefinition
from aegisforge.mcp.adapter import MCPToolAdapter, MCPToolManager
from aegisforge.mcp.client import MCPToolResult, MockMCPClient
from aegisforge.mcp.lifecycle import configure_mcp_registry
from aegisforge.tools.registry import ToolRegistry


def _make_server_config(**overrides) -> MCPServerConfig:
    defaults = {
        "server_id": "test-server",
        "name": "Test MCP Server",
        "command": "echo",
        "args": [],
        "timeout_seconds": 5,
        "enabled": True,
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


# --- MCP Client Tests ---


def test_mock_mcp_client_connect() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    assert client.connect(config) is True
    assert client.is_connected("test-server") is True


def test_mock_mcp_client_disconnect() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)
    client.disconnect("test-server")
    assert client.is_connected("test-server") is False


def test_mock_mcp_client_discover_tools() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    tools = [
        MCPToolDefinition(name="search", description="Search tool", server_id="test-server"),
        MCPToolDefinition(name="create", description="Create tool", server_id="test-server"),
    ]
    client.register_tools("test-server", tools)

    discovered = client.discover_tools("test-server")
    assert len(discovered) == 2
    assert discovered[0].name == "search"


def test_mock_mcp_client_invoke_tool() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    client.set_tool_response(
        "test-server",
        "search",
        MCPToolResult(
            status="completed",
            output={"results": ["found something"]},
            tool_name="search",
            server_id="test-server",
        ),
    )

    result = client.invoke_tool("test-server", "search", {"query": "test"})
    assert result.status == "completed"
    assert result.output["results"] == ["found something"]


def test_mock_mcp_client_invoke_tool_default_response() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    result = client.invoke_tool("test-server", "unknown_tool", {})
    assert result.status == "completed"


def test_mock_mcp_client_not_connected() -> None:
    client = MockMCPClient()
    assert client.is_connected("nonexistent") is False


# --- MCP Tool Adapter Tests ---


def test_mcp_tool_adapter_creation() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    mcp_tool = MCPToolDefinition(
        name="test_tool",
        description="A test MCP tool",
        server_id="test-server",
    )

    adapter = MCPToolAdapter(
        mcp_tool=mcp_tool,
        mcp_client=client,
        server_config=config,
    )

    assert adapter.name == "mcp.test-server.test_tool"
    assert "mcp.test-server" in adapter.required_permissions


def test_mcp_tool_adapter_execute() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    client.set_tool_response(
        "test-server",
        "echo",
        MCPToolResult(
            status="completed",
            output={"text": "hello"},
            tool_name="echo",
            server_id="test-server",
        ),
    )

    mcp_tool = MCPToolDefinition(name="echo", description="Echo tool", server_id="test-server")
    adapter = MCPToolAdapter(
        mcp_tool=mcp_tool,
        mcp_client=client,
        server_config=config,
    )

    result = adapter.execute({}, granted_permissions=["mcp.test-server"])
    assert result.status == "completed"
    assert result.output["text"] == "hello"


def test_mcp_tool_adapter_permission_denied() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    mcp_tool = MCPToolDefinition(name="tool", description="Tool", server_id="test-server")
    adapter = MCPToolAdapter(
        mcp_tool=mcp_tool,
        mcp_client=client,
        server_config=config,
    )

    # No permissions granted
    result = adapter.execute({}, granted_permissions=[])
    assert result.status == "denied"


def test_mcp_tool_adapter_server_not_connected() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    # Don't connect

    mcp_tool = MCPToolDefinition(name="tool", description="Tool", server_id="test-server")
    adapter = MCPToolAdapter(
        mcp_tool=mcp_tool,
        mcp_client=client,
        server_config=config,
    )

    result = adapter.execute({}, granted_permissions=None)
    assert result.status == "failed"
    assert "not connected" in result.error.lower()


def test_mcp_tool_adapter_timeout() -> None:
    client = MockMCPClient()
    config = _make_server_config(timeout_seconds=1)
    client.connect(config)

    # Set a response that simulates timeout
    client.set_tool_response(
        "test-server",
        "slow_tool",
        MCPToolResult(
            status="timeout",
            error="Tool timed out",
            tool_name="slow_tool",
            server_id="test-server",
        ),
    )

    mcp_tool = MCPToolDefinition(name="slow_tool", description="Slow", server_id="test-server")
    adapter = MCPToolAdapter(
        mcp_tool=mcp_tool,
        mcp_client=client,
        server_config=config,
    )

    result = adapter.execute({}, granted_permissions=None)
    assert result.status == "timeout"


def test_mcp_tool_adapter_malformed_response() -> None:
    client = MockMCPClient()
    config = _make_server_config()
    client.connect(config)

    # Set a response that simulates failure
    client.set_tool_response(
        "test-server",
        "bad_tool",
        MCPToolResult(
            status="failed",
            error="Malformed response from server",
            tool_name="bad_tool",
            server_id="test-server",
        ),
    )

    mcp_tool = MCPToolDefinition(name="bad_tool", description="Bad", server_id="test-server")
    adapter = MCPToolAdapter(
        mcp_tool=mcp_tool,
        mcp_client=client,
        server_config=config,
    )

    result = adapter.execute({}, granted_permissions=None)
    assert result.status == "failed"


# --- MCP Tool Manager Tests ---


def test_mcp_tool_manager_configure_server() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    config = _make_server_config()
    manager.configure_server(config)
    assert "test-server" in manager.get_server_configs()


def test_mcp_tool_manager_configure_requires_server_id() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    with pytest.raises(ValueError, match="server_id"):
        manager.configure_server(MCPServerConfig(server_id="", name="No ID"))


def test_mcp_tool_manager_connect_and_discover() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    config = _make_server_config()
    manager.configure_server(config)

    assert manager.connect_server("test-server") is True

    # Register tools
    client.register_tools(
        "test-server",
        [MCPToolDefinition(name="tool1", description="Tool 1", server_id="test-server")],
    )

    adapters = manager.discover_and_create_adapters("test-server")
    assert len(adapters) == 1
    assert adapters[0].name == "mcp.test-server.tool1"


def test_mcp_tool_manager_allowed_tools_filter() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    config = _make_server_config(allowed_tools=["tool1"])
    manager.configure_server(config)
    manager.connect_server("test-server")

    client.register_tools(
        "test-server",
        [
            MCPToolDefinition(name="tool1", description="T1", server_id="test-server"),
            MCPToolDefinition(name="tool2", description="T2", server_id="test-server"),
        ],
    )

    adapters = manager.discover_and_create_adapters("test-server")
    assert len(adapters) == 1
    assert "tool1" in adapters[0].name


def test_mcp_tool_manager_disabled_server() -> None:
    client = MockMCPClient()
    manager = MCPToolManager(client)
    config = _make_server_config(enabled=False)
    manager.configure_server(config)

    assert manager.connect_server("test-server") is False


def test_mcp_tool_manager_register_in_registry() -> None:
    """Test that MCP adapters can be registered in the ToolRegistry."""
    client = MockMCPClient()
    manager = MCPToolManager(client)
    config = _make_server_config()
    manager.configure_server(config)
    manager.connect_server("test-server")

    client.register_tools(
        "test-server",
        [MCPToolDefinition(name="search", description="Search", server_id="test-server")],
    )

    adapters = manager.discover_and_create_adapters("test-server")
    registry = ToolRegistry()
    for adapter in adapters:
        registry.register(adapter)

    assert registry.has_tool("mcp.test-server.search")


def test_configured_mcp_registry_uses_lifecycle_and_allow_list(monkeypatch) -> None:
    client = MockMCPClient()
    client.register_tools(
        "test-server",
        [
            MCPToolDefinition(name="search", server_id="test-server"),
            MCPToolDefinition(name="blocked", server_id="test-server"),
        ],
    )
    monkeypatch.setattr("aegisforge.mcp.lifecycle.StdioMCPClient", lambda: client)
    settings = Settings(
        environment="test",
        mcp_enabled=True,
        mcp_catalog_json=(
            '[{"server_id":"test-server","name":"Test",'
            '"allowed_tools":["search"],"command":"echo"}]'
        ),
    )

    registry, lifecycle = configure_mcp_registry(settings)

    assert lifecycle is not None
    assert registry.has_tool("mcp.test-server.search")
    assert not registry.has_tool("mcp.test-server.blocked")
    lifecycle.shutdown()
