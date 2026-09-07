from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db
from aegisforge.services.auth_service import get_current_user
from aegisforge.tools.registry import get_tool_registry

router = APIRouter(prefix="/agents", tags=["agents"])

# Static capability catalog for the /agents listing endpoint. This is
# operator/platform metadata — not user data.
_AGENT_CATALOG: list[dict[str, Any]] = [
    {
        "agent_type": "planner",
        "name": "planner",
        "description": "Decomposes requests into validated execution plans",
        "capabilities": ["planning", "decomposition", "dependency-graph"],
    },
    {
        "agent_type": "research",
        "name": "research",
        "description": "Investigates a request using approved tools",
        "capabilities": ["tool-use", "knowledge.search"],
    },
    {
        "agent_type": "rag",
        "name": "rag",
        "description": "Retrieves tenant-scoped knowledge and returns cited evidence",
        "capabilities": ["retrieval", "hybrid-search", "citations"],
    },
    {
        "agent_type": "analysis",
        "name": "analysis",
        "description": "Structured reasoning over evidence supplied by other agents",
        "capabilities": ["evidence-analysis", "conflict-detection"],
    },
    {
        "agent_type": "synthesis",
        "name": "synthesis",
        "description": "Combines validated agent outputs into a grounded final answer",
        "capabilities": ["aggregation", "citations", "failure-flags"],
    },
    {
        "agent_type": "evaluator",
        "name": "evaluator",
        "description": "Evaluates results and workflow execution quality",
        "capabilities": ["deterministic-evaluation", "workflow-evaluation"],
    },
]


class AgentRead(BaseModel):
    agent_type: str
    name: str
    description: str
    capabilities: list[str]


class AgentListRead(BaseModel):
    agents: list[AgentRead]
    total: int


@router.get("", response_model=AgentListRead)
def list_agents(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> AgentListRead:
    """List agent types available to multi-agent workflows."""
    agents = [AgentRead(**entry) for entry in _AGENT_CATALOG]
    return AgentListRead(agents=agents, total=len(agents))


@router.post("/research")
def run_research_agent(payload: dict, db: Session = Depends(get_db)) -> dict:
    registry = get_tool_registry()
    agent = ResearchAgent(
        name="research-agent",
        description="Approved internal research tool",
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
        registry=registry,
    )
    context = AgentExecutionContext(
        request_id=payload.get("request_id", "req-demo"),
        user_id=payload.get("user_id", "user-demo"),
        organization_id=payload.get("organization_id", "org-demo"),
        permissions=[PermissionSpec(name="knowledge.search", allow=True)],
    )
    result = agent.execute({"query": str(payload.get("query", ""))}, context)
    return result.model_dump()
