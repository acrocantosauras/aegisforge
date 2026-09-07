"""LLM-assisted planner for AegisForge.

Produces structured ExecutionPlan output from an LLM, validates it
deterministically, and falls back to the rule-based planner when needed.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.agents.planner import PlannerAgent
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
    LLMPlanResult,
    PlannerType,
)
from aegisforge.llm.providers import ModelProvider

logger = logging.getLogger(__name__)

# Allowed agent types for plan validation
ALLOWED_AGENT_TYPES = {
    AgentType.RESEARCH.value,
    AgentType.RAG.value,
    AgentType.ANALYSIS.value,
    AgentType.SYNTHESIS.value,
}

# Maximum plan limits
MAX_TASKS = 20
MAX_WORKFLOW_DEPTH = 10


class PlanValidationError(Exception):
    """Raised when a plan fails validation."""


def validate_plan(plan: ExecutionPlan) -> list[str]:
    """Validate an ExecutionPlan deterministically.

    Returns a list of validation errors (empty if valid).
    """
    errors: list[str] = []

    # Schema validation
    if not plan.plan_id:
        errors.append("Missing plan_id")
    if not plan.request_id:
        errors.append("Missing request_id")
    if not plan.tasks:
        errors.append("Plan has no tasks")

    # Task count limit
    if len(plan.tasks) > MAX_TASKS:
        errors.append(f"Plan exceeds maximum task count ({MAX_TASKS})")

    # Check each task
    seen_task_ids: set[str] = set()
    for task in plan.tasks:
        # Required fields
        if not task.task_id:
            errors.append("Task missing task_id")
        elif task.task_id in seen_task_ids:
            errors.append(f"Duplicate task_id: {task.task_id}")
        else:
            seen_task_ids.add(task.task_id)

        if not task.description:
            errors.append(f"Task {task.task_id} missing description")

        # Allowed agent type
        if task.assigned_agent_type.value not in ALLOWED_AGENT_TYPES:
            errors.append(
                f"Task {task.task_id} has unauthorized agent type: {task.assigned_agent_type.value}"
            )

        # Dependencies reference valid tasks
        for dep in task.dependencies:
            if dep not in seen_task_ids and dep != task.task_id:
                # Note: This is a forward reference; we check at the end
                pass

    # Check dependency references
    for task in plan.tasks:
        for dep in task.dependencies:
            if dep not in seen_task_ids:
                errors.append(f"Task {task.task_id} references non-existent dependency: {dep}")

    # Circular dependency detection (topological sort)
    if not errors:
        task_map = {t.task_id: t for t in plan.tasks}
        visited: set[str] = set()
        in_stack: set[str] = set()

        def _has_cycle(tid: str) -> bool:
            if tid in in_stack:
                return True
            if tid in visited:
                return False
            visited.add(tid)
            in_stack.add(tid)
            t = task_map.get(tid)
            if t:
                for dep in t.dependencies:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(tid)
            return False

        for task in plan.tasks:
            if _has_cycle(task.task_id):
                errors.append(f"Circular dependency detected involving task {task.task_id}")
                break

    # Workflow depth check
    if not errors:
        depth = _compute_depth(plan.tasks)
        if depth > MAX_WORKFLOW_DEPTH:
            errors.append(f"Workflow depth ({depth}) exceeds maximum ({MAX_WORKFLOW_DEPTH})")

    return errors


def _compute_depth(tasks: list[ExecutionPlanTask]) -> int:
    """Compute the maximum depth of the task dependency graph."""
    task_map = {t.task_id: t for t in tasks}
    memo: dict[str, int] = {}

    def _depth(tid: str) -> int:
        if tid in memo:
            return memo[tid]
        t = task_map.get(tid)
        if not t or not t.dependencies:
            memo[tid] = 1
            return 1
        d = 1 + max((_depth(dep) for dep in t.dependencies), default=0)
        memo[tid] = d
        return d

    return max((_depth(t.task_id) for t in tasks), default=0) if tasks else 0


PLANNER_SYSTEM_PROMPT = """You are a planning assistant. Given a user request, decompose it into structured tasks.

You must respond with valid JSON matching this exact schema:
{
    "plan_id": "plan-<random_id>",
    "tasks": [
        {
            "task_id": "task-<random_id>",
            "description": "Clear description of what this task does",
            "assigned_agent_type": "research|rag|analysis|synthesis",
            "input_data": {},
            "dependencies": [],
            "expected_output_description": "What this task should produce",
            "tool_permissions_required": ["knowledge.search"]
        }
    ]
}

