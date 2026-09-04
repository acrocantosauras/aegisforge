"""Evaluation and metrics API endpoints for AegisForge.

Provides: evaluation results, system metrics, execution metrics.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db
from aegisforge.observability.tracing import get_metrics_collector
from aegisforge.services.auth_service import get_current_user

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


class MetricsSummary(BaseModel):
    count: int = 0
    avg_workflow_duration_ms: int = 0
    avg_planner_latency_ms: int = 0
    avg_retrieval_latency_ms: int = 0
    total_tool_calls: int = 0
    total_retries: int = 0
    total_failures: int = 0
    success_rate: float = 0.0


class ExecutionMetricsRead(BaseModel):
    request_id: str
    workflow_id: str = ""
    planner_latency_ms: int = 0
    retrieval_latency_ms: int = 0
    model_latency_ms: int = 0
    tool_latency_ms: int = 0
    workflow_duration_ms: int = 0
    queue_latency_ms: int = 0
    retrieval_count: int = 0
    tool_call_count: int = 0
    retry_count: int = 0
    failure_count: int = 0
    approval_wait_count: int = 0
    final_status: str = ""
    planner_type: str = ""
    model_name: str = ""
    tokens_used: int = 0


@router.get("/metrics", response_model=MetricsSummary)
def get_metrics_summary(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> MetricsSummary:
    """Get aggregate evaluation metrics."""
    collector = get_metrics_collector()
    summary = collector.get_summary()
    return MetricsSummary(**summary)


@router.get("/metrics/{request_id}", response_model=ExecutionMetricsRead | None)
def get_execution_metrics(
    request_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> ExecutionMetricsRead | None:
    """Get execution metrics for a specific request."""
    collector = get_metrics_collector()
    metrics = collector.get(request_id)
    if metrics is None:
        return None
    return ExecutionMetricsRead(**metrics.to_dict())
