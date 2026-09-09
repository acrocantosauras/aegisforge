"""MCP Tool Adapter for AegisForge.

Wraps an MCP tool to appear as an AegisForge BaseTool.
The existing ToolRegistry remains the controlled entry point.
"""
from __future__ import annotations

import logging
from typing import Any

from aegisforge.domain.models import MCPServerConfig, MCPToolDefinition
from aegisforge.mcp.client import MCPClient, MCPToolResult
from aegisforge.tools.base import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)


class MCPToolAdapter(BaseTool):
    """Adapts an MCP tool to the AegisForge Tool interface.

    All invocations go through the MCP client, with permission checks
    enforced by the ToolRegistry before this adapter is called.
    """

    def __init__(
        self,
        mcp_tool: MCPToolDefinition,
        mcp_client: MCPClient,
        server_config: MCPServerConfig,
        permission_requirements: list[str] | None = None,
    ) -> None:
        definition = ToolDefinition(
            name=f"mcp.{server_config.server_id}.{mcp_tool.name}",
            description=mcp_tool.description or f"MCP tool: {mcp_tool.name}",
            version="1.0",
            input_schema=mcp_tool.input_schema,
            output_schema=mcp_tool.output_schema,
            permission_requirements=permission_requirements or [f"mcp.{server_config.server_id}"],
            timeout_seconds=server_config.timeout_seconds,
        )
        super().__init__(definition)
        self._mcp_client = mcp_client
        self._server_config = server_config
        self._mcp_tool_name = mcp_tool.name

    def _execute(
        self, input_data: dict[str, Any], context: Any | None = None
    ) -> dict[str, Any]:
        # Verify connection
        if not self._mcp_client.is_connected(self._server_config.server_id):
            raise RuntimeError(
                f"MCP server '{self._server_config.server_id}' is not connected"
            )

        # Invoke the MCP tool
        result: MCPToolResult = self._mcp_client.invoke_tool(
            server_id=self._server_config.server_id,
            tool_name=self._mcp_tool_name,
            arguments=input_data,
            timeout_seconds=self._server_config.timeout_seconds,
        )

        if result.status == "completed":
            return result.output
        elif result.status == "timeout":
            raise TimeoutError(result.error or "MCP tool timed out")
        else:
            raise RuntimeError(result.error or f"MCP tool failed with status: {result.status}")


class MCPToolManager:
    """Manages MCP server connections and tool registration.

    Tools are discovered from MCP servers and registered into the
    AegisForge ToolRegistry through MCPToolAdapter.
    """

    def __init__(self, mcp_client: MCPClient) -> None:
        self._client = mcp_client
        self._server_configs: dict[str, MCPServerConfig] = {}

    def configure_server(self, config: MCPServerConfig) -> None:
        """Register an MCP server configuration."""
        if not config.server_id:
            raise ValueError("MCP server config must have a server_id")
        self._server_configs[config.server_id] = config
        logger.info("Configured MCP server: %s (%s)", config.name, config.server_id)

    def connect_server(self, server_id: str) -> bool:
        """Connect to an MCP server."""
        config = self._server_configs.get(server_id)
        if config is None:
            logger.error("Unknown MCP server: %s", server_id)
            return False

        if not config.enabled:
            logger.info("MCP server %s is disabled, skipping", server_id)
            return False

        return self._client.connect(config)

    def discover_and_create_adapters(
        self,
        server_id: str,
        allowed_tools: list[str] | None = None,
    ) -> list[MCPToolAdapter]:
        """Discover tools from a server and create AegisForge tool adapters."""
        config = self._server_configs.get(server_id)
        if config is None:
            return []

        if not self._client.is_connected(server_id):
            logger.warning("Cannot discover tools: server %s not connected", server_id)
            return []

        mcp_tools = self._client.discover_tools(server_id)
        adapters: list[MCPToolAdapter] = []

        for mcp_tool in mcp_tools:
            # Apply allowed tools filter
            if allowed_tools and mcp_tool.name not in allowed_tools:
                logger.debug(
                    "Skipping MCP tool %s (not in allowed list)", mcp_tool.name
                )
                continue

            # Also check server-level allowed_tools
            if config.allowed_tools and mcp_tool.name not in config.allowed_tools:
                logger.debug(
                    "Skipping MCP tool %s (not in server allowed list)", mcp_tool.name
                )
                continue

            adapter = MCPToolAdapter(
                mcp_tool=mcp_tool,
                mcp_client=self._client,
                server_config=config,
            )
            adapters.append(adapter)

        logger.info(
            "Created %d MCP tool adapters for server %s", len(adapters), server_id
        )
        return adapters

    def disconnect_server(self, server_id: str) -> None:
        """Disconnect from an MCP server."""
        self._client.disconnect(server_id)

    def get_server_configs(self) -> dict[str, MCPServerConfig]:
        """Return all configured server configs."""
        return dict(self._server_configs)