Rules:
- Each task must have a unique task_id
- assigned_agent_type must be one of: research, rag, analysis, synthesis
- dependencies reference other task_ids in the same plan
- Do not create circular dependencies
- Keep the plan focused and minimal
- For knowledge questions, use RAG agent type
- For research tasks, use research agent type
- Maximum 10 tasks per plan
"""


class LLMPlannerAgent(BaseAgent):
    """LLM-assisted planner that produces structured ExecutionPlan output.

    Architecture:
        User Request → LLM Planner → Structured Plan → Plan Validation
        → Valid? YES → Execute / NO → Deterministic Fallback
    """

    def __init__(
        self,
        name: str = "llm-planner-agent",
        description: str = "LLM-assisted planner with structured output validation and deterministic fallback.",
        permissions: list[PermissionSpec] | None = None,
        model_provider: ModelProvider | None = None,
        deterministic_fallback: PlannerAgent | None = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type=AgentType.PLANNER,
            description=description,
            permissions=permissions or [],
        )
        self._model_provider = model_provider
        self._fallback = deterministic_fallback or PlannerAgent()

    def _execute(self, input_data: dict[str, Any], context: AgentExecutionContext) -> AgentResult:
        intent = input_data.get("intent", "")
        if not intent:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="No intent provided for planning",
                errors=["Intent is required for plan generation"],
            )

        # Try LLM planning first
        if self._model_provider is not None:
            try:
                llm_result = self._plan_with_llm(intent, context)
                if llm_result.validation_passed:
                    return AgentResult(
                        agent_name=self.name,
                        agent_type=self.agent_type,
                        status=AgentExecutionStatus.COMPLETED,
                        summary=f"LLM-generated plan with {len(llm_result.plan.tasks)} task(s)",
                        result={
                            "plan_id": llm_result.plan.plan_id,
                            "request_id": llm_result.plan.request_id,
                            "task_count": len(llm_result.plan.tasks),
                            "tasks": [t.model_dump() for t in llm_result.plan.tasks],
                            "planner_type": llm_result.planner_type.value,
                            "model_used": llm_result.model_used,
                            "tokens_used": llm_result.tokens_used,
                            "latency_ms": llm_result.latency_ms,
                            "fallback_used": False,
                        },
                    )
                else:
                    logger.warning(
                        "LLM plan validation failed, using deterministic fallback: %s",
                        llm_result.validation_errors,
                    )
            except Exception as exc:
                logger.warning("LLM planning failed: %s, using fallback", exc)

        # Fallback to deterministic planner
        logger.info("Using deterministic planner fallback")
        fallback_result = self._fallback._execute(input_data, context)

        # Augment fallback result with planner metadata
        if fallback_result.status == AgentExecutionStatus.COMPLETED:
            fallback_result.result["planner_type"] = PlannerType.DETERMINISTIC.value
            fallback_result.result["fallback_used"] = True
            fallback_result.result["fallback_reason"] = "LLM planner unavailable or produced invalid output"

        return fallback_result

    def _plan_with_llm(
        self, intent: str, context: AgentExecutionContext
    ) -> LLMPlanResult:
        """Use the LLM to generate a structured plan."""
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Decompose this request into tasks:\n\n{intent}"},
        ]

        if self._model_provider is None:
            raise RuntimeError("LLM planner called without a model provider")

        start = time.monotonic()
        response = self._model_provider.generate_structured(
            messages=messages,
            model="",
            temperature=0.0,
            max_tokens=2048,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        # Parse the LLM response
        try:
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

            plan_data = json.loads(content)
        except (json.JSONDecodeError, ValueError) as exc:
            return LLMPlanResult(
                plan=ExecutionPlan(
                    plan_id=f"plan-{uuid.uuid4().hex[:12]}",
                    request_id=context.request_id,
                    tasks=[],
                ),
                planner_type=PlannerType.LLM,
                model_used=response.model,
                latency_ms=latency_ms,
                validation_passed=False,
                validation_errors=[f"Failed to parse LLM response as JSON: {exc}"],
            )

        # Convert to ExecutionPlan
        try:
            tasks = []
            for task_dict in plan_data.get("tasks", []):
                tasks.append(
                    ExecutionPlanTask(
                        task_id=task_dict.get("task_id", f"task-{uuid.uuid4().hex[:12]}"),
                        description=task_dict.get("description", ""),
                        assigned_agent_type=AgentType(
                            task_dict.get("assigned_agent_type", "research")
                        ),
                        input_data=task_dict.get("input_data", {}),
                        dependencies=task_dict.get("dependencies", []),
                        expected_output_description=task_dict.get("expected_output_description", ""),
                        tool_permissions_required=task_dict.get("tool_permissions_required", []),
                    )
                )

            plan = ExecutionPlan(
                plan_id=plan_data.get("plan_id", f"plan-{uuid.uuid4().hex[:12]}"),
                request_id=context.request_id,
                tasks=tasks,
            )
        except (ValueError, KeyError) as exc:
            return LLMPlanResult(
                plan=ExecutionPlan(
                    plan_id=f"plan-{uuid.uuid4().hex[:12]}",
                    request_id=context.request_id,
                    tasks=[],
                ),
                planner_type=PlannerType.LLM,
                model_used=response.model,
                latency_ms=latency_ms,
                validation_passed=False,
                validation_errors=[f"Failed to convert LLM output to plan: {exc}"],
            )

        # Validate the plan
        validation_errors = validate_plan(plan)

        return LLMPlanResult(
            plan=plan,
            planner_type=PlannerType.LLM,
            model_used=response.model,
            tokens_used=response.tokens_used,
            latency_ms=latency_ms,
            validation_passed=len(validation_errors) == 0,
            validation_errors=validation_errors,
        )
