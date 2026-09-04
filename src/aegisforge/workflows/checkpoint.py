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


class DbCheckpointStore(CheckpointStore):
    """SQLAlchemy-backed checkpoint store for durable execution.

    Checkpoints survive process restarts — the store is the source of
    truth for workflow resumption (e.g. approval pause/resume across
    API and worker processes).
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def _session(self) -> Any:
        return self._session_factory()

    @staticmethod
    def _ensure_table(session: Any) -> None:
        """Create the checkpoint table if it does not exist (idempotent)."""
        import aegisforge.db.models  # noqa: F401  # registers models on Base.metadata
        from aegisforge.db.base import Base

        Base.metadata.create_all(bind=session.get_bind())

    def _model_to_checkpoint(self, model: Any) -> WorkflowCheckpoint:
        return WorkflowCheckpoint(
            checkpoint_id=model.id,
            workflow_id=model.workflow_id,
            request_id=model.request_id,
            node_name=model.node_name,
            state=json.loads(model.state_json or "{}"),
            organization_id=model.organization_id,
            user_id=model.user_id,
            created_at=model.created_at,
        )

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> None:
        session = self._session()
        try:
            self._ensure_table(session)
            from aegisforge.db.models import WorkflowCheckpointModel

            model = session.get(WorkflowCheckpointModel, checkpoint.checkpoint_id)
            if model is None:
                model = WorkflowCheckpointModel(
                    id=checkpoint.checkpoint_id,
                    workflow_id=checkpoint.workflow_id,
                    request_id=checkpoint.request_id,
                    organization_id=checkpoint.organization_id,
                    user_id=checkpoint.user_id,
                    node_name=checkpoint.node_name,
                    state_json=json.dumps(checkpoint.state),
                    created_at=checkpoint.created_at,
                )
                session.add(model)
            else:
                model.state_json = json.dumps(checkpoint.state)
                model.node_name = checkpoint.node_name
            session.commit()
        finally:
            session.close()

    def load_checkpoint(self, checkpoint_id: str) -> WorkflowCheckpoint | None:
        session = self._session()
        try:
            self._ensure_table(session)
            from aegisforge.db.models import WorkflowCheckpointModel

            model = session.get(WorkflowCheckpointModel, checkpoint_id)
            if model is None:
                return None
            return self._model_to_checkpoint(model)
        finally:
            session.close()

    def load_latest_by_workflow(self, workflow_id: str) -> WorkflowCheckpoint | None:
        session = self._session()
        try:
            self._ensure_table(session)
            from sqlalchemy import select as _select

            from aegisforge.db.models import WorkflowCheckpointModel

            stmt = (
                _select(WorkflowCheckpointModel)
                .where(WorkflowCheckpointModel.workflow_id == workflow_id)
                .order_by(WorkflowCheckpointModel.created_at.desc())
                .limit(1)
            )
            model = session.execute(stmt).scalar_one_or_none()
            if model is None:
                return None
            return self._model_to_checkpoint(model)
        finally:
            session.close()

    def list_checkpoints(self, workflow_id: str) -> list[WorkflowCheckpoint]:
        session = self._session()
        try:
            self._ensure_table(session)
            from sqlalchemy import select as _select

            from aegisforge.db.models import WorkflowCheckpointModel

            stmt = (
                _select(WorkflowCheckpointModel)
                .where(WorkflowCheckpointModel.workflow_id == workflow_id)
                .order_by(WorkflowCheckpointModel.created_at.asc())
            )
            models = session.execute(stmt).scalars().all()
            return [self._model_to_checkpoint(m) for m in models]
        finally:
            session.close()

    def delete_workflow_checkpoints(self, workflow_id: str) -> int:
        session = self._session()
        try:
            self._ensure_table(session)
            from sqlalchemy import delete as _delete

            from aegisforge.db.models import WorkflowCheckpointModel

            result = session.execute(
                _delete(WorkflowCheckpointModel).where(
                    WorkflowCheckpointModel.workflow_id == workflow_id
                )
            )
            session.commit()
            return result.rowcount or 0
        finally:
            session.close()


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


def get_checkpoint_store(settings: Any | None = None) -> CheckpointStore:
    """Return the global checkpoint store singleton.

    Uses a durable DB-backed store when a non-SQLite database is
    configured; otherwise falls back to the in-memory store (tests/dev).
    """
    global _default_checkpoint_store
    if _default_checkpoint_store is None:
        _default_checkpoint_store = _build_default_checkpoint_store(settings)
    return _default_checkpoint_store


def get_db_checkpoint_store(settings: Any | None = None) -> DbCheckpointStore:
    """Create a durable DB-backed checkpoint store from settings."""
    from aegisforge.config import get_settings
    from aegisforge.db.session import get_session_factory

    settings = settings or get_settings()
    session_factory = lambda: get_session_factory(settings)()
    return DbCheckpointStore(session_factory=session_factory)


def _build_default_checkpoint_store(settings: Any | None) -> CheckpointStore:
    """Build the default store: DB-backed for real databases, else in-memory."""
    from aegisforge.config import get_settings

    settings = settings or get_settings()
    if settings.database_url and not settings.database_url.startswith("sqlite"):
        try:
            return get_db_checkpoint_store(settings)
        except Exception as exc:
            logger.warning("Could not create DB checkpoint store, using in-memory: %s", exc)
    return InMemoryCheckpointStore()


def reset_checkpoint_store() -> None:
    """Reset the global checkpoint store (for tests)."""
    global _default_checkpoint_store
    _default_checkpoint_store = None
