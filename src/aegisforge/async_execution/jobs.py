"""Async job system for AegisForge.

Provides job submission, queue management, worker execution, and failure recovery.
Uses Redis for queue backing with database persistence for state recovery.

Phase 6B: Distributed-safe multi-worker execution.

Key properties:
- At-most-one active execution per job (atomic claim with visibility timeout)
- Crash recovery (expired claims are re-enqueued automatically)
- Worker registry (health monitoring for multiple workers)
- Idempotency via Redis-backed duplicate detection
- Bounded retry with terminal states
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus
from aegisforge.observability.metrics import (
    record_job_duration,
    record_queue_event,
    record_worker_event,
    set_queue_depth,
)

logger = logging.getLogger(__name__)

# --- Redis key patterns ---
_JOBS_QUEUE = "aegisforge:jobs"  # List: pending job payloads
_JOB_DATA_PREFIX = "aegisforge:job:"  # Hash: full job state per job_id
_JOB_CLAIM_PREFIX = "aegisforge:claim:"  # String: worker_id owning the claim (with TTL)
_ACTIVE_CLAIMS_SET = "aegisforge:active_claims"  # Sorted set: job_id -> expiration timestamp
_WORKERS_SET = "aegisforge:workers"  # Sorted set: worker_id -> last_heartbeat timestamp
_IDEMPOTENCY_PREFIX = "aegisforge:idempotency:"  # String: job_id for dedup

# Defaults (overridable for testing)
DEFAULT_VISIBILITY_TIMEOUT = 300  # 5 minutes
DEFAULT_HEARTBEAT_INTERVAL = 30  # seconds
DEFAULT_WORKER_TTL = 120  # seconds before worker considered dead


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
    """Redis-backed job queue for production use.

    Distributed-safe properties:
    - ``rpop`` is atomic: only one worker receives each job.
    - Claim keys with TTL provide visibility timeout: if a worker crashes,
      the claim expires and the job is recoverable.
    - Heartbeats extend the claim TTL while work is in progress.
    - Worker registry tracks active workers via sorted set.
    """

    def __init__(
        self,
        redis_client: Any,
        queue_name: str = _JOBS_QUEUE,
        visibility_timeout: int = DEFAULT_VISIBILITY_TIMEOUT,
    ) -> None:
        self._redis = redis_client
        self._queue_name = queue_name
        self._visibility_timeout = visibility_timeout

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    def enqueue(self, job: ExecutionJob) -> bool:
        try:
            job_data = job.model_dump_json()
            self._redis.lpush(self._queue_name, job_data)
            return True
        except Exception:
            logger.exception("Failed to enqueue job %s", job.job_id)
            return False

    def dequeue(self) -> ExecutionJob | None:
        """Atomically pop the next job from the queue.

        Uses RPOP which is a single atomic Redis command — safe for
        multiple concurrent workers.
        """
        try:
            data = self._redis.rpop(self._queue_name)
            if data:
                return ExecutionJob.model_validate_json(data)
            return None
        except Exception:
            logger.exception("Failed to dequeue job")
            return None

    def requeue(self, job: ExecutionJob) -> bool:
        return self.enqueue(job)

    def size(self) -> int:
        try:
            return self._redis.llen(self._queue_name)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Claim management (Phase 6B)
    # ------------------------------------------------------------------

    def claim_job(self, job: ExecutionJob, worker_id: str) -> bool:
        """Atomically claim a job for a specific worker.

        Uses SET NX EX to ensure at-most-one claim per job.
        Also tracks the claim in a sorted set for crash recovery.
        Returns True if this worker successfully claimed the job.

        Race-condition guard: before claiming, checks the job's current status
        in Redis.  If the job has already reached a terminal state (e.g. another
        worker completed it while this worker's claim was expired), the claim is
        rejected to prevent duplicate execution.
        """
        claim_key = f"{_JOB_CLAIM_PREFIX}{job.job_id}"
        try:
            # Race-condition guard: reject claim if job is already terminal
            existing = self.get_job_data(job.job_id)
            if existing is not None and existing.status in (
                ExecutionJobStatus.COMPLETED,
                ExecutionJobStatus.FAILED,
                ExecutionJobStatus.CANCELLED,
            ):
                logger.info(
                    "Job %s already in terminal state %s, rejecting claim",
                    job.job_id, existing.status.value,
                )
                return False

            # SET key value NX EX seconds — atomic claim
            claimed = self._redis.set(
                claim_key, worker_id, nx=True, ex=self._visibility_timeout
            )
            if claimed:
                # Track in sorted set for recovery (score = expiration timestamp)
                expires_at = time.time() + self._visibility_timeout
                self._redis.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: expires_at})
                # Persist full job data for distributed access
                self._store_job_data(job)
                return True
            return False
        except Exception:
            logger.exception("Failed to claim job %s", job.job_id)
            return False

    def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        """Refresh the claim TTL to prevent timeout during long execution."""
        claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
        try:
            # Only refresh if we still own the claim
            current = self._redis.get(claim_key)
            if current == worker_id:
                self._redis.expire(claim_key, self._visibility_timeout)
                # Update sorted set expiration timestamp
                expires_at = time.time() + self._visibility_timeout
                self._redis.zadd(_ACTIVE_CLAIMS_SET, {job_id: expires_at})
                return True
            return False
        except Exception:
            logger.warning("Failed to heartbeat job %s", job_id)
            return False

    def release_claim(self, job_id: str, worker_id: str) -> None:
        """Release a claim after job completion or failure."""
        claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
        try:
            current = self._redis.get(claim_key)
            if current == worker_id:
                self._redis.delete(claim_key)
                # Remove from active claims sorted set
                self._redis.zrem(_ACTIVE_CLAIMS_SET, job_id)
        except Exception:
            logger.warning("Failed to release claim for job %s", job_id)

    def is_claimed(self, job_id: str) -> bool:
        """Check if a job is currently claimed by any worker."""
        claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
        try:
            return self._redis.exists(claim_key) > 0
        except Exception:
            return False

    def get_claim_owner(self, job_id: str) -> str | None:
        """Return the worker_id that claimed this job, or None."""
        claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
        try:
            return self._redis.get(claim_key)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Job data persistence (Phase 6B)
    # ------------------------------------------------------------------

    def _store_job_data(self, job: ExecutionJob) -> None:
        """Persist job data in Redis for distributed access."""
        key = f"{_JOB_DATA_PREFIX}{job.job_id}"
        try:
            self._redis.set(key, job.model_dump_json(), ex=3600)
        except Exception:
            logger.warning("Failed to store job data for %s", job.job_id)

    def get_job_data(self, job_id: str) -> ExecutionJob | None:
        """Retrieve job data from Redis."""
        key = f"{_JOB_DATA_PREFIX}{job_id}"
        try:
            data = self._redis.get(key)
            if data:
                return ExecutionJob.model_validate_json(data)
            return None
        except Exception:
            return None

    def update_job_status(
        self,
        job_id: str,
        status: ExecutionJobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Update job status in Redis (distributed state)."""
        job = self.get_job_data(job_id)
        if job is None:
            return
        job.status = status
        if result is not None:
            job.result = result
        if error is not None and error not in job.errors:
            job.errors.append(error)
        self._store_job_data(job)

    # ------------------------------------------------------------------
    # Idempotency (Phase 6B — Redis-backed)
    # ------------------------------------------------------------------

    def check_idempotency(self, idempotency_key: str) -> str | None:
        """Check if an idempotency key already has an active job.

        Returns the existing job_id if found, None otherwise.
        """
        if not idempotency_key:
            return None
        key = f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"
        try:
            return self._redis.get(key)
        except Exception:
            return None

    def register_idempotency(self, idempotency_key: str, job_id: str, ttl: int = 3600) -> None:
        """Register an idempotency key mapping to a job."""
        if not idempotency_key:
            return
        key = f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"
        try:
            self._redis.set(key, job_id, ex=ttl)
        except Exception:
            logger.warning("Failed to register idempotency key %s", idempotency_key)

    # ------------------------------------------------------------------
    # Crash recovery (Phase 6B)
    # ------------------------------------------------------------------

    def recover_expired_claims(self, max_recover: int = 10) -> list[str]:
        """Scan for jobs whose claim has expired and re-enqueue them.

        Uses a sorted set (aegisforge:active_claims) where the score is the
        expiration timestamp. Unlike per-key TTLs, the sorted set entry persists
        after Redis auto-deletes the claim key, so the recovery thread can
        always discover it.

        Returns list of recovered job_ids.
        """
        recovered: list[str] = []
        try:
            now = time.time()
            # ZRANGEBYSCORE: find claims whose expiration timestamp <= now
            expired_job_ids: list[str] = self._redis.zrangebyscore(
                _ACTIVE_CLAIMS_SET, "-inf", str(now)
            )
            for job_id in expired_job_ids:
                if len(recovered) >= max_recover:
                    break
                # Remove from the sorted set (we're processing this claim)
                self._redis.zrem(_ACTIVE_CLAIMS_SET, job_id)
                # Also clean up the claim key if it still lingers
                claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
                self._redis.delete(claim_key)
                # Get job data and re-enqueue
                job = self.get_job_data(job_id)
                if job is None:
                    continue
                if job.status in (
                    ExecutionJobStatus.COMPLETED,
                    ExecutionJobStatus.FAILED,
                    ExecutionJobStatus.CANCELLED,
                ):
                    continue
                if job.retry_count >= job.max_retries:
                    # Terminal failure — mark and don't re-enqueue
                    self.update_job_status(
                        job_id,
                        ExecutionJobStatus.FAILED,
                        error="Worker crashed; max retries exceeded",
                    )
                    continue
                # Re-enqueue for another worker
                job.retry_count += 1
                job.status = ExecutionJobStatus.RETRYING
                self._store_job_data(job)
                self.enqueue(job)
                recovered.append(job_id)
                logger.info(
                    "Recovered expired job %s (attempt %d/%d)",
                    job_id, job.retry_count, job.max_retries,
                )
        except Exception:
            logger.exception("Error during crash recovery scan")
        return recovered

    # ------------------------------------------------------------------
    # Worker registry (Phase 6B)
    # ------------------------------------------------------------------

    def register_worker(self, worker_id: str, ttl: int = DEFAULT_WORKER_TTL) -> None:
        """Register a worker heartbeat in the sorted set."""
        try:
            self._redis.zadd(_WORKERS_SET, {worker_id: time.time()})
        except Exception:
            logger.warning("Failed to register worker %s", worker_id)

    def heartbeat_worker(self, worker_id: str) -> None:
        """Refresh the worker's heartbeat timestamp."""
        try:
            self._redis.zadd(_WORKERS_SET, {worker_id: time.time()})
        except Exception:  # noqa: S110 — observability must never break execution
            pass

    def unregister_worker(self, worker_id: str) -> None:
        """Remove a worker from the registry."""
        try:
            self._redis.zrem(_WORKERS_SET, worker_id)
        except Exception:  # noqa: S110 — observability must never break execution
            pass

    def get_active_workers(self, ttl: int = DEFAULT_WORKER_TTL) -> list[str]:
        """Return worker IDs that have heartbeated within the TTL window."""
        try:
            cutoff = time.time() - ttl
            return self._redis.zrangebyscore(_WORKERS_SET, cutoff, "+inf")
        except Exception:
            return []

    def get_queue_depth(self) -> int:
        """Return the number of pending jobs in the queue."""
        return self.size()


# --- Lua script for atomic claim (belt-and-suspenders) ---
# This is an alternative to SET NX EX if you need claim + state update atomically.
CLAIM_LUA = """
local claim_key = KEYS[1]
local job_key = KEYS[2]
local worker_id = ARGV[1]
local ttl = ARGV[2]
local terminal_statuses = {'completed', 'failed', 'cancelled'}

-- Check if already claimed
local current = redis.call('GET', claim_key)
if current then
    return 0
end

-- Race-condition guard: reject if job is already terminal
local job_data = redis.call('GET', job_key)
if job_data then
    -- Simple string match for terminal statuses in the JSON
    for _, s in ipairs(terminal_statuses) do
        if string.find(job_data, '"status":"' .. s .. '"') then
            return 0
        end
    end
end

-- Claim the job
redis.call('SET', claim_key, worker_id, 'EX', ttl)
return 1
"""


class JobManager:
    """Manages job lifecycle: submission, tracking, retry, and status.

    Phase 6B: Supports both in-memory (testing) and Redis-backed (distributed)
    job tracking. When a RedisJobQueue is provided, idempotency and state
    are distributed across workers.
    """

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
        trace_id: str = "",
    ) -> ExecutionJob:
        """Submit a new execution job to the queue."""
        # Check idempotency — Redis-backed when available
        if idempotency_key and isinstance(self._queue, RedisJobQueue):
            existing_id = self._queue.check_idempotency(idempotency_key)
            if existing_id is not None:
                existing = self._queue.get_job_data(existing_id)
                if existing is not None and existing.status not in (
                    ExecutionJobStatus.FAILED,
                    ExecutionJobStatus.CANCELLED,
                ):
                    logger.info("Idempotent request: returning existing job %s", existing_id)
                    return existing
        elif idempotency_key:
            # In-memory fallback for testing
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
            organization_id=organization_id,
            status=ExecutionJobStatus.QUEUED,
            max_retries=max_retries,
            idempotency_key=idempotency_key or f"job-{uuid.uuid4().hex[:16]}",
            trace_id=trace_id,
        )

        self._jobs[job.job_id] = job
        self._queue.enqueue(job)

        # Persist job data in Redis for distributed access and idempotency
        if isinstance(self._queue, RedisJobQueue):
            self._queue._store_job_data(job)
            self._queue.register_idempotency(job.idempotency_key, job.job_id)

        record_queue_event("queued")
        set_queue_depth(self._queue.size())

        logger.info(
            "Submitted job %s for request %s (workflow %s)",
            job.job_id,
            request_id,
            workflow_id,
        )
        return job

    def get_job(self, job_id: str) -> ExecutionJob | None:
        """Get a job by ID."""
        # Try Redis first for distributed access
        if isinstance(self._queue, RedisJobQueue):
            redis_job = self._queue.get_job_data(job_id)
            if redis_job is not None:
                return redis_job
        return self._jobs.get(job_id)

    def update_job_status(
        self,
        job_id: str,
        status: ExecutionJobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ExecutionJob | None:
        """Update a job's status."""
        job = self.get_job(job_id)
        if job is None:
            return None

        job.status = status
        if result is not None:
            job.result = result
        if error is not None and error not in job.errors:
            job.errors.append(error)

        # Persist to Redis for distributed visibility
        if isinstance(self._queue, RedisJobQueue):
            self._queue.update_job_status(job_id, status, result, error)

        # Also update in-memory for local consistency
        self._jobs[job_id] = job
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
        job = self.get_job(job_id)
        if job is None or not self.should_retry(job):
            return None

        job.retry_count += 1
        job.status = ExecutionJobStatus.RETRYING
        self._queue.requeue(job)

        # Persist updated state
        if isinstance(self._queue, RedisJobQueue):
            self._queue._store_job_data(job)

        logger.info(
            "Requeued job %s for retry (attempt %d/%d)",
            job_id,
            job.retry_count,
            job.max_retries,
        )
        return job

    def cancel_job(self, job_id: str) -> ExecutionJob | None:
        """Cancel a queued job."""
        job = self.get_job(job_id)
        if job is None:
            return None
        if job.status in (ExecutionJobStatus.COMPLETED, ExecutionJobStatus.FAILED):
            return None
        job.status = ExecutionJobStatus.CANCELLED
        if isinstance(self._queue, RedisJobQueue):
            self._queue.update_job_status(job_id, ExecutionJobStatus.CANCELLED)
        record_queue_event("cancelled")
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
    """Worker that processes jobs from the queue.

    Phase 6B: Supports distributed execution with:
    - Worker identity for tracking
    - Claim verification before processing
    - Heartbeat refresh during execution
    - Crash recovery scanning
    """

    def __init__(
        self,
        job_manager: JobManager,
        job_handler: Callable[[ExecutionJob], dict[str, Any]],
        worker_id: str | None = None,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self._job_manager = job_manager
        self._job_handler = job_handler
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._heartbeat_interval = heartbeat_interval
        self._active_job_id: str | None = None
        self._heartbeat_active = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def process_next_job(self) -> ExecutionJob | None:
        """Process the next job in the queue. Returns the updated job or None."""
        job = self._job_manager._queue.dequeue()
        if job is None:
            return None
        set_queue_depth(self._job_manager._queue.size())
        start_time = time.monotonic()
        self._active_job_id = job.job_id

        # Phase 6B: Atomic claim verification
        if isinstance(self._job_manager._queue, RedisJobQueue):
            claimed = self._job_manager._queue.claim_job(job, self._worker_id)
            if not claimed:
                logger.warning(
                    "Job %s already claimed by another worker, skipping", job.job_id
                )
                return None
            record_worker_event("claimed")
            # Start heartbeat
            self._heartbeat_active = True

        # Update to running
        self._job_manager.update_job_status(
            job.job_id, ExecutionJobStatus.RUNNING
        )
        job.status = ExecutionJobStatus.RUNNING
        record_queue_event("running")

        try:
            result = self._job_handler(job)
            self._job_manager.update_job_status(
                job.job_id,
                ExecutionJobStatus.COMPLETED,
                result=result,
            )
            job.status = ExecutionJobStatus.COMPLETED
            job.result = result
            record_queue_event("completed")
            record_job_duration("completed", time.monotonic() - start_time)
            record_worker_event("completed")
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
            record_queue_event("failed")
            record_job_duration("failed", time.monotonic() - start_time)
            record_worker_event("failed")

            # Try to requeue for retry
            if self._job_manager.should_retry(job):
                requeued = self._job_manager.requeue_for_retry(job.job_id)
                if requeued is not None:
                    job.status = requeued.status
                    job.retry_count = requeued.retry_count
                    record_queue_event("retried")
                    record_worker_event("retried")
        finally:
            self._heartbeat_active = False
            self._active_job_id = None
            # Phase 6B: Release the claim
            if isinstance(self._job_manager._queue, RedisJobQueue):
                self._job_manager._queue.release_claim(job.job_id, self._worker_id)

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

    def send_heartbeat(self) -> None:
        """Send heartbeat for the active job (call from background thread)."""
        if (
            self._active_job_id
            and self._heartbeat_active
            and isinstance(self._job_manager._queue, RedisJobQueue)
        ):
            self._job_manager._queue.heartbeat_job(self._active_job_id, self._worker_id)

    def register_in_registry(self) -> None:
        """Register this worker in the Redis worker registry."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            self._job_manager._queue.register_worker(self._worker_id)

    def unregister_from_registry(self) -> None:
        """Remove this worker from the Redis worker registry."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            self._job_manager._queue.unregister_worker(self._worker_id)

    def refresh_registry(self) -> None:
        """Refresh this worker's heartbeat in the registry."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            self._job_manager._queue.heartbeat_worker(self._worker_id)

    def recover_expired_jobs(self, max_recover: int = 5) -> list[str]:
        """Trigger crash recovery for expired claims."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            return self._job_manager._queue.recover_expired_claims(max_recover)
        return []
