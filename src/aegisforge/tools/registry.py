from __future__ import annotations

import logging
from typing import Any

from aegisforge.observability.metrics import record_circuit_probe
from aegisforge.tools.base import BaseTool, ToolDefinition, ToolExecutionResult, TStatus
from aegisforge.tools.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitState,
    ToolCircuitBreaker,
    get_default_circuit_breaker,
)
from aegisforge.tools.health import (
    ToolHealthConfig,
    ToolHealthSnapshot,
    ToolHealthTracker,
    get_default_tool_health_tracker,
)

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for all approved tools.

    Tools are registered at startup.  Agents discover and execute tools
    through this registry — they never access tools directly.

    Phase 6F: every real execution outcome feeds a bounded, in-process tool
    health tracker.  Health is derived ONLY from registered-tool executions:
    permission denials and unknown-tool lookups are policy outcomes, not
    availability evidence, and are never recorded as failures.

    Phase 6G: a bounded circuit breaker wraps execution at this same choke
    point.  Availability failures (timeout/connection/transient) trip the
    circuit; policy outcomes (DENIED, disabled, unknown tool) never do.
    The breaker fast-fails OPEN circuits with a structured FAILED result so
    callers see a normal tool failure shape — retries, recovery, and
    permissions stay owned by the existing layers.  Circuit state can never
    bypass the permission check (which still runs first).
    """

    def __init__(
        self,
        health_config: ToolHealthConfig | None = None,
        health_tracker: ToolHealthTracker | None = None,
        circuit_config: CircuitBreakerConfig | None = None,
        circuit_breaker: ToolCircuitBreaker | None = None,
    ) -> None:
        self._tools: dict[str, BaseTool] = {}
        # Phase 6F: health evidence accumulates process-wide (registries are
        # built per workflow node, but health must persist across them).
        # Explicit tracker/config injection is supported for tests/embedders.
        if health_tracker is not None:
            self._health = health_tracker
        elif health_config is not None:
            self._health = ToolHealthTracker(health_config)
        else:
            self._health = get_default_tool_health_tracker()
        # Phase 6G: circuit state also accumulates process-wide.
        if circuit_breaker is not None:
            self._circuit = circuit_breaker
        elif circuit_config is not None:
            self._circuit = ToolCircuitBreaker(circuit_config)
        else:
            self._circuit = get_default_circuit_breaker()

    def register(self, tool: BaseTool) -> None:
        """Register a tool.  Raises ValueError on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s v%s", tool.name, tool.definition.version)

    def get(self, name: str) -> BaseTool | None:
        """Retrieve a tool by name, or None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """Return definitions for all registered tools."""
        return [t.definition for t in self._tools.values()]

    def list_tool_names(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        granted_permissions: list[str] | None = None,
        context: Any | None = None,
    ) -> ToolExecutionResult:
        """Discover and execute a tool by name.

        Order of checks (security first):

        1. Registry lookup — unknown tools never execute.
        2. Disabled check — disabled tools never execute.
        3. Permission check (via ``tool.execute``) — unsatisfied permissions
           never execute.  These three are POLICY outcomes: they are never
           recorded as health/circuit evidence and can never be bypassed by
           circuit state.
        4. Circuit admission — an OPEN circuit fast-fails with a structured
           FAILED result (CIRCUIT_OPEN).  HALF_OPEN admits a single probe.
        5. Execution + health/circuit evidence recording.

        Circuit state only ever *rejects* executions; it can never cause an
        execution that policy would have denied.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolExecutionResult(
                status=TStatus.FAILED,
                error=f"Tool '{tool_name}' not found in registry",
                tool_name=tool_name,
            )
        # Fail closed: disabled tools must not execute.
        if not getattr(tool.definition, "enabled", True):
            return ToolExecutionResult(
                status=TStatus.DENIED,
                error=f"Tool '{tool_name}' is disabled",
                tool_name=tool_name,
            )

        # Phase 6G: permission check runs BEFORE circuit admission so circuit
        # state can never bypass permissions.  To avoid double-execution we
        # check permissions here and then call the tool's internal execute
        # with granted_permissions=None (permissions already verified).
        if granted_permissions is not None and not tool.has_permissions(
            granted_permissions
        ):
            return ToolExecutionResult(
                status=TStatus.DENIED,
                error=f"Missing required permissions: {tool.required_permissions}",
                tool_name=tool_name,
                execution_id=_new_execution_id(),
            )

        # Circuit admission (after policy checks).
        if not self._circuit.allow(tool_name):
            message, _remaining = self._circuit.fast_fail(tool_name)
            logger.warning(
                "Tool %s fast-failed by OPEN circuit", tool_name
            )
            return ToolExecutionResult(
                status=TStatus.FAILED,
                error=message,
                duration_ms=0,
                tool_name=tool_name,
                execution_id=_new_execution_id(),
            )

        # Execute with timing + Prometheus instrumentation.  Permissions were
        # already verified above; pass None so BaseTool.execute skips its own
        # (duplicated) permission check.
        result = tool.execute(input_data, None, context=context)

        # Phase 6F/6G: record real availability evidence (bounded, process-
        # local).  DENIED (permission) outcomes are policy, not health — never
        # recorded.  A TIMEOUT is a qualifying availability failure for the
        # circuit; FAILED is treated as availability failure only when it is
        # not a policy error surfaced by the tool itself.
        is_probe = self._circuit.get_state(tool_name) == CircuitState.HALF_OPEN
        try:
            if result.status == TStatus.COMPLETED:
                self._health.record_success(tool_name)
                self._circuit.record_success(tool_name)
                if is_probe:
                    _record_probe_metric(True)
            elif result.status == TStatus.TIMEOUT:
                self._health.record_failure(tool_name, timeout=True)
                self._circuit.record_failure(tool_name)
                if is_probe:
                    _record_probe_metric(False)
            elif result.status == TStatus.FAILED:
                self._health.record_failure(tool_name)
                self._circuit.record_failure(tool_name)
                if is_probe:
                    _record_probe_metric(False)
        except Exception:  # noqa: S110 — health/circuit must never break execution
            pass
        logger.info(
            "Tool execution: %s -> %s (%d ms)",
            tool_name,
            result.status,
            result.duration_ms,
        )
        return result

    # ------------------------------------------------------------------
    # Phase 6F: tool health (read-only for planners)
    # ------------------------------------------------------------------

    @property
    def health_tracker(self) -> ToolHealthTracker:
        """Direct tracker access (admin/reset paths only — planners use snapshots)."""
        return self._health

    def get_tool_health(self, tool_name: str) -> ToolHealthSnapshot:
        """Bounded health snapshot for one tool (immutable, planner-safe)."""
        return self._health.get_health(tool_name)

    def list_tool_health(self) -> list[ToolHealthSnapshot]:
        """Bounded health snapshots for all tools with recorded evidence."""
        return self._health.list_health()

    # ------------------------------------------------------------------
    # Phase 6G: circuit breaker (read-only for planners)
    # ------------------------------------------------------------------

    @property
    def circuit_breaker(self) -> ToolCircuitBreaker:
        """Direct breaker access (admin/reset paths only — planners use states)."""
        return self._circuit

    def get_circuit_state(self, tool_name: str) -> str:
        """Bounded circuit state for one tool (closed/open/half_open)."""
        return self._circuit.get_state(tool_name)

    def list_circuit_states(self) -> dict[str, str]:
        """Bounded circuit-state map for tools with recorded evidence."""
        return self._circuit.list_states()

    def update(self, tool: BaseTool) -> None:
        """Register or replace a tool by name."""
        self._tools[tool.name] = tool
        logger.info("Updated tool: %s v%s", tool.name, tool.definition.version)

    def validate_permissions(self, tool_name: str, granted: list[str]) -> bool:
        """Check whether *granted* permissions satisfy the tool's requirements."""
        tool = self._tools.get(tool_name)
        if tool is None:
            return False
        return tool.has_permissions(granted)


def _new_execution_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _record_probe_metric(success: bool) -> None:
    try:
        record_circuit_probe(success)
    except Exception:  # noqa: S110 — observability must never break execution
        pass


# Module-level singleton for convenience.
_default_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Return the default singleton registry, creating it if needed."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry


def reset_tool_registry() -> None:
    """Reset the singleton (useful in tests)."""
    global _default_registry
    _default_registry = None
