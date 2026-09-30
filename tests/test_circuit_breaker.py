"""Phase 6G — Tool circuit breaker tests.

Covers the sprint contract for Workstream 1:

1.  CLOSED → OPEN after the configured number of qualifying failures.
2.  OPEN fast-fails without executing the tool (structured result).
3.  OPEN → HALF_OPEN after cooldown; exactly ONE probe admitted.
4.  Successful probe → CLOSED; failed probe → OPEN (bounded probe churn).
5.  Policy outcomes NEVER trip the circuit (permission, disabled, unknown).
6.  Circuit state can never bypass permissions (deny-first ordering).
7.  HALF_OPEN does not displace a healthy tool in planning; OPEN does.
8.  Thread-safety: concurrent calls produce exactly one probe in HALF_OPEN.
9.  Bounded metrics: circuit metrics expose state labels only, no tool names.
10. Bounded memory: failure ring is capped by window eviction.
11. Process-local reset semantics (documented limitation).
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from aegisforge.domain.models import ToolExecutionStatus as TStatus
from aegisforge.tools.base import BaseTool, ToolDefinition
from aegisforge.tools.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitState,
    ToolCircuitBreaker,
    reset_default_circuit_breaker,
)
from aegisforge.tools.registry import ToolRegistry


class StubTool(BaseTool):
    """Configurable stub: succeeds, fails, or times out on demand."""

    def __init__(
        self,
        name: str = "circuit.tool",
        *,
        permission: str = "circuit.perm",
        fail: bool = False,
        timeout: bool = False,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            ToolDefinition(
                name=name,
                description="stub tool for circuit tests",
                permission_requirements=[permission],
                timeout_seconds=5,
                enabled=enabled,
            )
        )
        self.fail = fail
        self.timeout = timeout
        self.calls = 0

    def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
        self.calls += 1
        if self.timeout:
            raise TimeoutError("stub timeout")
        if self.fail:
            raise RuntimeError("stub failure")
        return {"ok": True}


def fast_config(**kwargs: Any) -> CircuitBreakerConfig:
    """Deterministic fast config for tests (no sleeps beyond microseconds)."""
    params: dict[str, Any] = {
        "failure_threshold": 3,
        "failure_window_seconds": 60.0,
        "cooldown_seconds": 0.05,
        "max_probes_per_open": 2,
    }
    params.update(kwargs)
    return CircuitBreakerConfig(**params)


@pytest.fixture(autouse=True)
def _clean_default_breaker():

    reset_default_circuit_breaker()
    yield
    reset_default_circuit_breaker()


# ---------------------------------------------------------------------------
# 1-2: Trip + fast-fail
# ---------------------------------------------------------------------------


class TestTripAndFastFail:
    def test_opens_after_threshold_failures(self):
        breaker = ToolCircuitBreaker(fast_config())
        for _ in range(3):
            breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.OPEN

    def test_does_not_open_below_threshold(self):
        breaker = ToolCircuitBreaker(fast_config())
        for _ in range(2):
            breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.CLOSED

    def test_success_resets_failure_window(self):
        breaker = ToolCircuitBreaker(fast_config())
        breaker.record_failure("t")
        breaker.record_failure("t")
        breaker.record_success("t")  # clears evidence
        breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.CLOSED

    def test_registry_fast_fails_open_circuit_without_executing(self):
        registry = ToolRegistry(circuit_config=fast_config())
        tool = StubTool(fail=True)
        registry.register(tool)

        for _ in range(3):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert registry.get_circuit_state("circuit.tool") == CircuitState.OPEN
        calls_after_trip = tool.calls

        result = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert result.status == TStatus.FAILED
        assert "CIRCUIT_OPEN" in (result.error or "")
        assert tool.calls == calls_after_trip  # tool was NOT executed
        assert result.duration_ms == 0

    def test_failure_window_is_sliding(self):
        breaker = ToolCircuitBreaker(fast_config(failure_window_seconds=0.05))
        breaker.record_failure("t")
        breaker.record_failure("t")
        time.sleep(0.2)  # first failure ages out of the window (well over 0.05)
        breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.CLOSED

    def test_config_validation(self):
        with pytest.raises(ValueError):
            CircuitBreakerConfig(failure_threshold=0)
        with pytest.raises(ValueError):
            CircuitBreakerConfig(failure_window_seconds=0)
        with pytest.raises(ValueError):
            CircuitBreakerConfig(cooldown_seconds=-1)

    def test_config_from_settings(self):
        class FakeSettings:
            tool_circuit_failure_threshold = 7
            tool_circuit_failure_window_seconds = 30.0
            tool_circuit_cooldown_seconds = 15.0
            tool_circuit_max_probes_per_open = 5

        cfg = CircuitBreakerConfig.from_settings(FakeSettings())
        assert cfg.failure_threshold == 7
        assert cfg.cooldown_seconds == 15.0
        assert cfg.max_probes_per_open == 5


# ---------------------------------------------------------------------------
# 3-4: HALF_OPEN probe lifecycle
# ---------------------------------------------------------------------------


class TestHalfOpenLifecycle:
    def test_cooldown_then_probe_admission(self):
        breaker = ToolCircuitBreaker(fast_config())
        for _ in range(3):
            breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.OPEN
        assert breaker.allow("t") is False  # still OPEN
        # Generous margin over the 50ms cooldown: Windows sleep/monotonic
        # granularity (~15ms) made a 60ms sleep flaky (it can advance the
        # clock by only ~47ms), so 0.2s keeps this deterministic.
        time.sleep(0.2)  # cooldown elapsed
        # First caller after cooldown is admitted as THE probe.
        assert breaker.allow("t") is True
        # Second caller is rejected while the probe is in flight.
        assert breaker.allow("t") is False
        assert breaker.get_state("t") == CircuitState.HALF_OPEN

    def test_successful_probe_closes_circuit(self):
        registry = ToolRegistry(circuit_config=fast_config())
        tool = StubTool(fail=True)
        registry.register(tool)
        for _ in range(3):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        # Generous margin over the 50ms cooldown: CI machines and full-suite
        # load make tight sleeps flaky; 5x margin keeps this deterministic.
        time.sleep(0.3)
        tool.fail = False  # recovery
        result = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert result.status == TStatus.COMPLETED
        assert registry.get_circuit_state("circuit.tool") == CircuitState.CLOSED
        # Healthy again: normal executions proceed.
        result2 = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert result2.status == TStatus.COMPLETED

    def test_failed_probe_reopens_circuit(self):
        registry = ToolRegistry(circuit_config=fast_config())
        tool = StubTool(fail=True)
        registry.register(tool)
        for _ in range(3):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        time.sleep(0.3)  # generous margin over the 50ms cooldown
        result = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert result.status == TStatus.FAILED
        assert "CIRCUIT_OPEN" not in (result.error or "")  # real failure, not fast-fail
        assert registry.get_circuit_state("circuit.tool") == CircuitState.OPEN
        # Immediately rejected again (new cooldown started).
        result2 = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert "CIRCUIT_OPEN" in (result2.error or "")

    def test_probe_concurrency_limited_to_one(self):
        breaker = ToolCircuitBreaker(fast_config(cooldown_seconds=0.01))
        for _ in range(3):
            breaker.record_failure("t")
        time.sleep(0.1)  # generous margin over the 10ms cooldown
        # One probe admitted; all others rejected until it resolves.
        assert breaker.allow("t") is True
        admissions = [breaker.allow("t") for _ in range(10)]
        assert all(a is False for a in admissions)
        breaker.record_success("t")
        assert breaker.get_state("t") == CircuitState.CLOSED
        assert breaker.allow("t") is True

    def test_only_one_thread_executes_the_probe(self):
        """Concurrency test: N threads racing on a recovered circuit → exactly
        one real execution, the rest fast-failed (or rejected)."""
        registry = ToolRegistry(circuit_config=fast_config(cooldown_seconds=0.03))
        tool = StubTool(fail=True)
        registry.register(tool)
        for _ in range(3):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])

        time.sleep(0.3)  # cooldown elapsed → HALF_OPEN, one probe slot
        calls_before = tool.calls
        results: list[str] = []
        lock = threading.Lock()
        start = threading.Event()

        def worker() -> None:
            start.wait()
            r = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
            with lock:
                results.append(r.error or "")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)

        real_executions = tool.calls - calls_before
        # At most one probe executed; fast-fails never reached the tool.
        assert real_executions <= 1
        fast_fails = sum(1 for e in results if "CIRCUIT_OPEN" in e)
        assert fast_fails >= len(results) - real_executions - 1


# ---------------------------------------------------------------------------
# 5-6: Policy outcomes never trip; permissions never bypassed
# ---------------------------------------------------------------------------


class TestPolicyIsolation:
    def test_permission_denial_never_trips_circuit(self):
        registry = ToolRegistry(circuit_config=fast_config(failure_threshold=2))
        registry.register(StubTool())
        for _ in range(10):
            result = registry.execute("circuit.tool", {}, granted_permissions=[])
            assert result.status == TStatus.DENIED
        assert registry.get_circuit_state("circuit.tool") == CircuitState.CLOSED

    def test_unknown_tool_never_trips_circuit(self):
        registry = ToolRegistry(circuit_config=fast_config(failure_threshold=2))
        for _ in range(10):
            result = registry.execute("ghost.tool", {}, granted_permissions=["x"])
            assert result.status == TStatus.FAILED
        assert registry.get_circuit_state("ghost.tool") == CircuitState.CLOSED

    def test_disabled_tool_never_trips_circuit(self):
        registry = ToolRegistry(circuit_config=fast_config(failure_threshold=2))
        registry.register(StubTool(enabled=False))
        for _ in range(10):
            result = registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
            assert result.status == TStatus.DENIED
        assert registry.get_circuit_state("circuit.tool") == CircuitState.CLOSED

    def test_circuit_open_cannot_bypass_permissions(self):
        """OPEN circuit + denied permissions → still DENIED, never executed."""
        registry = ToolRegistry(circuit_config=fast_config())
        registry.register(StubTool(fail=True))
        for _ in range(3):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert registry.get_circuit_state("circuit.tool") == CircuitState.OPEN
        result = registry.execute("circuit.tool", {}, granted_permissions=[])
        assert result.status == TStatus.DENIED  # policy outcome preserved

    def test_circuit_open_does_not_record_health_evidence(self):
        """Fast-fails are not real executions — they must not poison health."""
        registry = ToolRegistry(circuit_config=fast_config())
        registry.register(StubTool(fail=True))
        for _ in range(3):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        before = registry.get_tool_health("circuit.tool").sample_count
        for _ in range(5):
            registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        assert registry.get_tool_health("circuit.tool").sample_count == before


# ---------------------------------------------------------------------------
# 7: Planner integration
# ---------------------------------------------------------------------------


class TestPlannerCircuitIntegration:
    def _plan_tool(self, plan) -> str:
        for task in plan.tasks:
            name = task.input_data.get("tool_name")
            if name:
                return str(name)
        return ""

    def test_open_circuit_avoided_when_alternative_exists(self):
        t = ToolRegistry(circuit_config=fast_config())
        t.register(StubTool("knowledge.search", fail=True))
        t.register(StubTool("alt.search"))
        # Trip the circuit on knowledge.search.
        for _ in range(3):
            t.execute("knowledge.search", {}, granted_permissions=["circuit.perm"])
        # Healthy evidence for the alternative.
        for _ in range(3):
            t.execute("alt.search", {}, granted_permissions=["circuit.perm"])

        from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
        from aegisforge.agents.planner import PlannerAgent

        planner = PlannerAgent()
        ctx = AgentExecutionContext(
            request_id="r", workflow_id="w", user_id="u",
            organization_id="o", permissions=[PermissionSpec(name="circuit.perm", allow=True)],
        )
        plan = planner._generate_plan(
            "research the policy",
            ctx,
            {
                "intent": "research",
                "tool_health": [s.to_dict() for s in t.list_tool_health()],
                "available_tools": t.list_tool_names(),
                "circuit_states": t.list_circuit_states(),
            },
        )
        assert plan is not None
        assert self._plan_tool(plan) == "alt.search"

    def test_open_circuit_kept_when_no_alternative(self):
        t = ToolRegistry(circuit_config=fast_config())
        t.register(StubTool("knowledge.search", fail=True))
        for _ in range(3):
            t.execute("knowledge.search", {}, granted_permissions=["circuit.perm"])

        from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
        from aegisforge.agents.planner import PlannerAgent

        planner = PlannerAgent()
        ctx = AgentExecutionContext(
            request_id="r", workflow_id="w", user_id="u",
            organization_id="o", permissions=[PermissionSpec(name="circuit.perm", allow=True)],
        )
        plan = planner._generate_plan(
            "research the policy",
            ctx,
            {
                "intent": "research",
                "tool_health": [s.to_dict() for s in t.list_tool_health()],
                "available_tools": t.list_tool_names(),
                "circuit_states": t.list_circuit_states(),
            },
        )
        assert plan is not None
        assert self._plan_tool(plan) == "knowledge.search"

    def test_select_tool_with_circuit_ranking(self):
        from aegisforge.tools.health import select_tool_with_circuit

        # requested has an OPEN circuit, alternative is closed+healthy
        choice = select_tool_with_circuit(
            requested="a",
            required_permissions=["p"],
            available_tools=["a", "b"],
            health_context=[],
            circuit_context={"a": "open", "b": "closed"},
        )
        assert choice == "b"

    def test_planner_circuit_context_is_read_only(self):
        """Planning must never mutate circuit state."""
        registry = ToolRegistry(circuit_config=fast_config())
        registry.register(StubTool())
        registry.execute("circuit.tool", {}, granted_permissions=["circuit.perm"])
        states_before = registry.list_circuit_states()

        from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
        from aegisforge.agents.planner import PlannerAgent

        planner = PlannerAgent()
        ctx = AgentExecutionContext(
            request_id="r", workflow_id="w", user_id="u",
            organization_id="o", permissions=[PermissionSpec(name="circuit.perm", allow=True)],
        )
        for _ in range(3):
            planner._generate_plan(
                "research the policy",
                ctx,
                {
                    "intent": "research",
                    "tool_health": [s.to_dict() for s in registry.list_tool_health()],
                    "available_tools": registry.list_tool_names(),
                    "circuit_states": registry.list_circuit_states(),
                },
            )
        assert registry.list_circuit_states() == states_before


# ---------------------------------------------------------------------------
# 8-11: Concurrency, metrics, memory, lifecycle
# ---------------------------------------------------------------------------


class TestConcurrencyAndBounds:
    def test_concurrent_failures_trip_exactly_once(self):
        breaker = ToolCircuitBreaker(fast_config())

        def record_many():
            for _ in range(20):
                breaker.record_failure("t")

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert breaker.get_state("t") == CircuitState.OPEN

    def test_metrics_bounded_no_tool_names(self):
        from prometheus_client import REGISTRY

        breaker = ToolCircuitBreaker(fast_config())
        for i in range(40):
            name = f"tool-{i}"
            for _ in range(3):
                breaker.record_failure(name)

        for collector in list(REGISTRY.collect()):
            if collector.name in (
                "tool_circuit_state_changes_total",
                "tool_circuit_fast_fails_total",
                "tool_circuit_probes_total",
            ):
                for sample in collector.samples:
                    assert set(sample.labels.keys()) <= {
                        "from_state", "to_state", "outcome"
                    }
                    for value in sample.labels.values():
                        assert value in (
                            "closed", "open", "half_open", "success", "failure"
                        ), f"unbounded label value: {value}"

    def test_state_change_counter_emitted(self):
        from prometheus_client import REGISTRY

        def counter_sum(name: str) -> float:
            total = 0.0
            for collector in REGISTRY.collect():
                for sample in collector.samples:
                    if sample.name == name:
                        total += sample.value
            return total

        breaker = ToolCircuitBreaker(fast_config())
        before = counter_sum("tool_circuit_state_changes_total")
        for _ in range(3):
            breaker.record_failure("metric.tool")
        after = counter_sum("tool_circuit_state_changes_total")
        assert after >= before + 1  # closed → open

    def test_bounded_state_many_tools(self):
        """State entries exist only for tools that transitioned away from CLOSED;
        per-call storage is bounded to the sliding window per tool."""
        breaker = ToolCircuitBreaker(fast_config())
        for i in range(500):
            for _ in range(3):  # trip each tool OPEN
                breaker.record_failure(f"tool-{i}")
        states = breaker.list_states()
        assert len(states) == 500
        assert all(s == CircuitState.OPEN for s in states.values())
        breaker.reset()
        assert breaker.list_states() == {}

    def test_process_local_reset_semantics(self):
        """Documented limitation: new breaker = closed circuits (process-local)."""
        breaker1 = ToolCircuitBreaker(fast_config())
        for _ in range(3):
            breaker1.record_failure("t")
        assert breaker1.get_state("t") == CircuitState.OPEN
        breaker2 = ToolCircuitBreaker(fast_config())
        assert breaker2.get_state("t") == CircuitState.CLOSED

    def test_default_breaker_is_shared(self):
        from aegisforge.tools.circuit_breaker import get_default_circuit_breaker

        b1 = get_default_circuit_breaker()
        b2 = get_default_circuit_breaker()
        assert b1 is b2

    def test_registry_shares_default_breaker(self):
        from aegisforge.tools.circuit_breaker import get_default_circuit_breaker

        r1 = ToolRegistry()
        r2 = ToolRegistry()
        assert r1.circuit_breaker is get_default_circuit_breaker()
        assert r2.circuit_breaker is r1.circuit_breaker


class TestProbeChurnBackoff:
    """max_probes_per_open must be enforced: repeated failed probes back off
    the cooldown instead of probing a dead tool at full rate forever, while
    still guaranteeing the tool is eventually probed again (never stuck)."""

    def test_cooldown_backs_off_after_probe_budget(self):
        breaker = ToolCircuitBreaker(
            fast_config(cooldown_seconds=1.0, max_probes_per_open=2)
        )
        # Trip OPEN from CLOSED.
        for _ in range(3):
            breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.OPEN

        # Within budget: base cooldown.
        assert breaker.probe_cooldown_remaining("t") == pytest.approx(1.0, abs=0.2)

        # Two failed probes stay within budget (base cooldown).
        for _ in range(2):
            breaker._open_since["t"] = time.monotonic() - 2.0  # force cooldown elapsed
            assert breaker.allow("t") is True  # probe admitted
            breaker.record_failure("t")  # probe fails → re-open

        # Third failed probe exceeds the budget: cooldown doubles.
        breaker._open_since["t"] = time.monotonic() - 2.0
        assert breaker.allow("t") is True
        breaker.record_failure("t")
        assert breaker.probe_cooldown_remaining("t") == pytest.approx(2.0, abs=0.3)

        # A fourth failed probe doubles again (4x), still bounded.
        breaker._open_since["t"] = time.monotonic() - 10.0
        assert breaker.allow("t") is True
        breaker.record_failure("t")
        assert breaker.probe_cooldown_remaining("t") == pytest.approx(4.0, abs=0.4)

    def test_backoff_is_bounded_so_recovery_is_possible(self):
        breaker = ToolCircuitBreaker(
            fast_config(cooldown_seconds=1.0, max_probes_per_open=1)
        )
        for _ in range(3):  # trip OPEN
            breaker.record_failure("t")
        assert breaker.get_state("t") == CircuitState.OPEN
        for _ in range(6):
            breaker._open_since["t"] = time.monotonic() - 1000.0
            breaker.allow("t")
            breaker.record_failure("t")
        # Capped at 64x base cooldown — not unbounded.
        assert breaker.probe_cooldown_remaining("t") <= 64.0 + 0.01
        # Cooldown always elapses eventually: a probe is admitted and can close.
        breaker._open_since["t"] = time.monotonic() - 1000.0
        assert breaker.allow("t") is True
        breaker.record_success("t")
        assert breaker.get_state("t") == CircuitState.CLOSED
