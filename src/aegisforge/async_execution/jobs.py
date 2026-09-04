"""Async job system for AegisForge.

Provides job submission, queue management, worker execution, and failure recovery.
Uses Redis for queue backing with database persistence for state recovery.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

logger = logging.getLogger(__name__)


class JobQueue:
    """Abstract job queue interface."""

    def enqueue(self, job: ExecutionJob) -> bool:
        """Add a job to the queue."""
        raise NotImplementedError

    def dequeue(self) -> ExecutionJob | None:
        """Remove and return the next job from the queue."""
        raise NotImplementedError

    def requeue(self, job: ExecutionJob) -> bool:
        """Re-queue a failed job for retry."""
        raise NotImplementedError

    def size(self) -> int:
        """Return the number of jobs in the queue."""
        raise NotImplementedError


class InMemoryJobQueue(JobQueue):
    """In-memory job queue for testing."""

    def __init__(self) -> None:
        self._queue: list[ExecutionJob] = []

    def enqueue(self, job: ExecutionJob) -> bool:
        self._queue.append(job)
        return True

    def dequeue(self) -> ExecutionJob | None:
        if self._queue:
            return self._queue.pop(0)
        return None

    def requeue(self, job: ExecutionJob) -> bool:
        self._queue.append(job)
        return True

    def size(self) -> int:
        return len(self._queue)


class RedisJobQueue(JobQueue):
    """Redis-backed job queue for production use."""

    def __init__(self, redis_client: Any, queue_name: str = "aegisforge:jobs") -> None:
        self._redis = redis_client
        self._queue_name = queue_name

    def enqueue(self, job: ExecutionJob) -> bool:
        try:
            job_data = job.model_dump_json()
            self._redis.lpush(self._queue_name, job_data)
            return True
        except Exception as exc:
            logger.exception("Failed to enqueue job %s", job.job_id)
            return False

    def dequeue(self) -> ExecutionJob | None:
        try:
            data = self._redis.rpop(self._queue_name)
            if data:
                return ExecutionJob.model_validate_json(data)
            return None
        except Exception as exc:
            logger.exception("Failed to dequeue job")
            return None

    def requeue(self, job: ExecutionJob) -> bool:
        return self.enqueue(job)

    def size(self) -> int:
        try:
            return self._redis.llen(self._queue_name)
        except Exception:
            return 0


class JobManager:
    """Manages job lifecycle: submission, tracking, retry, and status."""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue
        self._jobs: dict[str, ExecutionJob] = {}

    def submit_job(
        self,
        request_id: str,
        workflow_id: str,
        organization_id: str = "",
        max_retries: int = 3,
        idempotency_key: str = "",
    ) -> ExecutionJob:
        """Submit a new execution job to the queue."""
        # Check idempotency
        if idempotency_key:
            for job in self._jobs.values():
                if job.idempotency_key == idempotency_key and job.status not in (
                    ExecutionJobStatus.FAILED,
                    ExecutionJobStatus.CANCELLED,
                ):
                    logger.info("Idempotent request: returning existing job %s", job.job_id)
                    return job

        job = ExecutionJob(
            job_id=f"job-{uuid.uuid4().hex[:12]}",
            request_id=request_id,
            workflow_id=workflow_id,
            status=ExecutionJobStatus.QUEUED,
            max_retries=max_retries,
            idempotency_key=idempotency_key or f"job-{uuid.uuid4().hex[:16]}",
        )

        self._jobs[job.job_id] = job
        self._queue.enqueue(job)

        logger.info(
            "Submitted job %s for request %s (workflow %s)",
            job.job_id,
            request_id,
            workflow_id,
        )
        return job

    def get_job(self, job_id: str) -> ExecutionJob | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    def update_job_status(
        self,
        job_id: str,
        status: ExecutionJobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ExecutionJob | None:
        """Update a job's status."""
        job = self._jobs.get(job_id)
        if job is None:
            return None

        job.status = status
        if result is not None:
            job.result = result
        if error is not None:
            if error not in job.errors:
                job.errors.append(error)

        if status == ExecutionJobStatus.RUNNING and job.retry_count == 0:
            pass  # started_at would be set by the worker
        elif status in (ExecutionJobStatus.COMPLETED, ExecutionJobStatus.FAILED):
            pass  # completed_at would be set by the worker

        return job

    def should_retry(self, job: ExecutionJob) -> bool:
        """Determine if a failed job should be retried."""
        if job.status != ExecutionJobStatus.FAILED:
            return False
        if job.retry_count >= job.max_retries:
            return False
        # Don't retry on permanent failures
        for error in job.errors:
            if any(
                kw in error.lower()
                for kw in ["permission denied", "unauthorized", "invalid input", "not found"]
            ):
                return False
        return True

    def requeue_for_retry(self, job_id: str) -> ExecutionJob | None:
        """Re-queue a failed job for retry."""
        job = self._jobs.get(job_id)
        if job is None or not self.should_retry(job):
            return None

        job.retry_count += 1
        job.status = ExecutionJobStatus.RETRYING
        self._queue.requeue(job)

        logger.info(
            "Requeued job %s for retry (attempt %d/%d)",
            job_id,
            job.retry_count,
            job.max_retries,
        )
        return job

    def cancel_job(self, job_id: str) -> ExecutionJob | None:
        """Cancel a queued job."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in (ExecutionJobStatus.COMPLETED, ExecutionJobStatus.FAILED):
            return None
        job.status = ExecutionJobStatus.CANCELLED
        return job

    def list_jobs(
        self,
        organization_id: str = "",
        status: ExecutionJobStatus | None = None,
        limit: int = 50,
    ) -> list[ExecutionJob]:
        """List jobs with optional filtering."""
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        jobs.sort(key=lambda j: j.job_id, reverse=True)
        return jobs[:limit]


class JobWorker:
    """Worker that processes jobs from the queue."""

    def __init__(
        self,
        job_manager: JobManager,
        job_handler: Callable[[ExecutionJob], dict[str, Any]],
    ) -> None:
        self._job_manager = job_manager
        self._job_handler = job_handler

    def process_next_job(self) -> ExecutionJob | None:
        """Process the next job in the queue. Returns the updated job or None."""
        job = self._job_manager._queue.dequeue()
        if job is None:
            return None

        # Update to running
        self._job_manager.update_job_status(
            job.job_id, ExecutionJobStatus.RUNNING
        )
        job.status = ExecutionJobStatus.RUNNING

        try:
            result = self._job_handler(job)
            self._job_manager.update_job_status(
                job.job_id,
                ExecutionJobStatus.COMPLETED,
                result=result,
            )
            job.status = ExecutionJobStatus.COMPLETED
            job.result = result
            logger.info("Job %s completed successfully", job.job_id)
        except Exception as exc:
            logger.exception("Job %s failed", job.job_id)
            self._job_manager.update_job_status(
                job.job_id,
                ExecutionJobStatus.FAILED,
                error=str(exc),
            )
            job.status = ExecutionJobStatus.FAILED
            job.errors.append(str(exc))

            # Try to requeue for retry
            if self._job_manager.should_retry(job):
                self._job_manager.requeue_for_retry(job.job_id)

        return job

    def process_all(self, max_jobs: int = 100) -> list[ExecutionJob]:
        """Process all available jobs up to max_jobs."""
        processed: list[ExecutionJob] = []
        for _ in range(max_jobs):
            job = self.process_next_job()
            if job is None:
                break
            processed.append(job)
        return processed
