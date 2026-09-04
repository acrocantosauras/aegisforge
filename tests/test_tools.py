"""Tests for tool abstraction, registry, and permission enforcement."""

from __future__ import annotations

import pytest

from aegisforge.tools.base import BaseTool, ToolDefinition
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry, reset_tool_registry

# --- Fixtures ---


def _make_tool_def(**overrides) -> ToolDefinition:
    defaults = {
        "name": "test.tool",
        "description": "A test tool",
        "permission_requirements": ["test.execute"],
    }
    defaults.update(overrides)
    return ToolDefinition(**defaults)


class DummyTool(BaseTool):
    def __init__(self, **overrides):
        super().__init__(_make_tool_def(**overrides))

    def _execute(self, input_data: dict) -> dict:
        return {"result": "ok", "input": input_data}


class FailingTool(BaseTool):
    def __init__(self):
        super().__init__(_make_tool_def(name="failing.tool"))

    def _execute(self, input_data: dict) -> dict:
        raise RuntimeError("Tool intentionally failed")


# --- Tool Definition Tests ---


def test_tool_definition_defaults() -> None:
    defn = ToolDefinition(name="x", description="y")
    assert defn.version == "1.0"
    assert defn.timeout_seconds == 30
    assert defn.permission_requirements == []
    assert defn.requires_approval is False


def test_tool_base_execute_returns_completed() -> None:
    tool = DummyTool()
    result = tool.execute({}, granted_permissions=["test.execute"])
    assert result.status == "completed"
    assert result.output["result"] == "ok"
    assert result.duration_ms >= 0
    assert result.execution_id


def test_tool_base_execute_permission_denied() -> None:
    tool = DummyTool()
    result = tool.execute({}, granted_permissions=[])
    assert result.status == "denied"
    assert "Missing required permissions" in result.error


def test_tool_base_execute_no_permission_check_when_none() -> None:
    """When granted_permissions is None, permission check is skipped."""
    tool = DummyTool()
    result = tool.execute({}, granted_permissions=None)
    assert result.status == "completed"


def test_tool_base_execute_exception_handling() -> None:
    tool = FailingTool()
    result = tool.execute({})
    assert result.status == "failed"
    assert "Tool intentionally failed" in result.error


def test_tool_has_permissions() -> None:
    tool = DummyTool()
    assert tool.has_permissions(["test.execute"]) is True
    assert tool.has_permissions(["other.perm"]) is False
    assert tool.has_permissions(["test.execute", "extra.perm"]) is True


# --- Knowledge Tool Tests ---


def test_knowledge_tool_search_escalation() -> None:
    tool = KnowledgeSearchTool()
    result = tool.execute({"query": "support escalation"})
    assert result.status == "completed"
    assert result.output["topic"] == "Support Escalation Policy"
    assert "triaged" in result.output["summary"]
    assert result.output["source"] == "internal-policy/support-escalation-v2.1"


def test_knowledge_tool_search_incident() -> None:
    tool = KnowledgeSearchTool()
    result = tool.execute({"query": "incident response procedure"})
    assert result.status == "completed"
    assert result.output["topic"] == "Incident Response Procedure"
    assert "P1" in result.output["summary"]


def test_knowledge_tool_empty_query() -> None:
    tool = KnowledgeSearchTool()
    result = tool.execute({"query": ""})
    assert result.status == "completed"
    assert result.output["topic"] == ""


def test_knowledge_tool_no_match() -> None:
    tool = KnowledgeSearchTool()
    result = tool.execute({"query": "quantum computing"})
    assert result.status == "completed"
    assert result.output["topic"] == "No direct match found"


def test_knowledge_tool_permission_requirements() -> None:
    tool = KnowledgeSearchTool()
    assert "knowledge.search" in tool.required_permissions


# --- Tool Registry Tests ---


def test_registry_register_and_discover() -> None:
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)

    assert registry.has_tool("test.tool")
    assert not registry.has_tool("nonexistent")
    assert "test.tool" in registry.list_tool_names()


def test_registry_duplicate_raises() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(DummyTool())


def test_registry_get_returns_tool() -> None:
    registry = ToolRegistry()
    tool = DummyTool()
    registry.register(tool)
    assert registry.get("test.tool") is tool


def test_registry_get_nonexistent_returns_none() -> None:
    registry = ToolRegistry()
    assert registry.get("nope") is None


def test_registry_list_tools() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    registry.register(DummyTool(name="test.tool2"))
    defs = registry.list_tools()
    assert len(defs) == 2
    names = {d.name for d in defs}
    assert "test.tool" in names
    assert "test.tool2" in names


def test_registry_execute_success() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    result = registry.execute("test.tool", {}, granted_permissions=["test.execute"])
    assert result.status == "completed"


def test_registry_execute_permission_denied() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    result = registry.execute("test.tool", {}, granted_permissions=[])
    assert result.status == "denied"


def test_registry_execute_not_found() -> None:
    registry = ToolRegistry()
    result = registry.execute("nonexistent", {})
    assert result.status == "failed"
    assert "not found" in result.error


def test_registry_validate_permissions() -> None:
    registry = ToolRegistry()
    registry.register(DummyTool())
    assert registry.validate_permissions("test.tool", ["test.execute"]) is True
    assert registry.validate_permissions("test.tool", []) is False
    assert registry.validate_permissions("nonexistent", []) is False


def test_reset_tool_registry() -> None:
    reset_tool_registry()
    registry = ToolRegistry()
    registry.register(DummyTool())
    # After reset, the singleton should be gone
    reset_tool_registry()
    from aegisforge.tools.registry import get_tool_registry

    fresh = get_tool_registry()
    assert not fresh.has_tool("test.tool")
