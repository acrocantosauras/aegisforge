"""MCP client abstraction for AegisForge.

Provides an abstract MCPClient interface and a mock implementation for testing.
MCP servers are treated as external/untrusted capabilities.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import MCPServerConfig, MCPToolDefinition

logger = logging.getLogger(__name__)


@dataclass
class MCPToolResult:
    """Result of an MCP tool invocation."""

    status: str  # "completed", "failed", "timeout", "denied"
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    tool_name: str = ""
    server_id: str = ""


class MCPClient(ABC):
    """Abstract MCP client interface."""

    @abstractmethod
    def connect(self, server_config: MCPServerConfig) -> bool:
        """Connect to an MCP server."""
        ...

    @abstractmethod
    def disconnect(self, server_id: str) -> None:
        """Disconnect from an MCP server."""
        ...

    @abstractmethod
    def discover_tools(self, server_id: str) -> list[MCPToolDefinition]:
        """Discover available tools from an MCP server."""
        ...

    @abstractmethod
    def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: int = 30,
    ) -> MCPToolResult:
        """Invoke a tool on an MCP server."""
        ...

    @abstractmethod
    def is_connected(self, server_id: str) -> bool:
        """Check if connected to a server."""
        ...


class MockMCPClient(MCPClient):
    """Mock MCP client for testing.

    Allows registering fake tools and responses.
    """

    def __init__(self) -> None:
        self._connected: dict[str, bool] = {}
        self._tools: dict[str, list[MCPToolDefinition]] = {}
        self._tool_responses: dict[str, MCPToolResult] = {}

    def connect(self, server_config: MCPServerConfig) -> bool:
        self._connected[server_config.server_id] = True
        logger.info("Mock MCP: connected to %s", server_config.server_id)
        return True

    def disconnect(self, server_id: str) -> None:
        self._connected.pop(server_id, None)
        logger.info("Mock MCP: disconnected from %s", server_id)

    def discover_tools(self, server_id: str) -> list[MCPToolDefinition]:
        return self._tools.get(server_id, [])

    def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: int = 30,
    ) -> MCPToolResult:
        key = f"{server_id}:{tool_name}"
        if key in self._tool_responses:
            return self._tool_responses[key]

        return MCPToolResult(
            status="completed",
            output={"result": f"Mock response for {tool_name}"},
            tool_name=tool_name,
            server_id=server_id,
        )

    def is_connected(self, server_id: str) -> bool:
        return self._connected.get(server_id, False)

    # Test helpers
    def register_tools(self, server_id: str, tools: list[MCPToolDefinition]) -> None:
        self._tools[server_id] = tools

    def set_tool_response(self, server_id: str, tool_name: str, response: MCPToolResult) -> None:
        self._tool_responses[f"{server_id}:{tool_name}"] = response


class StdioMCPClient(MCPClient):
    """MCP client using stdio transport.

    Communicates with MCP servers via subprocess stdio.
    """

    def __init__(self) -> None:
        self._processes: dict[str, Any] = {}
        self._connected: dict[str, bool] = {}

    def connect(self, server_config: MCPServerConfig) -> bool:
        if not server_config.command:
            logger.error("MCP server %s has no command configured", server_config.server_id)
            return False

        try:
            import subprocess

            process = subprocess.Popen(
                [server_config.command] + server_config.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._processes[server_config.server_id] = process
            self._connected[server_config.server_id] = True
            logger.info("Connected to MCP server: %s", server_config.server_id)
            return True
        except Exception:
            logger.exception("Failed to connect to MCP server %s", server_config.server_id)
            return False

    def disconnect(self, server_id: str) -> None:
        process = self._processes.pop(server_id, None)
        if process:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()
        self._connected.pop(server_id, None)

    def discover_tools(self, server_id: str) -> list[MCPToolDefinition]:
        # Send tools/list request via JSON-RPC
        response = self._send_request(server_id, "tools/list", {})
        if response and "tools" in response:
            return [
                MCPToolDefinition(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    server_id=server_id,
                    input_schema=tool.get("inputSchema", {}),
                )
                for tool in response["tools"]
            ]
        return []

    def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: int = 30,
    ) -> MCPToolResult:
        start = time.monotonic()
        try:
            response = self._send_request(
                server_id,
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                timeout=timeout_seconds,
            )
            elapsed = int((time.monotonic() - start) * 1000)

            if response is None:
                return MCPToolResult(
                    status="failed",
                    error="No response from MCP server",
                    tool_name=tool_name,
                    server_id=server_id,
                    duration_ms=elapsed,
                )

            if "error" in response:
                return MCPToolResult(
                    status="failed",
                    error=response["error"].get("message", "Unknown MCP error"),
                    tool_name=tool_name,
                    server_id=server_id,
                    duration_ms=elapsed,
                )

            content = response.get("result", {}).get("content", [])
            output = {}
            for item in content:
                if item.get("type") == "text":
                    output["text"] = item.get("text", "")

            return MCPToolResult(
                status="completed",
                output=output,
                tool_name=tool_name,
                server_id=server_id,
                duration_ms=elapsed,
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            return MCPToolResult(
                status="timeout",
                error=f"MCP tool {tool_name} timed out after {timeout_seconds}s",
                tool_name=tool_name,
                server_id=server_id,
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            return MCPToolResult(
                status="failed",
                error=str(exc),
                tool_name=tool_name,
                server_id=server_id,
                duration_ms=elapsed,
            )

    def is_connected(self, server_id: str) -> bool:
        return self._connected.get(server_id, False)

    def _send_request(
        self,
        server_id: str,
        method: str,
        params: dict[str, Any],
        timeout: int = 30,
    ) -> dict[str, Any] | None:
        process = self._processes.get(server_id)
        if process is None or process.poll() is not None:
            return None

        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }

        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()

            import select

            ready, _, _ = select.select([process.stdout], [], [], timeout)
            if not ready:
                return None

            line = process.stdout.readline()
            return json.loads(line) if line else None
        except Exception:
            logger.exception("MCP communication error with %s", server_id)
            return None


def get_mcp_client(client_type: str = "mock", **kwargs: Any) -> MCPClient:
    """Factory function to create MCP clients."""
    if client_type == "mock":
        return MockMCPClient()
    elif client_type == "stdio":
        return StdioMCPClient()
    else:
        raise ValueError(f"Unknown MCP client type: {client_type}")
