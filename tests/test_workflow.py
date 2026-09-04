"""Tests for the LangGraph workflow, evaluation, and retry logic."""

from __future__ import annotations

from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    EvaluationVerdict,
    RequestStatus,
)
from aegisforge.workflows.evaluation import ResultEvaluator
from aegisforge.workflows.langgraph_workflow import (
    evaluate_node,
    execute_agent_node,
    execute_workflow,
    plan_node,
    retry_or_complete_node,
    validate_request_node,
)

# --- Evaluation Tests ---


def test_evaluation_passes_on_good_result() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.COMPLETED,
        summary="Done",
        result={"query": "test", "answer": "result"},
        tool_calls=[{"tool_name": "test", "status": "completed"}],
    )
    ev = evaluator.evaluate(result, expected_fields=["query", "answer"])
    assert ev.verdict == EvaluationVerdict.PASSED
    assert ev.score >= 0.7


def test_evaluation_fails_on_empty_result() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.COMPLETED,
        summary="Done",
        result={},
    )
    ev = evaluator.evaluate(result, expected_fields=["query"])
    assert ev.verdict != EvaluationVerdict.PASSED


def test_evaluation_fails_on_failed_status() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.FAILED,
        summary="Failed",
        result={"query": "x"},
        errors=["Something went wrong"],
    )
    ev = evaluator.evaluate(result)
    assert ev.verdict == EvaluationVerdict.FAILED


def test_evaluation_retryable_on_retry_status() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.TIMEOUT,
        summary="Timed out",
        result={},
    )
    ev = evaluator.evaluate(result)
    # Timeout with empty result: score is 0.0 (non-completed → * 0.0), so verdict is FAILED
    # but retryable is True because timeout is a retryable condition
    assert ev.verdict == EvaluationVerdict.FAILED
    assert ev.retryable is True


def test_evaluation_retry_on_partial_result() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.COMPLETED,
        summary="Partial",
        result={"query": "x"},  # Missing 'answer' field
        tool_calls=[{"tool_name": "t", "status": "failed"}],
    )
    ev = evaluator.evaluate(result, expected_fields=["query", "answer"])
    # Completed status, but missing fields and failed tool call → low score → RETRY
    assert ev.verdict == EvaluationVerdict.RETRY
    assert ev.retryable is True


def test_evaluation_missing_required_fields() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.COMPLETED,
        summary="Done",
        result={"other": "value"},
    )
    ev = evaluator.evaluate(result, expected_fields=["query", "answer"])
    assert ev.score < 1.0
    assert any("missing" in r.lower() for r in ev.reasons)


def test_evaluation_tool_call_failure() -> None:
    evaluator = ResultEvaluator()
    result = AgentResult(
        agent_name="test",
        agent_type=AgentType.RESEARCH,
        status=AgentExecutionStatus.COMPLETED,
        summary="Done",
        result={"query": "x", "answer": "y"},
        tool_calls=[{"tool_name": "test", "status": "failed"}],
    )
    ev = evaluator.evaluate(result)
    assert ev.score < 1.0
    assert any("tool call" in r.lower() for r in ev.reasons)


# --- Workflow Node Tests ---


def test_validate_request_valid() -> None:
    state = {
        "request_id": "req-001",
        "intent": "Find support policies",
        "status": "created",
        "errors": [],
    }
    result = validate_request_node(state)
    assert result["status"] == RequestStatus.PLANNING.value


def test_validate_request_missing_request_id() -> None:
    state = {"intent": "test", "status": "created", "errors": []}
    result = validate_request_node(state)
    assert result["status"] == RequestStatus.FAILED.value
    assert any("request_id" in e for e in result["errors"])


def test_validate_request_missing_intent() -> None:
    state = {"request_id": "req-001", "status": "created", "errors": []}
    result = validate_request_node(state)
    assert result["status"] == RequestStatus.FAILED.value
    assert any("intent" in e for e in result["errors"])


def test_plan_node_creates_plan() -> None:
    state = {
        "request_id": "req-001",
        "intent": "Research support escalation policies",
        "status": "planning",
        "errors": [],
    }
    result = plan_node(state)
    assert result["status"] == RequestStatus.EXECUTING.value
    assert result["plan"]
    assert result["plan"]["tasks"]


