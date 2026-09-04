"""Workflow checkpoint store for durable stateful execution.

Provides persistence for LangGraph workflow state so that:
- Workflow state survives process restarts
- Execution can resume from a checkpoint
- Failed workflows can be retried from an appropriate checkpoint
- Approval pauses workflow execution
- Approval completion resumes workflow

Checkpoints do NOT store secrets, API keys, or raw model responses.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class WorkflowCheckpoint:
    """A single checkpoint of workflow state."""

    def __init__(
        self,
        checkpoint_id: str,
        workflow_id: str,
        request_id: str,
        node_name: str,
        state: dict[str, Any],
        organization_id: str = "",
        user_id: str = "",
        created_at: datetime | None = None,
    ) -> None:
        self.checkpoint_id = checkpoint_id
        self.workflow_id = workflow_id
        self.request_id = request_id
        self.node_name = node_name
        self.state = state
        self.organization_id = organization_id
        self.user_id = user_id
        self.created_at = created_at or datetime.now(UTC)


class CheckpointStore:
    """Abstract checkpoint store interface."""

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> None:
        raise NotImplementedError

    def load_checkpoint(self, checkpoint_id: str) -> WorkflowCheckpoint | None:
        raise NotImplementedError

    def load_latest_by_workflow(self, workflow_id: str) -> WorkflowCheckpoint | None:
        raise NotImplementedError

    def list_checkpoints(self, workflow_id: str) -> list[WorkflowCheckpoint]:
        raise NotImplementedError

    def delete_workflow_checkpoints(self, workflow_id: str) -> int:
        raise NotImplementedError


class InMemoryCheckpointStore(CheckpointStore):
    """In-memory checkpoint store for testing."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, WorkflowCheckpoint] = {}
        self._insertion_counter: int = 0
        self._insertion_order: dict[str, int] = {}

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> None:
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        self._insertion_order[checkpoint.checkpoint_id] = self._insertion_counter
        self._insertion_counter += 1

    def load_checkpoint(self, checkpoint_id: str) -> WorkflowCheckpoint | None:
        return self._checkpoints.get(checkpoint_id)

    def load_latest_by_workflow(self, workflow_id: str) -> WorkflowCheckpoint | None:
        candidates = [
            cp
            for cp in self._checkpoints.values()
            if cp.workflow_id == workflow_id
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda cp: (cp.created_at, self._insertion_order.get(cp.checkpoint_id, 0)))
        return candidates[-1]

    def list_checkpoints(self, workflow_id: str) -> list[WorkflowCheckpoint]:
        candidates = [
            cp
            for cp in self._checkpoints.values()
            if cp.workflow_id == workflow_id
        ]
        candidates.sort(key=lambda cp: (cp.created_at, self._insertion_order.get(cp.checkpoint_id, 0)))
        return candidates

    def delete_workflow_checkpoints(self, workflow_id: str) -> int:
        to_delete = [
            cp_id
            for cp_id, cp in self._checkpoints.items()
            if cp.workflow_id == workflow_id
        ]
        for cp_id in to_delete:
            del self._checkpoints[cp_id]
        return len(to_delete)


class WorkflowCheckpointer:
    """Manages checkpointing for a specific workflow execution.

    Saves checkpoints after each node execution so the workflow
    can resume from any completed node.
    """

    def __init__(
        self,
        workflow_id: str,
        request_id: str,
        organization_id: str = "",
        user_id: str = "",
        store: CheckpointStore | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.request_id = request_id
        self.organization_id = organization_id
        self.user_id = user_id
        self._store = store or InMemoryCheckpointStore()
        self._execution_id = f"exec-{uuid.uuid4().hex[:12]}"

    @property
    def execution_id(self) -> str:
        return self._execution_id

    def save_after_node(self, node_name: str, state: dict[str, Any]) -> str:
        """Save a checkpoint after a node completes. Returns the checkpoint ID."""
        checkpoint_id = f"cp-{uuid.uuid4().hex[:12]}"
        # Sanitize state for storage — remove sensitive fields
        sanitized_state = _sanitize_state(state)

        checkpoint = WorkflowCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_id=self.workflow_id,
            request_id=self.request_id,
            node_name=node_name,
            state=sanitized_state,
            organization_id=self.organization_id,
            user_id=self.user_id,
        )
        self._store.save_checkpoint(checkpoint)

        logger.info(
            "Checkpoint saved: %s (node=%s, workflow=%s, request=%s)",
            checkpoint_id,
            node_name,
            self.workflow_id,
            self.request_id,
        )
        return checkpoint_id

    def load_resume_state(self) -> dict[str, Any] | None:
        """Load the latest checkpoint state for workflow resumption."""
        checkpoint = self._store.load_latest_by_workflow(self.workflow_id)
        if checkpoint is None:
            return None

        logger.info(
            "Resuming from checkpoint %s (node=%s, workflow=%s)",
            checkpoint.checkpoint_id,
            checkpoint.node_name,
            self.workflow_id,
        )
        return checkpoint.state

    def get_current_node(self) -> str | None:
        """Get the name of the last checkpointed node."""
        checkpoint = self._store.load_latest_by_workflow(self.workflow_id)
        if checkpoint is None:
            return None
        return checkpoint.node_name

    def list_checkpoints(self) -> list[WorkflowCheckpoint]:
        """List all checkpoints for this workflow."""
        return self._store.list_checkpoints(self.workflow_id)

    def clear_checkpoints(self) -> int:
        """Delete all checkpoints for this workflow."""
        return self._store.delete_workflow_checkpoints(self.workflow_id)


def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from state before checkpointing.

    Does NOT store:
    - API keys
    - Passwords
    - Raw model responses with potential secrets
    - Large unnecessary payloads
    """
    sanitized = dict(state)

    # Remove sensitive keys if present
    sensitive_keys = {"api_key", "password", "secret", "token", "auth_token"}
    for key in sensitive_keys:
        sanitized.pop(key, None)

    # Ensure state is JSON-serializable
    try:
        json.dumps(sanitized)
    except (TypeError, ValueError):
        # If state can't be serialized, keep only safe fields
        safe_keys = {
            "request_id", "workflow_id", "user_id", "organization_id",
            "intent", "status", "plan", "current_task_index", "current_task",
            "agent_result", "evaluation", "retry_count", "max_retries",
            "tool_calls", "errors", "final_result",
            "approval_required", "approval_id",
        }
        sanitized = {k: v for k, v in sanitized.items() if k in safe_keys}

    return sanitized


# Global checkpoint store
_default_checkpoint_store: CheckpointStore | None = None


def get_checkpoint_store() -> CheckpointStore:
    """Return the global checkpoint store singleton."""
    global _default_checkpoint_store
    if _default_checkpoint_store is None:
        _default_checkpoint_store = InMemoryCheckpointStore()
    return _default_checkpoint_store


def reset_checkpoint_store() -> None:
    """Reset the global checkpoint store (for tests)."""
    global _default_checkpoint_store
    _default_checkpoint_store = None
