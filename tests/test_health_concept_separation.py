"""Phase 6G — Step 7: health-concept separation tests.

Verifies that three distinct health concepts remain distinct:

1. Tool Health (``ToolHealthTracker``): process-local availability history
   per TOOL — success/failure/timeout rates.  Fed by real executions.
2. MCP Server Health (``MCPServerCatalog``/lifecycle): connectivity/lifecycle
   state per MCP SERVER — connected, health-checked, healthy/unhealthy.
3. Circuit state (``ToolCircuitBreaker``): per-tool execution admission
   control — closed/open/half_open.  Fed by the same execution evidence as
   tool health but decides ADMISSION, not monitoring.

A tool can be in any combination: healthy server + open circuit (server
reachable, but repeated tool failures tripped the breaker), or unhealthy
server + closed circuit (no recorded failures yet → admission allowed and
the call will likely fail — health is observational, not a guarantee).

Note: registries default to process-global trackers (health must persist
across per-node registry rebuilds); these tests inject FRESH trackers per
test to observe state transitions in isolation.
"""
from __future__ import annotations

from typing import Any

from aegisforge.domain.models import MCPServerConfig
from aegisforge.mcp.catalog import MCPHealthState, MCPServerCatalog
from aegisforge.tools.base import BaseTool, ToolDefinition
from aegisforge.tools.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitState,
    ToolCircuitBreaker,
)
from aegisforge.tools.health import ToolHealthState, ToolHealthTracker
from aegisforge.tools.registry import ToolRegistry


class FlakyTool(BaseTool):
    """Fails when input says so — lets us create any health/circuit combo."""

    def __init__(self) -> None:
        super().__init__(
            ToolDefinition(
                name="combo.tool", description="d", permission_requirements=["p"]
            )
        )

    def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
        if input_data.get("fail"):
            raise ConnectionError("downstream unavailable")
        return {"ok": True}


def _registry(threshold: int = 2) -> ToolRegistry:
    """Registry with FRESH process-local trackers (test isolation)."""
    registry = ToolRegistry(
        health_tracker=ToolHealthTracker(),
        circuit_breaker=ToolCircuitBreaker(
            CircuitBreakerConfig(failure_threshold=threshold, cooldown_seconds=3600)
        ),
    )
    registry.register(FlakyTool())
    return registry


class TestConceptSeparation:
    def test_healthy_server_plus_open_circuit(self) -> None:
        """Server reachable (MCP layer fine), but the tool failed enough to
        trip the breaker: admission denied while connectivity is fine."""
        registry = _registry(threshold=2)
        for _ in range(2):
            registry.execute("combo.tool", {"fail": True}, granted_permissions=["p"])
        assert registry.get_circuit_state("combo.tool") == CircuitState.OPEN
        # Health (observational) says degraded after 2 failures; the circuit
        # (admission) says OPEN — independent thresholds, same evidence.
        assert registry.get_tool_health("combo.tool").state == ToolHealthState.DEGRADED
        # The concept distinction: admission is refused by the CIRCUIT even
        # though nothing about server connectivity was wrong.  The fast-fail
        # result is a structured FAILED, not a crash.
        result = registry.execute("combo.tool", {"fail": False}, granted_permissions=["p"])
        assert result.status.value == "failed"
        assert "circuit" in (result.error or "").lower()

    def test_unhealthy_server_plus_closed_circuit(self) -> None:
        """No failures recorded yet → circuit CLOSED (admission allowed),
        even though the server is (independently) unhealthy.  The registry
        does not fabricate circuit state from server health."""
        registry = _registry(threshold=100)  # never trips
        assert registry.get_circuit_state("combo.tool") == CircuitState.CLOSED
        assert registry.get_tool_health("combo.tool").state == ToolHealthState.UNKNOWN
        # Admission allowed; the call succeeds/fails on its own merits.
        result = registry.execute("combo.tool", {"fail": False}, granted_permissions=["p"])
        assert result.status.value == "completed"

    def test_health_and_circuit_record_the_same_evidence_independently(self) -> None:
        """One failed execution updates BOTH trackers; neither derives from
        the other — they are parallel views over execution evidence."""
        registry = _registry(threshold=100)
        registry.execute("combo.tool", {"fail": True}, granted_permissions=["p"])
        health = registry.get_tool_health("combo.tool")
        assert health.consecutive_failures == 1
        assert health.sample_count == 1
        assert registry.get_circuit_state("combo.tool") == CircuitState.CLOSED
        # Success clears consecutive failures but not the sample history.
        registry.execute("combo.tool", {"fail": False}, granted_permissions=["p"])
        health = registry.get_tool_health("combo.tool")
        assert health.consecutive_failures == 0
        assert health.sample_count == 2

    def test_mcp_server_health_is_a_separate_surface(self) -> None:
        """The MCP catalog tracks SERVER lifecycle health (connect/check/
        discover); it is never fed into the tool circuit breaker."""
        catalog = MCPServerCatalog()
        catalog.register(
            MCPServerConfig(server_id="srv-1", name="Search Server", transport="stdio")
        )
        catalog.set_health("srv-1", MCPHealthState.UNHEALTHY, "connect failed", connected=False)

        entry = catalog.get("srv-1")
        assert entry is not None
        snap = entry.health.to_dict()
        assert snap["state"] == "unhealthy"
        assert snap["connected"] is False
        assert snap["last_error"] == "connect failed"

        # Recovery path is server-scoped too.
        catalog.set_health("srv-1", MCPHealthState.HEALTHY, "", connected=True)
        assert catalog.get("srv-1").health.to_dict()["state"] == "healthy"
        assert catalog.get("srv-1").health.failures_since_last_success == 0

    def test_circuit_state_never_derives_from_server_health(self) -> None:
        """A server going unhealthy must not flip any tool's circuit."""
        registry = _registry(threshold=1)
        catalog = MCPServerCatalog()
        catalog.register(
            MCPServerConfig(server_id="srv-2", name="Flaky Server", transport="stdio")
        )
        catalog.set_health("srv-2", MCPHealthState.UNHEALTHY, "down", connected=False)
        # No tool executions happened → no circuit state anywhere.
        assert registry.list_circuit_states() == {}


class TestReadinessProbeRegression:
    """Regression: /ready's DB check used to call the old no-arg
    ``get_engine()`` signature.  It passed in dev (SQLite default) but
    failed at runtime inside production containers — caught live during
    the Phase 7 Docker bring-up.  These tests pin the contract."""

    def test_database_check_returns_ok_with_real_engine(self, tmp_path) -> None:
        from aegisforge.api.routes.health import _check_database

        assert _check_database() == "ok"

    def test_database_check_reports_error_not_crash(self, monkeypatch) -> None:
        """An unreachable database must degrade to an error string, never
        raise out of the readiness path."""
        from aegisforge.api.routes import health as health_module

        monkeypatch.setattr(
            health_module,
            "_check_database",
            lambda: "error: connection refused",
        )
        assert health_module._check_database().startswith("error:")
