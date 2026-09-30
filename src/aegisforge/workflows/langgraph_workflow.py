from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

from langgraph.graph import END, StateGraph

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.agents.llm_planner import LLMPlannerAgent
from aegisforge.agents.planner import PlannerAgent
from aegisforge.agents.rag_agent import RAGAgent
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    EvaluationResult,
    EvaluationVerdict,
    ExecutionPlan,
    RequestStatus,
    RiskLevel,
)
from aegisforge.evaluation.critic import LLMCritic
from aegisforge.llm.providers import ModelProvider
from aegisforge.observability.metrics import (
    record_workflow_retry,
    track_workflow,
)
from aegisforge.observability.tracing import Tracer
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.tools.health import (
    build_planner_health_context,
    get_default_tool_health_tracker,
)
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry
from aegisforge.workflows.checkpoint import (
    WorkflowCheckpointer,
    get_checkpoint_store,
)
from aegisforge.workflows.evaluation import ResultEvaluator
from aegisforge.workflows.scheduler import (
    AgentFactory,
    ExecutionConfig,
    MultiAgentExecutor,
)

logger = logging.getLogger(__name__)

# Levels that require approval
_APPROVAL_REQUIRED_LEVELS = {"high", "critical"}

# Maximum evaluation/retry loops to prevent infinite cycles
_MAX_EVALUATION_LOOPS = 10

# Ordering of risk levels (server-side escalation checks)
_RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _task_effective_risk(task: dict[str, Any], registry: ToolRegistry | None) -> str:
    """Highest of the task's declared risk and its tool's server-side risk.

    Mirrors ``MultiAgentExecutor._effective_risk``: a planner (or a tampered
    checkpoint) can understate ``risk_level``, but the operator-controlled
    registry definition always wins, so a downgrade in the plan cannot skip
    the approval gate.
    """
    declared = task.get("risk_level", "low")
    risk = (declared.value if hasattr(declared, "value") else str(declared)).lower()
    if registry is None:
        return risk
    tool_name = str((task.get("input_data") or {}).get("tool_name", ""))
    if not tool_name:
        return risk
    tool = registry.get(tool_name)
    if tool is None:
        return risk
    tool_risk = str(tool.definition.risk_level).lower()
    if _RISK_RANK.get(tool_risk, 0) > _RISK_RANK.get(risk, 0):
        return tool_risk
    return risk


def _legacy_approval_gate(
    state: dict[str, Any],
    task: dict[str, Any],
    registry: ToolRegistry | None,
) -> dict[str, Any] | None:
    """Pause BEFORE executing a high-risk task in the serial (legacy) path.

    Returns the paused workflow state, or ``None`` when the task may run.

    WS6: the serial path used to gate approvals in ``retry_or_complete``
    AFTER the task had already executed — the approval could not stop an
    action that already happened.  The gate now runs pre-execution and
    fails CLOSED: if a required approval cannot be persisted, the workflow
    fails instead of running the protected action.
    """
    approval_service = _current_context().approval_service
    if approval_service is None:
        # No approval service configured — identical to the multi-agent
        # executor, which only gates when a service exists.
        return None

    task_id = str(task.get("task_id", ""))
    approved = set(state.get("approved_task_ids", []) or [])
    if task_id and task_id in approved:
        # Granted via approval resume — this task may run.
        return None

    risk = _task_effective_risk(task, registry)
    if risk not in _APPROVAL_REQUIRED_LEVELS:
        return None

    # Reuse a pending approval already created for THIS task; never adopt
    # one that belongs to a different task.
    approval_id = str(state.get("approval_id", ""))
    approval_task_id = str(state.get("approval_task_id", ""))
    if approval_id and approval_task_id not in ("", task_id):
        approval_id = ""

    if not approval_id:
        try:
            from aegisforge.domain.models import RiskLevel

            approval = approval_service.create_approval_request(
                job_id=state.get("job_id", "") or state.get("plan", {}).get("plan_id", ""),
                request_id=state.get("request_id", ""),
                workflow_id=state.get("workflow_id", ""),
                action_description=task.get("action_description")
                or task.get("description", "Task execution"),
                requested_by=state.get("user_id", "") or "system",
                organization_id=state.get("organization_id", ""),
                risk_level=RiskLevel(risk),
                reason=task.get("description", ""),
            )
            approval_id = approval.approval_id
        except Exception as exc:
            # Fail closed: approval required but not persisted → never run.
            logger.exception(
                "Could not create approval for high-risk task %s — failing workflow",
                task_id,
            )
            failure_message = (
                f"Approval required for high-risk task {task_id} "
                f"but could not be created: {exc}"
            )
            return _copy_state(
                state,
                status=RequestStatus.FAILED.value,
                errors=list(state.get("errors", [])) + [failure_message],
            )

    logger.info(
        "Workflow %s paused BEFORE executing high-risk task %s (risk=%s, approval=%s)",
        state.get("workflow_id", "unknown"),
        task_id,
        risk,
        approval_id,
    )
    return _copy_state(
        state,
        status=RequestStatus.ACTION_REQUIRES_APPROVAL.value,
        approval_required=True,
        approval_risk_level=risk,
        approval_action=task.get("action_description") or task.get("description", "Task execution"),
        approval_id=approval_id,
        approval_task_id=task_id,
        current_task=task,
    )


# --- Dependency injection via module-level context ---
# LangGraph nodes are plain functions; we store dependencies here
# and set them before workflow execution.


class _WorkflowContext:
    """Holds injected dependencies for the current workflow execution."""

    def __init__(self) -> None:
        self.model_provider: ModelProvider | None = None
        self.retrieval_service: RetrievalService | None = None
        self.critic: LLMCritic | None = None
        self.checkpointer: WorkflowCheckpointer | None = None
        self.approval_service: Any | None = None
        self.tracer: Tracer | None = None
        self.mcp_lifecycle: Any | None = None

    def reset(self) -> None:
        if self.mcp_lifecycle is not None:
            try:
                self.mcp_lifecycle.shutdown()
            except Exception as exc:
                logger.warning("Could not shut down MCP lifecycle: %s", exc)
        self.model_provider = None
        self.retrieval_service = None
        self.critic = None
        self.checkpointer = None
        self.approval_service = None
        self.tracer = None
        self.mcp_lifecycle = None


