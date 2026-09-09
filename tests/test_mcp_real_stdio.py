"""
Real MCP server proof for Phase 5.2.

This proves the existing `StdioMCPClient` works against an actual
JSON-RPC stdio MCP server, not just the mock client.

Design choices on purpose:
- The server is a tiny deterministic Python script checked into the repo.
- No cloud services, no API keys, no secrets, no external downloads.
- The test exercises configuration -> connect -> discovery -> registration/
  adaptation -> health -> allowlist -> permission -> risk -> execution ->
  disconnect/shutdown.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aegisforge.domain.models import MCPServerConfig
from aegisforge.mcp.catalog import MCPServerCatalog
from aegisforge.mcp.client import StdioMCPClient
from aegisforge.mcp.lifecycle import MCPLifecycleError, MCPLifecycleManager
from aegisforge.tools.base import TStatus
from aegisforge.tools.registry import ToolRegistry

PYTHON = sys.executable

_MCP_ECHO_SERVER_SCRIPT = """\
import json
import sys


def _main():
    # Single-threaded JSON-RPC loop: read one line, respond, repeat.
    # (A separate reader thread must NOT also read stdin — two concurrent
    # readers on the same pipe race and swallow requests.)
    def respond(req_id, payload):
        resp = {
            'jsonrpc': '2.0',
            'id': req_id,
        }
        resp.update(payload)
        print(json.dumps(resp), flush=True)

    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if not line:
            break
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get('method', '')
        params = req.get('params', {})
        req_id = req.get('id')

        if method == 'initialize':
            respond(req_id, {
                'result': {
                    'protocolVersion': '2024-10-07',
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'aegisforge-echo-test', 'version': '1.0.0'},
                }
            })
        elif method == 'notifications/initialized':
            continue
        elif method == 'tools/list':
            respond(req_id, {
                'result': {
                    'tools': [
                        {
                            'name': 'echo',
                            'description': 'Echo input back as structured text',
                            'inputSchema': {
                                'type': 'object',
                                'properties': {'message': {'type': 'string'}},
                                'required': ['message'],
                            },
                        },
                        {
                            'name': 'add',
                            'description': 'Add two numbers and return the sum',
                            'inputSchema': {
                                'type': 'object',
                                'properties': {'a': {'type': 'number'}, 'b': {'type': 'number'}},
                                'required': ['a', 'b'],
                            },
                        },
                    ]
                }
            })
        elif method == 'tools/call':
            name = params.get('name', '')
            args = params.get('arguments', {})
            if name == 'echo':
                out = {'type': 'text', 'text': 'echo:' + args.get('message', '')}
            elif name == 'add':
                out = {'type': 'text', 'text': str(float(args.get('a', 0)) + float(args.get('b', 0)))}
            else:
                # Unknown tools are a JSON-RPC protocol error, not a fake success.
                respond(req_id, {'error': {'code': -32602, 'message': 'unknown tool: ' + name}})
                continue
            respond(req_id, {'result': {'content': [out]}})
        else:
            respond(req_id, {
                'error': {'code': -32601, 'message': 'method not found: ' + method}
            })


if __name__ == '__main__':
    _main()
