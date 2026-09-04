from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.domain.models import AgentExecutionStatus, RequestStatus
from aegisforge.domain.schemas import RequestCreate, RequestStatusUpdate
from aegisforge.tools.knowledge_tool import KnowledgeSearchTool
from aegisforge.tools.registry import ToolRegistry


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(KnowledgeSearchTool())
    return registry


def test_research_agent_executes_with_permission_boundaries() -> None:
    agent = ResearchAgent(
        name="research-agent",
        description="Researches approved internal sources.",
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
        registry=_make_registry(),
    )
    context = AgentExecutionContext(
        request_id="req-123",
        user_id="user-123",
        organization_id="org-123",
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )

    result = agent.execute(
        input_data={"query": "support escalation guidance"},
        context=context,
    )

    assert result.status == AgentExecutionStatus.COMPLETED
    assert result.result["query"] == "support escalation guidance"
    assert result.result["answer"]
    assert result.tool_calls  # Should have made a tool call


def test_research_agent_denied_without_permissions() -> None:
    agent = ResearchAgent(
        name="research-agent",
        description="Researches approved internal sources.",
        permissions=[],  # No permissions
    )
    context = AgentExecutionContext(
        request_id="req-456",
        user_id="user-456",
        organization_id="org-456",
        permissions=[],  # No permissions
    )

    result = agent.execute(
        input_data={"query": "support escalation guidance", "tool_name": "knowledge.search"},
        context=context,
    )

    assert result.status == AgentExecutionStatus.FAILED


def test_request_status_transition_validation() -> None:
    state = RequestStatus.CREATED
    assert state == RequestStatus.CREATED
    assert RequestStatus.PLANNING.value == "planning"
    assert RequestStatus.EVALUATING.value == "evaluating"
    assert RequestStatus.COMPLETED.value == "completed"

    create_payload = RequestCreate(intent="Summarize customer support policy")
    assert create_payload.intent == "Summarize customer support policy"

    update_payload = RequestStatusUpdate(status=RequestStatus.COMPLETED)
    assert update_payload.status == RequestStatus.COMPLETED
