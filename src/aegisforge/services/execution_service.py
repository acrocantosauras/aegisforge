from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_settings, get_model_provider_from_settings
from aegisforge.db.models import (
    AgentExecutionModel,
    RequestModel,
    TaskModel,
    ToolExecutionModel,
    WorkflowModel,
)
from aegisforge.domain.models import (
    RequestStatus,
)
from aegisforge.services.audit_service import record_audit_event
from aegisforge.services.request_service import update_request_status

logger = logging.getLogger(__name__)


def _build_dependencies(settings: Settings | None = None):  # type: ignore[no-untyped-def]
    """Build the dependencies for workflow execution."""
    settings = settings or get_settings()
    model_provider = get_model_provider_from_settings(settings)

    retrieval_service = None
    try:
        from aegisforge.rag.embeddings import get_embedding_provider
        from aegisforge.rag.retrieval import RetrievalService
        from aegisforge.rag.vector_store import get_vector_store
        from aegisforge.db.session import get_session_factory as _gsf

        embedding_provider = get_embedding_provider(
            settings.embedding_provider,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            batch_size=settings.embedding_batch_size,
        )
        vector_store = get_vector_store(
            "pgvector" if settings.database_url.startswith("postgresql") else "memory",
            db_session_factory=lambda: _gsf(settings)(),
            dimension=settings.embedding_dimension,
        )
        retrieval_service = RetrievalService(
            embedding_provider=embedding_provider,
            vector_store=vector_store,
        )
    except Exception as exc:
        logger.warning("Could not create retrieval service: %s", exc)

    return model_provider, retrieval_service


def execute_request(
    db: Session,
    request_id: str,
    user_id: str,
    organization_id: str,
) -> dict[str, Any]:
    """Execute a full workflow for a request.

    This orchestrates: validate → plan → execute → evaluate → complete/fail.
    Returns the final workflow state.
    """
    from aegisforge.workflows.checkpoint import WorkflowCheckpointer, get_checkpoint_store
    from aegisforge.workflows.langgraph_workflow import execute_workflow

    # Fetch the request
    request = db.query(RequestModel).filter(RequestModel.id == request_id).first()
    if request is None:
        raise ValueError(f"Request {request_id} not found")

    # Record workflow start
    record_audit_event(
        db,
        organization_id=organization_id,
        actor_id=user_id,
        action="workflow.started",
        resource_type="request",
        resource_id=request_id,
        outcome="success",
        request_id=request_id,
    )

    # Update status to executing
    update_request_status(db, request_id, RequestStatus.EXECUTING)

    # Create workflow record
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
    workflow_model = WorkflowModel(
        id=workflow_id,
        organization_id=organization_id,
        name=f"workflow-for-{request_id}",
        definition=json.dumps({"request_id": request_id}),
    )
    db.add(workflow_model)
    db.commit()

    # Build dependencies
    model_provider, retrieval_service = _build_dependencies()

    # Create checkpointer
    checkpointer = WorkflowCheckpointer(
        workflow_id=workflow_id,
        request_id=request_id,
        organization_id=organization_id,
        store=get_checkpoint_store(),
    )

    try:
        # Execute the LangGraph workflow with all dependencies
        final_state = execute_workflow(
            request_id=request_id,
            intent=request.intent,
            user_id=user_id,
            organization_id=organization_id,
            workflow_id=workflow_id,
            checkpointer=checkpointer,
            model_provider=model_provider,
            retrieval_service=retrieval_service,
        )

        # Persist agent executions
        _persist_agent_executions(db, request_id, final_state)

        # Persist tool executions
        _persist_tool_executions(db, request_id, final_state)

        # Persist workflow tasks
        _persist_tasks(db, request_id, final_state)

        # Determine final status
        final_status = final_state.get("status", "failed")
        try:
            status_enum = RequestStatus(final_status)
        except ValueError:
            status_enum = RequestStatus.COMPLETED

        update_request_status(db, request_id, status_enum)

        # Record completion
        record_audit_event(
            db,
            organization_id=organization_id,
            actor_id=user_id,
            action="workflow.completed",
            resource_type="request",
            resource_id=request_id,
            outcome="success" if status_enum == RequestStatus.COMPLETED else "failure",
            metadata={"final_status": final_status, "workflow_id": workflow_id},
            request_id=request_id,
        )

        return {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "status": final_status,
            "final_result": final_state.get("final_result", {}),
            "errors": final_state.get("errors", []),
        }

    except Exception as exc:
        logger.exception("Workflow execution failed for request %s", request_id)
        update_request_status(db, request_id, RequestStatus.FAILED)

        record_audit_event(
            db,
            organization_id=organization_id,
            actor_id=user_id,
            action="workflow.failed",
            resource_type="request",
            resource_id=request_id,
            outcome="failure",
            metadata={"error": str(exc), "workflow_id": workflow_id},
            request_id=request_id,
        )

        return {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "status": "failed",
            "errors": [str(exc)],
        }


def _persist_agent_executions(
    db: Session, request_id: str, state: dict[str, Any]
) -> None:
    """Persist agent execution records from workflow state."""
    agent_result_dict = state.get("agent_result", {})
    if not agent_result_dict:
        return

    execution = AgentExecutionModel(
        id=str(uuid.uuid4()),
        request_id=request_id,
        agent_id=agent_result_dict.get("agent_name", "unknown"),
        agent_name=agent_result_dict.get("agent_name", "unknown"),
        status=agent_result_dict.get("status", "unknown"),
        result=json.dumps(agent_result_dict),
    )
    db.add(execution)
    db.commit()


def _persist_tool_executions(
    db: Session, request_id: str, state: dict[str, Any]
) -> None:
    """Persist tool execution records from workflow state."""
    for tc in state.get("tool_calls", []):
        execution = ToolExecutionModel(
            id=str(uuid.uuid4()),
            request_id=request_id,
            tool_name=tc.get("tool_name", "unknown"),
            status=tc.get("status", "unknown"),
            result=json.dumps(tc),
        )
        db.add(execution)
    db.commit()


def _persist_tasks(db: Session, request_id: str, state: dict[str, Any]) -> None:
    """Persist task records from the execution plan."""
    plan = state.get("plan", {})
    tasks = plan.get("tasks", [])
    for task_dict in tasks:
        task = TaskModel(
            id=task_dict.get("task_id", str(uuid.uuid4())),
            request_id=request_id,
            title=task_dict.get("description", "")[:255],
            description=task_dict.get("description", ""),
            agent_type=task_dict.get("assigned_agent_type", "research"),
            status="completed",
            dependencies=json.dumps(task_dict.get("dependencies", [])),
        )
        db.add(task)
    db.commit()