_ctx = _WorkflowContext()

# Phase 6G: per-execution workflow context override.
#
# The module-level ``_ctx`` singleton is shared mutable state: two
# concurrent synchronous workflow executions in the same process (e.g. the
# FastAPI threadpool running two requests) would clobber each other's
# checkpointer/model provider/MCP lifecycle, causing checkpoints and
# approvals to leak across workflows.  ``execute_workflow`` now publishes a
# ContextVar-scoped context for the duration of the run; graph nodes resolve
# dependencies from it first and fall back to ``_ctx`` only when unset
# (which preserves direct node-call usage in tests and external callers).
_execution_ctx: ContextVar[_WorkflowContext | None] = ContextVar(
    "aegisforge_execution_ctx", default=None
)


def _current_context() -> _WorkflowContext:
    """Resolve the active workflow context (execution-scoped, then global)."""
    override = _execution_ctx.get()
    return override if override is not None else _ctx


def _build_registry() -> ToolRegistry:
    """Create and populate the tool registry for this workflow."""
    registry = ToolRegistry()
    registry.register(KnowledgeSearchTool())
    from aegisforge.config import get_settings
    from aegisforge.mcp.lifecycle import configure_mcp_registry

    registry, _ctx.mcp_lifecycle = configure_mcp_registry(get_settings(), registry)
    return registry


def _build_planner_input(state: dict[str, Any]) -> dict[str, Any]:
    """Build the bounded planner input, including tool-health context (6F/6G).

    Side-effect free: health evidence is read from the process-wide tracker
    (fed by real executions); candidate alternatives come from a lightweight
    built-ins-only registry so planning never connects MCP servers.  MCP tool
    health still reaches the LLM planner via the rendered context block.

    Phase 6G: bounded circuit states are included so the deterministic
    planner avoids OPEN circuits the same way it avoids unavailable tools.
    Circuit data is read-only state (closed/open/half_open) — no errors, no
    payloads, identical for all tenants.
    """
    planner_input: dict[str, Any] = {"intent": state.get("intent", "")}
    try:
        snapshots = get_default_tool_health_tracker().list_health()
        planner_input["tool_health"] = [s.to_dict() for s in snapshots]
        planner_input["tool_health_context"] = build_planner_health_context(snapshots)
        # Phase 6G: bounded circuit-state context for planner selection.
        try:
            from aegisforge.tools.circuit_breaker import get_default_circuit_breaker

            planner_input["circuit_states"] = dict(
                get_default_circuit_breaker().list_states()
            )
        except Exception:  # circuit context must never break planning
            planner_input["circuit_states"] = {}
        lightweight = ToolRegistry()
        lightweight.register(KnowledgeSearchTool())
        grants = ["knowledge.search"]
        planner_input["available_tools"] = [
            name
            for name in lightweight.list_tool_names()
            if lightweight.validate_permissions(name, grants)
        ]
        # MCP server-level health (existing lifecycle evidence, read-only):
        # informational context for the LLM planner.  Kept distinct from
        # tool-level health — a healthy server does not imply healthy tools.
        lifecycle = _current_context().mcp_lifecycle
        if lifecycle is not None:
            servers = lifecycle.server_health_summary()
            if servers:
                rendered = "; ".join(
                    f"{s['server_id']}={s['state']}" for s in servers
                )
                suffix = f"MCP servers: {rendered}"
                block = str(planner_input["tool_health_context"])
                planner_input["tool_health_context"] = (
                    f"{block}\n{suffix}" if block else suffix
                )
    except Exception:  # planning must not fail on health context
        logger.warning("Failed to build tool-health planning context", exc_info=True)
        planner_input["tool_health"] = []
        planner_input["tool_health_context"] = ""
        planner_input["available_tools"] = []
    return planner_input


def _make_context(state: dict[str, Any]) -> AgentExecutionContext:
    """Build an AgentExecutionContext from the current workflow state."""
    return AgentExecutionContext(
        request_id=state.get("request_id", ""),
        workflow_id=state.get("workflow_id", ""),
        user_id=state.get("user_id", ""),
        organization_id=state.get("organization_id", ""),
        permissions=[
            PermissionSpec(name="knowledge.search", allow=True),
        ],
    )


