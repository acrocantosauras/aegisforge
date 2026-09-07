"""Workflow introspection endpoints for multi-agent runs (Phase 5).

Exposes the task dependency graph, per-task execution state, and the
workflow-level evaluation for an authenticated user's own organization.
Data is read from the durable checkpoint state (the source of truth after a
run) so no internal prompts, secrets, or unrelated tenant data leak out.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_settings
from aegisforge.db.models import ExecutionJobModel, UserModel, WorkflowModel
from aegisforge.db.session import get_db
from aegisforge.domain.models import ExecutionPlan
from aegisforge.services.auth_service import get_current_user
from aegisforge.workflows.checkpoint import WorkflowCheckpointer, get_db_checkpoint_store

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowTaskRead(BaseModel):
    task_id: str
    description: str
    agent_type: str
    dependencies: list[str] = Field(default_factory=list)
    status: str = "pending"
    retries: int = 0
    duration_ms: int | None = None
    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    approval_required: bool = False
    tools_used: list[str] = Field(default_factory=list)
    evidence_count: int = 0


class WorkflowGraphRead(BaseModel):
    workflow_id: str
    request_id: str
    status: str
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowTasksRead(BaseModel):
    workflow_id: str
    request_id: str
    status: str
    tasks: list[WorkflowTaskRead] = Field(default_factory=list)


class WorkflowEvaluationsRead(BaseModel):
    workflow_id: str
    request_id: str
    evaluation: dict[str, Any] = Field(default_factory=dict)


def _load_run_state(
    db: Session,
    workflow_id: str,
    organization_id: str,
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    """Load the latest checkpoint state for a workflow (tenant-scoped)."""
    workflow_model = (
        db.query(WorkflowModel)
        .filter(
            WorkflowModel.id == workflow_id,
            WorkflowModel.organization_id == organization_id,
        )
        .first()
    )
    if workflow_model is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    job = (
        db.query(ExecutionJobModel)
        .filter(
            ExecutionJobModel.workflow_id == workflow_id,
            ExecutionJobModel.organization_id == organization_id,
        )
        .first()
    )
    request_id = job.request_id if job is not None else ""

    store = get_db_checkpoint_store(settings)
    checkpointer = WorkflowCheckpointer(
        workflow_id=workflow_id,
        request_id=request_id,
        organization_id=organization_id,
        store=store,
    )
    state = checkpointer.load_resume_state() or {}
    return state, request_id


def _task_views(state: dict[str, Any]) -> list[WorkflowTaskRead]:
    plan_dict = state.get("plan", {})
    tasks = plan_dict.get("tasks", []) or []
    records = state.get("task_records", {}) or {}
    views: list[WorkflowTaskRead] = []
    for task in tasks:
        task_id = task.get("task_id", "")
        record = records.get(task_id, {})
        tools_used: list[str] = []
        for tc in record.get("tool_calls", []) or []:
            name = tc.get("tool_name") or tc.get("name")
            if name and name not in tools_used:
                tools_used.append(name)
        output = record.get("output") or {}
        views.append(
            WorkflowTaskRead(
                task_id=task_id,
                description=task.get("description", ""),
                agent_type=str(task.get("assigned_agent_type", record.get("agent_type", ""))),
                dependencies=list(task.get("dependencies", []) or []),
                status=str(record.get("status", "pending")),
                retries=int(record.get("retry_count", 0) or 0),
                duration_ms=record.get("duration_ms"),
                summary=str(record.get("summary", "") or "")[:400],
                errors=list(record.get("errors", []) or [])[:5],
                approval_required=bool(record.get("approval_required", False)),
                tools_used=tools_used,
                evidence_count=len(record.get("evidence", []) or []) + len(output.get("citations", []) or []),
            )
        )
    return views


@router.get("/{workflow_id}", response_model=WorkflowGraphRead)
def get_workflow_overview(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WorkflowGraphRead:
    """Get workflow overview + task dependency graph."""
    state, request_id = _load_run_state(db, workflow_id, user.organization_id, settings)
    status = state.get("status", "unknown")
    if not state.get("plan"):
        # Fall back to an empty graph rather than leaking partial data.
        return WorkflowGraphRead(
            workflow_id=workflow_id, request_id=request_id, status=status
        )

    tasks = state.get("plan", {}).get("tasks", []) or []
    records = state.get("task_records", {}) or {}
    nodes = [
        {
            "task_id": task.get("task_id", ""),
            "agent_type": str(task.get("assigned_agent_type", "")),
            "status": str((records.get(task.get("task_id", "")) or {}).get("status", "pending")),
        }
        for task in tasks
    ]
    edges = [
        {"from": dep, "to": task.get("task_id", "")}
        for task in tasks
        for dep in (task.get("dependencies", []) or [])
    ]
    return WorkflowGraphRead(
        workflow_id=workflow_id,
        request_id=request_id,
        status=status,
        nodes=nodes,
        edges=edges,
    )


@router.get("/{workflow_id}/tasks", response_model=WorkflowTasksRead)
def get_workflow_tasks(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WorkflowTasksRead:
    """List tasks (with state) for a workflow — used by the frontend graph."""
    state, request_id = _load_run_state(db, workflow_id, user.organization_id, settings)
    return WorkflowTasksRead(
        workflow_id=workflow_id,
        request_id=request_id,
        status=state.get("status", "unknown"),
        tasks=_task_views(state),
    )


@router.get("/{workflow_id}/evaluations", response_model=WorkflowEvaluationsRead)
def get_workflow_evaluations(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WorkflowEvaluationsRead:
    """Get the workflow-level evaluation (measured, never fabricated)."""
    state, request_id = _load_run_state(db, workflow_id, user.organization_id, settings)
    evaluation = state.get("workflow_evaluation")

    if evaluation is None and state.get("plan") and state.get("task_records"):
        try:
            plan = ExecutionPlan(**state["plan"])
            from aegisforge.evaluation.workflow_evaluator import evaluate_workflow_run

            records = state.get("task_records", {}) or {}
            final_output: dict[str, Any] = {}
            for rec in records.values():
                if str(rec.get("agent_type", "")) == "synthesis":
                    final_output = rec.get("output") or {}
                    break
            evaluation = evaluate_workflow_run(
                plan, records, final_output=final_output
            ).to_dict()
        except Exception:
            evaluation = {}

    return WorkflowEvaluationsRead(
        workflow_id=workflow_id,
        request_id=request_id,
        evaluation=evaluation or {},
    )
