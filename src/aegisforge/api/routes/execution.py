from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_settings
from aegisforge.db.models import UserModel
from aegisforge.db.session import get_db
from aegisforge.observability.metrics import record_queue_event, set_queue_depth
from aegisforge.services.auth_service import get_current_user
from aegisforge.services.execution_service import execute_request

router = APIRouter(prefix="/execution", tags=["execution"])


class ExecuteRequestInput(BaseModel):
    request_id: str = Field(min_length=1, description="Request ID to execute")


class ExecutionResultResponse(BaseModel):
    request_id: str
    workflow_id: str
    status: str
    final_result: dict = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class AsyncExecutionResponse(BaseModel):
    request_id: str
    workflow_id: str
    job_id: str
    status: str
    message: str


@router.post(
    "/requests/{request_id}/execute",
    response_model=ExecutionResultResponse,
    status_code=status.HTTP_200_OK,
)
def execute_request_route(
    request_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Execute the full workflow for a request (synchronous).

    This triggers: validate → plan → execute → evaluate → complete/fail.
    Blocks until the workflow completes. Use for testing and internal use.
    """
    # Tenant isolation: the request must belong to the caller's organization
    from aegisforge.db.models import RequestModel

    request = (
        db.query(RequestModel)
        .filter(RequestModel.id == request_id, RequestModel.organization_id == user.organization_id)
        .first()
    )
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    try:
        result = execute_request(
            db=db,
            request_id=request_id,
            user_id=user.id,
            organization_id=user.organization_id,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Execution failed: {exc}",
        )

    return result


@router.post(
    "/requests/{request_id}/execute-async",
    response_model=AsyncExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def execute_request_async(
    request_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Execute the full workflow for a request (asynchronous).

    F5: Submits the job to Redis/worker queue and returns immediately.
    The worker will process the workflow in the background.
    """
    from aegisforge.async_execution.jobs import (
        InMemoryJobQueue,
        JobManager,
        RedisJobQueue,
    )
    from aegisforge.db.models import ExecutionJobModel, RequestModel, WorkflowModel

    # Verify request exists AND belongs to the caller's organization
    request_row = (
        db.query(RequestModel)
        .filter(RequestModel.id == request_id, RequestModel.organization_id == user.organization_id)
        .first()
    )
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"

    # Verify the queue before creating durable records. Production execution
    # must never accept a job that cannot reach the Redis-backed worker.
    from aegisforge.async_execution.jobs import JobQueue as _JobQueue

    queue: _JobQueue
    try:
        import redis as redis_lib

        redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        redis_client.ping()
        queue = RedisJobQueue(redis_client)
    except Exception as exc:
        is_production = settings.environment not in ("development", "test", "")
        if is_production:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Asynchronous execution is unavailable because Redis is not reachable",
            ) from exc
        queue = InMemoryJobQueue()

    # Create job record in database
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    job_model = ExecutionJobModel(
        id=job_id,
        request_id=request_id,
        workflow_id=workflow_id,
        organization_id=user.organization_id,
        status="queued",
        retry_count=0,
        max_retries=settings.job_max_retries,
    )
    db.add(job_model)

    # Create workflow record
    workflow_model = WorkflowModel(
        id=workflow_id,
        organization_id=user.organization_id,
        name=f"workflow-for-{request_id}",
    )
    db.add(workflow_model)
    db.commit()

    job_manager = JobManager(queue)
    from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

    trace_id = request.headers.get("x-request-id", "") or ""
    job = ExecutionJob(
        job_id=job_id,
        request_id=request_id,
        workflow_id=workflow_id,
        organization_id=user.organization_id,
        status=ExecutionJobStatus.QUEUED,
        max_retries=settings.job_max_retries,
        trace_id=trace_id,
    )
    job_manager._jobs[job_id] = job
    if not queue.enqueue(job):
        job_model.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Asynchronous execution could not be queued",
        )
    record_queue_event("queued")
    set_queue_depth(queue.size())

    return {
        "request_id": request_id,
        "workflow_id": workflow_id,
        "job_id": job_id,
        "status": "queued",
        "message": f"Workflow submitted for execution. Job ID: {job_id}",
    }