def _copy_state(state: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a shallow copy of state with overrides applied."""
    result = dict(state)
    result.update(overrides)
    return result


def _save_checkpoint_if_available(
    checkpointer: WorkflowCheckpointer | None,
    node_name: str,
    state: dict[str, Any],
) -> str | None:
    """Save checkpoint if a checkpointer is available. Returns checkpoint ID."""
    if checkpointer is None:
        return None
    try:
        return checkpointer.save_after_node(node_name, state)
    except Exception as exc:
        logger.warning("Failed to save checkpoint for node %s: %s", node_name, exc)
        return None


def _plan_has_tasks(state: dict[str, Any]) -> bool:
    return bool(state.get("plan", {}).get("tasks"))


def _is_multi_agent_run(state: dict[str, Any]) -> bool:
    """A run uses the dependency-aware engine when its plan needs it.

    The engine is required when the plan carries explicit dependency edges,
    references between tasks, analysis/synthesis agents, or a run already
    produced ``task_records`` (resume).  Plain multi-task serial plans
    (e.g. legacy Phase 4.2 states without dependencies) keep the classic
    serial path so their semantics are preserved exactly.
    """
    if state.get("task_records"):
        return True
    tasks = state.get("plan", {}).get("tasks", [])
    for task in tasks:
        if task.get("dependencies"):
            return True
        if task.get("input_references") or task.get("evidence_from") or task.get("agent_outputs_from"):
            return True
        if str(task.get("assigned_agent_type", "")) in (
            AgentType.ANALYSIS.value,
            AgentType.SYNTHESIS.value,
        ):
            return True
    return False


# --- Graph Nodes ---


def validate_request_node(state: dict[str, Any]) -> dict[str, Any]:
    """Validate the incoming request has the required fields."""
    errors: list[str] = []

    if not state.get("request_id"):
        errors.append("Missing request_id")
    if not state.get("intent"):
        errors.append("Missing intent")

    if errors:
        return _copy_state(state, status=RequestStatus.FAILED.value, errors=errors)

    # When resuming from a checkpoint, do NOT reset accumulated state
    if state.get("resumed"):
        result = _copy_state(state, errors=list(state.get("errors", [])))
        _save_checkpoint_if_available(_current_context().checkpointer, "validate_request", result)
        return result

    result = _copy_state(
        state,
        status=RequestStatus.PLANNING.value,
        retry_count=0,
        max_retries=state.get("max_retries", 3),
        tool_calls=[],
        errors=[],
        evaluation_loop_count=0,
    )
    _save_checkpoint_if_available(_current_context().checkpointer, "validate_request", result)
    return result


def plan_node(state: dict[str, Any]) -> dict[str, Any]:
    """Generate an execution plan using LLM planner with deterministic fallback.

    F1: Uses LLMPlannerAgent when a ModelProvider is configured.
    Falls back to PlannerAgent (deterministic) on failure.
    """
    context = _make_context(state)

    # F1: Use LLM planner when model provider is available
    provider = _current_context().model_provider
    if provider is not None:
        planner: BaseAgent = LLMPlannerAgent(model_provider=provider)
        logger.info("Using LLM planner (provider=%s)", provider.provider_name)
    else:
        planner = PlannerAgent()
        logger.info("Using deterministic planner (no model provider configured)")

    # Resume-safe: reuse an existing plan instead of re-planning
    existing_tasks = state.get("plan", {}).get("tasks", [])
    if existing_tasks and state.get("resumed"):
        logger.info("Reusing existing plan with %d task(s) after resume", len(existing_tasks))
        result_state = _copy_state(
            state,
            status=RequestStatus.EXECUTING.value,
        )
        _save_checkpoint_if_available(_current_context().checkpointer, "plan", result_state)
        return result_state

    result = planner.execute(
        _build_planner_input(state),
        context,
    )

    if result.status.value != "completed":
        return _copy_state(
            state,
            status=RequestStatus.FAILED.value,
            errors=list(state.get("errors", [])) + result.errors,
        )

    result_state = _copy_state(
        state,
        plan=result.result,
        current_task_index=0,
        status=RequestStatus.EXECUTING.value,
    )
    _save_checkpoint_if_available(_current_context().checkpointer, "plan", result_state)
    return result_state


def execute_agent_node(state: dict[str, Any]) -> dict[str, Any]:
    """Execute the current task using the assigned agent.

    F2: Routes to RAGAgent when task type is 'rag' and retrieval_service is available.
    Falls back to ResearchAgent for other task types.
    """
    plan_dict = state.get("plan", {})
    tasks = plan_dict.get("tasks", [])
    task_index = state.get("current_task_index", 0)

    if task_index >= len(tasks):
        return _copy_state(
            state,
            status=RequestStatus.COMPLETED.value,
            errors=list(state.get("errors", [])) + ["No tasks remaining to execute"],
        )

    task = tasks[task_index]
    context = _make_context(state)
    context.task_id = task.get("task_id", "")

    # F2: Route to appropriate agent based on task type
    agent_type = task.get("assigned_agent_type", "research")

    # Build the tool registry once per node execution: the WS6 approval gate
    # reads server-side tool risk from it and the research agent executes
    # through it (one registry/MCP lifecycle per task, never two).
    tool_name = str((task.get("input_data") or {}).get("tool_name", ""))
    registry: ToolRegistry | None = None
    if tool_name or agent_type != "rag":
        registry = _build_registry()

    # WS6: pause BEFORE executing a high-risk task (the legacy post-execution
    # gate in retry_or_complete could not stop an action that already ran).
    gate_state = _legacy_approval_gate(state, task, registry)
    if gate_state is not None:
        # Persist the paused/failed state — resume loads it from here.
        _save_checkpoint_if_available(
            _current_context().checkpointer, "execute_agent", gate_state
        )
        return gate_state

    if agent_type == "rag" and _current_context().retrieval_service is not None:
        agent: BaseAgent = RAGAgent(retrieval_service=_current_context().retrieval_service)
        logger.info("Using RAGAgent for task %s", task.get("task_id", ""))
    else:
        agent = ResearchAgent(registry=registry or _build_registry())
        logger.info("Using ResearchAgent for task %s", task.get("task_id", ""))

    input_data = task.get("input_data", {})
    result = agent.execute(input_data, context)

    # Track tool calls (append to existing list)
    existing_tool_calls = list(state.get("tool_calls", []))
    existing_tool_calls.extend(result.tool_calls)

    result_state = _copy_state(
        state,
        agent_result=result.model_dump(),
        current_task=task,
        tool_calls=existing_tool_calls,
    )
    _save_checkpoint_if_available(_current_context().checkpointer, "execute_agent", result_state)
    return result_state


def _pick_final_agent_result(
    records: dict[str, Any],
    plan: ExecutionPlan,
) -> dict[str, Any] | None:
    """Choose the workflow's final agent result for evaluation/display.

    Prefers a completed synthesis task, then the last completed task in
    dependency order.  Returns None when nothing completed.
    """
    completed = [
        tid for tid, rec in records.items()
        if rec.get("status") == AgentExecutionStatus.COMPLETED.value
    ]
    if not completed:
        return None

    for tid in completed:
        if str(records[tid].get("agent_type", "")) == AgentType.SYNTHESIS.value:
            return records[tid]

    order: list[str] = []
    visited: set[str] = set()
    task_map = {t.task_id: t for t in plan.tasks}

    def _visit(tid: str) -> None:
        if tid in visited:
            return
        visited.add(tid)
        task = task_map.get(tid)
        for dep in (task.dependencies if task else []):
            _visit(dep)
        order.append(tid)

    for tid in completed:
        _visit(tid)
    for tid in reversed(order):
        if tid in completed:
            return records[tid]
    return records[completed[-1]]


def multi_agent_execute_node(state: dict[str, Any]) -> dict[str, Any]:
    """Execute a multi-task plan with the dependency-aware engine.

    Runs the whole plan (parallel waves where dependencies allow, per-task
    retries/timeouts, partial-failure policies), pauses for human approval
    at task gates when an approval service is configured, then hands the
    final result to the evaluation stage.
    """
    from aegisforge.config import get_settings

    plan_dict = state.get("plan", {})
    try:
        plan = ExecutionPlan(**plan_dict)
    except Exception as exc:
        return _copy_state(
            state,
            status=RequestStatus.FAILED.value,
            errors=list(state.get("errors", [])) + [f"Multi-agent plan invalid: {exc}"],
        )

    settings = get_settings()
    config = ExecutionConfig(
        max_concurrency=max(1, settings.max_parallel_tasks),
        default_task_timeout_seconds=settings.task_default_timeout_seconds,
        default_max_retries=settings.task_default_max_retries,
    )
    factory = AgentFactory(
        retrieval_service=_current_context().retrieval_service,
        model_provider=_current_context().model_provider,
        tool_registry=_build_registry(),
    )

    collected_audit: list[dict[str, Any]] = []

    def _audit(action: str, resource_type: str, meta: dict[str, Any]) -> None:
        collected_audit.append(
            {
                "action": action,
                "resource_type": resource_type,
                "resource_id": state.get("workflow_id", ""),
                "outcome": "success",
                "metadata": meta,
            }
        )

    def _checkpoint(snapshot: dict[str, Any]) -> None:
        if _current_context().checkpointer is None:
            return
        full_snapshot = dict(state)
        full_snapshot["task_records"] = snapshot.get("task_records", {})
        full_snapshot["errors"] = list(state.get("errors", [])) + list(snapshot.get("errors", []))
        _save_checkpoint_if_available(_current_context().checkpointer, "multi_agent_execute", full_snapshot)

    approved_task_ids = set(state.get("approved_task_ids", []) or [])
    executor = MultiAgentExecutor(
        config=config,
        agent_factory=factory,
        approval_service=_current_context().approval_service,
        checkpoint_callback=_checkpoint,
        audit_callback=_audit,
        tracer=_current_context().tracer,
    )

    try:
        outcome = executor.execute(
            plan,
            request_id=state.get("request_id", ""),
            workflow_id=state.get("workflow_id", ""),
            organization_id=state.get("organization_id", ""),
            user_id=state.get("user_id", ""),
            intent=state.get("intent", ""),
            job_id=state.get("job_id", ""),
            preexisting_records=state.get("task_records", {}) or {},
            approved_task_ids=approved_task_ids,
        )
    except Exception as exc:
        logger.exception("Multi-agent execution failed for workflow %s", state.get("workflow_id"))
        return _copy_state(
            state,
            status=RequestStatus.FAILED.value,
            errors=list(state.get("errors", [])) + [f"Multi-agent execution error: {exc}"],
            audit_events=list(state.get("audit_events", [])) + collected_audit,
        )

    records = outcome.record_dicts
    tool_calls: list[dict[str, Any]] = []
    for rec in records.values():
        tool_calls.extend(rec.get("tool_calls", []) or [])
    existing_tool_calls = list(state.get("tool_calls", []))
    all_tool_calls = existing_tool_calls + tool_calls

    # Paused for human approval at a task boundary.
    if outcome.status == RequestStatus.ACTION_REQUIRES_APPROVAL:
        result_state = _copy_state(
            state,
            status=RequestStatus.ACTION_REQUIRES_APPROVAL.value,
            approval_required=True,
            approval_risk_level="high",
            approval_id=outcome.approval_id,
            approval_task_id=outcome.approval_task_id,
            approval_action=next(
                (t.description for t in plan.tasks if t.task_id == outcome.approval_task_id),
                "Multi-agent task execution",
            ),
            task_records=records,
            tool_calls=all_tool_calls,
            audit_events=list(state.get("audit_events", [])) + collected_audit,
            errors=list(state.get("errors", [])) + outcome.errors,
        )
        _save_checkpoint_if_available(_current_context().checkpointer, "multi_agent_execute", result_state)
        return result_state

    # Final agent result for evaluation/display.
    final_record = _pick_final_agent_result(records, plan)
    if final_record is None:
        result_state = _copy_state(
            state,
            status=RequestStatus.FAILED.value,
            agent_result={
                "agent_name": "",
                "agent_type": "",
                "status": AgentExecutionStatus.FAILED.value,
                "summary": "All tasks failed; no final result produced",
                "result": {},
                "errors": outcome.errors,
            },
            task_records=records,
            multi_agent_summary=outcome.summary.model_dump(),
            tool_calls=all_tool_calls,
            audit_events=list(state.get("audit_events", [])) + collected_audit,
            errors=list(state.get("errors", [])) + outcome.errors,
        )
        _save_checkpoint_if_available(_current_context().checkpointer, "multi_agent_execute", result_state)
        return result_state

    answer = final_record.get("output", {}).get("answer", final_record.get("summary", ""))
    final_errors = (
        outcome.errors
        if outcome.status == RequestStatus.FAILED
        else []
    )
    agent_result = {
        "agent_name": final_record.get("task_id", ""),
        "agent_type": final_record.get("agent_type", "synthesis"),
        "status": (
            AgentExecutionStatus.FAILED.value
            if outcome.status == RequestStatus.FAILED
            else AgentExecutionStatus.COMPLETED.value
        ),
        "summary": final_record.get("summary", ""),
        "result": {
            "query": state.get("intent", ""),
            "answer": answer,
            "task_id": final_record.get("task_id", ""),
            "citations": final_record.get("output", {}).get("citations", []),
            "failed_upstream": final_record.get("output", {}).get("failed_upstream", []),
        },
        "evidence": final_record.get("evidence", []),
        "tool_calls": final_record.get("tool_calls", []),
        "errors": final_errors,
        "confidence": final_record.get("output", {}).get("confidence"),
    }

    # A partially-failed run is failed: its (possibly partial) result is kept
    # for reporting, but the run never silently claims full completion.
    run_status = (
        RequestStatus.FAILED.value
        if outcome.status == RequestStatus.FAILED
        else RequestStatus.EVALUATING.value
    )
    result_state = _copy_state(
        state,
        status=run_status,
        agent_result=agent_result,
        current_task_index=max(len(plan.tasks) - 1, 0),
        task_records=records,
        multi_agent_summary=outcome.summary.model_dump(),
        tool_calls=all_tool_calls,
        audit_events=list(state.get("audit_events", [])) + collected_audit,
        errors=list(state.get("errors", [])) + outcome.errors,
    )
    # Workflow-level evaluation (planning/collaboration/final-response).
    try:
        from aegisforge.evaluation.workflow_evaluator import evaluate_workflow_run
        from aegisforge.observability.metrics import record_workflow_evaluation_score

        wf_eval = evaluate_workflow_run(
            plan,
            records,
            final_output=agent_result.get("result") or {},
        )
        result_state["workflow_evaluation"] = wf_eval.to_dict()
        try:
            verdict = "failed" if run_status == RequestStatus.FAILED.value else "passed"
            record_workflow_evaluation_score(verdict, wf_eval.overall_score)
        except Exception:  # noqa: S110 - observability must never break execution
            pass
    except Exception as exc:
        logger.warning("Workflow evaluation failed (non-fatal): %s", exc)
    _save_checkpoint_if_available(_current_context().checkpointer, "multi_agent_execute", result_state)
    return result_state


def route_after_plan(state: dict[str, Any]) -> str:
    """Multi-task plans go through the dependency engine; single tasks stay
    on the classic serial path."""
    if _is_multi_agent_run(state):
        return "multi_agent"
    return "execute_agent"


def route_after_multi_agent(state: dict[str, Any]) -> str:
    """Route from the multi-agent node based on resulting status."""
    status = state.get("status", "")
    if status in (RequestStatus.COMPLETED.value, RequestStatus.FAILED.value):
        return "end"
    if status == RequestStatus.EVALUATING.value:
        return "evaluate"
    if status == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
        return "end"
    return "end"


def evaluate_node(state: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the agent result.

    Phase 5.3D/5.3G: Uses deterministic ResultEvaluator + optional LLM Critic,
    with evidence-aware assessment and structured evaluation decisions.
    """
    agent_result_dict = state.get("agent_result", {})

    if not agent_result_dict:
        return _copy_state(
            state,
            evaluation=EvaluationResult(
                verdict=EvaluationVerdict.FAILED,
                score=0.0,
                reasons=["No agent result to evaluate"],
            ).model_dump(),
            status=RequestStatus.EVALUATING.value,
        )

    agent_result = AgentResult(**agent_result_dict)

    # Deterministic evaluation (always runs)
    evaluator = ResultEvaluator()
    evaluation = evaluator.evaluate(agent_result, expected_fields=["query", "answer"])

    # Phase 5.3C: Evidence-aware evaluation
    evidence_assessment: dict[str, Any] = {}
    try:
        from aegisforge.evaluation.evidence import classify_evidence_quality
        evidence_items = []
        # Gather evidence from agent result
        for item in (agent_result.result or {}).get("evidence", []):
            if isinstance(item, dict):
                evidence_items.append(item)
        for item in agent_result.evidence:
            if isinstance(item, dict):
                evidence_items.append(item)
        if evidence_items:
            e_assessment = classify_evidence_quality(evidence_items, query=state.get("intent", ""))
            evidence_assessment = e_assessment.to_dict()
            # If evidence is insufficient, penalize the evaluation score
            if not e_assessment.evidence_sufficient and evaluation.score > 0.3:
                evaluation.score *= 0.85
                evaluation.reasons.append("Evidence quality is insufficient for high confidence")
    except Exception as exc:
        logger.debug("Evidence assessment failed (non-fatal): %s", exc)

    # Phase 5.3D: LLM critic (optional, when provider is configured)
    critic_details: dict[str, Any] = {}
    critic = _current_context().critic
    if critic is not None:
        try:
            # Build evidence context for the critic
            evidence_text = ""
            for item in (agent_result.result or {}).get("citations", []):
                if isinstance(item, dict):
                    evidence_text += f"\n- {item.get('source', 'unknown')}: {str(item.get('snippet', ''))[:200]}"

            critic_result = critic.evaluate_response(
                query=state.get("intent", ""),
                response_text=agent_result.result.get("answer", agent_result.summary),
                evidence=evidence_text[:2000],
            )
            critic_details = {
                "critic_score": critic_result.score,
                "critic_verdict": critic_result.verdict,
                "critic_reasoning": critic_result.reasoning,
                "critic_failures": critic_result.failures,
                "deterministic_score": evaluation.score,
            }
            if critic_result.verdict == "failed":
                # LLM critic says the response is bad — override to RETRY if possible
                evaluation = EvaluationResult(
                    verdict=EvaluationVerdict.RETRY,
                    score=critic_result.score,
                    reasons=critic_result.failures or ["LLM critic flagged response quality"],
                    retryable=True,
                    details={
                        **critic_details,
                        "evidence_assessment": evidence_assessment,
                    },
                )
                logger.info(
                    "LLM critic flagged response (score=%.2f), overriding to RETRY",
                    critic_result.score,
                )
            else:
                logger.debug(
                    "LLM critic passed (score=%.2f, verdict=%s)",
                    critic_result.score,
                    critic_result.verdict,
                )
        except Exception as exc:
            logger.warning("LLM critic evaluation failed (non-fatal): %s", exc)

    # Phase 5.3G: Attach intelligence observability metadata
    if not evaluation.details:
        evaluation.details = {}
    evaluation.details["evidence_assessment"] = evidence_assessment
    if critic_details:
        evaluation.details["critic"] = critic_details

    result_state = _copy_state(
        state,
        evaluation=evaluation.model_dump(),
        status=RequestStatus.EVALUATING.value,
    )
    _save_checkpoint_if_available(_current_context().checkpointer, "evaluate", result_state)
    return result_state


def retry_or_complete_node(state: dict[str, Any]) -> dict[str, Any]:
    """Decide whether to retry, check for approval, or move to the next task.

    Phase 5.3E/5.3G: Adds structured recovery decisions and intelligence
    observability (retry reasoning, failure analysis).
    """
    eval_dict = state.get("evaluation", {})
    evaluation = EvaluationResult(**eval_dict)
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)
    task_index = state.get("current_task_index", 0)
    plan_dict = state.get("plan", {})
    tasks = plan_dict.get("tasks", [])

    # Prevent infinite evaluation loops
    eval_loop_count = state.get("evaluation_loop_count", 0) + 1
    if eval_loop_count > _MAX_EVALUATION_LOOPS:
        logger.warning(
            "Max evaluation loops (%d) reached for workflow %s",
            _MAX_EVALUATION_LOOPS,
            state.get("workflow_id", "unknown"),
        )
        return _copy_state(
            state,
            status=RequestStatus.FAILED.value,
            errors=list(state.get("errors", [])) + [
                f"Max evaluation loops ({_MAX_EVALUATION_LOOPS}) exceeded"
            ],
            evaluation_loop_count=eval_loop_count,
        )

    # Phase 5.3G: Intelligence decision log
    recovery_decision: dict[str, Any] = {
        "eval_loop_count": eval_loop_count,
        "verdict": evaluation.verdict.value if hasattr(evaluation.verdict, 'value') else str(evaluation.verdict),
        "score": evaluation.score,
        "retry_count": retry_count,
    }

    if evaluation.verdict == EvaluationVerdict.PASSED:
        # Check if this task requires approval before moving on
        task = state.get("current_task", {})
        raw_risk = task.get("risk_level", "low")
        risk_level = raw_risk.value if hasattr(raw_risk, "value") else str(raw_risk)
        approved_ids = set(state.get("approved_task_ids", []) or [])
        current_task_id = str(task.get("task_id", ""))
        if (
            risk_level.lower() in _APPROVAL_REQUIRED_LEVELS
            and current_task_id not in approved_ids
        ):
            # Persist an approval request via the DB-backed service
            approval_id = state.get("approval_id", "")
            approval_service = _current_context().approval_service
            if approval_service is not None and not approval_id:
                try:
                    approval = approval_service.create_approval_request(
                        job_id=state.get("job_id", ""),
                        request_id=state.get("request_id", ""),
                        workflow_id=state.get("workflow_id", ""),
                        action_description=task.get("action_description") or task.get("description", "Task execution"),
                        requested_by=state.get("user_id", ""),
                        organization_id=state.get("organization_id", ""),
                        risk_level=RiskLevel(risk_level),
                        reason=task.get("description", ""),
                    )
                    approval_id = approval.approval_id
                    recovery_decision["action"] = "approval_required"
                    recovery_decision["approval_id"] = approval_id
                    logger.info(
                        "Workflow %s paused: created approval %s (risk=%s)",
                        state.get("workflow_id", ""),
                        approval_id,
                        risk_level,
                    )
                except Exception as exc:
                    logger.warning("Failed to create approval request: %s", exc)

            result_state = _copy_state(
                state,
                status=RequestStatus.ACTION_REQUIRES_APPROVAL.value,
                approval_required=True,
                approval_risk_level=risk_level,
                approval_action=task.get("action_description", "Task execution"),
                approval_id=approval_id,
                evaluation_loop_count=eval_loop_count,
                recovery_decision=recovery_decision,
            )
            _save_checkpoint_if_available(_current_context().checkpointer, "retry_or_complete", result_state)
            return result_state

        # Move to next task or complete
        next_index = task_index + 1
        if next_index >= len(tasks):
            recovery_decision["action"] = "workflow_completed"
            result_state = _copy_state(
                state,
                status=RequestStatus.COMPLETED.value,
                final_result=state.get("agent_result", {}),
                current_task_index=next_index,
                evaluation_loop_count=eval_loop_count,
                recovery_decision=recovery_decision,
            )
            _save_checkpoint_if_available(_current_context().checkpointer, "retry_or_complete", result_state)
            return result_state
        recovery_decision["action"] = "advance_to_next_task"
        recovery_decision["next_task_index"] = next_index
        result_state = _copy_state(
            state,
            current_task_index=next_index,
            status=RequestStatus.EXECUTING.value,
            retry_count=0,
            evaluation_loop_count=eval_loop_count,
            recovery_decision=recovery_decision,
        )
        _save_checkpoint_if_available(_current_context().checkpointer, "retry_or_complete", result_state)
        return result_state

    if evaluation.verdict == EvaluationVerdict.RETRY and retry_count < max_retries:
        record_workflow_retry()
        recovery_decision["action"] = "retry"
        recovery_decision["reasons"] = evaluation.reasons
        recovery_decision["next_attempt"] = retry_count + 1
        logger.info(
            "Retrying task (attempt %d/%d): %s",
            retry_count + 1,
            max_retries,
            evaluation.reasons,
        )
        result_state = _copy_state(
            state,
            retry_count=retry_count + 1,
            status=RequestStatus.RETRYING.value,
            evaluation_loop_count=eval_loop_count,
            recovery_decision=recovery_decision,
        )
        _save_checkpoint_if_available(_current_context().checkpointer, "retry_or_complete", result_state)
        return result_state

    # Terminal failure
    recovery_decision["action"] = "terminal_failure"
    recovery_decision["reasons"] = evaluation.reasons
    result_state = _copy_state(
        state,
        status=RequestStatus.FAILED.value,
        errors=list(state.get("errors", [])) + [
            f"Evaluation failed: {'; '.join(evaluation.reasons)}"
        ],
        evaluation_loop_count=eval_loop_count,
        recovery_decision=recovery_decision,
    )
    _save_checkpoint_if_available(_current_context().checkpointer, "retry_or_complete", result_state)
    return result_state


