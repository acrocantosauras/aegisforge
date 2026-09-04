"""Tests for extended evaluation: RAG quality, plan validity, and execution metrics."""
from __future__ import annotations

from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
    RetrievalResult,
)
from aegisforge.evaluation.evaluators import (
    evaluate_execution,
    evaluate_plan,
    evaluate_rag_result,
)

# --- RAG Evaluation Tests ---


def test_evaluate_rag_with_results() -> None:
    agent_result = AgentResult(
        agent_name="rag",
        agent_type=AgentType.RAG,
        status=AgentExecutionStatus.COMPLETED,
        summary="Retrieved chunks",
        result={
            "query": "test",
            "retrieval_count": 3,
            "citations": [{"source": "doc1"}, {"source": "doc2"}],
            "context_available": True,
        },
    )
    retrieval_results = [
        RetrievalResult(chunk_id="c1", document_id="d1", content="A", score=0.9, source="doc1"),
        RetrievalResult(chunk_id="c2", document_id="d2", content="B", score=0.7, source="doc2"),
    ]

    result = evaluate_rag_result(agent_result, retrieval_results)
    assert result.retrieval_count == 2
    assert result.retrieval_relevance > 0
    assert result.has_citations is True
    assert result.groundedness > 0
    assert result.overall_score > 0


def test_evaluate_rag_insufficient_context() -> None:
    agent_result = AgentResult(
        agent_name="rag",
        agent_type=AgentType.RAG,
        status=AgentExecutionStatus.COMPLETED,
        summary="No results",
        result={
            "query": "test",
            "retrieval_count": 0,
            "citations": [],
            "context_available": False,
        },
    )

    result = evaluate_rag_result(agent_result)
    assert result.insufficient_context is True
    assert result.groundedness == 0.0


def test_evaluate_rag_empty_result() -> None:
    agent_result = AgentResult(
        agent_name="rag",
        agent_type=AgentType.RAG,
        status=AgentExecutionStatus.COMPLETED,
        summary="Empty",
        result={},
    )
    result = evaluate_rag_result(agent_result)
    assert result.overall_score == 0.0


# --- Plan Evaluation Tests ---


def test_evaluate_plan_valid() -> None:
    plan = ExecutionPlan(
        plan_id="plan-1",
        request_id="req-1",
        tasks=[
            ExecutionPlanTask(
                task_id="t1",
                description="Research",
                assigned_agent_type=AgentType.RESEARCH,
                tool_permissions_required=["knowledge.search"],
            ),
            ExecutionPlanTask(
                task_id="t2",
                description="Evaluate",
                assigned_agent_type=AgentType.EVALUATOR,
                dependencies=["t1"],
            ),
        ],
    )
    result = evaluate_plan(plan)
    assert result.plan_valid is True
    assert result.task_count == 2
    assert result.has_dependencies is True
    assert result.has_tool_permissions is True
    assert result.agent_type_diversity == 2
    assert result.overall_score > 0.5


def test_evaluate_plan_with_errors() -> None:
    plan = ExecutionPlan(plan_id="plan-1", request_id="req-1", tasks=[])
    result = evaluate_plan(plan, validation_errors=["No tasks", "Missing plan_id"])
    assert result.plan_valid is False
    assert len(result.validation_errors) == 2
    assert result.overall_score == 0.0


def test_evaluate_plan_empty() -> None:
    plan = ExecutionPlan(plan_id="plan-1", request_id="req-1", tasks=[])
    result = evaluate_plan(plan)
    assert result.overall_score == 0.0


# --- Execution Evaluation Tests ---


def test_evaluate_execution_all_passed() -> None:
    results = [
        AgentResult(
            agent_name="a1",
            agent_type=AgentType.RESEARCH,
            status=AgentExecutionStatus.COMPLETED,
            summary="Done",
            tool_calls=[{"tool_name": "t1", "status": "completed"}],
        ),
        AgentResult(
            agent_name="a2",
            agent_type=AgentType.RAG,
            status=AgentExecutionStatus.COMPLETED,
            summary="Done",
            tool_calls=[{"tool_name": "t2", "status": "completed"}],
        ),
    ]
    result = evaluate_execution(results)
    assert result.task_completion_rate == 1.0
    assert result.tool_success_rate == 1.0
    assert result.failure_rate == 0.0
    assert result.overall_score > 0.8


def test_evaluate_execution_with_failures() -> None:
    results = [
        AgentResult(
            agent_name="a1",
            agent_type=AgentType.RESEARCH,
            status=AgentExecutionStatus.COMPLETED,
            summary="Done",
        ),
        AgentResult(
            agent_name="a2",
            agent_type=AgentType.RESEARCH,
            status=AgentExecutionStatus.FAILED,
            summary="Failed",
        ),
    ]
    result = evaluate_execution(results)
    assert result.task_completion_rate == 0.5
    assert result.failure_rate == 0.5
    assert result.overall_score < 1.0


def test_evaluate_execution_empty() -> None:
    result = evaluate_execution([])
    assert result.total_tasks == 0
    assert result.overall_score == 0.0 or result.tool_success_rate == 1.0


def test_evaluate_execution_with_retries() -> None:
    results = [
        AgentResult(
            agent_name="a1",
            agent_type=AgentType.RESEARCH,
            status=AgentExecutionStatus.COMPLETED,
            summary="Done",
        ),
    ]
    result = evaluate_execution(results, retry_count=2)
    assert result.retried_tasks == 2
    assert result.retry_rate > 0


def test_evaluate_execution_tool_failure() -> None:
    results = [
        AgentResult(
            agent_name="a1",
            agent_type=AgentType.RESEARCH,
            status=AgentExecutionStatus.COMPLETED,
            summary="Done",
            tool_calls=[
                {"tool_name": "t1", "status": "completed"},
                {"tool_name": "t2", "status": "failed"},
            ],
        ),
    ]
    result = evaluate_execution(results)
    assert result.total_tool_calls == 2
    assert result.successful_tool_calls == 1
    assert result.tool_success_rate == 0.5
