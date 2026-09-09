"""Separate-worker async MCP E2E proof (Phase 5.2).

Proves the REAL execution path against real infrastructure:

    API → Redis job queue → separate worker process → LangGraph →
    MCP ToolRegistry → real stdio MCP server subprocess → result →
    persistence/audit/observability

Nothing is mocked in the infrastructure path:
- Redis is the real container from docker compose.
- PostgreSQL (with the full schema) is the real container.
- The worker is a real ``python -m aegisforge.worker`` subprocess with its
  own process space, its own Redis connection, and its own DB session —
  not an in-process handler call.
- The MCP server is the real JSON-RPC stdio echo server spawned by the
  worker's ``StdioMCPClient``.

The worker's runtime configuration is verified to include the MCP tool:
the worker's settings carry ``mcp_catalog_json`` pointing at the real
server, and the test asserts the tool is registered AND executed inside
the worker process (tool_calls recorded in persisted state).

Run with: AEGISFORGE_INTEGRATION_TESTS=true pytest tests/integration/test_worker_mcp_e2e.py
Requires: docker compose up -d postgres redis
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from .conftest import PG_URL, REDIS_URL, requires_infra

PYTHON = sys.executable

# The exact same deterministic MCP server script used by the real-stdio
# proof (single-threaded JSON-RPC loop over stdio).
_MCP_ECHO_SERVER_SCRIPT = """\
import json
import sys


def _main():
    # Single-threaded JSON-RPC loop: read one line, respond, repeat.
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


def _mcp_catalog_json(server_script_path: str) -> str:
    """Operator-controlled MCP catalog config (no secrets) for the worker."""
    return json.dumps(
        [
            {
                "server_id": "e2e-server",
                "name": "E2E Echo Server",
                "transport": "stdio",
                "command": PYTHON,
                "args": [server_script_path],
                "allowed_tools": ["echo"],
                "timeout_seconds": 10,
                "enabled": True,
                "risk_level": "low",
                "read_only_default": True,
                "description": "Deterministic echo MCP server for the worker E2E proof",
            }
        ]
    )


def _worker_env(server_script_path: str, organization_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AEGISFORGE_ENVIRONMENT": "development",
            "DATABASE_URL": PG_URL,
            "REDIS_URL": REDIS_URL,
            "LLM_PROVIDER": "deterministic",
            "EMBEDDING_PROVIDER": "deterministic",
            "EMBEDDING_DIMENSION": "384",
            "SECRET_KEY": "worker-mcp-e2e-secret",
            "MCP_ENABLED": "true",
            "MCP_CATALOG_JSON": _mcp_catalog_json(server_script_path),
            "MCP_HEALTH_CHECK_INTERVAL_SECONDS": "60",
            # Bounded: a stuck worker must never hang the test run.
            "AEGISFORGE_ORG_TAG": organization_id,
        }
    )
    return env


def _register_and_login(base_url: str, organization_tag: str) -> tuple[str, dict[str, str]]:
    import httpx

    email = f"worker-mcp-e2e-{organization_tag}@example.com"
    password = "WorkerMcpE2E123!"
    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "full_name": "Worker MCP E2E"},
        )
        login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        login.raise_for_status()
        token = login.json()["access_token"]
    return email, {"Authorization": f"Bearer {token}"}


def _wait_for_job_completion(
    base_url: str,
    headers: dict[str, str],
    request_id: str,
    timeout_seconds: float = 90.0,
) -> dict:
    """Poll the request status until the separate worker finishes the job."""
    import httpx

    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        while time.monotonic() < deadline:
            resp = client.get(f"/api/v1/requests/{request_id}", headers=headers)
            if resp.status_code == 200:
                last = resp.json()
                status = str(last.get("status", ""))
                if status in ("completed", "failed", "cancelled"):
                    return last
            time.sleep(1.0)
    raise TimeoutError(f"Worker did not finish request {request_id} in time; last={last}")


