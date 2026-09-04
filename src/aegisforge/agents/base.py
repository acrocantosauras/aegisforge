from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
)
from aegisforge.observability.metrics import record_agent_execution


def _record_agent_metric(agent_type: str, status: str, duration_ms: int) -> None:
    """Record an agent execution metric (no-op if prometheus unavailable)."""
    try:
        record_agent_execution(agent_type, status, duration_ms)
    except Exception as exc:
        # Metrics must never break agent execution
        import logging

        logging.getLogger(__name__).debug("Failed to record agent metric: %s", exc)

# --- Permission Model ---


@dataclass(frozen=True)
class PermissionSpec:
    """Declares a single permission grant or denial for an agent."""

    name: str
    allow: bool = True


# --- Execution Context ---


@dataclass
class AgentExecutionContext:
    """Immutable context passed to every agent execution.

    Contains only identifiers needed for traceability — no secrets,
    no raw user content, no credentials.
    """

    request_id: str
    workflow_id: str = ""
    task_id: str = ""
    agent_execution_id: str = ""
    user_id: str = ""
    organization_id: str = ""
    correlation_id: str = ""
    permissions: list[PermissionSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_permission(self, permission_name: str) -> bool:
        """Check if a permission is explicitly allowed."""
        for perm in self.permissions:
            if perm.name == permission_name:
                return perm.allow
        return False


# --- Base Agent ---


class BaseAgent(ABC):
    """Abstract base for all agents.

    Subclasses must implement ``execute``.  The base class handles
    lifecycle tracking, timing, and permission gating.
    """

    def __init__(
        self,
        name: str,
        agent_type: AgentType,
        description: str = "",
        permissions: list[PermissionSpec] | None = None,
    ) -> None:
        self.name = name
        self.agent_type = agent_type
        self.description = description
        self.permissions = permissions or []

    def check_permissions(self, required: list[str]) -> bool:
        """Return True only if every *required* permission is granted."""
        for perm_name in required:
            if not any(p.name == perm_name and p.allow for p in self.permissions):
                return False
        return True

    def execute(self, input_data: dict[str, Any], context: AgentExecutionContext) -> AgentResult:
        """Execute the agent with full lifecycle tracking.

        Subclasses override ``_execute``; this wrapper captures timing,
        records Prometheus metrics, and converts exceptions into failed
        AgentResults.
        """
        start = time.monotonic()
        try:
            result = self._execute(input_data, context)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            result.execution_time_ms = elapsed_ms
            _record_agent_metric(self.agent_type.value, result.status.value, elapsed_ms)
            return result
        except PermissionError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_agent_metric(self.agent_type.value, "denied", elapsed_ms)
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.DENIED,
                summary="Permission denied",
                errors=[str(exc)],
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _record_agent_metric(self.agent_type.value, "failed", elapsed_ms)
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="Agent execution failed",
                errors=[str(exc)],
                execution_time_ms=elapsed_ms,
            )

    @abstractmethod
    def _execute(self, input_data: dict[str, Any], context: AgentExecutionContext) -> AgentResult:
        """Override in subclasses to implement agent logic."""
        ...
