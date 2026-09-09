"""Worker process for AegisForge async job execution.

Runs as a separate process, consuming jobs from Redis and executing workflows.
Supports graceful shutdown and health checks.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

from aegisforge.async_execution.jobs import (
    InMemoryJobQueue,
    JobManager,
    JobWorker,
    RedisJobQueue,
)
from aegisforge.config import Settings, get_model_provider_from_settings, get_settings
from aegisforge.db.session import get_session_factory

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown_requested = False


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown_requested
    logger.info("Received signal %d, requesting shutdown...", signum)
    _shutdown_requested = True


def _create_job_handler(settings: Settings) -> Any:
    """Create the job handler that executes workflows."""
    from aegisforge.approval.service import ApprovalService
    from aegisforge.domain.models import ExecutionJobStatus, RequestStatus
    from aegisforge.services.audit_service import record_audit_event
    from aegisforge.services.request_service import update_request_status

    session_factory = get_session_factory(settings)

    # F1: Create model provider from settings
    model_provider = get_model_provider_from_settings(settings)
    if model_provider is not None:
        logger.info("Worker LLM provider: %s", model_provider.provider_name)
    else:
        logger.info("Worker: No real LLM provider configured, using deterministic fallback")

    # F2: Create retrieval service
    retrieval_service = _create_retrieval_service(settings)

    # F9: Durable (DB-backed) checkpoint store shared with the API process
    from aegisforge.workflows.checkpoint import WorkflowCheckpointer, get_db_checkpoint_store

    checkpoint_store = get_db_checkpoint_store(settings)
    approval_service = ApprovalService(session_factory=session_factory, approval_timeout_hours=settings.approval_timeout_hours)

    def _update_job_model(db: Any, job: Any, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        """Persist job lifecycle state to the database (source of truth)."""
        from aegisforge.db.models import ExecutionJobModel

        job_model = (
            db.query(ExecutionJobModel)
            .filter(
                ExecutionJobModel.id == job.job_id,
                ExecutionJobModel.organization_id == job.organization_id,
            )
            .first()
        )
        if job_model is None:
            return
        job_model.status = status
        if result is not None:
            import json as _json

            job_model.result_json = _json.dumps(result)
        if error is not None:
            import json as _json

            job_model.error_json = _json.dumps([error])
        db.commit()

    def handler(job: Any) -> dict[str, Any]:
        """Execute a workflow for a job."""
        db = session_factory()
        try:
            from aegisforge.db.models import RequestModel

            organization_id = job.organization_id or ""
            if not organization_id:
                raise ValueError(f"Job {job.job_id} is missing an organization")
            request = (
                db.query(RequestModel)
                .filter(
                    RequestModel.id == job.request_id,
                    RequestModel.organization_id == organization_id,
                )
                .first()
            )
            if request is None:
                raise ValueError(
                    f"Request {job.request_id} not found for organization {organization_id}"
                )

            # Persist job status: queued -> running
            _update_job_model(db, job, ExecutionJobStatus.RUNNING.value)

            # Record workflow start
            record_audit_event(
                db,
                organization_id=organization_id,
                actor_id=request.requested_by,
                action="workflow.started",
                resource_type="request",
                resource_id=job.request_id,
                outcome="success",
                request_id=job.request_id,
            )

            # Update status
            update_request_status(db, job.request_id, RequestStatus.EXECUTING)

            # F9: Create checkpointer for this workflow
            checkpointer = WorkflowCheckpointer(
                workflow_id=job.workflow_id,
                request_id=job.request_id,
                organization_id=organization_id,
                store=checkpoint_store,
            )

            # Execute the workflow with all dependencies
            from aegisforge.workflows.langgraph_workflow import execute_workflow

            final_state = execute_workflow(
                request_id=job.request_id,
                intent=request.intent,
                user_id=request.requested_by,
                organization_id=organization_id,
                workflow_id=job.workflow_id,
                checkpointer=checkpointer,
                model_provider=model_provider,
                retrieval_service=retrieval_service,
                approval_service=approval_service,
                job_id=job.job_id,
                trace_id=job.trace_id,
            )

            final_status = final_state.get("status", "failed")

            # Persist results
            from aegisforge.services.execution_service import (
                _persist_agent_executions,
                _persist_tasks,
                _persist_tool_executions,
            )

            _persist_agent_executions(db, job.request_id, final_state)
            _persist_tool_executions(db, job.request_id, final_state)
            _persist_tasks(db, job.request_id, final_state)

            # If the workflow paused for approval, record that in the job
            if final_status == RequestStatus.ACTION_REQUIRES_APPROVAL.value:
                _update_job_model(
                    db,
                    job,
                    ExecutionJobStatus.WAITING_FOR_APPROVAL.value,
                    result={"workflow_id": job.workflow_id, "status": final_status},
                )
                record_audit_event(
                    db,
                    organization_id=organization_id,
                    actor_id=request.requested_by,
                    action="workflow.paused_for_approval",
                    resource_type="request",
                    resource_id=job.request_id,
                    outcome="success",
                    metadata={
                        "final_status": final_status,
                        "workflow_id": job.workflow_id,
                        "approval_id": final_state.get("approval_id", ""),
                    },
                    request_id=job.request_id,
                )
                return {
                    "request_id": job.request_id,
                    "workflow_id": job.workflow_id,
                    "status": final_status,
                    "approval_id": final_state.get("approval_id", ""),
                    "final_result": {},
                    "errors": [],
                }

            # Update final status
            try:
                status_enum = RequestStatus(final_status)
            except ValueError:
                status_enum = RequestStatus.COMPLETED

            update_request_status(db, job.request_id, status_enum)

            job_status = (
                ExecutionJobStatus.COMPLETED.value
                if status_enum == RequestStatus.COMPLETED
                else ExecutionJobStatus.FAILED.value
            )
            _update_job_model(db, job, job_status, result=final_state)

            # Record completion
            record_audit_event(
                db,
                organization_id=organization_id,
                actor_id=request.requested_by,
                action="workflow.completed",
                resource_type="request",
                resource_id=job.request_id,
                outcome="success" if status_enum == RequestStatus.COMPLETED else "failure",
                metadata={"final_status": final_status, "workflow_id": job.workflow_id},
                request_id=job.request_id,
            )

            return {
                "request_id": job.request_id,
                "workflow_id": job.workflow_id,
                "status": final_status,
                "final_result": final_state.get("final_result", {}),
                "errors": final_state.get("errors", []),
            }
        except Exception as exc:
            # Ensure the request always reaches a terminal state.
            # Without this handler the request would remain stuck at
            # "executing" whenever the workflow throws, because the
            # try/finally above never updated the status on the error path.
            logger.exception("Worker handler exception for job %s", job.job_id)
            _org = job.organization_id or ""
            _actor = getattr(request, "requested_by", "") if "request" in locals() else ""
            try:
                update_request_status(
                    db, job.request_id, RequestStatus.FAILED
                )
                record_audit_event(
                    db,
                    organization_id=_org,
                    actor_id=_actor,
                    action="workflow.failed",
                    resource_type="request",
                    resource_id=job.request_id,
                    outcome="failure",
                    metadata={
                        "error": str(exc),
                        "workflow_id": job.workflow_id,
                    },
                    request_id=job.request_id,
                )
                _update_job_model(
                    db, job, ExecutionJobStatus.FAILED.value, error=str(exc)
                )
            except Exception:
                logger.exception(
                    "Failed to persist failure state for job %s", job.job_id
                )
            raise
        finally:
            db.close()

    return handler


def _create_retrieval_service(settings: Settings) -> Any:
    """Create a RetrievalService if embedding provider is configured.

    In production, PostgreSQL/pgvector is REQUIRED when a PostgreSQL
    database is configured — failure is raised, not silently swallowed,
    so the worker fails clearly instead of degrading to in-memory.
    """
    is_production = settings.environment not in ("development", "test", "")
    try:
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
            db_session_factory=lambda: get_session_factory(settings)(),
            dimension=settings.embedding_dimension,
        )

        retrieval_service: Any = RetrievalService(
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
        return retrieval_service
    except Exception as exc:
        if is_production and settings.database_url.startswith("postgresql"):
            logger.critical(
                "PostgreSQL/pgvector retrieval service required in production "
                "but failed to initialize: %s. Failing clearly instead of "
                "silently falling back to in-memory vector store.",
                exc,
            )
            raise
        logger.warning("Could not create retrieval service: %s", exc)
        return None


def run_worker(settings: Settings | None = None) -> None:
    """Main worker loop."""
    settings = settings or get_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Register signal handlers
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Starting AegisForge worker...")
    logger.info("Redis URL: %s", settings.redis_url)

    # F7: Create queue — fail clearly if Redis is unavailable in production
    is_production = settings.environment not in ("development", "test", "")
    from aegisforge.async_execution.jobs import JobQueue as _JobQueue

    queue: _JobQueue
    try:
        import redis

        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        redis_client.ping()
        queue = RedisJobQueue(redis_client)
        logger.info("Connected to Redis at %s", settings.redis_url)
    except Exception as exc:
        if is_production:
            logger.critical(
                "Redis is required in production but unavailable: %s. "
                "Worker cannot start without durable queue. Exiting.",
                exc,
            )
            sys.exit(1)
        else:
            logger.warning(
                "Redis not available (%s) in %s environment, using in-memory queue. "
                "Jobs will NOT survive worker restart.",
                exc,
                settings.environment,
            )
            queue = InMemoryJobQueue()

    # Create job manager and worker
    job_manager = JobManager(queue)
    handler = _create_job_handler(settings)
    worker = JobWorker(job_manager, handler)

    logger.info("Worker ready, polling for jobs...")

    poll_interval = 1.0
    while not _shutdown_requested:
        try:
            job = worker.process_next_job()
            if job is not None:
                logger.info("Processed job %s: %s", job.job_id, job.status.value)
                poll_interval = 1.0
            else:
                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 10.0)
        except Exception:
            logger.exception("Worker error")
            time.sleep(5.0)

    logger.info("Worker shutdown complete")


if __name__ == "__main__":
    run_worker()