def should_continue(state: dict[str, Any]) -> str:
    """Conditional edge: route based on status."""
    status = state.get("status", "")

    if status in (RequestStatus.COMPLETED.value, RequestStatus.FAILED.value):
        return "end"
    if status == RequestStatus.EVALUATING.value:
        return "retry_or_complete"
    if status == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
        return "end"  # Pause here; resume will re-enter
    if status in (RequestStatus.RETRYING.value, RequestStatus.EXECUTING.value):
        return "execute_agent"
    return "end"


def _traced_node(fn: Any, name: str) -> Any:
    """Wrap a graph node with an OpenTelemetry-style trace span."""

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        tracer = _current_context().tracer
        if tracer is None:
            return fn(state)
        with tracer.span(name):
            return fn(state)

    wrapped.__name__ = f"{name}_traced"
    return wrapped


def route_after_execute(state: dict[str, Any]) -> str:
    """Conditional edge after execute_agent.

    WS6: when the serial path's pre-execution approval gate paused the run,
    the workflow must END at the pause — flowing into ``evaluate`` would
    overwrite the ACTION_REQUIRES_APPROVAL status with a terminal failure.
    """
    if state.get("status") == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
        return "end"
    return "evaluate"


def build_execution_graph() -> StateGraph:
    """Build the LangGraph workflow graph.

    Multi-task (multi-agent) plans route through the dependency-aware
    scheduler; single-task plans keep the classic linear path so that
    Phase 0–4 behavior is unchanged.
    """
    graph = StateGraph(dict)  # type: ignore[type-var]

    # Add nodes (wrapped with tracing when a tracer is active)
    graph.add_node("validate_request", _traced_node(validate_request_node, "validate_request"))
    graph.add_node("plan", _traced_node(plan_node, "plan"))
    graph.add_node("execute_agent", _traced_node(execute_agent_node, "execute_agent"))
    graph.add_node("multi_agent_execute", _traced_node(multi_agent_execute_node, "multi_agent_execute"))
    graph.add_node("evaluate", _traced_node(evaluate_node, "evaluate"))
    graph.add_node("retry_or_complete", _traced_node(retry_or_complete_node, "retry_or_complete"))

    # Entry point
    graph.set_entry_point("validate_request")

    # Linear edges
    graph.add_edge("validate_request", "plan")
    graph.add_conditional_edges(
        "execute_agent",
        route_after_execute,
        {
            "evaluate": "evaluate",
            "end": END,
        },
    )
    graph.add_edge("evaluate", "retry_or_complete")

    # After planning, route multi-task plans to the dependency engine.
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "execute_agent": "execute_agent",
            "multi_agent": "multi_agent_execute",
        },
    )

    # After the multi-agent engine finishes.
    graph.add_conditional_edges(
        "multi_agent_execute",
        route_after_multi_agent,
        {
            "evaluate": "evaluate",
            "end": END,
        },
    )

    # Conditional edge from retry_or_complete
    graph.add_conditional_edges(
        "retry_or_complete",
        should_continue,
        {
            "execute_agent": "execute_agent",
            "end": END,
        },
    )

    return graph


