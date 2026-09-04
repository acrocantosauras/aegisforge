from __future__ import annotations

import logging
import uuid
from typing import Any

from langgraph.graph import END, StateGraph

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.agents.llm_planner import LLMPlannerAgent
from aegisforge.agents.planner import PlannerAgent
from aegisforge.agents.rag_agent import RAGAgent
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.domain.models import (
    AgentResult,
    EvaluationResult,
    EvaluationVerdict,
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
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry
from aegisforge.workflows.checkpoint import (
    WorkflowCheckpointer,
    get_checkpoint_store,
)
from aegisforge.workflows.evaluation import ResultEvaluator

logger = logging.getLogger(__name__)

# Levels that require approval
_APPROVAL_REQUIRED_LEVELS = {"high", "critical"}

# Maximum evaluation/retry loops to prevent infinite cycles
_MAX_EVALUATION_LOOPS = 10


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

    def reset(self) -> None:
        self.model_provider = None
        self.retrieval_service = None
        self.critic = None
        self.checkpointer = None
        self.approval_service = None
        self.tracer = None


_ctx = _WorkflowContext()


def _build_registry() -> ToolRegistry:
    """Create and populate the tool registry for this workflow."""
    registry = ToolRegistry()
    registry.register(KnowledgeSearchTool())
    return registry


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
        _save_checkpoint_if_available(_ctx.checkpointer, "validate_request", result)
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
    _save_checkpoint_if_available(_ctx.checkpointer, "validate_request", result)
    return result


def plan_node(state: dict[str, Any]) -> dict[str, Any]:
    """Generate an execution plan using LLM planner with deterministic fallback.

    F1: Uses LLMPlannerAgent when a ModelProvider is configured.
    Falls back to PlannerAgent (deterministic) on failure.
    """
    context = _make_context(state)

    # F1: Use LLM planner when model provider is available
    if _ctx.model_provider is not None:
        planner: BaseAgent = LLMPlannerAgent(model_provider=_ctx.model_provider)
        logger.info("Using LLM planner (provider=%s)", _ctx.model_provider.provider_name)
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
        _save_checkpoint_if_available(_ctx.checkpointer, "plan", result_state)
        return result_state

    result = planner.execute(
        {"intent": state.get("intent", "")},
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
    _save_checkpoint_if_available(_ctx.checkpointer, "plan", result_state)
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

    if agent_type == "rag" and _ctx.retrieval_service is not None:
        agent: BaseAgent = RAGAgent(retrieval_service=_ctx.retrieval_service)
        logger.info("Using RAGAgent for task %s", task.get("task_id", ""))
    else:
        registry = _build_registry()
        agent = ResearchAgent(registry=registry)
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
    _save_checkpoint_if_available(_ctx.checkpointer, "execute_agent", result_state)
    return result_state


def evaluate_node(state: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the agent result.

    F8: Uses deterministic ResultEvaluator + optional LLM Critic.
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

    # F8: LLM critic (optional, when provider is configured)
    if _ctx.critic is not None:
        try:
            critic_result = _ctx.critic.evaluate_response(
                query=state.get("intent", ""),
                response_text=agent_result.result.get("answer", agent_result.summary),
            )
            if critic_result.verdict == "failed":
                # LLM critic says the response is bad — override to RETRY if possible
                evaluation = EvaluationResult(
                    verdict=EvaluationVerdict.RETRY,
                    score=critic_result.score,
                    reasons=critic_result.failures or ["LLM critic flagged response quality"],
                    retryable=True,
                    details={
                        "critic_score": critic_result.score,
                        "critic_reasoning": critic_result.reasoning,
                        "deterministic_score": evaluation.score,
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

    result_state = _copy_state(
        state,
        evaluation=evaluation.model_dump(),
        status=RequestStatus.EVALUATING.value,
    )
    _save_checkpoint_if_available(_ctx.checkpointer, "evaluate", result_state)
    return result_state


def retry_or_complete_node(state: dict[str, Any]) -> dict[str, Any]:
    """Decide whether to retry, check for approval, or move to the next task."""
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

    if evaluation.verdict == EvaluationVerdict.PASSED:
        # Check if this task requires approval before moving on
        task = state.get("current_task", {})
        raw_risk = task.get("risk_level", "low")
        risk_level = raw_risk.value if hasattr(raw_risk, "value") else str(raw_risk)
        if risk_level.lower() in _APPROVAL_REQUIRED_LEVELS:
            # Persist an approval request via the DB-backed service
            approval_id = state.get("approval_id", "")
            if _ctx.approval_service is not None and not approval_id:
                try:
                    approval = _ctx.approval_service.create_approval_request(
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
            )
            _save_checkpoint_if_available(_ctx.checkpointer, "retry_or_complete", result_state)
            return result_state

        # Move to next task or complete
        next_index = task_index + 1
        if next_index >= len(tasks):
            result_state = _copy_state(
                state,
                status=RequestStatus.COMPLETED.value,
                final_result=state.get("agent_result", {}),
                current_task_index=next_index,
                evaluation_loop_count=eval_loop_count,
            )
            _save_checkpoint_if_available(_ctx.checkpointer, "retry_or_complete", result_state)
            return result_state
        result_state = _copy_state(
            state,
            current_task_index=next_index,
            status=RequestStatus.EXECUTING.value,
            retry_count=0,
            evaluation_loop_count=eval_loop_count,
        )
        _save_checkpoint_if_available(_ctx.checkpointer, "retry_or_complete", result_state)
        return result_state

    if evaluation.verdict == EvaluationVerdict.RETRY and retry_count < max_retries:
        record_workflow_retry()
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
        )
        _save_checkpoint_if_available(_ctx.checkpointer, "retry_or_complete", result_state)
        return result_state

    # Terminal failure
    result_state = _copy_state(
        state,
        status=RequestStatus.FAILED.value,
        errors=list(state.get("errors", [])) + [
            f"Evaluation failed: {'; '.join(evaluation.reasons)}"
        ],
        evaluation_loop_count=eval_loop_count,
    )
    _save_checkpoint_if_available(_ctx.checkpointer, "retry_or_complete", result_state)
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
        tracer = _ctx.tracer
        if tracer is None:
            return fn(state)
        with tracer.span(name):
            return fn(state)

    wrapped.__name__ = f"{name}_traced"
    return wrapped


def build_execution_graph() -> StateGraph:
    """Build the LangGraph workflow graph."""
    graph = StateGraph(dict)  # type: ignore[type-var]

    # Add nodes (wrapped with tracing when a tracer is active)
    graph.add_node("validate_request", _traced_node(validate_request_node, "validate_request"))
    graph.add_node("plan", _traced_node(plan_node, "plan"))
    graph.add_node("execute_agent", _traced_node(execute_agent_node, "execute_agent"))
    graph.add_node("evaluate", _traced_node(evaluate_node, "evaluate"))
    graph.add_node("retry_or_complete", _traced_node(retry_or_complete_node, "retry_or_complete"))

    # Entry point
    graph.set_entry_point("validate_request")

    # Linear edges
    graph.add_edge("validate_request", "plan")
    graph.add_edge("plan", "execute_agent")
    graph.add_edge("execute_agent", "evaluate")
    graph.add_edge("evaluate", "retry_or_complete")

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
_CHECKPOINTABLE_NODES = {"validate_request", "plan", "execute_agent", "evaluate", "retry_or_complete"}


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

    # F1+F8+F2: Set workflow context with injected dependencies
    _ctx.model_provider = model_provider
    _ctx.retrieval_service = retrieval_service
    _ctx.critic = LLMCritic(model_provider=model_provider) if model_provider else None
    _ctx.approval_service = approval_service
    _ctx.tracer = Tracer(request_id=request_id, workflow_id=workflow_id)
    if trace_id:
        _ctx.tracer.context.request_id = trace_id

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
            final_state["trace_summary"] = _ctx.tracer.log_summary()
        except Exception as exc:
            logger.warning("Could not build trace summary: %s", exc)
        return final_state
    finally:
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

    The approved task has already executed; advance to the next task
    (or complete) without re-running the approved task and without
    re-creating its approval.
    """
    state["status"] = RequestStatus.EXECUTING.value
    state["approval_required"] = False
    state["approval_resolved"] = True
    state["resumed"] = True
    state.pop("approval_id", None)

    tasks = state.get("plan", {}).get("tasks", [])
    next_index = int(state.get("current_task_index", 0)) + 1

    if next_index >= len(tasks):
        # All tasks approved and complete
        return _copy_state(
            state,
            status=RequestStatus.COMPLETED.value,
            final_result=state.get("agent_result", {}),
            current_task_index=next_index,
        )

    state["current_task_index"] = next_index
    state["current_task"] = {}
    return _execute_with_state(state, workflow_id, checkpointer)


def _execute_with_state(
    state: dict[str, Any],
    workflow_id: str,
    checkpointer: WorkflowCheckpointer | None = None,
) -> dict[str, Any]:
    """Execute the workflow starting from a given state."""
    # Store checkpointer in context so nodes can save checkpoints
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
