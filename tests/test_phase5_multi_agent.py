"""Phase 5 tests: dependency-aware multi-agent scheduling and execution.

Covers: plan validation, parallel execution with bounded concurrency,
structured result passing, retries, timeouts, partial-failure policies,
approval gating + resume, and tool-risk gating that a planner cannot lower.
"""
from __future__ import annotations

import time

from aegisforge.agents.analysis_agent import AnalysisAgent
from aegisforge.agents.base import BaseAgent
from aegisforge.approval.service import ApprovalService
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
    RequestStatus,
    TaskFailurePolicy,
)
from aegisforge.workflows.scheduler import (
    AgentFactory,
    ExecutionConfig,
    MultiAgentExecutor,
    validate_execution_plan,
)


def _plan(*tasks: ExecutionPlanTask) -> ExecutionPlan:
    return ExecutionPlan(plan_id="plan-test", request_id="req-test", tasks=list(tasks))


def _task(
    task_id: str,
    agent_type: AgentType = AgentType.RESEARCH,
    dependencies: list[str] | None = None,
    **kwargs: object,
) -> ExecutionPlanTask:
    defaults: dict[str, object] = {
        "task_id": task_id,
        "description": f"Task {task_id}",
        "assigned_agent_type": agent_type,
        "input_data": {"query": f"query {task_id}"},
        "dependencies": dependencies or [],
    }
    defaults.update(kwargs)
    return ExecutionPlanTask(**defaults)


class FakeAgent(BaseAgent):
    """Agent with scripted behavior: optional delay and fail counts."""

    def __init__(
        self,
        agent_type: AgentType = AgentType.RESEARCH,
        delay_seconds: float = 0.0,
        fail_attempts: int = 0,
        answer: str = "fake answer",
        name: str = "fake-agent",
    ) -> None:
        super().__init__(name=name, agent_type=agent_type, description="test agent")
        self._delay = delay_seconds
        self._fail_attempts = fail_attempts
        self._attempts = 0
        self._answer = answer

    def _execute(self, input_data: dict, context) -> AgentResult:  # type: ignore[no-untyped-def]
        self._attempts += 1
        if self._delay > 0:
            time.sleep(self._delay)
        if self._attempts <= self._fail_attempts:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="scripted failure",
                errors=["scripted failure"],
            )
        return AgentResult(
            agent_name=self.name,
            agent_type=self.agent_type,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"done {input_data.get('query', '')}",
            result={
                "query": input_data.get("query", ""),
                "answer": f"{self._answer}:{input_data.get('query', '')}",
                "source": f"source-{input_data.get('query', '')}",
            },
            tool_calls=[],
            confidence=0.9,
        )


class FakeAgentFactory(AgentFactory):
    def __init__(self, agents: dict[str, BaseAgent]) -> None:
        super().__init__(retrieval_service=None)
        self._agents = agents

    def build(self, task: ExecutionPlanTask, run_id: str) -> BaseAgent:  # type: ignore[override]
        return self._agents.get(task.task_id) or FakeAgent(agent_type=task.assigned_agent_type)


def _run(
    plan: ExecutionPlan,
    agents: dict[str, BaseAgent],
    config: ExecutionConfig | None = None,
    factory: AgentFactory | None = None,
    **kwargs: object,
) -> object:
    used_factory = factory or FakeAgentFactory(agents)
    executor = MultiAgentExecutor(config=config, agent_factory=used_factory, **kwargs)  # type: ignore[arg-type]
    return executor.execute(
        plan,
        request_id="req-test",
        workflow_id="wf-test",
        organization_id="org-1",
        user_id="u-1",
        intent="test intent",
    )


class TestPlanValidation:
    def test_valid_parallel_plan(self):
        plan = _plan(_task("t1"), _task("t2"), _task("t3", dependencies=["t1", "t2"]))
        assert validate_execution_plan(plan).valid

    def test_unknown_dependency_rejected(self):
        result = validate_execution_plan(_plan(_task("t1", dependencies=["ghost"])))
        assert not result.valid
        assert any("ghost" in e for e in result.errors)

    def test_duplicate_ids_rejected(self):
        result = validate_execution_plan(_plan(_task("dup"), _task("dup")))
        assert not result.valid
        assert any("duplicate" in e.lower() for e in result.errors)

    def test_circular_dependency_rejected(self):
        plan = _plan(_task("a", dependencies=["b"]), _task("b", dependencies=["a"]))
        result = validate_execution_plan(plan)
        assert not result.valid
        assert any("circular" in e.lower() for e in result.errors)

    def test_unknown_agent_type_rejected(self):
        result = validate_execution_plan(_plan(_task("t1", agent_type=AgentType.CODE)))
        assert not result.valid
        assert any("not available" in e for e in result.errors)

    def test_reference_to_non_dependency_rejected(self):
        plan = _plan(_task("t1", input_references={"data": "other.output"}))
        result = validate_execution_plan(plan)
        assert not result.valid
        assert any("not a declared dependency" in e for e in result.errors)

    def test_empty_plan_rejected(self):
        assert not validate_execution_plan(_plan()).valid


