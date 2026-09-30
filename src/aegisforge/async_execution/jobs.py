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
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus
from aegisforge.observability.metrics import (
    record_claim_expiration,
    record_claim_extension,
    record_job_abandoned,
    record_job_claimed,
    record_job_dequeued,
    record_job_duration,
    record_job_recovered,
    record_queue_event,
    record_queue_wait_time,
    record_worker_event,
    record_worker_heartbeat_failure,
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
DEFAULT_MAX_TRACKED_JOBS = 1000  # bounded in-memory job history per manager

# ---------------------------------------------------------------------------
# Lua scripts for atomic claim maintenance.
#
# GET-then-EXPIRE / GET-then-DELETE are TOCTOU races: a claim can expire and
# be re-claimed by another worker BETWEEN the GET and the EXPIRE/DELETE, in
# which case the slow worker would extend or delete ANOTHER worker's claim.
# The scripts below compare the stored owner and act only on a match, atomically.
# ---------------------------------------------------------------------------

# Atomic compare-and-expire: refresh the claim TTL only if we still own it.
# KEYS[1]=claim key, ARGV[1]=worker_id, ARGV[2]=ttl_seconds
HEARTBEAT_CLAIM_LUA = """
local owner = redis.call('GET', KEYS[1])
if owner == ARGV[1] then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
end
return 0
"""

# Atomic compare-and-delete: release the claim only if we still own it.
# KEYS[1]=claim key, KEYS[2]=active claims sorted set, ARGV[1]=worker_id, ARGV[2]=job_id
RELEASE_CLAIM_LUA = """
local owner = redis.call('GET', KEYS[1])
if owner == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('ZREM', KEYS[2], ARGV[2])
    return 1
end
return 0
"""

# Atomic compare-and-delete for the recovery lock: release it ONLY if this
# scanner still owns it.  Without the ownership check, a scan that outlives
# its 5s lock TTL would delete the lock a newer scanner had just acquired,
# letting a third scan run concurrently and double-requeue the same job.
# KEYS[1]=lock key, ARGV[1]=owner token
RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Atomic capacity slot acquisition: increment the worker's active-job counter
# only while it is below capacity. KEYS[1]=active hash, ARGV[1]=worker_id, ARGV[2]=capacity
ACQUIRE_SLOT_LUA = """
local active = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0')
local capacity = tonumber(ARGV[2])
if active < capacity then
    redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
    return 1
end
return 0
"""

_WORKER_STATE_KEY = "aegisforge:worker_state"  # Hash: worker_id -> {capacity, active, ts}
_WORKER_ACTIVE_KEY = "aegisforge:worker_active"  # Hash: worker_id -> active job count


class JobQueue:
    """Abstract job queue interface."""

    def enqueue(self, job: ExecutionJob) -> bool:
        """Add a job to the queue."""
        raise NotImplementedError

    def dequeue(
        self, worker_id: str = "", capacity: int | None = None
    ) -> ExecutionJob | None:
        """Remove and return the next job from the queue.

        Redis-backed implementations may use ``worker_id``/``capacity`` to
        claim the job atomically during dequeue; other implementations
        ignore them.
        """
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

    def dequeue(
        self, worker_id: str = "", capacity: int | None = None
    ) -> ExecutionJob | None:
        """Remove and return the next job (worker_id/capacity ignored)."""
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
        self._claim_script: Any = None
        self._heartbeat_script: Any = None
        self._release_script: Any = None
        self._acquire_slot_script: Any = None
        self._dequeue_claim_script: Any = None
        self._enqueue_idempotent_script: Any = None
        self._release_lock_script: Any = None
        self._register_scripts()

    def _register_scripts(self) -> None:
        """Register Lua scripts (best-effort; falls back to plain commands).

        Script registration can only fail if Redis itself is unreachable; in
        that case execute() calls also fail and are handled by callers.
        """
        try:
            self._claim_script = self._redis.register_script(CLAIM_LUA)
            self._heartbeat_script = self._redis.register_script(HEARTBEAT_CLAIM_LUA)
            self._release_script = self._redis.register_script(RELEASE_CLAIM_LUA)
            self._acquire_slot_script = self._redis.register_script(ACQUIRE_SLOT_LUA)
            self._dequeue_claim_script = self._redis.register_script(DEQUEUE_CLAIM_LUA)
            self._enqueue_idempotent_script = self._redis.register_script(
                ENQUEUE_IDEMPOTENT_LUA
            )
            self._release_lock_script = self._redis.register_script(RELEASE_LOCK_LUA)
        except Exception:
            self._claim_script = None
            self._heartbeat_script = None
            self._release_script = None
            self._acquire_slot_script = None
            self._dequeue_claim_script = None
            self._enqueue_idempotent_script = None
            self._release_lock_script = None

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

    @property
    def atomic_dequeue_available(self) -> bool:
        """Whether atomic pop+claim dequeue is available (Lua registered)."""
        return self._dequeue_claim_script is not None

    def dequeue(
        self, worker_id: str = "", capacity: int | None = None
    ) -> ExecutionJob | None:
        """Atomically pop AND claim the next job from the queue.

        Phase 6G: when ``worker_id`` is provided and the Lua script is
        available, a single Redis script performs RPOP + claim + bookkeeping
        in ONE atomic operation.  The previous RPOP-then-claim-again sequence
        had a failure window: if Redis failed (or the claim was rejected)
        after the pop but before the claim was registered, the popped copy
        was gone from the queue and registered nowhere recoverable — genuine
        job loss.  Now a successfully returned job is ALWAYS atomically
        claimed (registered in the active-claims zset), so the
        visibility-timeout recovery path covers it even if the worker dies
        before executing.

        Stale copies (terminal/already-claimed/unparseable) are discarded;
        the authoritative state lives in the job-data key.

        Without ``worker_id`` (direct/test callers) the legacy plain-RPOP
        path is used and the caller is responsible for ``claim_job``.
        """
        try:
            if self._dequeue_claim_script is not None and worker_id:
                expires_at = time.time() + self._visibility_timeout
                raw = self._dequeue_claim_script(
                    keys=[
                        self._queue_name,
                        _ACTIVE_CLAIMS_SET,
                        _JOB_CLAIM_PREFIX,
                    ],
                    args=[
                        worker_id,
                        str(int(self._visibility_timeout)),
                        str(expires_at),
                        _WORKER_ACTIVE_KEY,
                        _WORKER_STATE_KEY,
                        str(int(capacity)) if capacity is not None else "0",
                        _JOB_DATA_PREFIX,
                        20,
                    ],
                )
                if not raw:
                    return None
                record_job_dequeued()
                return ExecutionJob.model_validate_json(raw)
            # Fallback: plain RPOP (scripts unavailable, or caller without a
            # worker identity).  Pre-6G semantics: caller must claim_job().
            data = self._redis.rpop(self._queue_name)
            if data:
                record_job_dequeued()
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

    def claim_job(self, job: ExecutionJob, worker_id: str, capacity: int | None = None) -> bool:
        """Atomically claim a job for a specific worker.

        Phase 6G: the claim is one atomic Lua operation that enforces, in a
        single Redis round-trip:

        1. Job not already claimed (SET NX semantics).
        2. Job not in a terminal state (the terminal guard previously ran as
           a separate GET before SET — a TOCTOU window allowed a stale worker
           to claim a job another worker had just completed).
        3. Capacity check: when ``capacity`` is provided, the claim fails if
           the worker already holds ``capacity`` active jobs (bounded Redis
           state in a hash; stale workers disappear with the hash TTL).

        The script stores ``worker_id:expiry`` in the claim key and sets both
        the claim TTL and the active-claims sorted-set score atomically.
        Returns True only if this worker successfully claimed the job.
        """
        claim_key = f"{_JOB_CLAIM_PREFIX}{job.job_id}"
        job_data_key = f"{_JOB_DATA_PREFIX}{job.job_id}"
        try:
            expires_at = time.time() + self._visibility_timeout
            if self._claim_script is not None:
                args = [
                    worker_id,
                    str(int(self._visibility_timeout)),
                    str(expires_at),
                    _WORKER_ACTIVE_KEY,
                    _WORKER_STATE_KEY,
                    str(int(capacity)) if capacity is not None else "0",
                    job_data_key,
                    job.job_id,
                ]
                claimed = self._claim_script(keys=[claim_key], args=args)
            else:  # pragma: no cover - only when script registration failed
                claimed = self._redis.set(
                    claim_key, worker_id, nx=True, ex=self._visibility_timeout
                )
                if claimed:
                    self._redis.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: expires_at})
            if claimed:
                # Persist full job data for distributed access (idempotent).
                self._store_job_data(job)
                record_job_claimed()
                return True
            return False
        except Exception:
            logger.exception("Failed to claim job %s", job.job_id)
            return False

    def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        """Refresh the claim TTL to prevent timeout during long execution.

        Phase 6G: uses an atomic compare-and-expire Lua script.  The previous
        GET-then-EXPIRE implementation could extend ANOTHER worker's claim
        when this worker's claim expired between the GET and the EXPIRE and
        the job was re-claimed in that window (claim → execution race D).
        """
        claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
        try:
            if self._heartbeat_script is not None:
                refreshed = self._heartbeat_script(
                    keys=[claim_key], args=[worker_id, self._visibility_timeout]
                )
            else:  # pragma: no cover - only when script registration failed
                current = self._redis.get(claim_key)
                if current != worker_id:
                    return False
                self._redis.expire(claim_key, self._visibility_timeout)
                refreshed = 1
            if refreshed:
                # Update sorted set expiration timestamp (idempotent, bounded).
                expires_at = time.time() + self._visibility_timeout
                self._redis.zadd(_ACTIVE_CLAIMS_SET, {job_id: expires_at})
                record_claim_extension()
                return True
            return False
        except Exception:
            logger.warning("Failed to heartbeat job %s", job_id)
            record_worker_heartbeat_failure()
            return False

    def release_claim(self, job_id: str, worker_id: str) -> None:
        """Release a claim after job completion or failure.

        Phase 6G: atomic compare-and-delete.  The previous GET-then-DELETE
        could delete ANOTHER worker's claim if this worker's claim expired
        and the job was re-claimed mid-completion (double-completion race).
        A late worker losing its claim now cannot clobber the new owner.
        """
        claim_key = f"{_JOB_CLAIM_PREFIX}{job_id}"
        try:
            if self._release_script is not None:
                self._release_script(
                    keys=[claim_key, _ACTIVE_CLAIMS_SET], args=[worker_id, job_id]
                )
            else:  # pragma: no cover - only when script registration failed
                current = self._redis.get(claim_key)
                if current == worker_id:
                    self._redis.delete(claim_key)
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

    def register_idempotency(self, idempotency_key: str, job_id: str, ttl: int = 3600) -> bool:
        """Register an idempotency key mapping to a job.

        Phase 6G: returns True only when the key was freshly claimed by THIS
        registration (SET NX).  Callers use this for atomic duplicate
        submission detection: a False return means another submission holds
        or already bound the key.
        """
        if not idempotency_key:
            return False
        key = f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"
        try:
            return bool(self._redis.set(key, job_id, nx=True, ex=ttl))
        except Exception:
            logger.warning("Failed to register idempotency key %s", idempotency_key)
            return False

    def bind_idempotency(self, idempotency_key: str, job_id: str, ttl: int = 3600) -> None:
        """Finalize an idempotency binding previously reserved via SET NX.

        Overwrites the ``pending`` reservation marker with the real job id.
        Only the submitter that won the reservation may call this.
        """
        if not idempotency_key:
            return
        key = f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"
        try:
            self._redis.set(key, job_id, ex=ttl)
        except Exception:
            logger.warning("Failed to bind idempotency key %s", idempotency_key)

    def enqueue_idempotent(
        self, job: ExecutionJob, idempotency_key: str, ttl: int = 3600
    ) -> str:
        """Atomically reserve-and-enqueue a job under an idempotency key.

        Phase 6G (Task 4): the previous flow was reserve (SET NX of a
        ``pending`` marker) → create/enqueue → bind the real job id.  Two
        concurrent submissions could both observe ``pending`` and each
        create + enqueue a job (duplicate queue entries).

        This single Lua round-trip makes the decision and the side effect
        one atomic operation:

        * If the key is absent, it is bound to THIS job's id AND the job is
          LPUSHed onto the queue in the same operation.  No window exists in
          which the key is bound but the job is not yet queued (a crash
          after the script leaves the job durably queued).
        * If the key already exists, nothing is enqueued and the EXISTING
          job id is returned, so the caller can return the original job.

        Returns the winning job id (this job's id if we won, otherwise the
        already-bound job id).  Raises on Redis failure so the caller never
        reports a successful submission for a job that was not queued.
        """
        key = f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"
        job_data = job.model_dump_json()
        if self._enqueue_idempotent_script is not None:
            winner = self._enqueue_idempotent_script(
                keys=[key, self._queue_name],
                args=[
                    job.job_id,
                    str(int(ttl)),
                    job_data,
                    _JOB_DATA_PREFIX,
                ],
            )
            return str(winner)
        # Fallback (script registration failed): best-effort atomic NX + push.
        # Still safer than check-then-create: only the NX winner pushes.
        if self._redis.set(key, job.job_id, nx=True, ex=int(ttl)):
            self._redis.lpush(self._queue_name, job_data)
            return job.job_id
        existing = self._redis.get(key)
        return str(existing) if existing else job.job_id

    # ------------------------------------------------------------------
    # Crash recovery (Phase 6B)
    # ------------------------------------------------------------------

    def recover_expired_claims(self, max_recover: int = 10) -> list[str]:
        """Scan for jobs whose claim has expired and re-enqueue them.

        Phase 6G hardening:

        - A recovery lock (``SET NX EX`` per scan cycle) prevents multiple
          workers from concurrently recovering the SAME expired job and
          enqueueing it twice (recovery → claim race C / duplicate delivery).
          The lock is a heuristic backstop: the per-job claim key plus the
          terminal-status guard remain the real correctness boundary.
        - The claim key is deleted BEFORE the job is re-enqueued so a worker
          racing to dequeue the recovered copy cannot lose the claim race to
          the stale claim.
        - Terminal-status guard unchanged: terminal jobs are never re-enqueued.
        """
        recovered: list[str] = []
        lock_key = f"{_ACTIVE_CLAIMS_SET}:recovery_lock"
        lock_token = uuid.uuid4().hex
        try:
            # Heuristic mutual exclusion between recovery scans.  Short TTL:
            # if a scanner crashes mid-scan the next scan proceeds quickly.
            lock_acquired = self._redis.set(lock_key, lock_token, nx=True, ex=5)
            if not lock_acquired:
                return recovered
            now = time.time()
            # ZRANGEBYSCORE: find claims whose expiration timestamp <= now
            expired_job_ids: list[str] = self._redis.zrangebyscore(
                _ACTIVE_CLAIMS_SET, "-inf", str(now)
            )
            for job_id in expired_job_ids:
                if len(recovered) >= max_recover:
                    break
                record_claim_expiration()
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
                    record_job_abandoned()
                    continue
                # Re-enqueue for another worker
                job.retry_count += 1
                job.status = ExecutionJobStatus.RETRYING
                # Persist BEFORE enqueueing so the dequeuing worker reads the
                # RETRYING state, not a stale pre-recovery snapshot.
                self._store_job_data(job)
                self.enqueue(job)
                recovered.append(job_id)
                record_job_recovered()
                logger.info(
                    "Recovered expired job %s (attempt %d/%d)",
                    job_id, job.retry_count, job.max_retries,
                )
        except Exception:
            logger.exception("Error during crash recovery scan")
        finally:
            try:
                # Release only if we still own the lock (atomic compare-and-
                # delete): a scan whose lock expired must not clobber the lock
                # of a newer scanner.
                if self._release_lock_script is not None:
                    self._release_lock_script(keys=[lock_key], args=[lock_token])
                elif self._redis.get(lock_key) == lock_token:
                    self._redis.delete(lock_key)
            except Exception:  # noqa: S110 — best-effort lock release
                pass
        return recovered

    # ------------------------------------------------------------------
    # Worker registry (Phase 6B)
    # ------------------------------------------------------------------

    def register_worker(self, worker_id: str, ttl: int = DEFAULT_WORKER_TTL) -> None:
        """Register a worker heartbeat in the sorted set."""
        try:
            self._redis.zadd(_WORKERS_SET, {worker_id: time.time()})
            from aegisforge.observability.metrics import record_worker_registration

            record_worker_registration()
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
            from aegisforge.observability.metrics import record_worker_deregistration

            record_worker_deregistration()
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

    # ------------------------------------------------------------------
    # Phase 6G: Worker capacity (application-level, bounded Redis state)
    # ------------------------------------------------------------------

    def set_worker_capacity(self, worker_id: str, capacity: int) -> None:
        """Advertise this worker's capacity in bounded Redis state.

        The state hash is refreshed by worker heartbeats and removed on
        graceful shutdown, so stale workers disappear with their entries.
        """
        try:
            self._redis.hset(_WORKER_STATE_KEY, worker_id, str(int(capacity)))
            self._redis.zadd(_WORKERS_SET, {worker_id: time.time()})
        except Exception:  # noqa: S110 — capacity is advisory, never critical
            pass

    def cleanup_dead_worker_state(self, ttl: int = DEFAULT_WORKER_TTL) -> int:
        """Remove registry/state entries for workers whose heartbeat expired.

        Phase 6G boundedness fix: the worker sorted set and the capacity /
        active-jobs hashes previously only GREW — a crashed worker's entries
        lived forever.  Entries are now lifecycle-bound: refreshed by
        heartbeats, removed on graceful shutdown, and swept by this cleanup
        when the worker's registration TTL (heartbeat window) expires.

        Called from the worker heartbeat loop (cheap: one ZRANGEBYSCORE over
        a set bounded by the number of workers ever seen in the sweep window
        plus per-worker HDELs).  Returns the number of workers removed.
        """
        removed = 0
        try:
            cutoff = time.time() - ttl
            dead = self._redis.zrangebyscore(_WORKERS_SET, "-inf", str(cutoff))
            for worker_id in dead:
                # Re-check liveness to avoid racing a heartbeat that lands
                # between the score read and the removal.
                score = self._redis.zscore(_WORKERS_SET, worker_id)
                if score is not None and float(score) > cutoff:
                    continue
                self._redis.zrem(_WORKERS_SET, worker_id)
                self._redis.hdel(_WORKER_STATE_KEY, worker_id)
                self._redis.hdel(_WORKER_ACTIVE_KEY, worker_id)
                removed += 1
        except Exception:
            return 0
        return removed

    def get_worker_capacity(self, worker_id: str) -> int:
        """Return the advertised capacity for a worker (0 if unknown)."""
        try:
            value = self._redis.hget(_WORKER_STATE_KEY, worker_id)
            return int(value) if value else 0
        except Exception:
            return 0

    def get_worker_active_jobs(self, worker_id: str) -> int:
        """Return the worker's active-job count (bounded, advisory)."""
        try:
            value = self._redis.hget(_WORKER_ACTIVE_KEY, worker_id)
            return int(value) if value else 0
        except Exception:
            return 0

    def list_worker_utilization(self) -> list[dict[str, int | str]]:
        """Bounded snapshot of live workers' capacity utilization.

        Only workers with a recent heartbeat are returned; stale workers are
        filtered by the heartbeat timestamp so a dead worker's counters can
        never make the scheduler avoid it forever (they also disappear with
        the hash on graceful shutdown).
        """
        result: list[dict[str, int | str]] = []
        try:
            cutoff = time.time() - DEFAULT_WORKER_TTL
            live = self._redis.zrangebyscore(_WORKERS_SET, cutoff, "+inf")
            states = self._redis.hgetall(_WORKER_STATE_KEY) if live else {}
            actives = self._redis.hgetall(_WORKER_ACTIVE_KEY) if live else {}
            for worker_id in live:
                capacity = int(states.get(worker_id, 0) or 0)
                active = int(actives.get(worker_id, 0) or 0)
                result.append(
                    {
                        "worker_id": worker_id,
                        "capacity": capacity,
                        "active": active,
                        "available": max(capacity - active, 0),
                    }
                )
        except Exception:  # noqa: S110 — utilization is advisory, never critical
            pass
        return result

    def release_worker_slot(self, worker_id: str) -> None:
        """Decrement the worker's active-job counter after a job finishes."""
        try:
            active = self._redis.hget(_WORKER_ACTIVE_KEY, worker_id)
            if active is not None and int(active) > 0:
                self._redis.hincrby(_WORKER_ACTIVE_KEY, worker_id, -1)
        except Exception:  # noqa: S110 — capacity is advisory, never critical
            pass

    def cleanup_worker_state(self, worker_id: str) -> None:
        """Remove a worker's capacity state (graceful shutdown)."""
        try:
            self._redis.hdel(_WORKER_STATE_KEY, worker_id)
            self._redis.hdel(_WORKER_ACTIVE_KEY, worker_id)
        except Exception:  # noqa: S110 — capacity is advisory, never critical
            pass


