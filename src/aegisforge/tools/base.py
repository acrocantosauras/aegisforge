from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from aegisforge.domain.models import ToolExecutionStatus
from aegisforge.domain.models import ToolExecutionStatus as TStatus
from aegisforge.observability.metrics import track_tool_execution


class _NoOpCtx:
    """Fallback context manager when prometheus is unavailable."""

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _track_tool(tool_name: str) -> Any:
    """Return a tracking context manager, degrading gracefully."""
    try:
        return track_tool_execution(tool_name)
    except Exception:
        return _NoOpCtx()

# --- Tool Definition ---


class ToolInputSchema(BaseModel):
    """Base schema for tool inputs. Subclass per tool."""

    model_config = {"extra": "forbid"}


class ToolOutputSchema(BaseModel):
    """Base schema for tool outputs. Subclass per tool."""

    model_config = {"extra": "allow"}


@dataclass
class ToolDefinition:
    """Declarative metadata for a tool."""

    name: str
    description: str
    version: str = "1.0"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    permission_requirements: list[str] = field(default_factory=list)
    timeout_seconds: int = 30
    requires_approval: bool = False
    # Phase 5 — risk classification. Server/operator-controlled metadata:
    # the planner/LLM can never downgrade these values.
    risk_level: str = "low"  # low | medium | high | critical
    read_only: bool = True
    external_side_effect: bool = False
    data_sensitivity: str = "internal"  # internal | confidential | restricted


# --- Tool Execution Result ---


@dataclass
class ToolExecutionResult:
    """Outcome of a single tool invocation."""

    status: ToolExecutionStatus
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    tool_name: str = ""
    execution_id: str = ""


# --- Base Tool ---


class BaseTool(ABC):
    """Abstract base for all tools.

    Subclasses implement ``_execute``.  The base class handles
    timing, timeout awareness, and error wrapping.
    """

    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def required_permissions(self) -> list[str]:
        return self.definition.permission_requirements

    def has_permissions(self, granted: list[str]) -> bool:
        """Check whether *granted* permissions satisfy this tool's requirements."""
        for req in self.required_permissions:
            if req not in granted:
                return False
        return True

    def execute(
        self,
        input_data: dict[str, Any],
        granted_permissions: list[str] | None = None,
        context: Any | None = None,
    ) -> ToolExecutionResult:
        """Public entry point: permission check → validation → execution."""
        exec_id = str(uuid.uuid4())

        # Permission check
        if granted_permissions is not None and not self.has_permissions(granted_permissions):
            return ToolExecutionResult(
                status=TStatus.DENIED,
                error=f"Missing required permissions: {self.required_permissions}",
                tool_name=self.name,
                execution_id=exec_id,
            )

        # Execute with timing + Prometheus instrumentation
        start = time.monotonic()
        try:
            with _track_tool(self.name):
                output = self._execute(input_data, context=context)
            elapsed = int((time.monotonic() - start) * 1000)
            return ToolExecutionResult(
                status=TStatus.COMPLETED,
                output=output,
                duration_ms=elapsed,
                tool_name=self.name,
                execution_id=exec_id,
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            return ToolExecutionResult(
                status=TStatus.TIMEOUT,
                error=f"Tool {self.name} timed out after {self.definition.timeout_seconds}s",
                duration_ms=elapsed,
                tool_name=self.name,
                execution_id=exec_id,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            return ToolExecutionResult(
                status=TStatus.FAILED,
                error=str(exc),
                duration_ms=elapsed,
                tool_name=self.name,
                execution_id=exec_id,
            )

    @abstractmethod
    def _execute(
        self, input_data: dict[str, Any], context: Any | None = None
    ) -> dict[str, Any]:
        """Override in subclasses with actual tool logic."""
        ...
