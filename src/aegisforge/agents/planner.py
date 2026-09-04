from __future__ import annotations

import logging
import uuid
from typing import Any

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
)

logger = logging.getLogger(__name__)


class PlannerAgent(BaseAgent):
    """Decomposes a validated request into a structured execution plan.

    The Planner:
    1. Receives a validated request
    2. Determines which capabilities are required
    3. Produces a structured ExecutionPlan with task dependencies
    4. Assigns tasks to available agent types
    """

    def __init__(
        self,
        name: str = "planner-agent",
        description: str = "Decomposes requests into structured execution plans.",
        permissions: list[PermissionSpec] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type=AgentType.PLANNER,
            description=description,
            permissions=permissions or [],
        )

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

        plan = self._generate_plan(intent, context)

        if plan is None:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="Failed to generate a valid execution plan",
                errors=["Could not decompose the request into executable tasks"],
            )

        return AgentResult(
            agent_name=self.name,
            agent_type=self.agent_type,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Generated plan with {len(plan.tasks)} task(s)",
            result={
                "plan_id": plan.plan_id,
                "request_id": plan.request_id,
                "task_count": len(plan.tasks),
                "tasks": [t.model_dump() for t in plan.tasks],
            },
        )

    def _generate_plan(
        self, intent: str, context: AgentExecutionContext
    ) -> ExecutionPlan | None:
        """Generate a structured plan from the intent.

        This uses deterministic rule-based planning.  A future version
        may use an LLM for more flexible decomposition, but the output
        will always be validated against the ExecutionPlan schema.
        """
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        intent_lower = intent.lower()
        tasks: list[ExecutionPlanTask] = []

        # Rule-based decomposition: determine required agent types
        needs_research = any(
            kw in intent_lower
            for kw in ["research", "find", "search", "investigate", "summarize", "review", "policy", "guidance", "lookup"]
        )

        if needs_research:
            task_id = f"task-{uuid.uuid4().hex[:12]}"
            tasks.append(
                ExecutionPlanTask(
                    task_id=task_id,
                    description=f"Research and investigate: {intent}",
                    assigned_agent_type=AgentType.RESEARCH,
                    input_data={"query": intent, "tool_name": "knowledge.search"},
                    dependencies=[],
                    expected_output_description="Structured research result with evidence and sources",
                    tool_permissions_required=["knowledge.search"],
                )
            )

        # If no specific capability matched, create a generic research task
        if not tasks:
            task_id = f"task-{uuid.uuid4().hex[:12]}"
            tasks.append(
                ExecutionPlanTask(
                    task_id=task_id,
                    description=f"Investigate and address: {intent}",
                    assigned_agent_type=AgentType.RESEARCH,
                    input_data={"query": intent},
                    dependencies=[],
                    expected_output_description="Investigation result",
                    tool_permissions_required=["knowledge.search"],
                )
            )

        return ExecutionPlan(
            plan_id=plan_id,
            request_id=context.request_id,
            tasks=tasks,
        )
