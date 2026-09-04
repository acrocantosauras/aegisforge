from __future__ import annotations

from typing import Any, TypedDict


class WorkflowState(TypedDict, total=False):
    """Typed state for the LangGraph execution workflow.

    Contains only information required for workflow execution.
    No secrets, no large prompts, no unnecessary payloads.
    """

    request_id: str
    workflow_id: str
    user_id: str
    organization_id: str
    intent: str
    status: str  # RequestStatus value

    # Planning
    plan: dict[str, Any]  # Serialized ExecutionPlan
    current_task_index: int

    # Execution
    current_task: dict[str, Any]  # Serialized ExecutionPlanTask
    agent_result: dict[str, Any]  # Serialized AgentResult

    # Evaluation
    evaluation: dict[str, Any]  # Serialized EvaluationResult

    # Retry
    retry_count: int
    max_retries: int

    # Tool calls trace
    tool_calls: list[dict[str, Any]]

    # Errors
    errors: list[str]

    # Final
    final_result: dict[str, Any]
