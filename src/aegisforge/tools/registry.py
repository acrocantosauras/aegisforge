from __future__ import annotations

import logging
from typing import Any

from aegisforge.tools.base import BaseTool, ToolDefinition, ToolExecutionResult, TStatus

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for all approved tools.

    Tools are registered at startup.  Agents discover and execute tools
    through this registry — they never access tools directly.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool.  Raises ValueError on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s v%s", tool.name, tool.definition.version)

    def get(self, name: str) -> BaseTool | None:
        """Retrieve a tool by name, or None if not found."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """Return definitions for all registered tools."""
        return [t.definition for t in self._tools.values()]

    def list_tool_names(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        granted_permissions: list[str] | None = None,
        context: Any | None = None,
    ) -> ToolExecutionResult:
        """Discover and execute a tool by name.

        Permission checks run before execution. If *granted_permissions* is
        ``None``, permission checking is skipped (used for internal/trusted
        execution paths only).

        Deny-by-default: unknown tools, disabled tools, and tools whose
        permission requirements are not satisfied must not execute.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolExecutionResult(
                status=TStatus.FAILED,
                error=f"Tool '{tool_name}' not found in registry",
                tool_name=tool_name,
            )
        # Fail closed: disabled tools must not execute.
        if not getattr(tool.definition, "enabled", True):
            return ToolExecutionResult(
                status=TStatus.DENIED,
                error=f"Tool '{tool_name}' is disabled",
                tool_name=tool_name,
            )
        result = tool.execute(input_data, granted_permissions, context=context)
        logger.info(
            "Tool execution: %s -> %s (%d ms)",
            tool_name,
            result.status,
            result.duration_ms,
        )
        return result

    def update(self, tool: BaseTool) -> None:
        """Register or replace a tool by name."""
        self._tools[tool.name] = tool
        logger.info("Updated tool: %s v%s", tool.name, tool.definition.version)

    def validate_permissions(self, tool_name: str, granted: list[str]) -> bool:
        """Check whether *granted* permissions satisfy the tool's requirements."""
        tool = self._tools.get(tool_name)
        if tool is None:
            return False
        return tool.has_permissions(granted)


# Module-level singleton for convenience.
_default_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Return the default singleton registry, creating it if needed."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry


def reset_tool_registry() -> None:
    """Reset the singleton (useful in tests)."""
    global _default_registry
    _default_registry = None