# --- Lua script for atomic claim (the real correctness boundary) ---
#
# One Redis round-trip enforces:
#   1. Not already claimed (any live claim key wins).
#   2. Job not terminal (guard against claiming after completion/cancellation).
#   3. Worker capacity not exceeded (when capacity > 0).
# and atomically sets: claim key (owner:expiry, TTL), active-claims zset score,
# and the worker's active-job counter.
#
# KEYS[1] = claim key                aegisforge:claim:<job_id>
# ARGV[1] = worker_id
# ARGV[2] = visibility timeout (seconds, as string)
# ARGV[3] = expiry timestamp for the active-claims zset
# ARGV[4] = worker active-jobs hash key
# ARGV[5] = worker state hash key
# ARGV[6] = capacity (0 = no capacity check)
# ARGV[7] = job data key (only written on success; payload is stored by Python)
CLAIM_LUA = """
local claim_key = KEYS[1]
local worker_id = ARGV[1]
local ttl = tonumber(ARGV[2])
local expires_at = tonumber(ARGV[3])
local active_key = ARGV[4]
local state_key = ARGV[5]
local capacity = tonumber(ARGV[6])

-- 1. Already claimed?
if redis.call('GET', claim_key) then
    return 0
end

-- 2. Capacity check (bounded hash state; 0 disables the check)
if capacity > 0 then
    local active = tonumber(redis.call('HGET', active_key, worker_id) or '0')
    if active >= capacity then
        return 0
    end
end

-- 3. Terminal-state guard: reject if the stored job is in a terminal
--    state.  The status is extracted with cjson (deterministic) rather than
--    substring matching — a result/error payload that merely CONTAINS the
--    text '"status":"completed"' must not cause a false terminal rejection.
--    The job payload is written by Python AFTER a successful claim, so
--    absence of job data here is normal for first-time claims.
local job_data = redis.call('GET', ARGV[7])
if job_data then
    local ok, parsed = pcall(cjson.decode, job_data)
    if ok and type(parsed) == 'table' and parsed.status ~= nil then
        local s = tostring(parsed.status)
        if s == 'completed' or s == 'failed' or s == 'cancelled' then
            return 0
        end
    end
end

-- Claim the job atomically with all bookkeeping.
-- The claim value is the bare worker_id (ownership checks compare it
-- directly); the expiry lives in the active-claims sorted set score.
redis.call('SET', claim_key, worker_id, 'EX', ttl)
redis.call('ZADD', 'aegisforge:active_claims', expires_at, ARGV[8])
if capacity > 0 then
    redis.call('HINCRBY', active_key, worker_id, 1)
end
redis.call('HSET', state_key, worker_id, tostring(capacity))
return 1
"""