# Nodes that should be checkpointed
_CHECKPOINTABLE_NODES = {
    "validate_request",
    "plan",
    "execute_agent",
    "multi_agent_execute",
    "evaluate",
    "retry_or_complete",
}


def _build_wrapped_node(
    original_fn: Any,
    node_name: str,
    checkpointer: WorkflowCheckpointer | None,
) -> Any:
    """Wrap a node function to add checkpointing."""
    if checkpointer is None:
        return original_fn

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        result = original_fn(state)
        _save_checkpoint_if_available(checkpointer, node_name, result)
        return result

    wrapped.__name__ = f"{node_name}_checkpointed"
    return wrapped


def execute_workflow(
    request_id: str,
    intent: str,
    user_id: str = "",
    organization_id: str = "",
    workflow_id: str | None = None,
    checkpointer: WorkflowCheckpointer | None = None,
    resume_from_checkpoint: bool = False,
    model_provider: ModelProvider | None = None,
    retrieval_service: RetrievalService | None = None,
    approval_service: Any | None = None,
    job_id: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """Execute the full workflow and return the final state.

    This is the main entry point for running a workflow.

    Args:
        request_id: The request ID.
        intent: The user intent.
        user_id: The user who initiated the request.
        organization_id: The organization ID.
        workflow_id: Optional workflow ID (generated if not provided).
        checkpointer: Optional checkpointer for durable execution.
        resume_from_checkpoint: If True, resume from the latest checkpoint.
        model_provider: Optional LLM provider for planner and critic.
        retrieval_service: Optional RAG retrieval service.
        approval_service: Optional approval service for persisting approvals.
        job_id: The execution job ID (used for approval persistence).
        trace_id: Correlation/trace ID propagated from the API layer.
    """
    workflow_id = workflow_id or f"wf-{uuid.uuid4().hex[:12]}"

    # Phase 6G: execution-scoped context (ContextVar).  Concurrent synchronous
    # executions in one process no longer share the module-level singleton.
    # _ctx is ALSO populated for backwards compatibility: direct graph-node
    # calls (tests, external callers) without an execution scope still work.
    exec_ctx = _WorkflowContext()
    exec_ctx.model_provider = model_provider
    exec_ctx.retrieval_service = retrieval_service
    exec_ctx.critic = LLMCritic(model_provider=model_provider) if model_provider else None
    exec_ctx.approval_service = approval_service
    exec_ctx.tracer = Tracer(request_id=request_id, workflow_id=workflow_id)
    if trace_id:
        exec_ctx.tracer.context.request_id = trace_id
    _ctx.model_provider = model_provider
    _ctx.retrieval_service = retrieval_service
    _ctx.critic = exec_ctx.critic
    _ctx.approval_service = approval_service
    _ctx.tracer = exec_ctx.tracer

    token = _execution_ctx.set(exec_ctx)
    try:
        with track_workflow() as wf_meta:
            final_state = _execute_workflow_inner(
                request_id, intent, user_id, organization_id,
                workflow_id, checkpointer, resume_from_checkpoint,
                job_id=job_id,
            )
            wf_meta["status"] = final_state.get("status", "failed")

        # Attach trace summary (sanitized) to the final state
        try:
            final_state["trace_summary"] = exec_ctx.tracer.log_summary()
        except Exception as exc:
            logger.warning("Could not build trace summary: %s", exc)
        return final_state
    finally:
        _execution_ctx.reset(token)
        # Reset the legacy global ONLY if it still matches this run (a nested
        # or racing execute_workflow call may have re-populated it).
        if _ctx.tracer is exec_ctx.tracer:
            _ctx.reset()


def _execute_workflow_inner(
    request_id: str,
    intent: str,
    user_id: str = "",
    organization_id: str = "",
    workflow_id: str | None = None,
    checkpointer: WorkflowCheckpointer | None = None,
    resume_from_checkpoint: bool = False,
    job_id: str = "",
) -> dict[str, Any]:
    """Internal workflow execution after context is set."""
    workflow_id = workflow_id or f"wf-{uuid.uuid4().hex[:12]}"

    # Try to resume from checkpoint
    if resume_from_checkpoint and checkpointer is not None:
        resumed_state = checkpointer.load_resume_state()
        if resumed_state is not None:
            logger.info(
                "Resuming workflow %s from checkpoint (node=%s)",
                workflow_id,
                checkpointer.get_current_node(),
            )
            if resumed_state.get("status") == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
                return _resume_after_approval(resumed_state, workflow_id, checkpointer)
            # Non-approval resume (e.g. failure recovery): continue from checkpoint
            resumed_state["resumed"] = True
            return _execute_with_state(resumed_state, workflow_id, checkpointer)

    initial_state: dict[str, Any] = {
        "request_id": request_id,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "organization_id": organization_id,
        "job_id": job_id,
        "intent": intent,
        "status": RequestStatus.CREATED.value,
        "plan": {},
        "current_task_index": 0,
        "current_task": {},
        "agent_result": {},
        "evaluation": {},
        "retry_count": 0,
        "max_retries": 3,
        "tool_calls": [],
        "errors": [],
        "final_result": {},
        "evaluation_loop_count": 0,
    }

    _save_checkpoint_if_available(checkpointer, "start", initial_state)
    return _execute_with_state(initial_state, workflow_id, checkpointer)


def _resume_after_approval(
    state: dict[str, Any],
    workflow_id: str,
    checkpointer: WorkflowCheckpointer | None,
) -> dict[str, Any]:
    """Continue a workflow after an approval decision.

    Multi-agent runs: the gated task has NOT executed yet (the engine pauses
    *before* running a task that needs approval).  We mark that task as
    approved and let the dependency engine continue from the checkpoint,
    skipping tasks that already completed.

    Legacy (serial) runs: the approval gate also pauses BEFORE execution
    (WS6), so the approved task has NOT run yet.  Mark it approved and
    re-enter the graph so execute_agent runs it exactly once; the graph
    skips tasks that already completed via status checks and the
    approved_task_ids set.
    """
    state["status"] = RequestStatus.EXECUTING.value
    state["approval_required"] = False
    state["approval_resolved"] = True
    state["resumed"] = True
    state.pop("approval_id", None)

    if _is_multi_agent_run(state):
        approved = set(state.get("approved_task_ids", []) or [])
        gated_task_id = state.get("approval_task_id", "")
        if gated_task_id:
            approved.add(gated_task_id)
        state["approved_task_ids"] = sorted(approved)
        state.pop("approval_task_id", None)
        return _execute_with_state(state, workflow_id, checkpointer)

    # Legacy serial path: grant approval to the task at the current index
    # (it paused pre-execution) and re-enter the graph to run it.
    tasks = state.get("plan", {}).get("tasks", [])
    task_index = int(state.get("current_task_index", 0))
    approved = set(state.get("approved_task_ids", []) or [])
    if 0 <= task_index < len(tasks):
        approved.add(str(tasks[task_index].get("task_id", "")))
    state["approved_task_ids"] = sorted(approved)
    state.pop("approval_task_id", None)
    state["current_task"] = {}
    return _execute_with_state(state, workflow_id, checkpointer)


def _execute_with_state(
    state: dict[str, Any],
    workflow_id: str,
    checkpointer: WorkflowCheckpointer | None = None,
) -> dict[str, Any]:
    """Execute the workflow starting from a given state."""
    # Store the checkpointer in BOTH the execution-scoped context (correct
    # under concurrency) and the legacy global (direct node-call compat).
    ctx = _current_context()
    ctx.checkpointer = checkpointer
    _ctx.checkpointer = checkpointer

    graph = build_execution_graph()
    compiled = graph.compile()
    final_state = compiled.invoke(state)

    return final_state


def resume_workflow_after_approval(
    workflow_id: str,
    approval_decision: str,
    approval_id: str = "",
    reviewer_id: str = "",
    store: Any | None = None,
    request_id: str = "",
    organization_id: str = "",
) -> dict[str, Any]:
    """Resume a workflow after an approval decision."""
    store = store or get_checkpoint_store()
    checkpointer = WorkflowCheckpointer(
        workflow_id=workflow_id,
        request_id=request_id,
        organization_id=organization_id,
        store=store,
    )

    state = checkpointer.load_resume_state()
    if state is None:
        logger.error("No checkpoint found for workflow %s", workflow_id)
        return {
            "workflow_id": workflow_id,
            "status": "failed",
            "errors": [f"No checkpoint found for workflow {workflow_id}"],
        }

    if approval_decision == "rejected":
        return {
            "workflow_id": workflow_id,
            "request_id": state.get("request_id", ""),
            "status": RequestStatus.FAILED.value,
            "errors": [f"Approval rejected by {reviewer_id} (approval: {approval_id})"],
            "final_result": {},
        }

    logger.info(
        "Resuming workflow %s after approval (reviewer=%s, approval=%s)",
        workflow_id,
        reviewer_id,
        approval_id,
    )

    return _resume_after_approval(state, workflow_id, checkpointer)
