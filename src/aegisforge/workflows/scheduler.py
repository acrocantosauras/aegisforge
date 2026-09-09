"""Dependency-aware, bounded-concurrency multi-agent scheduler.

Phase 5 core: turns a validated ``ExecutionPlan`` into an executed run where:

- tasks with satisfied dependencies run concurrently (bounded by config),
- results between tasks pass through validated references only,
- per-task timeout + retry configuration is honored,
- partial failures follow per-task policies (never silently ignored),
- the whole run is observable (per-task records, parallelism metrics) and
  checkpoint-friendly (state is JSON-serializable and resumable).

The scheduler is deterministic about *scheduling* (topological waves) while
still allowing real concurrency for network-bound agents.  It never creates
unbounded worker threads: concurrency is capped by ``ExecutionConfig``.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aegisforge.agents.analysis_agent import AnalysisAgent
from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.agents.rag_agent import RAGAgent
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.agents.synthesis_agent import SynthesisAgent
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
    ExecutionRunSummary,
    RequestStatus,
    TaskExecutionRecord,
    TaskFailurePolicy,
)
from aegisforge.observability.metrics import (
    record_agent_execution,
    record_task_concurrency,
    record_task_execution,
    record_task_retry,
)
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Agent types this platform can actually execute in a multi-agent run.
IMPLEMENTED_AGENT_TYPES = {
    AgentType.RESEARCH,
    AgentType.RAG,
    AgentType.ANALYSIS,
    AgentType.SYNTHESIS,
}


@dataclass
class ExecutionConfig:
    """Boundaries for a multi-agent run."""

    max_concurrency: int = 4
    default_task_timeout_seconds: int = 120
    default_max_retries: int = 2
    approval_required_risk_levels: set[str] = field(
        default_factory=lambda: {"high", "critical"}
    )


class PlanValidationResult:
    """Outcome of plan validation for execution."""

    def __init__(self, valid: bool, errors: list[str]) -> None:
        self.valid = valid
        self.errors = errors


def validate_execution_plan(plan: ExecutionPlan) -> PlanValidationResult:
    """Validate that a plan can be scheduled safely.

    All LLM-generated plans are untrusted input; this validation runs again
    immediately before execution regardless of planner-side checks.
    """
    errors: list[str] = []
    if plan is None:
        return PlanValidationResult(False, ["Execution plan is missing"])
    if not plan.plan_id:
        errors.append("Plan is missing plan_id")
    if not plan.tasks:
        errors.append("Plan contains no tasks")

    task_ids: set[str] = set()
    for task in plan.tasks:
        if not task.task_id:
            errors.append("A task is missing task_id")
            continue
        if task.task_id in task_ids:
            errors.append(f"Duplicate task_id: {task.task_id}")
        task_ids.add(task.task_id)

        if task.assigned_agent_type not in IMPLEMENTED_AGENT_TYPES:
            errors.append(
                f"Task {task.task_id} references agent type "
                f"'{task.assigned_agent_type.value}' which is not available for execution"
            )
        # Input references must point at *declared* dependencies only.
        for field_name, ref in (task.input_references or {}).items():
            dep_id = ref.split(".", 1)[0]
            if dep_id not in task.dependencies:
                errors.append(
                    f"Task {task.task_id} field '{field_name}' references '{dep_id}' "
                    "which is not a declared dependency"
                )
        if task.failure_policy == TaskFailurePolicy.RETRY_FAILED_TASK and task.max_retries < 1:
            errors.append(
                f"Task {task.task_id} declares retry policy but max_retries is 0"
            )

    for task in plan.tasks:
        for dep in task.dependencies:
            if dep not in task_ids:
                errors.append(
                    f"Task {task.task_id} references unknown dependency '{dep}'"
                )

    # Cycle detection (topological DFS).
    graph: dict[str, list[str]] = {t.task_id: t.dependencies for t in plan.tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(tid: str) -> bool:
        if tid in visiting:
            return True
        if tid in visited:
            return False
        visiting.add(tid)
        for dep in graph.get(tid, []):
            if _visit(dep):
                return True
        visiting.discard(tid)
        visited.add(tid)
        return False

    for tid in graph:
        if _visit(tid):
            errors.append("Circular dependency detected in execution plan")
            break

    return PlanValidationResult(valid=not errors, errors=errors)


class TaskRunError(Exception):
    """Raised when a task cannot be produced by the configured agent factory."""


class AgentFactory:
    """Builds agents for the implemented agent types.

    A single shared ToolRegistry is used for research-style tasks so that
    MCP-discovered tools (registered by the caller) remain available.
    """

    def __init__(
        self,
        retrieval_service: Any = None,
        model_provider: Any = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._model_provider = model_provider
        self._registry = tool_registry or self._build_default_registry()

    @staticmethod
    def _build_default_registry() -> ToolRegistry:
        registry = ToolRegistry()
        try:
            registry.register(KnowledgeSearchTool())
        except ValueError:
            pass  # already registered
        return registry

    def build(self, task: ExecutionPlanTask, run_id: str) -> BaseAgent:
        agent_type = task.assigned_agent_type
        if agent_type == AgentType.RESEARCH:
            return ResearchAgent(registry=self._registry)
        if agent_type == AgentType.RAG:
            if self._retrieval_service is None:
                raise TaskRunError(
                    f"Task {task.task_id}: RAG agent requires a retrieval service"
                )
            return RAGAgent(retrieval_service=self._retrieval_service)
        if agent_type == AgentType.ANALYSIS:
            return AnalysisAgent()
        if agent_type == AgentType.SYNTHESIS:
            return SynthesisAgent()
        raise TaskRunError(
            f"Task {task.task_id}: no execution implementation for agent type "
            f"'{agent_type.value}'"
        )

    @property
    def registry(self) -> ToolRegistry:
        return self._registry


def _pick_path(source: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path inside a result dict."""
    current: Any = source
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class TaskInputResolver:
    """Resolves a task's effective input from completed dependency outputs.

    Two special input fields are supported for inter-agent data passing:

    - ``evidence_from: [dep_task_id, ...]`` — expands each completed
      dependency's result into an evidence item (content + source + task_id).
    - ``agent_outputs_from: [dep_task_id, ...]`` — expands completed dependency
      records into serialized agent-output views for the synthesis agent.

    References declared in ``input_references`` must point at declared
    dependencies (validated earlier).  Missing/incomplete dependencies are
    reported back to the caller — never silently defaulted for FAIL_FAST plans.
    """

    @staticmethod
    def resolve(
        task: ExecutionPlanTask,
        records: dict[str, TaskExecutionRecord],
    ) -> tuple[dict[str, Any], list[str]]:
        input_data: dict[str, Any] = dict(task.input_data)
        warnings: list[str] = []

        for field_name, ref in (task.input_references or {}).items():
            dep_id, _, path = ref.partition(".")
            if dep_id not in task.dependencies:
                warnings.append(f"Field '{field_name}' references non-dependency '{dep_id}'")
                continue
            record = records.get(dep_id)
            if record is None or record.status != AgentExecutionStatus.COMPLETED:
                warnings.append(
                    f"Field '{field_name}' requires dependency '{dep_id}' which "
                    f"did not complete (status={record.status.value if record else 'missing'})"
                )
                input_data[field_name] = None
                continue
            value = _pick_path(record.output, path or "answer")
            if value is None:
                warnings.append(
                    f"Field '{field_name}' references '{ref}' but the dependency "
                    "output has no such value"
                )
                input_data[field_name] = None
            else:
                input_data[field_name] = value

        evidence_from = input_data.pop("evidence_from", None)
        if evidence_from:
            evidence: list[dict[str, Any]] = []
            for dep_id in evidence_from:
                record = records.get(dep_id)
                if record is None or record.status != AgentExecutionStatus.COMPLETED:
                    warnings.append(f"Evidence task '{dep_id}' did not complete")
                    continue
                evidence.append(
                    {
                        "content": record.output.get("answer", record.summary),
                        "summary": record.summary,
                        "source": _pick_path(record.output, "source")
                        or _pick_path(record.output, "query")
                        or record.task_id,
                        "task_id": dep_id,
                        "agent": record.agent_type,
                    }
                )
            input_data["evidence"] = evidence

        agent_outputs_from = input_data.pop("agent_outputs_from", None)
        if agent_outputs_from:
            outputs: list[dict[str, Any]] = []
            for dep_id in agent_outputs_from:
                record = records.get(dep_id)
                if record is None or record.status != AgentExecutionStatus.COMPLETED:
                    outputs.append(
                        {
                            "task_id": dep_id,
                            "agent": record.agent_type if record else "unknown",
                            "status": record.status.value if record else "missing",
                            "errors": record.errors if record else ["Dependency did not run"],
                        }
                    )
                    continue
                outputs.append(
                    {
                        "task_id": dep_id,
                        "agent": record.agent_type,
                        "status": record.status.value,
                        "summary": record.summary,
                        "result": record.output,
                        "evidence": record.evidence,
                    }
                )
            input_data["agent_outputs"] = outputs

        return input_data, warnings


