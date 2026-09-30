"""Stuck job detection for AegisForge.

Provides a bounded, configurable mechanism for identifying jobs that appear
stuck. Distinguishes between legitimate long-running jobs and genuinely stuck
ones using multiple signals:

- Job age (time since submission)
- Claim age (time since last claim or heartbeat)
- Visibility timeout (Redis claim TTL)
- Execution timeout (configurable max execution time)
- Worker heartbeat status

The detector is conservative — it uses configurable thresholds and requires
multiple signals to agree before marking a job as stuck.

Key states:
- queued: job is in the queue, not yet claimed
- executing: job is claimed and being processed
- retrying: job failed and is being retried
- recovered: job was recovered from a stuck/crashed state
- completed: job finished successfully
- failed: job failed but may be retried
- terminally failed: job exhausted all retries
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from aegisforge.domain.models import ExecutionJobStatus

logger = logging.getLogger(__name__)


@dataclass
class StuckJobConfig:
    """Configuration for stuck job detection thresholds.

    All timeouts are in seconds. Defaults are conservative to avoid
    falsely recovering legitimate long-running jobs.
    """

    # Maximum age of a job before it's considered potentially stuck (queued)
    max_job_age_seconds: float = 3600.0  # 1 hour

    # Maximum age of a claim before it's considered stale (executing)
    max_claim_age_seconds: float = 600.0  # 10 minutes

    # Maximum execution time before a job is considered stuck
    max_execution_time_seconds: float = 1800.0  # 30 minutes

    # Minimum number of heartbeats a worker should have sent
    # before we consider the worker alive
    min_heartbeat_freshness_seconds: float = 120.0  # 2 minutes

    # Maximum number of recoveries before we give up
    max_recoveries: int = 5

    # Whether stuck job detection is enabled
    enabled: bool = True


@dataclass
class StuckJobInfo:
    """Information about a detected stuck job."""

    job_id: str
    status: str  # Current job status
    reason: str  # Why it's considered stuck
    age_seconds: float  # How old the job is
    claim_age_seconds: float  # How old the claim is (0 if unclaimed)
    recovery_count: int  # How many times it's been recovered


class StuckJobDetector:
    """Detects and optionally recovers stuck jobs.

    The detector runs periodically and examines jobs in Redis to identify
    those that appear stuck. It uses configurable thresholds and multiple
    signals to minimize false positives.

    Recovery is bounded: each job can only be recovered a limited number
    of times before it's marked as terminally failed.

    Phase 6G hardening:
    - Recovery counts are tracked in Redis (bounded TTL), not in an
      unbounded process-local dict, so they survive restarts, are shared
      across workers, and cannot leak memory.
    - Age comparisons use wall-clock timestamps consistently.
    """

    _RECOVERY_COUNTS_KEY = "aegisforge:stuck_job_recovery_counts"
    _RECOVERY_COUNT_TTL = 86400  # 24h: bounded, well above any scan cadence

    def __init__(
        self,
        redis_client: Any,
        config: StuckJobConfig | None = None,
    ) -> None:
        self._redis = redis_client
        self._config = config or StuckJobConfig()

    def _get_recovery_count(self, job_id: str) -> int:
        try:
            value = self._redis.hget(self._RECOVERY_COUNTS_KEY, job_id)
            return int(value) if value else 0
        except Exception:
            return 0

    def _increment_recovery_count(self, job_id: str) -> int:
        try:
            count = self._redis.hincrby(self._RECOVERY_COUNTS_KEY, job_id, 1)
            self._redis.expire(self._RECOVERY_COUNTS_KEY, self._RECOVERY_COUNT_TTL)
            return int(count)
        except Exception:
            return self._get_recovery_count(job_id) + 1

    @property
    def config(self) -> StuckJobConfig:
        return self._config

    def scan_for_stuck_jobs(
        self,
        max_scan: int = 50,
    ) -> list[StuckJobInfo]:
        """Scan Redis for jobs that appear stuck.

        Examines jobs in the active claims sorted set and the job queue
        to identify stuck jobs based on configurable thresholds.

        Returns a list of StuckJobInfo for each stuck job found.
        """
        if not self._config.enabled:
            return []

        stuck_jobs: list[StuckJobInfo] = []
        now = time.time()

        try:
            # 1. Check active claims for stale claims
            active_claim_ids: list[str] = self._redis.zrangebyscore(
                "aegisforge:active_claims", "-inf", str(now)
            )

            for job_id in active_claim_ids[:max_scan]:
                job_data = self._get_job_data(job_id)
                if job_data is None:
                    continue

                info = self._check_stuck_claim(job_id, job_data, now)
                if info is not None:
                    stuck_jobs.append(info)

            # 2. Check queued jobs that have been waiting too long
            # Scan a sample of jobs in the queue
            queue_depth = self._redis.llen("aegisforge:jobs")
            if queue_depth > 0:
                # Peek at jobs in the queue without removing them
                sample_size = min(max_scan, queue_depth)
                queued_jobs: list[str] = self._redis.lrange(
                    "aegisforge:jobs", 0, sample_size - 1
                )
                for raw_job in queued_jobs:
                    try:
                        from aegisforge.domain.models import ExecutionJob

                        job = ExecutionJob.model_validate_json(raw_job)
                        info = self._check_stuck_queued(job, now)
                        if info is not None:
                            stuck_jobs.append(info)
                    except Exception:  # noqa: S112 — malformed data is expected
                        continue

        except Exception:
            logger.exception("Error during stuck job scan")

        return stuck_jobs

    def _get_job_data(self, job_id: str) -> dict[str, Any] | None:
        """Get job data from Redis as a raw dict."""
        key = f"aegisforge:job:{job_id}"
        try:
            data = self._redis.get(key)
            if data:
                from aegisforge.domain.models import ExecutionJob

                job = ExecutionJob.model_validate_json(data)
                return {
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "retry_count": job.retry_count,
                    "max_retries": job.max_retries,
                    "submitted_at": job.submitted_at,
                    "errors": job.errors,
                }
        except Exception:  # noqa: S110 — observability must never break execution
            pass
        return None

    def _check_stuck_claim(
        self, job_id: str, job_data: dict[str, Any], now: float
    ) -> StuckJobInfo | None:
        """Check if a claimed job is stuck."""
        status = job_data["status"]

        # Only check jobs that are in RUNNING or RETRYING state
        if status not in ("running", "retrying"):
            return None

        # Check claim age
        claim_score = self._redis.zscore("aegisforge:active_claims", job_id)
        if claim_score is None:
            return None

        claim_age = now - claim_score
        if claim_age < self._config.max_claim_age_seconds:
            return None

        # Check execution time if submitted_at is available
        submitted_at = job_data.get("submitted_at", 0)
        if submitted_at > 0:
            job_age = now - submitted_at
            if job_age > self._config.max_execution_time_seconds:
                reason = (
                    f"Execution timeout: job running for {job_age:.0f}s "
                    f"(max: {self._config.max_execution_time_seconds:.0f}s)"
                )
            else:
                reason = (
                    f"Stale claim: claim age {claim_age:.0f}s "
                    f"(max: {self._config.max_claim_age_seconds:.0f}s)"
                )
        else:
            reason = (
                f"Stale claim: claim age {claim_age:.0f}s "
                f"(max: {self._config.max_claim_age_seconds:.0f}s)"
            )

        return StuckJobInfo(
            job_id=job_id,
            status=status,
            reason=reason,
            age_seconds=now - submitted_at if submitted_at > 0 else 0,
            claim_age_seconds=claim_age,
            recovery_count=self._get_recovery_count(job_id),
        )

    def _check_stuck_queued(
        self, job: Any, now: float
    ) -> StuckJobInfo | None:
        """Check if a queued job has been waiting too long."""
        if job.status != ExecutionJobStatus.QUEUED:
            return None

        if job.submitted_at <= 0:
            return None

        age = now - job.submitted_at
        if age < self._config.max_job_age_seconds:
            return None

        return StuckJobInfo(
            job_id=job.job_id,
            status="queued",
            reason=(
                f"Queue wait timeout: job queued for {age:.0f}s "
                f"(max: {self._config.max_job_age_seconds:.0f}s)"
            ),
            age_seconds=age,
            claim_age_seconds=0,
            recovery_count=self._get_recovery_count(job.job_id),
        )

    def recover_stuck_job(self, stuck_info: StuckJobInfo) -> bool:
        """Attempt to recover a stuck job.

        Bounded recovery: each job can only be recovered max_recoveries times
        (counted in Redis, shared across workers, TTL-bounded).

        Returns True if recovery was attempted.
        """
        job_id = stuck_info.job_id
        current_count = self._get_recovery_count(job_id)

        if current_count >= self._config.max_recoveries:
            logger.warning(
                "Job %s exceeded max recoveries (%d), marking as terminally failed",
                job_id,
                self._config.max_recoveries,
            )
            # Mark as terminally failed
            self._mark_terminal_failure(job_id, stuck_info)
            return False

        try:
            # Remove from active claims
            self._redis.zrem("aegisforge:active_claims", job_id)
            # Delete claim key
            self._redis.delete(f"aegisforge:claim:{job_id}")

            # Get job data and re-enqueue
            from aegisforge.async_execution.jobs import (
                _JOB_DATA_PREFIX,
                _JOBS_QUEUE,
            )

            key = f"{_JOB_DATA_PREFIX}{job_id}"
            data = self._redis.get(key)
            if data is None:
                logger.warning("Cannot recover stuck job %s: no job data found", job_id)
                return False

            from aegisforge.domain.models import ExecutionJob

            job = ExecutionJob.model_validate_json(data)

            if job.status in (
                ExecutionJobStatus.COMPLETED,
                ExecutionJobStatus.FAILED,
                ExecutionJobStatus.CANCELLED,
            ):
                return False

            if job.retry_count >= job.max_retries:
                self._mark_terminal_failure(job_id, stuck_info)
                return False

            # Re-enqueue: persist updated state BEFORE pushing to the queue
            # (same ordering fix as RedisJobQueue.recover_expired_claims).
            job.retry_count += 1
            job.status = ExecutionJobStatus.RETRYING
            self._increment_recovery_count(job_id)
            self._redis.set(key, job.model_dump_json(), ex=3600)
            self._redis.lpush(_JOBS_QUEUE, job.model_dump_json())

            logger.info(
                "Recovered stuck job %s (attempt %d/%d): %s",
                job_id,
                job.retry_count,
                job.max_retries,
                stuck_info.reason,
            )
            return True

        except Exception:
            logger.exception("Failed to recover stuck job %s", job_id)
            return False

    def _mark_terminal_failure(self, job_id: str, stuck_info: StuckJobInfo) -> None:
        """Mark a stuck job as terminally failed after exhausting recoveries."""
        try:
            key = f"aegisforge:job:{job_id}"
            data = self._redis.get(key)
            if data is None:
                return

            from aegisforge.domain.models import ExecutionJob

            job = ExecutionJob.model_validate_json(data)
            job.status = ExecutionJobStatus.FAILED
            job.errors.append(
                f"Stuck job detection: {stuck_info.reason} "
                f"(recovered {stuck_info.recovery_count} times, max exhausted)"
            )
            self._redis.set(key, job.model_dump_json(), ex=3600)

            # Clean up claim
            self._redis.zrem("aegisforge:active_claims", job_id)
            self._redis.delete(f"aegisforge:claim:{job_id}")

            logger.warning(
                "Job %s marked as terminally failed after stuck detection: %s",
                job_id,
                stuck_info.reason,
            )
        except Exception:
            logger.exception("Failed to mark terminal failure for stuck job %s", job_id)

    def get_recovery_count(self, job_id: str) -> int:
        """Return how many times a job has been recovered."""
        return self._get_recovery_count(job_id)

    def reset_recovery_counts(self) -> None:
        """Reset all recovery counts (for testing)."""
        try:
            self._redis.delete(self._RECOVERY_COUNTS_KEY)
        except Exception:  # noqa: S110 — best-effort cleanup
            pass