class TestParallelExecution:
    def test_independent_tasks_run_concurrently(self):
        plan = _plan(_task("a"), _task("b"), _task("c"))
        agents = {k: FakeAgent(delay_seconds=0.12) for k in ("a", "b", "c")}
        start = time.monotonic()
        outcome = _run(plan, agents, config=ExecutionConfig(max_concurrency=3))
        elapsed = time.monotonic() - start

        assert outcome.status == RequestStatus.COMPLETED
        assert elapsed < 0.3  # parallel wall time ~0.12s, not ~0.36s
        assert outcome.summary.max_concurrency_reached == 3
        assert outcome.summary.completed_tasks == 3

    def test_bounded_concurrency_is_respected(self):
        plan = _plan(_task("a"), _task("b"), _task("c"))
        agents = {k: FakeAgent(delay_seconds=0.1) for k in ("a", "b", "c")}
        start = time.monotonic()
        outcome = _run(plan, agents, config=ExecutionConfig(max_concurrency=1))
        elapsed = time.monotonic() - start

        assert outcome.status == RequestStatus.COMPLETED
        assert elapsed >= 0.25  # effectively sequential
        assert outcome.summary.max_concurrency_reached == 1

    def test_dependency_chain_serializes_execution(self):
        plan = _plan(_task("a"), _task("b", dependencies=["a"]), _task("c", dependencies=["b"]))
        agents = {k: FakeAgent(delay_seconds=0.03) for k in ("a", "b", "c")}
        outcome = _run(plan, agents)
        assert outcome.status == RequestStatus.COMPLETED
        assert outcome.summary.wave_count == 3
        assert outcome.summary.max_concurrency_reached == 1


class TestResultPassing:
    def test_evidence_flows_between_agents(self):
        plan = _plan(
            _task("research", input_data={"query": "support policy"}),
            _task(
                "analysis",
                agent_type=AgentType.ANALYSIS,
                dependencies=["research"],
                input_data={"query": "support policy", "evidence_from": ["research"]},
            ),
        )
        agents = {
            "research": FakeAgent(answer="customer support requires manager approval"),
            "analysis": AnalysisAgent(),
        }
        outcome = _run(plan, agents)
        assert outcome.status == RequestStatus.COMPLETED
        analysis = outcome.records["analysis"]
        assert analysis.status == AgentExecutionStatus.COMPLETED
        assert analysis.output["evidence_reviewed"] >= 1
        findings = " ".join(str(f.get("content", "")) for f in analysis.output["findings"])
        assert "manager approval" in findings

    def test_reference_to_failed_dependency_is_reported(self):
        plan = _plan(
            _task("upstream", max_retries=0),
            _task(
                "downstream",
                agent_type=AgentType.ANALYSIS,
                dependencies=["upstream"],
                input_data={"query": "x", "evidence_from": ["upstream"]},
                failure_policy=TaskFailurePolicy.CONTINUE_WITH_PARTIAL_RESULTS,
            ),
        )
        outcome = _run(plan, {"upstream": FakeAgent(fail_attempts=5), "downstream": AnalysisAgent()})
        assert outcome.records["upstream"].status == AgentExecutionStatus.FAILED
        downstream = outcome.records["downstream"]
        assert downstream.status == AgentExecutionStatus.COMPLETED
        assert any("upstream" in (e or "") for e in downstream.errors or [])


class TestRetries:
    def test_task_retries_then_succeeds(self):
        outcome = _run(_plan(_task("t1", max_retries=2)), {"t1": FakeAgent(fail_attempts=2)})
        assert outcome.records["t1"].status == AgentExecutionStatus.COMPLETED
        assert outcome.records["t1"].retry_count == 2
        assert outcome.summary.retried_tasks == 1

    def test_task_fails_permanently_after_max_retries(self):
        outcome = _run(_plan(_task("t1", max_retries=1)), {"t1": FakeAgent(fail_attempts=10)})
        assert outcome.records["t1"].status == AgentExecutionStatus.FAILED
        assert outcome.status == RequestStatus.FAILED


