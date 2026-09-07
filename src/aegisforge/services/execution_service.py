from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_model_provider_from_settings, get_settings
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
    is_production = settings.environment not in ("development", "test", "")
    try:
        from aegisforge.db.session import get_session_factory as _gsf
        from aegisforge.rag.embeddings import get_embedding_provider
        from aegisforge.rag.retrieval import RetrievalService
        from aegisforge.rag.vector_store import get_vector_store

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
        if settings.rag_hybrid_enabled:
            from aegisforge.rag.hybrid import build_hybrid_retrieval_adapter

            retrieval_service = build_hybrid_retrieval_adapter(
                retrieval_service,
                vector_store,
                reranker_type=settings.rag_reranker,
                query_expansion_enabled=settings.rag_query_expansion_enabled,
                query_expansion_max=settings.rag_query_expansion_max,
                fusion_candidates=settings.rag_fusion_candidates,
                lexical_top_k=settings.rag_lexical_top_k,
                context_max_tokens=settings.rag_context_max_tokens,
            )
    except Exception as exc:
        if is_production and settings.database_url.startswith("postgresql"):
            logger.critical(
                "PostgreSQL/pgvector required in production but failed to "
                "initialize: %s", exc,
            )
            raise
        logger.warning("Could not create retrieval service: %s", exc)

    return model_provider, retrieval_service


def _create_execution_job_record(
    db: Session,
    request_id: str,
    workflow_id: str,
    organization_id: str,
    status: str = "running",
) -> str:
    """Create an ExecutionJobModel row so approvals can reference a real job."""
    from aegisforge.db.models import ExecutionJobModel

    job_id = f"job-{uuid.uuid4().hex[:12]}"
    db.add(
        ExecutionJobModel(
            id=job_id,
            request_id=request_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            status=status,
            retry_count=0,
            max_retries=3,
        )
    )
    db.commit()
    return job_id


