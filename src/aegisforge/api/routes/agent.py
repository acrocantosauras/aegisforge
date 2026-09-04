from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.agents.research_agent import ResearchAgent
from aegisforge.db.session import get_db
from aegisforge.tools.registry import get_tool_registry

router = APIRouter(prefix="/agents", tags=["agents"])


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