class TestTimeouts:
    def test_slow_agent_is_timed_out(self):
        plan = _plan(_task("t1", max_retries=0, timeout_seconds=0.05))
        start = time.monotonic()
        outcome = _run(plan, {"t1": FakeAgent(delay_seconds=1.0)})
        elapsed = time.monotonic() - start
        assert outcome.records["t1"].status == AgentExecutionStatus.TIMEOUT
        assert outcome.status == RequestStatus.FAILED
        assert elapsed < 0.3

    def test_late_timeout_result_cannot_overwrite_terminal_state(self):
        plan = _plan(_task("t1", max_retries=0, timeout_seconds=0.05))
        outcome = _run(plan, {"t1": FakeAgent(delay_seconds=0.2)})
        time.sleep(0.25)
        assert outcome.records["t1"].status == AgentExecutionStatus.TIMEOUT


class TestPartialFailures:
    @staticmethod
    def _plan(policy: TaskFailurePolicy) -> ExecutionPlan:
        return _plan(
            _task("research", max_retries=0),
            _task(
                "analysis",
                agent_type=AgentType.ANALYSIS,
                dependencies=["research"],
                input_data={"query": "q", "evidence_from": ["research"]},
                failure_policy=policy,
            ),
        )

    def test_fail_fast_aborts_dependent_task(self):
        outcome = _run(
            self._plan(TaskFailurePolicy.FAIL_FAST),
            {"research": FakeAgent(fail_attempts=5)},
        )
        assert outcome.records["research"].status == AgentExecutionStatus.FAILED
        assert outcome.records["analysis"].status == AgentExecutionStatus.FAILED
        assert outcome.records["analysis"].errors
        assert outcome.status == RequestStatus.FAILED

    def test_require_all_dependencies_blocks_execution(self):
        outcome = _run(
            self._plan(TaskFailurePolicy.REQUIRE_ALL_DEPENDENCIES),
            {"research": FakeAgent(fail_attempts=5)},
        )
        assert outcome.records["analysis"].status == AgentExecutionStatus.FAILED
        assert not outcome.records["analysis"].output

    def test_continue_with_partial_results_runs_downstream(self):
        outcome = _run(
            self._plan(TaskFailurePolicy.CONTINUE_WITH_PARTIAL_RESULTS),
            {"research": FakeAgent(fail_attempts=5)},
        )
        assert outcome.records["analysis"].status == AgentExecutionStatus.COMPLETED
        assert outcome.status == RequestStatus.FAILED  # upstream failure still recorded