# Atomic idempotent enqueue: bind the idempotency key to a job id AND push
# the job onto the queue in ONE operation, so a key is never bound without
# a corresponding queued job (no duplicate creation window, no lost job).
#
# KEYS[1] = idempotency key, KEYS[2] = queue list
# ARGV[1] = candidate job id, ARGV[2] = idempotency TTL (s), ARGV[3] = job JSON,
# ARGV[4] = job-data key prefix (to inspect the bound job's terminal state)
#
# Returns the job id that owns the key — the candidate if the key was absent OR
# the previously-bound job is terminal/dead (resubmission after failure is an
# explicit contract), otherwise the already-bound id.  The queue is only
# mutated by the winning submitter.
ENQUEUE_IDEMPOTENT_LUA = """
local idem_key = KEYS[1]
local queue_key = KEYS[2]
local job_id = ARGV[1]
local ttl = tonumber(ARGV[2])
local job_json = ARGV[3]
local data_prefix = ARGV[4]

local existing = redis.call('GET', idem_key)
if existing then
    -- A binding is replaceable only when the bound job is terminal
    -- (failed/cancelled) or its durable data is gone.  COMPLETED and
    -- in-flight bindings are never hijacked.
    local replace = false
    local data = redis.call('GET', data_prefix .. existing)
    if not data then
        replace = true
    else
        local ok, parsed = pcall(cjson.decode, data)
        if ok and type(parsed) == 'table' and parsed.status ~= nil then
            local s = tostring(parsed.status)
            replace = (s == 'failed' or s == 'cancelled')
        else
            replace = true
        end
    end
    if not replace then
        return existing
    end
end
redis.call('SET', idem_key, job_id, 'EX', ttl)
redis.call('LPUSH', queue_key, job_json)
return job_id
"""


