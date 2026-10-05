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
from aegisforge.workflows.langgraph_workflow import _pick_final_agent_result

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


class WorkflowResultRead(BaseModel):
    """Final synthesized result for a workflow (measured, never fabricated).

    Exposes the synthesis agent's answer, citations, evidence metadata,
    contributing tools, and confidence. Contains execution metadata only —
    never raw prompts, chain-of-thought, or secrets.
    """

    workflow_id: str
    request_id: str
    status: str
    agent_type: str = ""
    answer: str = ""
    summary: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    confidence: float | None = None
    failed_upstream: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class WorkflowLookupRead(BaseModel):
    """Resolve the latest workflow for a request (for UI navigation)."""

    request_id: str
    workflow_id: str
    status: str


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
    if not request_id:
        # Fallback: the checkpoint state always records the originating
        # request, so ownership can still be established when the job row
        # is missing (sync execution paths, cleaned-up jobs).
        request_id = str(state.get("request_id", ""))
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


def _load_state_by_request(
    db: Session,
    request_id: str,
    organization_id: str,
    settings: Settings,
    user_id: str = "",
) -> tuple[dict[str, Any], str] | None:
    """Resolve the latest workflow for a request and load its state.

    Ownership model matches GET /requests/{id}: the request must belong to
    the caller's organization AND (when user_id is provided) have been
    created by the caller. Returns None otherwise.
    """
    from aegisforge.db.models import RequestModel

    request_model = (
        db.query(RequestModel)
        .filter(
            RequestModel.id == request_id,
            RequestModel.organization_id == organization_id,
        )
        .first()
    )
    if request_model is None:
        return None
    if user_id and request_model.requested_by != user_id:
        return None

    job = (
        db.query(ExecutionJobModel)
        .filter(
            ExecutionJobModel.request_id == request_id,
            ExecutionJobModel.organization_id == organization_id,
        )
        .order_by(ExecutionJobModel.created_at.desc())  # type: ignore[union-attr]
        .first()
    )
    if job is None:
        return None
    state = _load_run_state(db, job.workflow_id, organization_id, settings)[0]
    return state, job.workflow_id


def _assert_request_owner(db: Session, request_id: str, user: UserModel) -> None:
    """Enforce GET /requests/{id} ownership on workflow introspection.

    The workflow belongs to the caller's organization (checked by
    _load_run_state) AND the underlying request must have been    created by the caller. Registration currently places users in shared
    organizations, so an org check alone would leak execution state
    between users — this keeps parity with GET /requests/{id}.
    Raises 404 (never 403) to avoid confirming resource existence.

    Fail-closed: if no request id can be resolved (no job row AND no
    request_id in the checkpoint state), ownership is unprovable and the
    endpoint answers 404 instead of exposing org-only state.
    """
    if not request_id:
        raise HTTPException(status_code=404, detail="Workflow not found")
    from aegisforge.db.models import RequestModel

    request_model = (
        db.query(RequestModel)
        .filter(
            RequestModel.id == request_id,
            RequestModel.organization_id == user.organization_id,
        )
        .first()
    )
    if request_model is None or request_model.requested_by != user.id:
        raise HTTPException(status_code=404, detail="Workflow not found")


