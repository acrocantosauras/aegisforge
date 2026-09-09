from __future__ import annotations

import logging
from typing import Any

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
    ToolCallRecord,
)
from aegisforge.tools.registry import ToolRegistry, get_tool_registry

logger = logging.getLogger(__name__)


class ResearchAgent(BaseAgent):
    """Investigates a request using approved tools and returns a structured result.

    The Research Agent:
    1. Receives a typed task description
    2. Determines which approved tool(s) to use
    3. Requests tool execution through the tool registry
    4. Produces a structured answer with evidence
    """

    def __init__(
        self,
        name: str = "research-agent",
        description: str = "Investigates requests using approved knowledge sources.",
        permissions: list[PermissionSpec] | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type=AgentType.RESEARCH,
            description=description,
            permissions=permissions or [],
        )
        self._registry = registry or get_tool_registry()

    def _execute(
        self, input_data: dict[str, Any], context: AgentExecutionContext
    ) -> AgentResult:
        query = input_data.get("query", "")
        if not query:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="No query provided",
                errors=["Query is required for research execution"],
            )

        # Determine which tools to use
        tool_name = input_data.get("tool_name", "knowledge.search")

        # Check agent has permission for the tool
        tool_def = self._registry.get(tool_name)
        if tool_def is None:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary=f"Tool '{tool_name}' not available",
                errors=[f"Tool '{tool_name}' is not registered in the tool registry"],
            )

        # Gather granted permissions from context
        granted = [p.name for p in context.permissions if p.allow]

        # Execute through the registry (permission check happens inside)
        exec_result = self._registry.execute(
            tool_name, {"query": query}, granted, context=context
        )

        # Record tool call
        tool_call = ToolCallRecord(
            tool_name=tool_name,
            input_data={"query": query},
            output_data=exec_result.output if exec_result.status == "completed" else {},
            status=exec_result.status,
            error=exec_result.error,
            duration_ms=exec_result.duration_ms,
        )

        if exec_result.status != "completed":
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary=f"Tool execution failed: {exec_result.error}",
                tool_calls=[tool_call.model_dump()],
                errors=[exec_result.error or "Unknown tool failure"],
            )

        # Build structured result
        output = exec_result.output
        summary = output.get("summary", "Research completed.")
        source = output.get("source", "unknown")

        return AgentResult(
            agent_name=self.name,
            agent_type=self.agent_type,
            status=AgentExecutionStatus.COMPLETED,
            summary=summary,
            result={
                "query": query,
                "answer": summary,
                "topic": output.get("topic", ""),
                "source": source,
                "last_reviewed": output.get("last_reviewed", ""),
            },
            evidence=[{"source": source, "topic": output.get("topic", "")}],
            tool_calls=[tool_call.model_dump()],
            confidence=0.85,
        )