# Atomic dequeue + claim: pops the next job AND claims it in ONE Redis
# operation, closing the RPOP→claim failure window (a plain RPOP followed by
# a failed claim would remove the job from the queue without registering it
# anywhere recoverable — genuine job loss).
#
# KEYS[1] = queue list, KEYS[2] = active claims sorted set, KEYS[3] = claim key prefix
# ARGV[1] = worker_id, ARGV[2] = visibility TTL (s), ARGV[3] = expires_at,
# ARGV[4] = worker-active hash key, ARGV[5] = worker-state hash key,
# ARGV[6] = capacity (0 disables), ARGV[7] = job-data key prefix,
# ARGV[8] = max popped copies to inspect
#
# Returns the raw job JSON if claimed, nil otherwise.  Stale copies (already
# claimed, terminal, or non-parseable) are DISCARDED: the authoritative job
# state lives in the job-data key, and a recoverable copy is always either
# still in the queue or registered in the active-claims zset.
DEQUEUE_CLAIM_LUA = """
local queue_key = KEYS[1]
local active_claims = KEYS[2]
local claim_prefix = KEYS[3]
local worker_id = ARGV[1]
local ttl = tonumber(ARGV[2])
local expires_at = tonumber(ARGV[3])
local active_key = ARGV[4]
local state_key = ARGV[5]
local capacity = tonumber(ARGV[6])
local data_prefix = ARGV[7]
local max_scan = tonumber(ARGV[8])

if capacity > 0 then
    local active = tonumber(redis.call('HGET', active_key, worker_id) or '0')
    if active >= capacity then
        return nil
    end
end

for _ = 1, max_scan do
    local raw = redis.call('RPOP', queue_key)
    if not raw then
        return nil
    end
    local ok, job = pcall(cjson.decode, raw)
    if not ok or type(job) ~= 'table' or job.job_id == nil then
        -- Unparseable payload: nothing recoverable remains for it.
        raw = nil
    else
        local job_id = tostring(job.job_id)
        local claim_key = claim_prefix .. job_id
        local job_data = redis.call('GET', data_prefix .. job_id)
        local terminal = false
        if job_data then
            local pok, parsed = pcall(cjson.decode, job_data)
            if pok and type(parsed) == 'table' and parsed.status ~= nil then
                local s = tostring(parsed.status)
                if s == 'completed' or s == 'failed' or s == 'cancelled' then
                    terminal = true
                end
            end
        end
        if terminal then
            redis.call('DEL', claim_key)
            redis.call('ZREM', active_claims, job_id)
            raw = nil
        elseif redis.call('EXISTS', claim_key) == 1 then
            -- Stale duplicate copy: another worker already owns this job.
            raw = nil
        else
            redis.call('SET', claim_key, worker_id, 'EX', ttl)
            redis.call('ZADD', active_claims, expires_at, job_id)
            if capacity > 0 then
                redis.call('HINCRBY', active_key, worker_id, 1)
            end
            redis.call('HSET', state_key, worker_id, tostring(capacity))
            return raw
        end
    end
end
return nil
"""