@router.get("/by-request/{request_id}", response_model=WorkflowLookupRead)
def get_workflow_by_request(
    request_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WorkflowLookupRead:
    """Resolve the latest workflow id for a request (tenant-isolated).

    Used by the frontend to navigate from a request to its execution
    workspace without embedding workflow ids in client state.
    """
    resolved = _load_state_by_request(
        db, request_id, user.organization_id, settings, user_id=user.id
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No workflow found for request")
    state, workflow_id = resolved
    return WorkflowLookupRead(
        request_id=request_id,
        workflow_id=workflow_id,
        status=str(state.get("status", "unknown")),
    )


@router.get("/{workflow_id}/result", response_model=WorkflowResultRead)
def get_workflow_result(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WorkflowResultRead:
    """Get the final synthesized result for a workflow.

    Reads the durable checkpoint (the same source the resume path uses),
    extracts the chosen final agent result, and exposes only safe,
    measured fields: answer, citations, evidence, tools, confidence.

    Request ownership follows GET /requests/{id}: the underlying request
    must belong to the caller's organization and have been created by the
    caller (org + owner check — no cross-tenant result leakage).
    """
    state, request_id = _load_run_state(db, workflow_id, user.organization_id, settings)
    _assert_request_owner(db, request_id, user)
    status = str(state.get("status", "unknown"))

    # Prefer explicit agent_result; fall back to the best task record.
    final: dict[str, Any] = dict(state.get("agent_result") or {})

    # Aggregate tool usage across ALL task records (the final synthesis task
    # itself rarely calls tools — upstream research/rag tasks do).
    all_tools: list[str] = []
    records = state.get("task_records", {}) or {}
    for rec in records.values():
        for tc in rec.get("tool_calls", []) or []:
            name = tc.get("tool_name") or tc.get("name") if isinstance(tc, dict) else None
            if name and name not in all_tools:
                all_tools.append(str(name))
    for tc in final.get("tool_calls", []) or []:
        name = tc.get("tool_name") or tc.get("name") if isinstance(tc, dict) else None
        if name and name not in all_tools:
            all_tools.append(str(name))
    if not final:
        plan_dict = state.get("plan", {}) or {}
        try:
            plan = ExecutionPlan(**plan_dict)
        except Exception:
            plan = None
        if plan is not None:
            candidate = _pick_final_agent_result(state.get("task_records") or {}, plan)
            if candidate is not None:
                final = {
                    "agent_type": candidate.get("agent_type", ""),
                    "summary": candidate.get("summary", ""),
                    "result": candidate.get("output", {}),
                    "evidence": candidate.get("evidence", []),
                    "tool_calls": candidate.get("tool_calls", []),
                    "confidence": (candidate.get("output", {}) or {}).get("confidence"),
                    "errors": candidate.get("errors", []),
                }

    result_payload = final.get("result") or {}
    if not isinstance(result_payload, dict):
        result_payload = {}
    citations = result_payload.get("citations", []) or []
    if not citations:
        # Fall back to evidence entries that carry a source (measured evidence
        # metadata, never raw payloads).
        citations = [e for e in (final.get("evidence", []) or []) if isinstance(e, dict) and e.get("source")]

    return WorkflowResultRead(
        workflow_id=workflow_id,
        request_id=request_id,
        status=status,
        agent_type=str(final.get("agent_type", "") or ""),
        answer=str(result_payload.get("answer", "") or ""),
        summary=str(final.get("summary", "") or ""),
        citations=[c for c in citations if isinstance(c, dict)],
        evidence=[e for e in (final.get("evidence", []) or []) if isinstance(e, dict)],
        tools_used=all_tools,
        confidence=final.get("confidence") if isinstance(final.get("confidence"), (int, float)) else None,
        failed_upstream=[str(x) for x in (result_payload.get("failed_upstream", []) or [])],
        errors=[str(e) for e in (final.get("errors", []) or [])][:5],
    )


@router.get("/{workflow_id}", response_model=WorkflowGraphRead)
def get_workflow_overview(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WorkflowGraphRead:
    """Get workflow overview + task dependency graph."""
    state, request_id = _load_run_state(db, workflow_id, user.organization_id, settings)
    _assert_request_owner(db, request_id, user)
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
    """List tasks (with state) for a workflow — used by the frontend graph.

    Owner-scoped: matches GET /requests/{id} so other users in the same
    organization cannot read this workflow's task state.
    """
    state, request_id = _load_run_state(db, workflow_id, user.organization_id, settings)
    _assert_request_owner(db, request_id, user)
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
    _assert_request_owner(db, request_id, user)
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
            if not final_output:
                # Serial (single-task) runs never produce a synthesis record.
                # Score the same final answer GET /result exposes, otherwise the
                # final-response domain reports produced=False / score 0.00 for a
                # run that did produce a result.
                agent_result = state.get("agent_result") or {}
                final_output = agent_result.get("result") or {}
            if not final_output:
                candidate = _pick_final_agent_result(records, plan)
                if candidate is not None:
                    final_output = candidate.get("output") or {}
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
