from __future__ import annotations

from typing import Any

from aegisforge.tools.base import BaseTool, ToolDefinition


class ResearchTool(BaseTool):
    def __init__(self) -> None:
        definition = ToolDefinition(
            name="knowledge.search",
            description="Search approved internal knowledge sources.",
            input_schema={"query": "string"},
            output_schema={"answer": "string", "source": "string"},
            permission_requirements=["knowledge.search"],
            timeout_seconds=30,
        )
        super().__init__(definition)

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query", ""))
        return {
            "answer": f"Approved internal guidance for '{query}' indicates a documented review process before final recommendation.",
            "source": "approved-internal-research-tool",
        }
