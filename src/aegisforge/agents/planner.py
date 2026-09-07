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
    TaskFailurePolicy,
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
        risk_level = _classify_risk(intent_lower)

        # Phase 5: compound intents decompose into real multi-agent graphs
        # (parallel retrieval/research → analysis → synthesis).
        multi_agent_plan = _generate_multi_agent_plan(plan_id, intent, intent_lower, risk_level)
        if multi_agent_plan is not None:
            multi_agent_plan.request_id = context.request_id
            return multi_agent_plan

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
                    risk_level=risk_level,
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
                    risk_level=risk_level,
                )
            )

        return ExecutionPlan(
            plan_id=plan_id,
            request_id=context.request_id,
            tasks=tasks,
        )


def _generate_multi_agent_plan(
    plan_id: str,
    intent: str,
    intent_lower: str,
    risk_level: str,
) -> ExecutionPlan | None:
    """Deterministic multi-agent decomposition for compound intents.

    Recognized compound request shapes (each maps to a dependency graph):

    - ``enterprise knowledge research`` → RAG ∥ Research → Analysis → Synthesis
    - ``compare ... and synthesize`` → Research(subject-a) ∥ Research(subject-b)
      → Analysis → Synthesis
    - ``analyze and recommend`` → Research → Analysis → Synthesis

    Returns ``None`` when the intent is not a recognized compound shape so the
    caller keeps producing the classic single-task plans.
    """
    task_ids = [f"task-{uuid.uuid4().hex[:10]}" for _ in range(6)]
    partial = TaskFailurePolicy.CONTINUE_WITH_PARTIAL_RESULTS

    if "enterprise knowledge research" in intent_lower:
        # t1 research + t2 rag run in parallel; t3 analysis digests both;
        # t4 synthesis produces the final grounded answer.
        tasks = [
            ExecutionPlanTask(
                task_id=task_ids[0],
                description=f"Gather internal knowledge on: {intent}",
                assigned_agent_type=AgentType.RESEARCH,
                input_data={"query": intent, "tool_name": "knowledge.search"},
                dependencies=[],
                expected_output_description="Curated internal knowledge with sources",
                tool_permissions_required=["knowledge.search"],
                risk_level="low",
            ),
            ExecutionPlanTask(
                task_id=task_ids[1],
                description=f"Retrieve enterprise knowledge base for: {intent}",
                assigned_agent_type=AgentType.RAG,
                input_data={"query": intent, "top_k": 4, "similarity_threshold": 0.0},
                dependencies=[],
                expected_output_description="Tenant-scoped retrieved chunks with citations",
                tool_permissions_required=["knowledge.search"],
                risk_level="low",
            ),
            ExecutionPlanTask(
                task_id=task_ids[2],
                description="Analyze gathered evidence for key findings and conflicts",
                assigned_agent_type=AgentType.ANALYSIS,
                input_data={
                    "query": intent,
                    "evidence_from": [task_ids[0], task_ids[1]],
                },
                dependencies=[task_ids[0], task_ids[1]],
                expected_output_description="Structured findings, conflicts, and gaps over the evidence",
                risk_level="low",
                failure_policy=partial,
            ),
            ExecutionPlanTask(
                task_id=task_ids[3],
                description="Synthesize a grounded final answer",
                assigned_agent_type=AgentType.SYNTHESIS,
                input_data={
                    "query": intent,
                    "agent_outputs_from": [task_ids[0], task_ids[1], task_ids[2]],
                },
                dependencies=[task_ids[2]],
                expected_output_description="Final answer with citations and explicit failure flags",
                risk_level=risk_level,
                failure_policy=partial,
            ),
        ]
        return ExecutionPlan(plan_id=plan_id, request_id="", tasks=tasks)

    if "synthesize" in intent_lower and ("compare" in intent_lower or "contrast" in intent_lower):
        tasks = [
            ExecutionPlanTask(
                task_id=task_ids[0],
                description=f"Research first perspective on: {intent}",
                assigned_agent_type=AgentType.RESEARCH,
                input_data={"query": intent, "tool_name": "knowledge.search"},
                dependencies=[],
                expected_output_description="Research on the first perspective",
                tool_permissions_required=["knowledge.search"],
                risk_level="low",
            ),
            ExecutionPlanTask(
                task_id=task_ids[1],
                description=f"Research second perspective on: {intent}",
                assigned_agent_type=AgentType.RESEARCH,
                input_data={"query": intent, "tool_name": "knowledge.search"},
                dependencies=[],
                expected_output_description="Research on the second perspective",
                tool_permissions_required=["knowledge.search"],
                risk_level="low",
            ),
            ExecutionPlanTask(
                task_id=task_ids[2],
                description="Analyze both perspectives for conflicts and gaps",
                assigned_agent_type=AgentType.ANALYSIS,
                input_data={
                    "query": intent,
                    "evidence_from": [task_ids[0], task_ids[1]],
                },
                dependencies=[task_ids[0], task_ids[1]],
                expected_output_description="Structured comparison with surfaced conflicts",
                risk_level="low",
                failure_policy=partial,
            ),
            ExecutionPlanTask(
                task_id=task_ids[3],
                description="Synthesize the comparison into a final answer",
                assigned_agent_type=AgentType.SYNTHESIS,
                input_data={
                    "query": intent,
                    "agent_outputs_from": [task_ids[0], task_ids[1], task_ids[2]],
                },
                dependencies=[task_ids[2]],
                expected_output_description="Synthesized comparison answer",
                risk_level=risk_level,
                failure_policy=partial,
            ),
        ]
        return ExecutionPlan(plan_id=plan_id, request_id="", tasks=tasks)

    if "analyze" in intent_lower and "recommend" in intent_lower:
        tasks = [
            ExecutionPlanTask(
                task_id=task_ids[0],
                description=f"Gather knowledge for analysis: {intent}",
                assigned_agent_type=AgentType.RESEARCH,
                input_data={"query": intent, "tool_name": "knowledge.search"},
                dependencies=[],
                expected_output_description="Knowledge source for the analysis",
                tool_permissions_required=["knowledge.search"],
                risk_level="low",
            ),
            ExecutionPlanTask(
                task_id=task_ids[1],
                description="Analyze the situation and derive recommendations",
                assigned_agent_type=AgentType.ANALYSIS,
                input_data={
                    "query": intent,
                    "evidence_from": [task_ids[0]],
                },
                dependencies=[task_ids[0]],
                expected_output_description="Analysis with recommendations and confidence",
                risk_level="low",
                failure_policy=partial,
            ),
            ExecutionPlanTask(
                task_id=task_ids[2],
                description="Synthesize the final recommendation",
                assigned_agent_type=AgentType.SYNTHESIS,
                input_data={
                    "query": intent,
                    "agent_outputs_from": [task_ids[0], task_ids[1]],
                },
                dependencies=[task_ids[1]],
                expected_output_description="Final recommendation answer",
                risk_level=risk_level,
                failure_policy=partial,
            ),
        ]
        return ExecutionPlan(plan_id=plan_id, request_id="", tasks=tasks)

    return None


def _classify_risk(intent_lower: str) -> str:
    """Classify intent risk level using conservative keyword rules.

    High-risk keywords gate the task behind human approval.
    """
    high_risk_keywords = [
        "restart", "deploy", "delete", "destroy", "terminate", "shutdown",
        "drop", "remove", "purge", "firewall", "credential", "password",
        "payment", "transfer", "migrate", "config change", "reconfigure",
        "downgrade", "revoke", "ban", "block", "disable", "wipe", "reset",
    ]
    if any(kw in intent_lower for kw in high_risk_keywords):
        return "high"
    return "low"