"""


def _server_script_path(tmp_path: Path) -> Path:
    path = tmp_path / "aegisforge_mcp_echo_server.py"
    path.write_text(_MCP_ECHO_SERVER_SCRIPT, encoding="utf-8")
    return path


@pytest.fixture()
def echo_server_path(tmp_path: Path) -> Path:
    return _server_script_path(tmp_path)


@pytest.fixture()
def echo_server(echo_server_path: Path):
    proc = subprocess.Popen(
        [PYTHON, str(echo_server_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.time()
    ready = False
    last_error = ""
    while time.time() - started < 5.0:
        if proc.poll() is not None:
            err = ""
            try:
                err = proc.stderr.read()
            except Exception:
                err = "<cannot read stderr>"
            raise RuntimeError(f"echo server exited during startup: rc={proc.returncode}\n{err}")
        try:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
            proc.stdin.flush()
            ready = True
            break
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.05)
    if not ready:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except (TimeoutError, subprocess.SubprocessError):
            pass
        raise RuntimeError(f"echo server did not start within timeout. last_error={last_error}")

    yield proc

    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (TimeoutError, subprocess.SubprocessError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except (TimeoutError, subprocess.SubprocessError):
            pass


def _new_stdio_client() -> StdioMCPClient:
    return StdioMCPClient()


# ---------------------------------------------------------------------------
# Real server connect / lifecycle
# ---------------------------------------------------------------------------


def test_real_stdio_mcp_connect(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    config = MCPServerConfig(
        server_id="real-server",
        name="Real Echo Server",
        command=PYTHON,
        args=[str(echo_server_path)],
        allowed_tools=["echo"],
        timeout_seconds=10,
        enabled=True,
        transport="stdio",
        risk_level="medium",
        read_only_default=True,
    )

    assert client.connect(config) is True
    assert client.is_connected("real-server") is True
    assert client.health_check("real-server") is True

    client.disconnect("real-server")
    assert client.is_connected("real-server") is False


def test_real_stdio_mcp_discover_tools(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    config = MCPServerConfig(
        server_id="real-server",
        name="Real Echo Server",
        command=PYTHON,
        args=[str(echo_server_path)],
        allowed_tools=[],
        timeout_seconds=10,
        enabled=True,
        transport="stdio",
        risk_level="low",
        read_only_default=True,
    )

    assert client.connect(config) is True
    tools = client.discover_tools("real-server")
    names = {t.name for t in tools}
    assert names == {"echo", "add"}, f"expected {{echo,add}} but got {names}"
    assert tools[0].server_id == "real-server"
    assert tools[0].input_schema == {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}


def test_real_stdio_mcp_invoke_echo(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    config = MCPServerConfig(
        server_id="real-server",
        name="Real Echo Server",
        command=PYTHON,
        args=[str(echo_server_path)],
        allowed_tools=["echo"],
        timeout_seconds=10,
        enabled=True,
        transport="stdio",
        risk_level="low",
        read_only_default=True,
    )
    assert client.connect(config) is True

    result = client.invoke_tool("real-server", "echo", {"message": "hello"}, timeout_seconds=3)
    assert result.status == "completed", f"expected completed but got {result.status}: {result.error}"
    assert result.output["text"] == "echo:hello"
    assert result.server_id == "real-server"


def test_real_stdio_mcp_invoke_add(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    config = MCPServerConfig(
        server_id="real-server",
        name="Real Echo Server",
        command=PYTHON,
        args=[str(echo_server_path)],
        allowed_tools=["add"],
        timeout_seconds=10,
        enabled=True,
        transport="stdio",
        risk_level="low",
        read_only_default=True,
    )
    assert client.connect(config) is True

    result = client.invoke_tool("real-server", "add", {"a": 2, "b": 3}, timeout_seconds=3)
    assert result.status == "completed", f"expected completed but got {result.status}: {result.error}"
    assert result.output["text"] == "5.0"
    assert result.duration_ms >= 0


def test_real_stdio_mcp_unknown_tool_returns_failed(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    config = MCPServerConfig(
        server_id="real-server",
        name="Real Echo Server",
        command=PYTHON,
        args=[str(echo_server_path)],
        allowed_tools=[],
        timeout_seconds=10,
        enabled=True,
        transport="stdio",
        risk_level="low",
        read_only_default=True,
    )
    assert client.connect(config) is True

    result = client.invoke_tool("real-server", "no_such_tool", {}, timeout_seconds=3)
    assert result.status == "failed"
    assert "unknown tool" in result.error.lower(), result.error


def test_real_stdio_mcp_error_method_returns_failed(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    config = MCPServerConfig(
        server_id="real-server",
        name="Real Echo Server",
        command=PYTHON,
        args=[str(echo_server_path)],
        allowed_tools=[],
        timeout_seconds=10,
        enabled=True,
        transport="stdio",
        risk_level="low",
        read_only_default=True,
    )
    assert client.connect(config) is True

    result = client.invoke_tool("real-server", "tools/call", {"name": "echo"}, timeout_seconds=3)
    assert result.status == "failed"
    # The server treats the unknown tool as a JSON-RPC parameter error.
    assert "unknown tool" in result.error.lower(), result.error


# ---------------------------------------------------------------------------
# Lifecycle over a real server
# ---------------------------------------------------------------------------


def test_real_lifecycle_connect_discover_register(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    catalog = MCPServerCatalog()
    catalog.register(
        MCPServerConfig(
            server_id="real-server",
            name="Real Echo Server",
            command=PYTHON,
            args=[str(echo_server_path)],
            allowed_tools=["echo"],
            timeout_seconds=10,
            enabled=True,
            transport="stdio",
            risk_level="high",
            read_only_default=True,
        )
    )

    lifecycle = MCPLifecycleManager(catalog, client, max_reconnect_attempts=1)
    assert lifecycle.connect("real-server") is True
    assert lifecycle.health_check("real-server", force=True) is True

    tools = lifecycle.discover("real-server")
    assert len(tools) == 1, f"expected 1 tool but got {len(tools)}"
    assert tools[0].name == "echo"

    registry = ToolRegistry()
    count = lifecycle.register_tools("real-server", registry=registry)
    assert count == 1

    tool = registry.get("mcp.real-server.echo")
    assert tool is not None
    assert tool.definition.risk_level == "high"
    assert tool.definition.external_side_effect is True
    assert tool.definition.read_only is True


def test_real_lifecycle_adapter_executes_via_registry(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    catalog = MCPServerCatalog()
    catalog.register(
        MCPServerConfig(
            server_id="real-server",
            name="Real Echo Server",
            command=PYTHON,
            args=[str(echo_server_path)],
            allowed_tools=["echo"],
            timeout_seconds=10,
            enabled=True,
            transport="stdio",
            risk_level="low",
            read_only_default=True,
        )
    )

    lifecycle = MCPLifecycleManager(catalog, client, max_reconnect_attempts=1)
    assert lifecycle.connect("real-server") is True
    count = lifecycle.register_tools("real-server")
    assert count == 1

    tool = lifecycle._registry.get("mcp.real-server.echo")
    assert tool is not None

    result = tool.execute({"message": "from registry"}, granted_permissions=["mcp.real-server"])
    assert result.status == TStatus.COMPLETED, f"expected completed but got {result.status}: {result.error}"
    assert result.output["text"] == "echo:from registry"


def test_real_lifecycle_execute_tool_safely_raises_when_unhealthy(echo_server: subprocess.Popen, echo_server_path: Path) -> None:
    client = _new_stdio_client()
    catalog = MCPServerCatalog()
    catalog.register(
        MCPServerConfig(
            server_id="real-server",
            name="Real Echo Server",
            command=PYTHON,
            args=[str(echo_server_path)],
            allowed_tools=["echo"],
            timeout_seconds=10,
            enabled=True,
            transport="stdio",
            risk_level="low",
            read_only_default=True,
        )
    )

    lifecycle = MCPLifecycleManager(catalog, client, max_reconnect_attempts=1)
    assert lifecycle.connect("real-server") is True
    lifecycle.disconnect("real-server")

    with pytest.raises(MCPLifecycleError, match="not healthy"):
        lifecycle.execute_tool_safely("real-server", "echo", {"message": "x"})


# ---------------------------------------------------------------------------
# Lifecycle shutdown leaves the real process terminated
# ---------------------------------------------------------------------------


def test_real_lifecycle_shutdown_terminates_server(echo_server_path: Path) -> None:
    client = _new_stdio_client()
    catalog = MCPServerCatalog()
    catalog.register(
        MCPServerConfig(
            server_id="real-server",
            name="Real Echo Server",
            command=PYTHON,
            args=[str(echo_server_path)],
            allowed_tools=["echo"],
            timeout_seconds=10,
            enabled=True,
            transport="stdio",
            risk_level="low",
            read_only_default=True,
        )
    )

    lifecycle = MCPLifecycleManager(catalog, client, max_reconnect_attempts=1)
    assert lifecycle.connect("real-server") is True
    assert client.is_connected("real-server") is True

    lifecycle.shutdown()
    assert client.is_connected("real-server") is False