def execute_request(
    db: Session,
    request_id: str,
    user_id: str,
    organization_id: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute a full workflow for a request.

    This orchestrates: validate → plan → execute → evaluate → complete/fail.
    Returns the final workflow state.
    """
    from aegisforge.approval.service import ApprovalService
    from aegisforge.workflows.checkpoint import WorkflowCheckpointer, get_db_checkpoint_store
    from aegisforge.workflows.langgraph_workflow import execute_workflow

    settings = settings or get_settings()

    # Fetch the request (tenant-isolated)
    request = (
        db.query(RequestModel)
        .filter(RequestModel.id == request_id, RequestModel.organization_id == organization_id)
        .first()
    )
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

    # Create a job record so approvals have a valid job FK
    job_id = _create_execution_job_record(db, request_id, workflow_id, organization_id)

    # Build dependencies
    model_provider, retrieval_service = _build_dependencies(settings)
    approval_service = ApprovalService(
        session_factory=lambda: db,
        approval_timeout_hours=settings.approval_timeout_hours,
    )

    # Durable (DB-backed) checkpoint store
    checkpoint_store = get_db_checkpoint_store(settings)
    checkpointer = WorkflowCheckpointer(
        workflow_id=workflow_id,
        request_id=request_id,
        organization_id=organization_id,
        store=checkpoint_store,
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
            approval_service=approval_service,
            job_id=job_id,
        )

        # Persist agent executions
        _persist_agent_executions(db, request_id, final_state)

        # Persist tool executions
        _persist_tool_executions(db, request_id, final_state)

        # Persist workflow tasks
        _persist_tasks(db, request_id, final_state)

        # Update the job record with the final state
        _update_job_record(db, job_id, final_state)

        # Determine final status
        final_status = final_state.get("status", "failed")
        if final_status == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
            # Workflow paused for human approval — record the paused state
            update_request_status(db, request_id, RequestStatus.ACTION_REQUIRES_APPROVAL)
            record_audit_event(
                db,
                organization_id=organization_id,
                actor_id=user_id,
                action="workflow.paused_for_approval",
                resource_type="request",
                resource_id=request_id,
                outcome="success",
                metadata={
                    "final_status": final_status,
                    "workflow_id": workflow_id,
                    "approval_id": final_state.get("approval_id", ""),
                },
                request_id=request_id,
            )
            return {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "job_id": job_id,
                "status": final_status,
                "approval_id": final_state.get("approval_id", ""),
                "final_result": {},
                "errors": [],
            }

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
            "job_id": job_id,
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
            "job_id": job_id,
            "status": "failed",
            "errors": [str(exc)],
        }


def _update_job_record(db: Session, job_id: str, final_state: dict[str, Any]) -> None:
    """Persist final job status/result to the ExecutionJobModel row."""
    from aegisforge.db.models import ExecutionJobModel

    job_model = db.query(ExecutionJobModel).filter(ExecutionJobModel.id == job_id).first()
    if job_model is None:
        return
    final_status = final_state.get("status", "failed")
    if final_status == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
        job_model.status = "waiting_for_approval"
    else:
        job_model.status = "completed" if final_status == RequestStatus.COMPLETED.value else "failed"
    job_model.result_json = json.dumps(
        {
            "status": final_status,
            "final_result": final_state.get("final_result", {}),
            "errors": final_state.get("errors", []),
        }
    )
    db.commit()


def resume_request_after_approval(
    db: Session,
    request_id: str,
    workflow_id: str,
    approval_decision: str,
    approval_id: str = "",
    reviewer_id: str = "",
    organization_id: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Resume a paused workflow after an approval decision.

    approved → continue the workflow from its checkpoint.
    rejected → mark the request failed; the protected action never executes.
    """
    from aegisforge.approval.service import ApprovalService
    from aegisforge.workflows.checkpoint import WorkflowCheckpointer, get_db_checkpoint_store
    from aegisforge.workflows.langgraph_workflow import execute_workflow

    settings = settings or get_settings()

    request = (
        db.query(RequestModel)
        .filter(RequestModel.id == request_id, RequestModel.organization_id == organization_id)
        .first()
    )
    if request is None:
        raise ValueError(f"Request {request_id} not found")

    if approval_decision == "rejected":
        update_request_status(db, request_id, RequestStatus.FAILED)
        record_audit_event(
            db,
            organization_id=organization_id,
            actor_id=reviewer_id,
            action="workflow.rejected_by_approval",
            resource_type="request",
            resource_id=request_id,
            outcome="failure",
            metadata={"approval_id": approval_id, "workflow_id": workflow_id},
            request_id=request_id,
        )
        return {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "status": RequestStatus.FAILED.value,
            "errors": [f"Approval {approval_id} rejected by {reviewer_id}"],
            "final_result": {},
        }

    model_provider, retrieval_service = _build_dependencies(settings)
    approval_service = ApprovalService(
        session_factory=lambda: db,
        approval_timeout_hours=settings.approval_timeout_hours,
    )
    checkpoint_store = get_db_checkpoint_store(settings)
    checkpointer = WorkflowCheckpointer(
        workflow_id=workflow_id,
        request_id=request_id,
        organization_id=organization_id,
        store=checkpoint_store,
    )

    final_state = execute_workflow(
        request_id=request_id,
        intent=request.intent,
        user_id=request.requested_by,
        organization_id=organization_id,
        workflow_id=workflow_id,
        checkpointer=checkpointer,
        resume_from_checkpoint=True,
        model_provider=model_provider,
        retrieval_service=retrieval_service,
        approval_service=approval_service,
        job_id="",
    )

    _persist_agent_executions(db, request_id, final_state)
    _persist_tool_executions(db, request_id, final_state)
    _persist_tasks(db, request_id, final_state)

    final_status = final_state.get("status", "failed")
    try:
        status_enum = RequestStatus(final_status)
    except ValueError:
        status_enum = RequestStatus.COMPLETED
    update_request_status(db, request_id, status_enum)

    record_audit_event(
        db,
        organization_id=organization_id,
        actor_id=reviewer_id or request.requested_by,
        action="workflow.resumed_after_approval",
        resource_type="request",
        resource_id=request_id,
        outcome="success" if status_enum == RequestStatus.COMPLETED else "failure",
        metadata={"approval_id": approval_id, "workflow_id": workflow_id, "final_status": final_status},
        request_id=request_id,
    )

    return {
        "request_id": request_id,
        "workflow_id": workflow_id,
        "status": final_status,
        "final_result": final_state.get("final_result", {}),
        "errors": final_state.get("errors", []),
    }


def _persist_agent_executions(
    db: Session, request_id: str, state: dict[str, Any]
) -> None:
    """Persist agent execution records from workflow state.

    Multi-agent runs persist one record per task (from ``task_records``);
    the classic single-agent path persists the single ``agent_result``.
    """
    task_records = state.get("task_records", {}) or {}
    if task_records:
        for task_id, record in task_records.items():
            execution = AgentExecutionModel(
                id=str(uuid.uuid4()),
                request_id=request_id,
                agent_id=task_id,
                agent_name=str(record.get("agent_type", task_id)),
                status=str(record.get("status", "unknown")),
                result=json.dumps(record),
            )
            db.add(execution)
        db.commit()
        return

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
    """Persist task records from the execution plan (idempotent).

    Multi-agent runs persist accurate per-task statuses from
    ``task_records``; the classic single-agent path records every planned
    task as completed (as before).
    """
    plan = state.get("plan", {})
    tasks = plan.get("tasks", [])
    task_records = state.get("task_records", {}) or {}

    for task_dict in tasks:
        task_id = task_dict.get("task_id", "")
        if not task_id:
            task_id = str(uuid.uuid4())
        record = task_records.get(task_id, {})
        status = str(record.get("status", "completed")) if task_records else "completed"
        agent_type = task_dict.get("assigned_agent_type", "research")
        dependencies = json.dumps(task_dict.get("dependencies", []))

        existing = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if existing is not None:
            existing.status = status
            existing.agent_type = agent_type
            existing.title = task_dict.get("description", "")[:255]
            existing.description = task_dict.get("description", "")
            existing.dependencies = dependencies
        else:
            db.add(
                TaskModel(
                    id=task_id,
                    request_id=request_id,
                    title=task_dict.get("description", "")[:255],
                    description=task_dict.get("description", ""),
                    agent_type=agent_type,
                    status=status,
                    dependencies=dependencies,
                )
            )
    db.commit()