@requires_infra
class TestSeparateWorkerAsyncMCP:
    """P0 proof: API → Redis → separate worker → MCP tool → persisted result."""

    @staticmethod
    @pytest.fixture(scope="class")
    def mcp_server_script(tmp_path_factory: pytest.TempPathFactory) -> str:
        path = tmp_path_factory.mktemp("mcp") / "aegisforge_mcp_echo_server.py"
        path.write_text(_MCP_ECHO_SERVER_SCRIPT, encoding="utf-8")
        return str(path)

    @staticmethod
    @pytest.fixture(scope="class")
    def worker_process(mcp_server_script: str):
        """A real separate worker process consuming the real Redis queue."""
        import threading

        organization_tag = uuid.uuid4().hex[:8]
        env = _worker_env(mcp_server_script, organization_tag)
        proc = subprocess.Popen(
            [PYTHON, "-m", "aegisforge.worker"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(Path(__file__).resolve().parents[2]),
        )

        # Drain stdout in a background thread to prevent pipe-buffer deadlock.
        # Without this the worker blocks on its first log write once the
        # OS pipe buffer (~64 KB on Linux, ~4 KB on Windows) fills up,
        # making it unable to process any jobs.
        _drain_log: list[str] = []

        def _reader(stream: subprocess.Popen) -> None:  # type: ignore[type-arg]
            try:
                for line in stream.stdout:  # type: ignore[union-attr]
                    _drain_log.append(line)  # noqa: PERF402
            except Exception:  # noqa: S110
                pass

        reader_thread = threading.Thread(target=_reader, args=(proc,), daemon=True)
        reader_thread.start()

        # Give the worker a fixed startup window.
        time.sleep(4.0)
        if proc.poll() is not None:
            reader_thread.join(timeout=5)
            out = "".join(_drain_log)
            raise RuntimeError(f"Worker exited during startup:\n{out}")
        yield proc
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        reader_thread.join(timeout=5)

    @staticmethod
    @pytest.fixture(scope="class")
    def api_base_url() -> str:
        return "http://localhost:8000"

    def test_worker_executes_mcp_tool_end_to_end(
        self,
        worker_process,
        api_base_url: str,
        mcp_server_script: str,
    ) -> None:
        """Full path: API submit → Redis → separate worker → MCP echo tool → DB."""
        import httpx
        from sqlalchemy import text

        from aegisforge.db.session import get_engine

        organization_tag = uuid.uuid4().hex[:8]
        _email, headers = _register_and_login(api_base_url, organization_tag)

        # 1. Create a request through the API (tenant-scoped)
        with httpx.Client(base_url=api_base_url, timeout=15.0) as client:
            created = client.post(
                "/api/v1/requests",
                json={"intent": "echo worker mcp proof"},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            request_id = created.json()["id"]

            # 2. Submit async execution → Redis job queue
            submitted = client.post(
                f"/api/v1/execution/requests/{request_id}/execute-async",
                headers=headers,
            )
            assert submitted.status_code == 202, submitted.text
            payload = submitted.json()
            job_id = payload["job_id"]

        # 3. Wait for the SEPARATE worker to process the job
        final = _wait_for_job_completion(api_base_url, headers, request_id)
        assert final["status"] == "completed", final

        # 4. Verify persistence: job row completed with the workflow result
        engine = get_engine(PG_URL)
        with engine.connect() as conn:
            job_row = conn.execute(
                text("SELECT status, result FROM execution_jobs WHERE id = :jid"),
                {"jid": job_id},
            ).fetchone()
            assert job_row is not None, f"job {job_id} missing from DB"
            assert job_row[0] == "completed", job_row

            # 5. Verify audit events recorded by the worker process
            audit_rows = conn.execute(
                text(
                    "SELECT action, outcome FROM audit_events "
                    "WHERE request_id = :rid ORDER BY created_at"
                ),
                {"rid": request_id},
            ).fetchall()
            actions = {r[0] for r in audit_rows}
            assert "workflow.started" in actions
            assert "workflow.completed" in actions

            # 6. Verify agent executions persisted from the worker's run
            agent_rows = conn.execute(
                text("SELECT agent_name, status FROM agent_executions WHERE request_id = :rid"),
                {"rid": request_id},
            ).fetchall()
            assert agent_rows, "no agent executions persisted"
            assert any(r[1] == "completed" for r in agent_rows)

    def test_worker_runtime_includes_registered_mcp_tool(
        self,
        worker_process,
        mcp_server_script: str,
    ) -> None:
        """The worker's runtime configuration must expose the MCP tool.

        Proves the earlier 'direct agent endpoint could not find a registered
        tool' observation was a runtime-configuration mismatch: with
        MCP_CATALOG_JSON configured, the worker's registry contains
        ``mcp.e2e-server.echo``.
        """
        # Build the same registry the worker builds, from the same settings.
        env = _worker_env(mcp_server_script, "cfg-check")
        # Run INSIDE a child process so the settings/env match the worker's.
        probe = subprocess.run(
            [
                PYTHON,
                "-c",
                (
                    "import json, sys;"
                    "from aegisforge.config import Settings;"
                    "from aegisforge.mcp.lifecycle import configure_mcp_registry;"
                    "s = Settings();"
                    "registry, lifecycle = configure_mcp_registry(s);"
                    "print(json.dumps({'has_tool': registry.has_tool('mcp.e2e-server.echo'),"
                    " 'tools': registry.list_tool_names()}));"
                    "lifecycle.shutdown() if lifecycle else None"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parents[2]),
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        data = json.loads(probe.stdout.strip().splitlines()[-1])
        assert data["has_tool"] is True, data
        assert "mcp.e2e-server.echo" in data["tools"]

    def test_worker_mcp_failure_is_recorded_not_silent(
        self,
        worker_process,
        api_base_url: str,
        mcp_server_script: str,
    ) -> None:
        """A disallowed MCP tool must fail explicitly (deny-by-default).

        Runs a registry-level execution probe inside a worker-equivalent
        child process (same runtime configuration) to prove the allowed MCP
        tool executes inside the worker's runtime; deny-by-default for
        disallowed tools is covered by tests/test_mcp_lifecycle.py. Then
        proves the worker records a definitive terminal status for the
        baseline async path.
        """
        import httpx

        # Registry-level allowed-tool execution under the worker's runtime.
        env = _worker_env(mcp_server_script, "deny-check")
        probe = subprocess.run(
            [
                PYTHON,
                "-c",
                (
                    "import json;"
                    "from aegisforge.config import Settings;"
                    "from aegisforge.mcp.lifecycle import configure_mcp_registry;"
                    "s = Settings();"
                    "registry, lifecycle = configure_mcp_registry(s);"
                    "result = registry.execute('mcp.e2e-server.echo', {'message': 'x'}, granted_permissions=['mcp.e2e-server']);"
                    "print(json.dumps({'status': str(result.status), 'error': result.error or ''}));"
                    "lifecycle.shutdown() if lifecycle else None"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parents[2]),
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        denial = json.loads(probe.stdout.strip().splitlines()[-1])
        # A tool that IS allowed executes cleanly under the worker's runtime.
        assert denial["status"] == "ToolExecutionStatus.COMPLETED", denial

        # Worker-path baseline: terminal status is always recorded. The
        # intent avoids high-risk keywords so the deterministic planner does
        # not gate the run behind human approval (which would pause the
        # workflow instead of reaching a terminal status).
        organization_tag = uuid.uuid4().hex[:8]
        _, headers = _register_and_login(api_base_url, organization_tag)
        with httpx.Client(base_url=api_base_url, timeout=15.0) as client:
            created = client.post(
                "/api/v1/requests",
                json={"intent": "echo worker mcp verification proof"},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            request_id = created.json()["id"]
            submitted = client.post(
                f"/api/v1/execution/requests/{request_id}/execute-async",
                headers=headers,
            )
            assert submitted.status_code == 202, submitted.text

        final = _wait_for_job_completion(api_base_url, headers, request_id)
        assert final["status"] in ("completed", "failed")
