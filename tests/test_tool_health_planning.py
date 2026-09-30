"""Phase 6F — Tool health-aware planning tests.

Covers:
1-6.   Health classification from bounded evidence (healthy/degraded/
       unavailable/transient recovery/repeated failure/successful recovery).
7-8.   Deterministic planner prefers healthy / avoids unavailable tools.
9.     LLM planner receives a bounded health context block.
10.    Planner cannot bypass permissions (denials never recorded as health).
11.    Tenant isolation (snapshots carry no tenant data; planner is read-only).
12.    MCP server health vs individual tool health remain distinct.
13.    Metric emission on state transitions.
14.    No high-cardinality metric labels.
15.    Existing registry/retry/recovery semantics remain intact.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.agents.llm_planner import LLMPlannerAgent
from aegisforge.agents.planner import DEFAULT_RESEARCH_TOOL, PlannerAgent
from aegisforge.domain.models import ToolExecutionStatus as TStatus
from aegisforge.mcp.catalog import MCPHealthState, MCPServerCatalog
from aegisforge.mcp.lifecycle import MCPLifecycleManager
from aegisforge.tools.base import BaseTool, ToolDefinition
from aegisforge.tools.health import (
    ToolHealthConfig,
    ToolHealthSnapshot,
    ToolHealthState,
    ToolHealthTracker,
    build_planner_health_context,
    reset_default_tool_health_tracker,
    select_preferred_tool,
)
from aegisforge.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures and stubs
# ---------------------------------------------------------------------------


class StubTool(BaseTool):
    """Configurable stub: succeeds, fails, or times out on demand."""

    def __init__(
        self,
        name: str = "knowledge.search",
        *,
        permission: str = "knowledge.search",
        fail: bool = False,
        timeout: bool = False,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            ToolDefinition(
                name=name,
                description="stub tool for tests",
                permission_requirements=[permission],
                timeout_seconds=5,
                enabled=enabled,
            )
        )
        self.fail = fail
        self.timeout = timeout

    def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
        if self.timeout:
            raise TimeoutError("stub timeout")
        if self.fail:
            raise RuntimeError("stub failure")
        return {"ok": True}


def make_context(request_id: str = "req-1") -> AgentExecutionContext:
    return AgentExecutionContext(
        request_id=request_id,
        workflow_id="wf-1",
        user_id="user-a",
        organization_id="org-a",
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )


@pytest.fixture(autouse=True)
def _clean_default_tracker():
    """Isolate the process-wide tracker between tests."""
    reset_default_tool_health_tracker()
    yield
    reset_default_tool_health_tracker()


# ---------------------------------------------------------------------------
# 1-6: Health classification
# ---------------------------------------------------------------------------


class TestHealthClassification:
    def test_healthy_after_successes(self):
        t = ToolHealthTracker()
        for _ in range(3):
            t.record_success("tool-a")
        snap = t.get_health("tool-a")
        assert snap.state == ToolHealthState.HEALTHY
        assert snap.recent_failure_rate == 0.0

    def test_degraded_after_repeated_failures(self):
        t = ToolHealthTracker()
        for _ in range(2):
            t.record_failure("tool-a")
        assert t.get_health("tool-a").state == ToolHealthState.DEGRADED

    def test_unavailable_after_sustained_failures(self):
        t = ToolHealthTracker()
        for _ in range(4):
            t.record_failure("tool-a")
        snap = t.get_health("tool-a")
        assert snap.state == ToolHealthState.UNAVAILABLE
        assert snap.consecutive_failures == 4

    def test_unavailable_by_failure_rate(self):
        t = ToolHealthTracker(ToolHealthConfig(min_samples=3))
        for outcome in ("success", "failure", "failure", "failure"):
            if outcome == "success":
                t.record_success("tool-a")
            else:
                t.record_failure("tool-a")
        # rate 3/4 = 0.75 >= unavailable threshold
        assert t.get_health("tool-a").state == ToolHealthState.UNAVAILABLE

    def test_single_transient_failure_does_not_degrade(self):
        t = ToolHealthTracker()
        t.record_success("tool-a")
        t.record_failure("tool-a")
        t.record_success("tool-a")
        snap = t.get_health("tool-a")
        # 1/3 failure rate is below the degraded threshold; not sticky.
        assert snap.state == ToolHealthState.HEALTHY
        assert snap.recent_failure_rate == pytest.approx(1 / 3, abs=1e-3)

    def test_unknown_without_evidence(self):
        t = ToolHealthTracker()
        assert t.get_health("never-run").state == ToolHealthState.UNKNOWN

    def test_successful_recovery_from_unavailable(self):
        t = ToolHealthTracker()
        for _ in range(4):
            t.record_failure("tool-a")
        assert t.get_health("tool-a").state == ToolHealthState.UNAVAILABLE
        t.record_success("tool-a")
        # sticky: one success is not enough
        assert t.get_health("tool-a").state == ToolHealthState.UNAVAILABLE
        t.record_success("tool-a")
        # graduated recovery: leaves unavailable into degraded (window rate
        # is still high), then continued successes return it to healthy.
        assert t.get_health("tool-a").state == ToolHealthState.DEGRADED
        for _ in range(20):
            t.record_success("tool-a")
            if t.get_health("tool-a").state == ToolHealthState.HEALTHY:
                break
        assert t.get_health("tool-a").state == ToolHealthState.HEALTHY

    def test_timeout_counts_as_failure_and_tracks_timeout_rate(self):
        t = ToolHealthTracker()
        t.record_failure("tool-a", timeout=True)
        t.record_failure("tool-a", timeout=True)
        snap = t.get_health("tool-a")
        assert snap.state == ToolHealthState.DEGRADED
        assert snap.recent_timeout_rate == pytest.approx(1.0)
        assert snap.recent_failure_rate == pytest.approx(1.0)

    def test_bounded_window_drops_old_evidence(self):
        t = ToolHealthTracker(ToolHealthConfig(window_size=5))
        for _ in range(10):
            t.record_failure("tool-a")
        for _ in range(5):
            t.record_success("tool-a")
        snap = t.get_health("tool-a")
        assert snap.sample_count == 5
        assert snap.state == ToolHealthState.HEALTHY

    def test_config_validation(self):
        with pytest.raises(ValueError):
            ToolHealthConfig(window_size=0)
        with pytest.raises(ValueError):
            ToolHealthConfig(degraded_failure_rate=0.9, unavailable_failure_rate=0.5)
        with pytest.raises(ValueError):
            ToolHealthConfig(unavailable_consecutive_failures=1, degraded_consecutive_failures=2)

    def test_config_from_settings(self):
        class FakeSettings:
            tool_health_window_size = 10
            tool_health_min_samples = 2
            tool_health_degraded_failure_rate = 0.5
            tool_health_unavailable_failure_rate = 0.9
            tool_health_degraded_consecutive_failures = 1
            tool_health_unavailable_consecutive_failures = 3
            tool_health_recovery_samples = 1

        cfg = ToolHealthConfig.from_settings(FakeSettings())
        assert cfg.window_size == 10
        assert cfg.unavailable_failure_rate == 0.9

    def test_default_tracker_is_shared(self):
        from aegisforge.tools.health import get_default_tool_health_tracker

        t1 = get_default_tool_health_tracker()
        t2 = get_default_tool_health_tracker()
        assert t1 is t2
        # ToolRegistry shares it too (health accumulates across registries).
        r1 = ToolRegistry()
        r1.register(StubTool("shared-tool"))
        r1.execute("shared-tool", ["knowledge.search"])
        r2 = ToolRegistry()
        assert r2.get_tool_health("shared-tool").sample_count == 1


# ---------------------------------------------------------------------------
# Health-aware selection
# ---------------------------------------------------------------------------


class TestHealthAwareSelection:
    def _snaps(self, t: ToolHealthTracker) -> list[ToolHealthSnapshot]:
        return t.list_health()

    def test_prefers_healthy_alternative_over_degraded(self):
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")
        for _ in range(3):
            t.record_success("alt.search")  # min_samples evidence → healthy
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "alt.search"],
            health_context=self._snaps(t),
        )
        assert choice == "alt.search"

    def test_unknown_single_success_alternative_is_not_preferred(self):
        """One success is not enough evidence to displace the requested tool."""
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_success("alt.search")  # n=1 < min_samples → unknown
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "alt.search"],
            health_context=self._snaps(t),
        )
        assert choice == "knowledge.search"

    def test_avoids_unavailable_when_alternative_exists(self):
        t = ToolHealthTracker()
        for _ in range(4):
            t.record_failure("knowledge.search")
        for _ in range(3):
            t.record_success("alt.search")
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "alt.search"],
            health_context=self._snaps(t),
        )
        assert choice == "alt.search"

    def test_keeps_requested_when_no_strictly_better_alternative(self):
        t = ToolHealthTracker()
        for _ in range(4):
            t.record_failure("knowledge.search")
        for _ in range(4):
            t.record_failure("alt.search")  # equally unavailable
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "alt.search"],
            health_context=self._snaps(t),
        )
        assert choice == "knowledge.search"

    def test_unknown_alternative_is_not_preferred(self):
        t = ToolHealthTracker()
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")
        # alt has NO evidence at all — absence of evidence is not health
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "unevidenced.tool"],
            health_context=self._snaps(t),
        )
        assert choice == "knowledge.search"

    def test_permission_checker_is_honored(self):
        t = ToolHealthTracker()
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_success("alt.search")
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "alt.search"],
            health_context=self._snaps(t),
            permission_checker=lambda name, perms: name != "alt.search",
        )
        assert choice == "knowledge.search"

    def test_healthy_requested_tool_is_kept(self):
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_success("knowledge.search")
        t.record_success("alt.search")
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search", "alt.search"],
            health_context=self._snaps(t),
        )
        assert choice == "knowledge.search"

    def test_no_evidence_keeps_requested(self):
        choice = select_preferred_tool(
            requested="knowledge.search",
            required_permissions=["knowledge.search"],
            available_tools=["knowledge.search"],
            health_context=[],
        )
        assert choice == "knowledge.search"


# ---------------------------------------------------------------------------
# 7-8: Deterministic planner integration
# ---------------------------------------------------------------------------


class TestDeterministicPlanner:
    def _planner_input(
        self, health: list[ToolHealthSnapshot], available: list[str]
    ) -> dict[str, Any]:
        return {
            "intent": "research the policy",
            "tool_health": [s.to_dict() for s in health],
            "available_tools": available,
        }

    def _research_tool(self, plan) -> str:
        for task in plan.tasks:
            name = task.input_data.get("tool_name")
            if name:
                return str(name)
        return ""

    def test_prefers_healthy_alternative(self):
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")
        for _ in range(3):
            t.record_success("alt.search")

        planner = PlannerAgent()
        plan = planner._generate_plan("research the policy", make_context(), self._planner_input(t.list_health(), ["knowledge.search", "alt.search"]))
        assert plan is not None
        assert self._research_tool(plan) == "alt.search"

    def test_avoids_unavailable_tool(self):
        t = ToolHealthTracker()
        for _ in range(4):
            t.record_failure("knowledge.search")
        t.record_success("alt.search")
        t.record_success("alt.search")

        planner = PlannerAgent()
        plan = planner._generate_plan("research the policy", make_context(), self._planner_input(t.list_health(), ["knowledge.search", "alt.search"]))
        assert plan is not None
        assert self._research_tool(plan) == "alt.search"

    def test_keeps_default_when_no_evidence(self):
        planner = PlannerAgent()
        plan = planner._generate_plan("research the policy", make_context(), {"intent": "research"})
        assert plan is not None
        assert self._research_tool(plan) == DEFAULT_RESEARCH_TOOL

    def test_keeps_default_when_no_better_alternative(self):
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")
        # alt is unhealthier — planner must keep the default
        t.record_failure("alt.search")

        planner = PlannerAgent()
        plan = planner._generate_plan(
            "research the policy",
            make_context(),
            self._planner_input(t.list_health(), ["knowledge.search", "alt.search"]),
        )
        assert plan is not None
        assert self._research_tool(plan) == DEFAULT_RESEARCH_TOOL

    def test_multi_agent_plan_uses_selected_tool(self):
        t = ToolHealthTracker()
        for _ in range(4):
            t.record_failure("knowledge.search")
        t.record_success("alt.search")
        t.record_success("alt.search")

        planner = PlannerAgent()
        plan = planner._generate_plan(
            "compare x and y and synthesize",
            make_context(),
            self._planner_input(t.list_health(), ["knowledge.search", "alt.search"]),
        )
        assert plan is not None
        tools = {
            str(task.input_data.get("tool_name"))
            for task in plan.tasks
            if task.input_data.get("tool_name")
        }
        assert tools == {"alt.search"}


# ---------------------------------------------------------------------------
# 9: LLM planner bounded context
# ---------------------------------------------------------------------------


class FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model = "fake-model"
        self.tokens_used = 10


class CapturingProvider:
    """Records the messages it receives; returns a trivial valid plan."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.captured: list[list[dict[str, str]]] = []

    def generate_structured(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.captured.append(messages)
        return FakeLLMResponse('{"plan_id": "plan-x", "tasks": []}')


class TestLLMPlannerContext:
    def test_health_context_reaches_llm_prompt(self):
        provider = CapturingProvider()
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_failure("knowledge.search")
        t.record_failure("knowledge.search")

        planner = LLMPlannerAgent(model_provider=provider)
        planner.execute(
            {
                "intent": "research the policy",
                "tool_health_context": build_planner_health_context(t.list_health()),
            },
            make_context(),
        )
        assert provider.captured, "LLM was not called"
        user_msg = provider.captured[0][-1]["content"]
        assert "knowledge.search" in user_msg
        assert "availability=degraded" in user_msg

    def test_context_is_bounded_and_sanitized(self):
        t = ToolHealthTracker()
        t.record_success("knowledge.search")
        t.record_failure("knowledge.search")

        block = build_planner_health_context(t.list_health())
        # No raw errors, payloads, or tenant identifiers
        assert "stub failure" not in block
        assert "user-a" not in block
        assert "org-a" not in block
        # Bounded structure: rates are rounded, one line per tool
        assert block.count("\n") == len(t.list_health())  # header + one line per tool

    def test_no_context_block_without_evidence(self):
        provider = CapturingProvider()
        planner = LLMPlannerAgent(model_provider=provider)
        planner.execute({"intent": "research the policy"}, make_context())
        user_msg = provider.captured[0][-1]["content"]
        assert "availability=" not in user_msg

    def test_context_capped_at_max_tools(self):
        t = ToolHealthTracker()
        for i in range(50):
            t.record_success(f"tool-{i:03d}")
        block = build_planner_health_context(t.list_health(), max_tools=5)
        assert block.count("\n") == 5  # header + 5 tool lines


# ---------------------------------------------------------------------------
# 10-11 + 15: Security, permissions, retry semantics
# ---------------------------------------------------------------------------


class TestRegistrySecurityAndSemantics:
    def test_denied_execution_not_recorded_as_failure(self):
        registry = ToolRegistry()
        registry.register(StubTool())
        result = registry.execute("knowledge.search", [], granted_permissions=[])
        assert result.status == TStatus.DENIED
        snap = registry.get_tool_health("knowledge.search")
        assert snap.sample_count == 0
        assert snap.state == ToolHealthState.UNKNOWN

    def test_unknown_tool_not_recorded(self):
        registry = ToolRegistry()
        result = registry.execute("ghost.tool", {}, granted_permissions=["knowledge.search"])
        assert result.status == TStatus.FAILED
        assert registry.get_tool_health("ghost.tool").sample_count == 0

    def test_outcomes_are_recorded(self):
        registry = ToolRegistry()
        registry.register(StubTool("ok.tool"))
        registry.register(StubTool("bad.tool", fail=True))
        registry.register(StubTool("slow.tool", timeout=True))
        registry.execute("ok.tool", ["knowledge.search"])
        registry.execute("bad.tool", ["knowledge.search"])
        registry.execute("slow.tool", ["knowledge.search"])
        assert registry.get_tool_health("ok.tool").state == ToolHealthState.UNKNOWN
        assert registry.get_tool_health("bad.tool").state == ToolHealthState.UNKNOWN
        assert registry.get_tool_health("slow.tool").state == ToolHealthState.UNKNOWN
        # with enough evidence the classification kicks in
        for _ in range(3):
            registry.execute("ok.tool", ["knowledge.search"])
            registry.execute("bad.tool", ["knowledge.search"])
        assert registry.get_tool_health("ok.tool").state == ToolHealthState.HEALTHY
        assert registry.get_tool_health("bad.tool").state == ToolHealthState.UNAVAILABLE
        assert registry.get_tool_health("slow.tool").recent_timeout_rate > 0

    def test_disabled_tool_stays_disabled(self):
        registry = ToolRegistry()
        registry.register(StubTool("disabled.tool", enabled=False))
        result = registry.execute("disabled.tool", ["knowledge.search"])
        assert result.status == TStatus.DENIED
        assert registry.get_tool_health("disabled.tool").sample_count == 0

    def test_execution_result_unchanged_by_health(self):
        """Existing retry/recovery semantics remain authoritative (15).

        Phase 6G: the tool-level execution contract (FAILED with the real
        error) is unchanged for health classification.  The circuit breaker
        is deliberately DISABLED here (fresh breaker per execution) because
        this test asserts on repeated-failure behavior beyond the circuit
        trip threshold; circuit fast-fail behavior is covered by the 6G
        circuit suite.
        """
        from aegisforge.tools.circuit_breaker import ToolCircuitBreaker

        registry = ToolRegistry(
            circuit_breaker=ToolCircuitBreaker(),
        )
        registry.register(StubTool("flaky.tool", fail=True))
        for _ in range(6):
            registry._circuit.reset()  # isolate from circuit transitions
            result = registry.execute("flaky.tool", ["knowledge.search"])
            # health degrades, but the execution contract never changes
            assert result.status == TStatus.FAILED
            assert "stub failure" in str(result.error)
        assert registry.get_tool_health("flaky.tool").state == ToolHealthState.UNAVAILABLE

    def test_snapshot_is_immutable_and_planner_safe(self):
        registry = ToolRegistry()
        registry.register(StubTool("t.tool"))
        registry.execute("t.tool", ["knowledge.search"])
        snap = registry.get_tool_health("t.tool")
        with pytest.raises(AttributeError):
            snap.state = ToolHealthState.UNAVAILABLE  # type: ignore[misc]

    def test_planner_cannot_write_health(self):
        """The planner API surface exposes no mutation path (11)."""
        registry = ToolRegistry()
        registry.register(StubTool("t.tool"))
        registry.execute("t.tool", ["knowledge.search"])
        before = registry.get_tool_health("t.tool").sample_count

        planner = PlannerAgent()
        # Planning repeatedly must not change health evidence
        for _ in range(3):
            planner._generate_plan(
                "research the policy",
                make_context(),
                {
                    "tool_health": [s.to_dict() for s in registry.list_tool_health()],
                    "available_tools": registry.list_tool_names(),
                },
            )
        assert registry.get_tool_health("t.tool").sample_count == before

    def test_health_snapshot_carries_no_tenant_data(self):
        registry = ToolRegistry()
        registry.register(StubTool("t.tool"))
        result = registry.execute(
            "t.tool",
            {"query": "tenant secret payload"},
            granted_permissions=["knowledge.search"],
        )
        assert result.status == TStatus.COMPLETED
        data = registry.get_tool_health("t.tool").to_dict()
        assert set(data.keys()) == {
            "tool_name",
            "state",
            "recent_failure_rate",
            "recent_timeout_rate",
            "consecutive_failures",
            "sample_count",
        }
        assert "tenant secret payload" not in str(data)

    def test_cross_tenant_planner_context_identical(self):
        """Health is process-wide operational data — no per-tenant divergence."""
        registry = ToolRegistry()
        registry.register(StubTool("t.tool"))
        for _ in range(4):
            registry.execute("t.tool", ["knowledge.search"])
        # Two different tenants planning see identical context
        block_a = build_planner_health_context(registry.list_tool_health())
        block_b = build_planner_health_context(registry.list_tool_health())
        assert block_a == block_b
        assert "user" not in block_a and "org" not in block_a


# ---------------------------------------------------------------------------
# 12: MCP server health vs tool health
# ---------------------------------------------------------------------------


class FakeMCPClient:
    """Minimal in-memory MCP client for lifecycle tests (duck-typed)."""

    def __init__(self) -> None:
        self.connected: set[str] = set()

    def connect(self, server_config: Any) -> bool:
        self.connected.add(server_config.server_id)
        return True

    def disconnect(self, server_id: str) -> None:
        self.connected.discard(server_id)

    def discover_tools(self, server_id: str) -> list[Any]:
        return []

    def invoke_tool(
        self, server_id: str, tool_name: str, arguments: dict[str, Any], timeout_seconds: int = 30
    ) -> dict[str, Any]:
        return {}

    def is_connected(self, server_id: str) -> bool:
        return server_id in self.connected

    def health_check(self, server_id: str) -> bool:
        return server_id in self.connected


class TestMcpServerVsToolHealth:
    def _lifecycle(self) -> tuple[MCPLifecycleManager, MCPServerCatalog, FakeMCPClient]:
        from aegisforge.domain.models import MCPServerConfig

        catalog = MCPServerCatalog()
        catalog.register(
            MCPServerConfig(server_id="srv-1", name="Server One", command="noop")
        )
        client = FakeMCPClient()
        manager = MCPLifecycleManager(catalog, client)
        return manager, catalog, client

    def test_server_health_summary_read_only(self):
        manager, _catalog, _client = self._lifecycle()
        manager.connect("srv-1")
        summary = manager.server_health_summary()
        assert summary == [{"server_id": "srv-1", "state": MCPHealthState.HEALTHY}]
        # No errors or payloads leak through the summary
        assert "error" not in str(summary).lower()

    def test_tool_failure_does_not_mark_server_unhealthy(self):
        manager, _catalog, _client = self._lifecycle()
        manager.connect("srv-1")

        registry = ToolRegistry()
        registry.register(StubTool("mcp.srv1.tool", fail=True))
        for _ in range(6):
            registry.execute("mcp.srv1.tool", ["knowledge.search"])

        assert registry.get_tool_health("mcp.srv1.tool").state == ToolHealthState.UNAVAILABLE
        assert manager.server_health_summary()[0]["state"] == MCPHealthState.HEALTHY

    def test_server_state_change_does_not_change_tool_health(self):
        manager, _catalog, client = self._lifecycle()
        manager.connect("srv-1")
        registry = ToolRegistry()
        registry.register(StubTool("mcp.tool"))
        registry.execute("mcp.tool", ["knowledge.search"])
        assert registry.get_tool_health("mcp.tool").state == ToolHealthState.UNKNOWN

        client.connected.discard("srv-1")  # simulate server going away
        assert manager.server_health_summary()[0]["state"] == MCPHealthState.HEALTHY  # stale until polled
        assert registry.get_tool_health("mcp.tool").sample_count == 1


# ---------------------------------------------------------------------------
# 13-14: Metrics
# ---------------------------------------------------------------------------


class TestToolHealthMetrics:
    def _counter_sum(self, name: str) -> float:
        """Sum all samples whose sample-name matches (robust to _total)."""
        try:
            from prometheus_client import REGISTRY

            total = 0.0
            for collector in REGISTRY.collect():
                for sample in collector.samples:
                    if sample.name == name:
                        total += sample.value
            return total
        except Exception:
            return 0.0

    def test_state_change_counter_emitted(self):
        t = ToolHealthTracker()
        t.record_success("metric.tool")
        before = self._counter_sum("tool_health_state_changes_total")
        for _ in range(4):
            t.record_failure("metric.tool")  # healthy→degraded→unavailable
        after = self._counter_sum("tool_health_state_changes_total")
        assert after >= before + 2

    def _sample_value(self, name: str, labels: dict[str, str]) -> float | None:
        try:
            from prometheus_client import REGISTRY

            for collector in REGISTRY.collect():
                for sample in collector.samples:
                    if sample.name == name and sample.labels == labels:
                        return sample.value
            return None
        except Exception:
            return None

    def test_state_gauge_tracks_counts(self):
        t = ToolHealthTracker()
        for _ in range(3):
            t.record_success("g1.tool")
        for _ in range(4):
            t.record_failure("g2.tool")
        healthy = self._sample_value("tool_health_states", {"state": "healthy"})
        unavailable = self._sample_value("tool_health_states", {"state": "unavailable"})
        assert healthy is not None and healthy >= 1
        assert unavailable is not None and unavailable >= 1

    def test_no_high_cardinality_labels(self):
        from prometheus_client import REGISTRY

        t = ToolHealthTracker()
        # many distinct tool names — labels must stay bounded
        for i in range(40):
            for _ in range(4):
                t.record_failure(f"tool-{i}")
        for collector in list(REGISTRY.collect()):
            if collector.name in ("tool_health_states", "tool_health_state_changes_total"):
                for sample in collector.samples:
                    assert set(sample.labels.keys()) <= {"state", "from_state", "to_state"}
                    for value in sample.labels.values():
                        assert value in (
                            "unknown",
                            "healthy",
                            "degraded",
                            "unavailable",
                        ), f"unbounded label value: {value}"
        # No tool name ever appears as a label
        exported = t.list_health()
        assert len(exported) == 40  # snapshots are fine; labels are not
