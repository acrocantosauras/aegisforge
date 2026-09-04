"""Extended evaluation for Phase 3 capabilities.

Measures: RAG quality, plan validity, task decomposition, and execution metrics.
No fabricated metrics — all measurements are based on actual system behavior.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import (
    AgentResult,
    ExecutionPlan,
    RetrievalResult,
)

logger = logging.getLogger(__name__)


# --- RAG Evaluation ---


@dataclass
class RAGEvaluationResult:
    """Evaluation metrics for RAG quality."""

    retrieval_relevance: float = 0.0  # Average relevance score
    retrieval_count: int = 0
    has_citations: bool = False
    citation_count: int = 0
    groundedness: float = 0.0  # How well the response is grounded in evidence
    insufficient_context: bool = False
    overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_relevance": round(self.retrieval_relevance, 4),
            "retrieval_count": self.retrieval_count,
            "has_citations": self.has_citations,
            "citation_count": self.citation_count,
            "groundedness": round(self.groundedness, 4),
            "insufficient_context": self.insufficient_context,
            "overall_score": round(self.overall_score, 4),
        }


def evaluate_rag_result(
    agent_result: AgentResult,
    retrieval_results: list[RetrievalResult] | None = None,
) -> RAGEvaluationResult:
    """Evaluate a RAG agent result."""
    result = RAGEvaluationResult()

    if not agent_result.result:
        return result

    # Measure retrieval relevance
    if retrieval_results:
        result.retrieval_count = len(retrieval_results)
        if retrieval_results:
            result.retrieval_relevance = sum(r.score for r in retrieval_results) / len(retrieval_results)
    else:
        # Extract from agent result
        result.retrieval_count = agent_result.result.get("retrieval_count", 0)
        # If we have evidence with scores, use those for relevance
        if agent_result.evidence:
            scores = [e.get("score", 0.5) for e in agent_result.evidence if isinstance(e, dict)]
            if scores:
                result.retrieval_relevance = sum(scores) / len(scores)
            else:
                result.retrieval_relevance = 0.5  # Default when evidence exists but no scores

    # Check citations
    citations = agent_result.result.get("citations", [])
    result.citation_count = len(citations)
    result.has_citations = len(citations) > 0

    # Check groundedness
    context_available = agent_result.result.get("context_available", False)
    if context_available and result.retrieval_count > 0:
        # Groundedness = relevance * citation presence
        result.groundedness = result.retrieval_relevance * (1.0 if result.has_citations else 0.5)
    elif result.retrieval_count == 0:
        result.insufficient_context = True
        result.groundedness = 0.0

    # Overall score
    scores = []
    if result.retrieval_count > 0:
        scores.append(result.retrieval_relevance)
    if result.has_citations:
        scores.append(1.0)
    scores.append(result.groundedness)

    result.overall_score = sum(scores) / len(scores) if scores else 0.0
    return result


# --- Plan Evaluation ---


@dataclass
class PlanEvaluationResult:
    """Evaluation metrics for planning quality."""

    plan_valid: bool = False
    task_count: int = 0
    has_dependencies: bool = False
    has_tool_permissions: bool = False
    agent_type_diversity: int = 0
    validation_errors: list[str] = field(default_factory=list)
    overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_valid": self.plan_valid,
            "task_count": self.task_count,
            "has_dependencies": self.has_dependencies,
            "has_tool_permissions": self.has_tool_permissions,
            "agent_type_diversity": self.agent_type_diversity,
            "validation_errors": self.validation_errors,
            "overall_score": round(self.overall_score, 4),
        }


def evaluate_plan(
    plan: ExecutionPlan,
    validation_errors: list[str] | None = None,
) -> PlanEvaluationResult:
    """Evaluate an execution plan."""
    result = PlanEvaluationResult()
    result.plan_valid = len(validation_errors or []) == 0
    result.validation_errors = validation_errors or []
    result.task_count = len(plan.tasks)

    if not plan.tasks:
        result.overall_score = 0.0
        return result

    # Check for dependencies
    result.has_dependencies = any(t.dependencies for t in plan.tasks)

    # Check for tool permissions
    result.has_tool_permissions = any(t.tool_permissions_required for t in plan.tasks)

    # Agent type diversity
    agent_types = {t.assigned_agent_type.value for t in plan.tasks}
    result.agent_type_diversity = len(agent_types)

    # Score
    score = 0.0
    if result.plan_valid:
        score += 0.4
    if result.task_count > 0:
        score += 0.2
    if result.has_dependencies:
        score += 0.1
    if result.has_tool_permissions:
        score += 0.1
    if result.agent_type_diversity > 1:
        score += 0.1
    if not result.validation_errors:
        score += 0.1

    result.overall_score = min(1.0, score)
    return result


# --- Execution Evaluation ---


@dataclass
class ExecutionEvaluationResult:
    """Evaluation metrics for agent execution quality."""

    task_completion_rate: float = 0.0
    tool_success_rate: float = 0.0
    retry_rate: float = 0.0
    failure_rate: float = 0.0
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    retried_tasks: int = 0
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_completion_rate": round(self.task_completion_rate, 4),
            "tool_success_rate": round(self.tool_success_rate, 4),
            "retry_rate": round(self.retry_rate, 4),
            "failure_rate": round(self.failure_rate, 4),
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "retried_tasks": self.retried_tasks,
            "total_tool_calls": self.total_tool_calls,
            "successful_tool_calls": self.successful_tool_calls,
            "overall_score": round(self.overall_score, 4),
        }


def evaluate_execution(
    agent_results: list[AgentResult],
    retry_count: int = 0,
) -> ExecutionEvaluationResult:
    """Evaluate execution quality across agent results."""
    result = ExecutionEvaluationResult()
    result.total_tasks = len(agent_results)
    result.retried_tasks = retry_count

    for ar in agent_results:
        if ar.status.value == "completed":
            result.completed_tasks += 1
        elif ar.status.value in ("failed", "denied", "timeout"):
            result.failed_tasks += 1

        # Count tool calls
        for tc in ar.tool_calls:
            result.total_tool_calls += 1
            if tc.get("status") == "completed":
                result.successful_tool_calls += 1

    # Compute rates
    if result.total_tasks > 0:
        result.task_completion_rate = result.completed_tasks / result.total_tasks
        result.failure_rate = result.failed_tasks / result.total_tasks
        result.retry_rate = result.retried_tasks / result.total_tasks

    if result.total_tool_calls > 0:
        result.tool_success_rate = result.successful_tool_calls / result.total_tool_calls
    else:
        result.tool_success_rate = 1.0  # No tools called = no failures

    # Overall score
    result.overall_score = (
        result.task_completion_rate * 0.5
        + result.tool_success_rate * 0.3
        + (1.0 - result.failure_rate) * 0.2
    )

    return result
