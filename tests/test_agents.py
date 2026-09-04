"""Tests for agent abstraction, research agent, and planner."""

from __future__ import annotations

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.agents.planner import PlannerAgent
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentType,
)
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry

# --- Fixtures ---


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(KnowledgeSearchTool())
    return registry


def _make_context(**overrides) -> AgentExecutionContext:
    defaults = {
        "request_id": "req-test",
        "user_id": "user-test",
        "organization_id": "org-test",
        "permissions": [PermissionSpec(name="knowledge.search", allow=True)],
    }
    defaults.update(overrides)
    return AgentExecutionContext(**defaults)


# --- Base Agent Tests ---


def test_base_agent_check_permissions() -> None:
    agent = ResearchAgent(
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )
    assert agent.check_permissions(["knowledge.search"]) is True
    assert agent.check_permissions(["other.perm"]) is False
    assert agent.check_permissions([]) is True


def test_base_agent_exception_returns_failed_result() -> None:
    """Verify that exceptions in _execute produce a failed AgentResult."""

    class BrokenAgent(BaseAgent):
        def _execute(self, input_data, context):
            raise RuntimeError("Something broke")

    agent = BrokenAgent(
        name="broken",
        agent_type=AgentType.RESEARCH,
    )
    result = agent.execute({}, _make_context())
    assert result.status == AgentExecutionStatus.FAILED
    assert "Something broke" in result.errors


def test_base_agent_permission_error_returns_denied() -> None:
    class DeniedAgent(BaseAgent):
        def _execute(self, input_data, context):
            raise PermissionError("Not allowed")

    agent = DeniedAgent(
        name="denied",
        agent_type=AgentType.RESEARCH,
    )
    result = agent.execute({}, _make_context())
    assert result.status == AgentExecutionStatus.DENIED


# --- Research Agent Tests ---


def test_research_agent_success() -> None:
    agent = ResearchAgent(
        registry=_make_registry(),
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )
    result = agent.execute(
        {"query": "support escalation"},
        _make_context(),
    )
    assert result.status == AgentExecutionStatus.COMPLETED
    assert result.result["query"] == "support escalation"
    assert result.result["answer"]
    assert result.result["source"]
    assert result.tool_calls
    assert result.confidence is not None
    assert result.execution_time_ms is not None


def test_research_agent_empty_query() -> None:
    agent = ResearchAgent(
        registry=_make_registry(),
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )
    result = agent.execute({"query": ""}, _make_context())
    assert result.status == AgentExecutionStatus.FAILED
    assert any("query" in e.lower() for e in result.errors)


def test_research_agent_tool_not_available() -> None:
    agent = ResearchAgent(
        registry=ToolRegistry(),  # Empty registry
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )
    result = agent.execute(
        {"query": "test", "tool_name": "knowledge.search"},
        _make_context(),
    )
    assert result.status == AgentExecutionStatus.FAILED
    assert any("not available" in e.lower() or "not registered" in e.lower() for e in result.errors)


def test_research_agent_permission_denied() -> None:
    agent = ResearchAgent(
        registry=_make_registry(),
        permissions=[],  # No permissions
    )
    result = agent.execute(
        {"query": "test", "tool_name": "knowledge.search"},
        _make_context(permissions=[]),
    )
    assert result.status == AgentExecutionStatus.FAILED


# --- Planner Agent Tests ---


def test_planner_generates_plan_for_research() -> None:
    planner = PlannerAgent()
    result = planner.execute(
        {"intent": "Find guidance on support escalation policies"},
        _make_context(),
    )
    assert result.status == AgentExecutionStatus.COMPLETED
    assert result.result["task_count"] >= 1
    tasks = result.result["tasks"]
    assert any("research" in t.get("assigned_agent_type", "").lower() for t in tasks)


def test_planner_generates_plan_for_generic_intent() -> None:
    planner = PlannerAgent()
    result = planner.execute(
        {"intent": "Help me understand something"},
        _make_context(),
    )
    assert result.status == AgentExecutionStatus.COMPLETED
    assert result.result["task_count"] >= 1


def test_planner_empty_intent_fails() -> None:
    planner = PlannerAgent()
    result = planner.execute({"intent": ""}, _make_context())
    assert result.status == AgentExecutionStatus.FAILED
    assert any("intent" in e.lower() for e in result.errors)


def test_planner_plan_has_valid_structure() -> None:
    planner = PlannerAgent()
    result = planner.execute(
        {"intent": "Research customer support policies"},
        _make_context(),
    )
    assert result.status == AgentExecutionStatus.COMPLETED
    plan = result.result
    assert "plan_id" in plan
    assert "request_id" in plan
    assert "tasks" in plan
    for task in plan["tasks"]:
        assert "task_id" in task
        assert "description" in task
        assert "assigned_agent_type" in task
        assert "dependencies" in task