def test_execute_agent_node_produces_result() -> None:
    state = {
        "request_id": "req-001",
        "workflow_id": "wf-001",
        "user_id": "u-001",
        "organization_id": "org-001",
        "intent": "Research support policies",
        "status": "executing",
        "plan": {
            "plan_id": "plan-001",
            "request_id": "req-001",
            "tasks": [
                {
                    "task_id": "task-001",
                    "description": "Research support policies",
                    "assigned_agent_type": "research",
                    "input_data": {"query": "support escalation"},
                    "dependencies": [],
                    "tool_permissions_required": ["knowledge.search"],
                }
            ],
        },
        "current_task_index": 0,
        "tool_calls": [],
        "errors": [],
    }
    result = execute_agent_node(state)
    assert result["agent_result"]
    assert result["agent_result"]["status"] == AgentExecutionStatus.COMPLETED.value
    assert result["tool_calls"]


def test_execute_agent_node_no_tasks() -> None:
    state = {
        "request_id": "req-001",
        "status": "executing",
        "plan": {"tasks": []},
        "current_task_index": 0,
        "tool_calls": [],
        "errors": [],
    }
    result = execute_agent_node(state)
    assert result["status"] == RequestStatus.COMPLETED.value


def test_evaluate_node_with_result() -> None:
    state = {
        "agent_result": {
            "agent_name": "test",
            "agent_type": "research",
            "status": "completed",
            "summary": "Done",
            "result": {"query": "x", "answer": "y"},
            "tool_calls": [],
            "errors": [],
        },
        "status": "executing",
    }
    result = evaluate_node(state)
    assert result["status"] == RequestStatus.EVALUATING.value
    assert result["evaluation"]["verdict"] == EvaluationVerdict.PASSED.value


def test_evaluate_node_without_result() -> None:
    state = {"agent_result": {}, "status": "executing"}
    result = evaluate_node(state)
    assert result["status"] == RequestStatus.EVALUATING.value
    assert result["evaluation"]["verdict"] == EvaluationVerdict.FAILED.value


def test_retry_or_complete_passed_advances() -> None:
    state = {
        "evaluation": {"verdict": "passed", "score": 1.0, "reasons": [], "retryable": False},
        "current_task_index": 0,
        "retry_count": 0,
        "max_retries": 3,
        "plan": {"tasks": [{"id": "t1"}, {"id": "t2"}]},
        "agent_result": {"result": "done"},
        "status": "evaluating",
    }
    result = retry_or_complete_node(state)
    assert result["current_task_index"] == 1
    assert result["status"] == RequestStatus.EXECUTING.value


def test_retry_or_complete_passed_final_task() -> None:
    state = {
        "evaluation": {"verdict": "passed", "score": 1.0, "reasons": [], "retryable": False},
        "current_task_index": 0,
        "retry_count": 0,
        "max_retries": 3,
        "plan": {"tasks": [{"id": "t1"}]},
        "agent_result": {"result": "done"},
        "status": "evaluating",
    }
    result = retry_or_complete_node(state)
    assert result["status"] == RequestStatus.COMPLETED.value
    assert result["final_result"]


def test_retry_or_complete_retry_below_limit() -> None:
    state = {
        "evaluation": {"verdict": "retry", "score": 0.3, "reasons": ["low score"], "retryable": True},
        "current_task_index": 0,
        "retry_count": 1,
        "max_retries": 3,
        "plan": {"tasks": [{"id": "t1"}]},
        "status": "evaluating",
    }
    result = retry_or_complete_node(state)
    assert result["status"] == RequestStatus.RETRYING.value
    assert result["retry_count"] == 2


def test_retry_or_complete_retry_at_limit() -> None:
    state = {
        "evaluation": {"verdict": "retry", "score": 0.3, "reasons": ["low score"], "retryable": True},
        "current_task_index": 0,
        "retry_count": 3,
        "max_retries": 3,
        "plan": {"tasks": [{"id": "t1"}]},
        "status": "evaluating",
    }
    result = retry_or_complete_node(state)
    assert result["status"] == RequestStatus.FAILED.value


# --- Full Workflow Integration Test ---


def test_full_workflow_success() -> None:
    result = execute_workflow(
        request_id="req-e2e-001",
        intent="Find recent guidance on support escalation policies",
        user_id="user-001",
        organization_id="org-001",
    )
    assert result["status"] == "completed"
    assert result["final_result"]
    assert result["final_result"]["status"] == "completed"
    assert len(result["tool_calls"]) >= 1
    assert result["errors"] == []


def test_full_workflow_with_generic_intent() -> None:
    result = execute_workflow(
        request_id="req-e2e-002",
        intent="Help me with something",
        user_id="user-002",
        organization_id="org-002",
    )
    assert result["status"] == "completed"
    assert result["final_result"]