class JobManager:
    """Manages job lifecycle: submission, tracking, retry, and status.

    Phase 6B: Supports both in-memory (testing) and Redis-backed (distributed)
    job tracking. When a RedisJobQueue is provided, idempotency and state
    are distributed across workers.

    Phase 6G: the in-memory job map is BOUNDED (LRU eviction of oldest
    entries beyond ``max_tracked_jobs``).  Previously it grew without limit,
    which is a memory leak in any long-lived API process.
    """

    def __init__(self, queue: JobQueue, max_tracked_jobs: int = DEFAULT_MAX_TRACKED_JOBS) -> None:
        self._queue = queue
        self._max_tracked = max(1, max_tracked_jobs)
        self._jobs: OrderedDict[str, ExecutionJob] = OrderedDict()

    def _remember_job(self, job: ExecutionJob) -> None:
        """Track a job in bounded in-memory state (LRU eviction)."""
        self._jobs[job.job_id] = job
        self._jobs.move_to_end(job.job_id)
        while len(self._jobs) > self._max_tracked:
            self._jobs.popitem(last=False)

    def submit_job(
        self,
        request_id: str,
        workflow_id: str,
        organization_id: str = "",
        max_retries: int = 3,
        idempotency_key: str = "",
        trace_id: str = "",
    ) -> ExecutionJob:
        """Submit a new execution job to the queue.

        Phase 6G: idempotent submissions are reserved AND enqueued atomically
        (see :meth:`RedisJobQueue.enqueue_idempotent`), so two concurrent
        submissions with the same key can never both create a queue entry.
        The returned job is always the single logical winner.
        """
        redis_queue: RedisJobQueue | None = None
        if idempotency_key and isinstance(self._queue, RedisJobQueue):
            redis_queue = self._queue
            # Fast path: an already-bound key returns the existing job without
            # creating anything.  The authoritative decision is still made
            # atomically below; this only avoids constructing a throwaway job
            # in the common repeat-request case.
            existing_id = redis_queue.check_idempotency(idempotency_key)
            if existing_id is not None:
                existing = redis_queue.get_job_data(existing_id)
                if existing is not None and existing.status not in (
                    ExecutionJobStatus.FAILED,
                    ExecutionJobStatus.CANCELLED,
                ):
                    logger.info(
                        "Idempotent request: returning existing job %s",
                        existing_id,
                    )
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
            submitted_at=time.time(),
        )

        if redis_queue is not None:
            # Persist job data BEFORE the atomic reserve+enqueue so a
            # concurrent submitter that loses the race can always read the
            # winner's job data (no 'bound id without data' window).  Orphan
            # data for a job that loses the reserve expires with its TTL.
            redis_queue._store_job_data(job)
            try:
                winner = redis_queue.enqueue_idempotent(job, idempotency_key)
            except Exception as exc:
                # Never report success for a job that was not queued.
                logger.exception("Atomic idempotent enqueue failed")
                raise RuntimeError(
                    f"Failed to enqueue job for idempotency key {idempotency_key!r}"
                ) from exc
            if winner != job.job_id:
                existing = redis_queue.get_job_data(winner)
                if existing is not None and existing.status not in (
                    ExecutionJobStatus.FAILED,
                    ExecutionJobStatus.CANCELLED,
                ):
                    logger.info(
                        "Idempotent race lost: returning existing job %s", winner
                    )
                    return existing
                # Winner is mid-flight or unreadable: return a reference to the
                # winning job id so the caller/duplicate request never spawns
                # a second logical job.
                job.job_id = winner
                job.idempotency_key = idempotency_key
                self._remember_job(job)
                record_queue_event("queued")
                return job
            self._remember_job(job)
            record_queue_event("queued")
            set_queue_depth(redis_queue.size())
            logger.info(
                "Submitted job %s for request %s (workflow %s)",
                job.job_id,
                request_id,
                workflow_id,
            )
            return job

        self._remember_job(job)
        enqueued = self._queue.enqueue(job)
        if not enqueued:
            # A failed enqueue must not be silently swallowed: the job would
            # be tracked in memory yet never executed.  Surface it so the API
            # can return an error instead of a phantom QUEUED job.
            self._jobs.pop(job.job_id, None)
            raise RuntimeError(
                f"Failed to enqueue job {job.job_id} (queue unavailable)"
            )

        # Persist job data for distributed visibility.
        if isinstance(self._queue, RedisJobQueue):
            self._queue._store_job_data(job)

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

        # Also update in-memory for local consistency (bounded map).
        self._remember_job(job)
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
        """Re-queue a failed job for retry.

        Phase 6G fix (retry → idempotency/status race E): the updated job
        state (RETRYING + incremented retry_count) is PERSISTED BEFORE the
        job is enqueued.  Previously the enqueue happened first, so a fast
        worker could dequeue the retry copy and read the stale pre-retry
        state from Redis.  With the stale state the worker's terminal guard
        could reject the claim (job still read as FAILED) and silently DROP
        the retry even though attempts remained.
        """
        job = self.get_job(job_id)
        if job is None or not self.should_retry(job):
            return None

        job.retry_count += 1
        job.status = ExecutionJobStatus.RETRYING

        # Persist BEFORE enqueue: any worker that dequeues the retry copy
        # must observe the new state.
        if isinstance(self._queue, RedisJobQueue):
            self._queue._store_job_data(job)

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
        if organization_id:
            # Tenant isolation: never return another organization's jobs.
            jobs = [j for j in jobs if j.organization_id == organization_id]
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
        capacity: int = 1,
    ) -> None:
        self._job_manager = job_manager
        self._job_handler = job_handler
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._heartbeat_interval = heartbeat_interval
        # Phase 6G: advertised capacity (max concurrent jobs).  A single
        # JobWorker executes one job at a time, so the effective slot count
        # is 1 per worker loop; operators run more loops/processes for more
        # capacity.  The claim-time capacity check prevents a worker loop
        # from being handed a job it cannot start promptly.
        self._capacity = max(1, capacity)
        self._active_job_id: str | None = None
        self._heartbeat_active = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def process_next_job(self) -> ExecutionJob | None:
        """Process the next job in the queue. Returns the updated job or None."""
        queue = self._job_manager._queue
        # Phase 6G: atomic pop+claim — the job is claimed for THIS worker in
        # the same Redis operation that pops it, closing the RPOP→claim loss
        # window.  Refresh the job-data TTL here: the payload may have been
        # queued longer than the job-data key's 1h TTL.
        atomic_claimed = False
        if isinstance(queue, RedisJobQueue) and queue.atomic_dequeue_available:
            job = queue.dequeue(self._worker_id, capacity=self._capacity)
            if job is not None:
                queue._store_job_data(job)
                atomic_claimed = True
        else:
            job = queue.dequeue()
        if job is None:
            return None
        set_queue_depth(self._job_manager._queue.size())
        start_time = time.monotonic()
        self._active_job_id = job.job_id

        # Track queue wait time (Phase 6G: wall-clock, not monotonic —
        # submitted_at may come from a different process, so both ends must
        # use the same clock).
        if job.submitted_at > 0:
            record_queue_wait_time(max(time.time() - job.submitted_at, 0.0))

        # Phase 6B/6G: atomic claim verification with capacity awareness.
        # A stale or duplicate job (e.g. an old copy read before another
        # worker finished it) is dropped here — the terminal guard in the
        # claim script prevents re-execution, and dropping the LOCAL copy
        # must not skip the job: its authoritative state lives in Redis and
        # (for retries) it was re-enqueued there.
        if atomic_claimed:
            # Claim was acquired atomically inside dequeue(); record it and
            # start the heartbeat.
            record_worker_event("claimed")
            self._heartbeat_active = True
        elif isinstance(self._job_manager._queue, RedisJobQueue):
            claimed = self._job_manager._queue.claim_job(
                job, self._worker_id, capacity=self._capacity
            )
            if not claimed:
                logger.warning(
                    "Job %s already claimed/terminal/capacity-blocked, skipping",
                    job.job_id,
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
        except BaseException as exc:
            # Phase 6G: BaseException (KeyboardInterrupt / SystemExit / hard
            # shutdown) must not strand the job in RUNNING with its claim
            # released in ``finally`` — that state was unreachable by every
            # recovery path (not queued, not in the active-claims zset).
            # Persist terminal FAILED state BEFORE propagating so the durable
            # state reflects reality; the terminal guard then prevents any
            # stale queued copy from re-executing.  At-least-once redelivery
            # for incomplete work remains the workflow-resume path's job.
            logger.exception("Job %s terminated by %s", job.job_id, type(exc).__name__)
            self._job_manager.update_job_status(
                job.job_id,
                ExecutionJobStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            job.status = ExecutionJobStatus.FAILED
            record_queue_event("failed")
            raise
        finally:
            self._heartbeat_active = False
            self._active_job_id = None
            # Phase 6B/6G: release the claim (atomic, ownership-checked) and
            # free this worker's capacity slot.
            if isinstance(self._job_manager._queue, RedisJobQueue):
                self._job_manager._queue.release_claim(job.job_id, self._worker_id)
                self._job_manager._queue.release_worker_slot(self._worker_id)

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
            self._job_manager._queue.set_worker_capacity(self._worker_id, self._capacity)

    def unregister_from_registry(self) -> None:
        """Remove this worker from the Redis worker registry."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            self._job_manager._queue.unregister_worker(self._worker_id)
            self._job_manager._queue.cleanup_worker_state(self._worker_id)

    def refresh_registry(self) -> None:
        """Refresh this worker's heartbeat in the registry."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            self._job_manager._queue.heartbeat_worker(self._worker_id)
            self._job_manager._queue.set_worker_capacity(self._worker_id, self._capacity)

    def recover_expired_jobs(self, max_recover: int = 5) -> list[str]:
        """Trigger crash recovery for expired claims."""
        if isinstance(self._job_manager._queue, RedisJobQueue):
            return self._job_manager._queue.recover_expired_claims(max_recover)
        return []