class TestApprovalGate:
    def test_high_risk_task_pauses_and_resumes(self):
        plan = _plan(_task("t1"), _task("t2", risk_level="high", max_retries=0))
        approval_service = ApprovalService()

        outcome = _run(
            plan,
            {"t1": FakeAgent(), "t2": FakeAgent()},
            approval_service=approval_service,
        )
        assert outcome.status == RequestStatus.ACTION_REQUIRES_APPROVAL
        assert outcome.approval_id
        assert outcome.approval_task_id == "t2"
        assert outcome.records["t1"].status == AgentExecutionStatus.COMPLETED
        assert outcome.records["t2"].status == AgentExecutionStatus.PENDING

        approved = approval_service.approve(outcome.approval_id, reviewer_id="admin-1")
        assert approved is not None

        executor = MultiAgentExecutor(
            config=ExecutionConfig(),
            agent_factory=FakeAgentFactory({"t1": FakeAgent(), "t2": FakeAgent()}),
            approval_service=approval_service,
        )
        final = executor.execute(
            plan,
            request_id="req-test",
            workflow_id="wf-test",
            organization_id="org-1",
            user_id="u-1",
            intent="x",
            preexisting_records=outcome.record_dicts,
            approved_task_ids={outcome.approval_task_id},
        )
        assert final.status == RequestStatus.COMPLETED
        assert final.records["t2"].status == AgentExecutionStatus.COMPLETED

    def test_planner_cannot_downgrade_tool_risk(self):
        """A task declared 'low' risk that requests a high-risk tool still gates."""
        from aegisforge.tools.base import BaseTool, ToolDefinition

        class HighRiskTool(BaseTool):
            def _execute(self, input_data: dict) -> dict:  # type: ignore[override]
                return {"ok": True}

        factory = AgentFactory()
        factory.registry.register(
            HighRiskTool(
                ToolDefinition(
                    name="ops.restart",
                    description="restart service",
                    permission_requirements=["ops.restart"],
                    risk_level="high",
                    requires_approval=True,
                )
            )
        )

        plan = _plan(
            _task("t1", risk_level="low", input_data={"query": "q", "tool_name": "ops.restart"})
        )
        executor = MultiAgentExecutor(
            config=ExecutionConfig(),
            agent_factory=factory,
            approval_service=ApprovalService(),
        )
        outcome = executor.execute(
            plan,
            request_id="req-test",
            workflow_id="wf-test",
            organization_id="org-1",
            user_id="u-1",
            intent="x",
        )
        # Even though the task claims low risk, the registry reports the tool
        # as high risk → approval is required before execution.
        assert outcome.status == RequestStatus.ACTION_REQUIRES_APPROVAL
        assert outcome.approval_task_id == "t1"

    def test_high_risk_task_alone_pauses_for_approval(self):
        """A single high-risk task pauses before running — never executes first."""
        plan = _plan(_task("t1", risk_level="high", max_retries=0))
        approval_service = ApprovalService()

        outcome = _run(plan, {"t1": FakeAgent()}, approval_service=approval_service)
        assert outcome.status == RequestStatus.ACTION_REQUIRES_APPROVAL
        assert outcome.approval_task_id == "t1"
        assert outcome.records["t1"].status == AgentExecutionStatus.PENDING
        assert outcome.records["t1"].approval_required is True
        assert outcome.records["t1"].summary == ""  # not executed

    def test_dependent_task_runs_only_after_approval(self):
        """A dependent task of a gated task must not run before approval."""
        plan = _plan(
            _task("t1", risk_level="high", max_retries=0),
            _task("t2", dependencies=["t1"], max_retries=0),
        )
        approval_service = ApprovalService()
        agents = {"t1": FakeAgent(answer="approved source"), "t2": FakeAgent(answer="final")}

        outcome = _run(plan, agents, approval_service=approval_service)
        assert outcome.status == RequestStatus.ACTION_REQUIRES_APPROVAL
        assert outcome.approval_task_id == "t1"
        assert outcome.records["t1"].status == AgentExecutionStatus.PENDING
        assert outcome.records["t2"].status == AgentExecutionStatus.PENDING

        assert approval_service.approve(outcome.approval_id, reviewer_id="admin-1") is not None

        final = MultiAgentExecutor(
            config=ExecutionConfig(),
            agent_factory=FakeAgentFactory(agents),
            approval_service=approval_service,
        ).execute(
            plan,
            request_id="req-test",
            workflow_id="wf-test",
            organization_id="org-1",
            user_id="u-1",
            intent="x",
            preexisting_records=outcome.record_dicts,
            approved_task_ids={outcome.approval_task_id},
        )
        assert final.status == RequestStatus.COMPLETED
        assert final.records["t1"].status == AgentExecutionStatus.COMPLETED
        assert final.records["t2"].status == AgentExecutionStatus.COMPLETED

    def test_rejected_approval_keeps_task_gated(self):
        """A rejected approval never unlocks the gated task on resume."""
        plan = _plan(_task("t1", risk_level="high", max_retries=0))
        approval_service = ApprovalService()
        agents = {"t1": FakeAgent()}

        outcome = _run(plan, agents, approval_service=approval_service)
        assert outcome.status == RequestStatus.ACTION_REQUIRES_APPROVAL
        assert outcome.records["t1"].status == AgentExecutionStatus.PENDING

        assert approval_service.reject(outcome.approval_id, reviewer_id="admin-1") is not None

        resumed = MultiAgentExecutor(
            config=ExecutionConfig(),
            agent_factory=FakeAgentFactory(agents),
            approval_service=approval_service,
        ).execute(
            plan,
            request_id="req-test",
            workflow_id="wf-test",
            organization_id="org-1",
            user_id="u-1",
            intent="x",
            preexisting_records=outcome.record_dicts,
            approved_task_ids=set(),
        )
        # Still gated: the task must not execute without a granted approval.
        assert resumed.status == RequestStatus.ACTION_REQUIRES_APPROVAL
        assert resumed.records["t1"].status == AgentExecutionStatus.PENDING
        assert resumed.records["t1"].summary == ""
        assert resumed.approval_task_id == "t1"
