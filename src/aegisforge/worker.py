"""Worker process for AegisForge async job execution.

Runs as a separate process, consuming jobs from Redis and executing workflows.
Supports graceful shutdown and health checks.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any

from aegisforge.config import get_settings, Settings, get_model_provider_from_settings
from aegisforge.db.session import get_session_factory
from aegisforge.async_execution.jobs import (
    InMemoryJobQueue,
    JobManager,
    JobWorker,
    RedisJobQueue,
)

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown_requested = False


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown_requested
    logger.info("Received signal %d, requesting shutdown...", signum)
    _shutdown_requested = True


def _create_job_handler(settings: Settings) -> Any:
    """Create the job handler that executes workflows."""
    from aegisforge.services.audit_service import record_audit_event
    from aegisforge.services.request_service import update_request_status
    from aegisforge.domain.models import RequestStatus

    session_factory = get_session_factory(settings)

    # F1: Create model provider from settings
    model_provider = get_model_provider_from_settings(settings)
    if model_provider is not None:
        logger.info("Worker LLM provider: %s", model_provider.provider_name)
    else:
        logger.info("Worker: No real LLM provider configured, using deterministic fallback")

    # F2: Create retrieval service
    retrieval_service = _create_retrieval_service(settings)

    # F9: Create checkpointer
    from aegisforge.workflows.checkpoint import WorkflowCheckpointer, get_checkpoint_store

    checkpoint_store = get_checkpoint_store()

    def handler(job: Any) -> dict[str, Any]:
        """Execute a workflow for a job."""
        db = session_factory()
        try:
            from aegisforge.db.models import RequestModel

            request = db.query(RequestModel).filter(RequestModel.id == job.request_id).first()
            if request is None:
                raise ValueError(f"Request {job.request_id} not found")

            # Record workflow start
            record_audit_event(
                db,
                organization_id=job.organization_id or request.organization_id,
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
                organization_id=job.organization_id or request.organization_id,
                store=checkpoint_store,
            )

            # Execute the workflow with all dependencies
            from aegisforge.workflows.langgraph_workflow import execute_workflow

            final_state = execute_workflow(
                request_id=job.request_id,
                intent=request.intent,
                user_id=request.requested_by,
                organization_id=job.organization_id or request.organization_id,
                workflow_id=job.workflow_id,
                checkpointer=checkpointer,
                model_provider=model_provider,
                retrieval_service=retrieval_service,
            )

            # Persist results
            from aegisforge.services.execution_service import (
                _persist_agent_executions,
                _persist_tool_executions,
                _persist_tasks,
            )

            _persist_agent_executions(db, job.request_id, final_state)
            _persist_tool_executions(db, job.request_id, final_state)
            _persist_tasks(db, job.request_id, final_state)

            # Update final status
            final_status = final_state.get("status", "failed")
            try:
                status_enum = RequestStatus(final_status)
            except ValueError:
                status_enum = RequestStatus.COMPLETED

            update_request_status(db, job.request_id, status_enum)

            # Record completion
            record_audit_event(
                db,
                organization_id=job.organization_id or request.organization_id,
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
        finally:
            db.close()

    return handler


def _create_retrieval_service(settings: Settings) -> Any:
    """Create a RetrievalService if embedding provider is configured."""
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

        return RetrievalService(
            embedding_provider=embedding_provider,
            vector_store=vector_store,
        )
    except Exception as exc:
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
        except Exception as exc:
            logger.exception("Worker error: %s", exc)
            time.sleep(5.0)

    logger.info("Worker shutdown complete")


if __name__ == "__main__":
    run_worker()