def _critical_path_ms(
    tasks: list[ExecutionPlanTask],
    records: dict[str, TaskExecutionRecord],
) -> int:
    """Longest dependency chain duration using per-task durations."""
    task_map = {t.task_id: t for t in tasks}
    best: dict[str, int] = {}

    def _finish(tid: str) -> int:
        if tid in best:
            return best[tid]
        record = records.get(tid)
        duration = record.duration_ms or 0 if record is not None else 0
        task = task_map.get(tid)
        deps = task.dependencies if task is not None else []
        dep_finish = max((_finish(d) for d in deps), default=0)
        best[tid] = dep_finish + (duration or 0)
        return best[tid]

    return max((_finish(t.task_id) for t in tasks), default=0)


class RunOutcome:
    """Result of a multi-agent run."""

    def __init__(
        self,
        records: dict[str, TaskExecutionRecord],
        summary: ExecutionRunSummary,
        status: RequestStatus,
        errors: list[str],
        approval_task_id: str = "",
        approval_id: str = "",
    ) -> None:
        self.records = records
        self.summary = summary
        self.status = status
        self.errors = errors
        self.approval_task_id = approval_task_id
        self.approval_id = approval_id

    @property
    def record_dicts(self) -> dict[str, dict[str, Any]]:
        """JSON-mode dicts so enums become plain strings in state/DB."""
        return {tid: rec.model_dump(mode="json") for tid, rec in self.records.items()}

    @property
    def failed_task_ids(self) -> list[str]:
        return [
            tid
            for tid, rec in self.records.items()
            if rec.status
            in (AgentExecutionStatus.FAILED, AgentExecutionStatus.TIMEOUT, AgentExecutionStatus.DENIED)
        ]

    @property
    def completed_task_ids(self) -> list[str]:
        return [
            tid
            for tid, rec in self.records.items()
            if rec.status == AgentExecutionStatus.COMPLETED
        ]


class MultiAgentExecutor:
    """Executes an ExecutionPlan with dependency-aware scheduling."""

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        agent_factory: AgentFactory | None = None,
        approval_service: Any | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
        audit_callback: Callable[[str, str, dict[str, Any]], None] | None = None,
        tracer: Any | None = None,
    ) -> None:
        self._config = config or ExecutionConfig()
        self._agent_factory = agent_factory
        self._approval_service = approval_service
        self._checkpoint_callback = checkpoint_callback
        self._audit_callback = audit_callback
        self._tracer = tracer

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        request_id: str,
        workflow_id: str,
        organization_id: str = "",
        user_id: str = "",
        intent: str = "",
        job_id: str = "",
        preexisting_records: dict[str, dict[str, Any]] | None = None,
        approved_task_ids: set[str] | None = None,
    ) -> RunOutcome:
        """Execute a plan. ``preexisting_records`` allows idempotent resume."""
        validation = validate_execution_plan(plan)
        if not validation.valid:
            return RunOutcome(
                records={},
                summary=ExecutionRunSummary(),
                status=RequestStatus.FAILED,
                errors=[f"Invalid execution plan: {'; '.join(validation.errors)}"],
            )

        records: dict[str, TaskExecutionRecord] = {
            tid: TaskExecutionRecord(**rec)
            for tid, rec in (preexisting_records or {}).items()
        }
        start = time.monotonic()
        errors: list[str] = []
        wave_count = 0
        max_in_flight = 0

        # Seed records for tasks that have not run yet.
        for task in plan.tasks:
            if task.task_id not in records:
                records[task.task_id] = TaskExecutionRecord(
                    task_id=task.task_id,
                    description=task.description,
                    agent_type=task.assigned_agent_type.value,
                    status=AgentExecutionStatus.PENDING,
                    dependencies=list(task.dependencies),
                    max_retries=task.max_retries,
                    timeout_seconds=task.timeout_seconds,
                    failure_policy=task.failure_policy,
                )

        def _truly_terminal(tid: str) -> bool:
            rec = records.get(tid)
            return rec is not None and rec.status != AgentExecutionStatus.PENDING

        while True:
            pending = [
                t for t in plan.tasks if not _truly_terminal(t.task_id)
            ]
            if not pending:
                break

            # Tasks whose dependencies have all finished (terminal, incl failed).
            runnable: list[ExecutionPlanTask] = []
            blocked: list[ExecutionPlanTask] = []
            for task in pending:
                deps_done = all(_truly_terminal(d) for d in task.dependencies)
                if not deps_done:
                    continue
                deps_failed = [
                    d for d in task.dependencies
                    if records.get(d)
                    and records[d].status
                    in (AgentExecutionStatus.FAILED, AgentExecutionStatus.TIMEOUT, AgentExecutionStatus.DENIED)
                ]
                if deps_failed:
                    if task.failure_policy in (
                        TaskFailurePolicy.REQUIRE_ALL_DEPENDENCIES,
                        TaskFailurePolicy.FAIL_FAST,
                    ):
                        blocked.append(task)
                    else:
                        runnable.append(task)  # partial-results policies may proceed
                else:
                    runnable.append(task)

            if blocked and not runnable:
                # No task can proceed → mark blocked tasks failed and abort.
                for task in blocked:
                    rec = records[task.task_id]
                    rec.status = AgentExecutionStatus.FAILED
                    rec.errors = [
                        "Dependency failed and task failure policy forbids partial execution"
                    ]
                    rec.completed_at = time.monotonic()
                for task in pending:
                    if not _truly_terminal(task.task_id):
                        records[task.task_id].status = AgentExecutionStatus.FAILED
                        records[task.task_id].errors = [
                            "Run aborted because required dependencies failed"
                        ]
                errors.append(
                    f"Workflow blocked: {len(blocked)} task(s) cannot run because "
                    "required dependencies failed"
                )
                self._save_checkpoint(plan, records, errors)
                break

            # Approval gating: tasks that may run now without human approval
            # are split from approval-gated tasks.  Gated tasks are never
            # executed in this pass; they pause the workflow instead.  The
            # effective risk considers *tool* risk metadata from the registry
            # (server-controlled), so a planner cannot downgrade a task's risk
            # below the tools it requests.  Tasks already granted approval
            # (resumed run) are never re-gated.
            approved = approved_task_ids or set()
            gated: list[ExecutionPlanTask] = []
            executable: list[ExecutionPlanTask] = []
            for t in runnable:
                needs_approval = (
                    self._approval_service is not None
                    and self._effective_risk(t) in self._config.approval_required_risk_levels
                    and t.task_id not in approved
                )
                (gated if needs_approval else executable).append(t)

            # Execute all currently eligible non-gated work with bounded
            # concurrency, then pause at the approval-gated work.
            if executable:
                wave_count += 1
                in_flight = self._execute_wave(
                    plan, executable, records,
                    request_id=request_id, workflow_id=workflow_id,
                    organization_id=organization_id, user_id=user_id, intent=intent,
                )
                max_in_flight = max(max_in_flight, in_flight)

                # FAIL_FAST propagation: a FAIL_FAST task that exhausted retries
                # aborts the whole run — remaining (incl. gated) tasks never run,
                # and no task executes without approval.
                fail_fast_failure = any(
                    records[t.task_id].status
                    in (AgentExecutionStatus.FAILED, AgentExecutionStatus.TIMEOUT, AgentExecutionStatus.DENIED)
                    and t.failure_policy == TaskFailurePolicy.FAIL_FAST
                    for t in executable
                )
                if fail_fast_failure:
                    for task in pending:
                        rec = records[task.task_id]
                        if rec.status == AgentExecutionStatus.PENDING:
                            rec.status = AgentExecutionStatus.FAILED
                            rec.errors = ["Run aborted after a FAIL_FAST task failed"]
                    errors.append("Run aborted: a FAIL_FAST task failed")
                    self._save_checkpoint(plan, records, errors)
                    break

                self._save_checkpoint(plan, records, errors)

            # Pause at approval-gated work.  Only one approval is outstanding
            # at a time; gated tasks left behind stay PENDING and are
            # re-evaluated on resume, keeping decisions deterministic and
            # serialized.  A task that requires approval is never executed
            # before its approval is granted.
            if gated:
                gated_task = gated[0]
                approval = self._request_approval(
                    plan, gated_task, request_id, workflow_id,
                    organization_id, user_id, job_id,
                    risk_level=self._effective_risk(gated_task),
                )
                if approval:
                    rec = records[gated_task.task_id]
                    rec.approval_required = True
                    rec.approval_id = approval
                    self._save_checkpoint(plan, records, errors)
                    return RunOutcome(
                        records=records,
                        summary=self._summarize(plan, records, wave_count, max_in_flight, start),
                        status=RequestStatus.ACTION_REQUIRES_APPROVAL,
                        errors=errors,
                        approval_task_id=gated_task.task_id,
                        approval_id=approval,
                    )

        # ------------------------------------------------------------------
        summary = self._summarize(plan, records, wave_count, max_in_flight, start)
        summary.total_duration_ms = int((time.monotonic() - start) * 1000)
        summary.critical_path_ms = _critical_path_ms(plan.tasks, records)

        failed_ids = [
            tid for tid, rec in records.items()
            if rec.status in (AgentExecutionStatus.FAILED, AgentExecutionStatus.TIMEOUT, AgentExecutionStatus.DENIED)
        ]
        if failed_ids:
            status = RequestStatus.FAILED
            for tid in failed_ids:
                errors.append(
                    f"Task {tid} failed: {'; '.join(records[tid].errors or ['unknown error'])}"
                )
        else:
            status = RequestStatus.COMPLETED

        return RunOutcome(
            records=records,
            summary=summary,
            status=status,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    _RISK_RANK: ClassVar[dict[str, int]] = {
        "low": 0, "medium": 1, "high": 2, "critical": 3
    }

    def _effective_risk(self, task: ExecutionPlanTask) -> str:
        """Highest of the task's declared risk and its tools' server risk."""
        best = task.risk_level.lower()
        if self._agent_factory is not None:
            tool_name = str((task.input_data or {}).get("tool_name", ""))
            tool = self._agent_factory.registry.get(tool_name)
            if tool is not None:
                tool_risk = tool.definition.risk_level.lower()
                if self._RISK_RANK.get(tool_risk, 0) > self._RISK_RANK.get(best, 0):
                    best = tool_risk
        return best

    def _request_approval(
        self,
        plan: ExecutionPlan,
        task: ExecutionPlanTask,
        request_id: str,
        workflow_id: str,
        organization_id: str,
        user_id: str,
        job_id: str,
        risk_level: str = "high",
    ) -> str:
        """Create a persisted approval request; returns its ID or ''."""
        if self._approval_service is None:
            return ""
        try:
            from aegisforge.domain.models import RiskLevel

            approval = self._approval_service.create_approval_request(
                job_id=job_id or plan.plan_id,
                request_id=request_id,
                workflow_id=workflow_id,
                action_description=task.description,
                requested_by=user_id or "system",
                organization_id=organization_id,
                risk_level=RiskLevel(risk_level),
                reason=task.expected_output_description or task.description,
            )
            if self._audit_callback:
                self._audit_callback(
                    "workflow.paused_for_approval",
                    "task",
                    {"task_id": task.task_id, "approval_id": approval.approval_id},
                )
            return approval.approval_id
        except Exception as exc:  # approval failure must not silently proceed
            logger.exception("Failed to create approval request for task %s", task.task_id)
            raise RuntimeError(f"Approval required for task {task.task_id} but could not be created: {exc}") from exc

    def _execute_wave(
        self,
        plan: ExecutionPlan,
        tasks: list[ExecutionPlanTask],
        records: dict[str, TaskExecutionRecord],
        *,
        request_id: str,
        workflow_id: str,
        organization_id: str,
        user_id: str,
        intent: str,
    ) -> int:
        """Run a set of ready tasks with bounded concurrency."""
        if not tasks:
            return 0
        concurrency = max(1, min(self._config.max_concurrency, len(tasks)))
        if self._agent_factory is None:
            self._agent_factory = AgentFactory()
        try:
            record_task_concurrency(concurrency)
        except Exception:  # noqa: S110 - observability must never break execution
            pass

        futures: dict[Any, ExecutionPlanTask] = {}
        pool = ThreadPoolExecutor(max_workers=concurrency)
        try:
            for task in tasks:
                if records[task.task_id].status != AgentExecutionStatus.PENDING:
                    continue
                future = pool.submit(
                    self._run_task_with_retries,
                    task,
                    records,
                    plan,
                    request_id=request_id,
                    workflow_id=workflow_id,
                    organization_id=organization_id,
                    user_id=user_id,
                    intent=intent,
                )
                futures[future] = task

            for future, task in futures.items():
                total_budget = task.timeout_seconds * (task.max_retries + 1)
                try:
                    future.result(timeout=total_budget)
                except _FutureTimeout:
                    rec = records[task.task_id]
                    if rec.status == AgentExecutionStatus.PENDING:
                        rec.status = AgentExecutionStatus.TIMEOUT
                        rec.errors = [f"Task exceeded total budget of {total_budget}s"]
                        rec.completed_at = time.monotonic()
                except Exception as exc:  # pragma: no cover - defensive
                    rec = records[task.task_id]
                    if rec.status == AgentExecutionStatus.PENDING:
                        rec.status = AgentExecutionStatus.FAILED
                        rec.errors = [f"Unhandled scheduler error: {exc}"]
                        rec.completed_at = time.monotonic()
        finally:
            # A running Python thread cannot be force-cancelled.  Do not wait
            # for timed-out agent work here; its result is never authoritative
            # after the record has been marked terminal.
            pool.shutdown(wait=False, cancel_futures=True)
        return concurrency

    def _classify_failure(self, errors: list[str], status: Any) -> str:
        """Classify a failure type for recovery decisions (Phase 5.3E).

        Returns one of: "transient", "configuration", "dependency",
        "permission", "timeout", "unknown".
        """
        error_text = ";".join(errors).lower() if errors else ""
        status_val = status.value if hasattr(status, "value") else str(status)

        if status_val == "timeout":
            return "timeout"
        if status_val == "denied":
            return "permission"
        if any(kw in error_text for kw in ["not connected", "unavailable", "refused", "connection"]):
            return "transient"
        if any(kw in error_text for kw in ["not configured", "not available", "not registered", "not found"]):
            return "configuration"
        if any(kw in error_text for kw in ["dependency", "required dependency", "blocked"]):
            return "dependency"
        return "unknown"

    def _suggest_recovery(self, failure_type: str, retry_count: int, max_retries: int) -> dict[str, Any]:
        """Suggest a recovery strategy based on failure classification (Phase 5.3E).

        Returns a structured decision: action + reason.
        """
        remaining = max_retries - retry_count

        if failure_type == "transient" and remaining > 0:
            return {
                "action": "retry",
                "reason": f"Transient failure with {remaining} retries remaining",
                "recoverable": True,
            }
        if failure_type == "timeout" and remaining > 0:
            return {
                "action": "retry_with_extended_timeout",
                "reason": f"Timeout with {remaining} retries remaining",
                "recoverable": True,
            }
        if failure_type == "configuration":
            return {
                "action": "skip",
                "reason": "Configuration error — tool/agent not available",
                "recoverable": False,
            }
        if failure_type == "permission":
            return {
                "action": "escalate",
                "reason": "Permission denied — requires policy review",
                "recoverable": False,
            }
        if failure_type == "dependency":
            return {
                "action": "skip",
                "reason": "Required dependency failed",
                "recoverable": False,
            }
        # Unknown or exhausted retries
        if remaining > 0:
            return {
                "action": "retry",
                "reason": f"Unknown failure with {remaining} retries remaining",
                "recoverable": True,
            }
        return {
            "action": "abort",
            "reason": "All retries exhausted",
            "recoverable": False,
        }

    def _run_task_with_retries(
        self,
        task: ExecutionPlanTask,
        records: dict[str, TaskExecutionRecord],
        plan: ExecutionPlan,
        *,
        request_id: str,
        workflow_id: str,
        organization_id: str,
        user_id: str,
        intent: str,
    ) -> None:
        """Run one task with its own retry/timeout handling (worker thread).

        Phase 5.3E: Adds structured failure classification and recovery
        decision logging for observability.
        """
        record = records[task.task_id]
        started = time.monotonic()
        record.started_at = started
        record.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        attempts = task.max_retries + 1
        recovery_log: list[dict[str, Any]] = []

        for attempt in range(attempts):
            if attempt > 0:
                if record.status == AgentExecutionStatus.TIMEOUT:
                    return
                if task.retry_delay_seconds > 0:
                    time.sleep(task.retry_delay_seconds)
                record.retry_count = attempt
                try:
                    record_agent_execution(task.assigned_agent_type.value, "retry", 0)
                    record_task_retry(task.assigned_agent_type.value)
                except Exception:  # noqa: S110 - observability must never break execution
                    pass
            try:
                result = self._execute_task_agent(
                    task, records, plan, request_id=request_id,
                    workflow_id=workflow_id, organization_id=organization_id,
                    user_id=user_id, intent=intent,
                )
            except TaskRunError as exc:
                if record.status == AgentExecutionStatus.TIMEOUT:
                    return
                record.status = AgentExecutionStatus.FAILED
                record.errors = [str(exc)]
                record.completed_at = time.monotonic()
                record.duration_ms = int((time.monotonic() - started) * 1000)
                return
            except Exception as exc:  # agent-level safety net
                if record.status == AgentExecutionStatus.TIMEOUT:
                    return
                record.status = AgentExecutionStatus.FAILED
                record.errors = [f"Agent execution failed: {exc}"]
                record.completed_at = time.monotonic()
                record.duration_ms = int((time.monotonic() - started) * 1000)
                return

            if record.status == AgentExecutionStatus.TIMEOUT:
                return
            if result.status == AgentExecutionStatus.COMPLETED:
                record.status = AgentExecutionStatus.COMPLETED
                record.output = dict(result.result)
                record.summary = result.summary
                record.evidence = result.evidence
                record.tool_calls = result.tool_calls
                record.errors = list(result.errors)
                record.completed_at = time.monotonic()
                record.duration_ms = int((time.monotonic() - started) * 1000)
                self._record_metrics(task, record)
                return

            # Failed / denied / timeout → record and retry if attempts remain.
            record.status = result.status
            record.errors = list(result.errors or [f"Agent finished with status {result.status.value}"])

            # Phase 5.3E: Classify failure and log recovery decision
            failure_type = self._classify_failure(record.errors, result.status)
            recovery = self._suggest_recovery(failure_type, attempt, task.max_retries)
            recovery_log.append({
                "attempt": attempt + 1,
                "failure_type": failure_type,
                "recovery_action": recovery["action"],
                "recoverable": recovery["recoverable"],
                "reason": recovery["reason"],
            })
            logger.info(
                "Task %s attempt %d/%d: failure_type=%s, recovery=%s",
                task.task_id, attempt + 1, attempts,
                failure_type, recovery["action"],
            )

            if attempt < attempts - 1:
                record.status = AgentExecutionStatus.PENDING  # allow retry loop
                continue
        record.completed_at = time.monotonic()
        record.duration_ms = int((time.monotonic() - started) * 1000)
        # Attach recovery log to record metadata for observability
        if recovery_log:
            record.output["_recovery_log"] = recovery_log
        self._record_metrics(task, record)

    @staticmethod
    def _record_metrics(task: ExecutionPlanTask, record: TaskExecutionRecord) -> None:
        """Record per-task Prometheus metrics (never allowed to break the run)."""
        try:
            record_task_execution(
                task.assigned_agent_type.value,
                record.status.value,
                record.duration_ms,
            )
        except Exception:  # noqa: S110 - observability must never break execution
            pass

    def _execute_task_agent(
        self,
        task: ExecutionPlanTask,
        records: dict[str, TaskExecutionRecord],
        plan: ExecutionPlan,
        *,
        request_id: str,
        workflow_id: str,
        organization_id: str,
        user_id: str,
        intent: str,
    ) -> AgentResult:
        assert self._agent_factory is not None
        agent = self._agent_factory.build(task, workflow_id)
        input_data, warnings = TaskInputResolver.resolve(task, records)

        permissions = [
            PermissionSpec(name=perm, allow=True)
            for perm in task.tool_permissions_required
        ]
        context = AgentExecutionContext(
            request_id=request_id,
            workflow_id=workflow_id,
            task_id=task.task_id,
            user_id=user_id,
            organization_id=organization_id,
            permissions=permissions,
            metadata={"run_phase": "multi_agent", "intent": intent[:500]},
        )
        single = ThreadPoolExecutor(max_workers=1)
        future = single.submit(agent.execute, input_data, context)
        try:
            result = future.result(timeout=task.timeout_seconds)
        except _FutureTimeout:
            future.cancel()
            result = AgentResult(
                agent_name=agent.name,
                agent_type=task.assigned_agent_type,
                status=AgentExecutionStatus.TIMEOUT,
                summary=f"Task {task.task_id} timed out after {task.timeout_seconds}s",
                errors=[f"Timeout after {task.timeout_seconds}s"],
            )
        finally:
            # Python cannot stop a running thread, but the caller must not
            # block and the late result is discarded by this invocation.
            single.shutdown(wait=False, cancel_futures=True)
        if warnings and not result.errors:
            # Missing referenced inputs are surfaced as warnings on the record.
            result = AgentResult(
                    agent_name=result.agent_name,
                    agent_type=result.agent_type,
                    status=result.status,
                    summary=result.summary,
                    result=result.result,
                    evidence=result.evidence,
                    tool_calls=result.tool_calls,
                    confidence=result.confidence,
                    errors=list(result.errors) + [f"Input warnings: {'; '.join(warnings)}"],
                )
        return result

    def _save_checkpoint(
        self,
        plan: ExecutionPlan,
        records: dict[str, TaskExecutionRecord],
        errors: list[str],
    ) -> None:
        if self._checkpoint_callback is None:
            return
        try:
            self._checkpoint_callback(
                {
                    "task_records": {
                        tid: rec.model_dump(mode="json") for tid, rec in records.items()
                    },
                    "errors": errors,
                }
            )
        except Exception as exc:  # checkpointing must never break the run
            logger.warning("Checkpoint callback failed: %s", exc)

    def _summarize(
        self,
        plan: ExecutionPlan,
        records: dict[str, TaskExecutionRecord],
        wave_count: int,
        max_in_flight: int,
        start: float,
    ) -> ExecutionRunSummary:
        summary = ExecutionRunSummary(total_tasks=len(plan.tasks))
        for rec in records.values():
            if rec.status == AgentExecutionStatus.COMPLETED:
                summary.completed_tasks += 1
            elif rec.status in (AgentExecutionStatus.FAILED, AgentExecutionStatus.DENIED):
                summary.failed_tasks += 1
            elif rec.status == AgentExecutionStatus.TIMEOUT:
                summary.timed_out_tasks += 1
            elif rec.status == AgentExecutionStatus.PENDING:
                summary.skipped_tasks += 1
            if rec.retry_count:
                summary.retried_tasks += 1
        summary.wave_count = wave_count
        summary.max_concurrency_reached = max_in_flight
        elapsed = max(time.monotonic() - start, 0.0)
        if elapsed > 0 and wave_count > 0:
            summary.average_concurrency = round(
                summary.total_tasks / (elapsed / max(1, wave_count)), 3
            )
        return summary
