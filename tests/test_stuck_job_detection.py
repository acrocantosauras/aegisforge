"""Phase 6C: Stuck job detection tests.

Verifies the StuckJobDetector identifies stuck jobs and recovers them
within configurable bounds.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from aegisforge.async_execution.stuck_job_detector import (
    StuckJobConfig,
    StuckJobDetector,
    StuckJobInfo,
)


def _redis_available() -> bool:
    try:
        import redis as _redis

        c = _redis.from_url("redis://localhost:6379/0", decode_responses=True)
        c.ping()
        c.close()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Stuck job detection tests require Redis",
)


@pytest.fixture()
def redis_client() -> Any:
    import redis

    client = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    client.ping()
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    client.delete("aegisforge:active_claims")
    client.delete("aegisforge:workers")
    yield client
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    client.delete("aegisforge:active_claims")
    client.delete("aegisforge:workers")


class TestStuckJobConfig:
    def test_default_config(self) -> None:
        config = StuckJobConfig()
        assert config.enabled is True
        assert config.max_job_age_seconds == 3600.0
        assert config.max_claim_age_seconds == 600.0
        assert config.max_execution_time_seconds == 1800.0
        assert config.max_recoveries == 5

    def test_custom_config(self) -> None:
        config = StuckJobConfig(
            max_job_age_seconds=60.0,
            max_claim_age_seconds=30.0,
            max_recoveries=2,
        )
        assert config.max_job_age_seconds == 60.0
        assert config.max_recoveries == 2


@requires_redis
class TestStuckJobDetector:
    def test_disabled_detector_returns_empty(self, redis_client: Any) -> None:
        detector = StuckJobDetector(
            redis_client=redis_client,
            config=StuckJobConfig(enabled=False),
        )
        stuck = detector.scan_for_stuck_jobs()
        assert stuck == []

    def test_no_stuck_jobs_when_claims_fresh(self, redis_client: Any) -> None:
        from aegisforge.async_execution.jobs import RedisJobQueue
        from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

        queue = RedisJobQueue(
            redis_client, queue_name="aegisforge:test-fresh", visibility_timeout=300
        )
        job = ExecutionJob(
            job_id="job-fresh-claim",
            request_id="req-fresh",
            workflow_id="wf-fresh",
            organization_id="org-test",
            status=ExecutionJobStatus.RUNNING,
            submitted_at=time.monotonic(),
        )
        queue._store_job_data(job)
        queue.claim_job(job, "worker-fresh")

        detector = StuckJobDetector(
            redis_client=redis_client,
            config=StuckJobConfig(max_claim_age_seconds=600),
        )
        stuck = detector.scan_for_stuck_jobs()
        # No stuck jobs — claim is fresh
        assert all(s.job_id != "job-fresh-claim" for s in stuck)

    def test_detects_stale_claim(self, redis_client: Any) -> None:
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET
        from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

        job = ExecutionJob(
            job_id="job-stale-claim",
            request_id="req-stale",
            workflow_id="wf-stale",
            organization_id="org-test",
            status=ExecutionJobStatus.RUNNING,
            submitted_at=time.monotonic() - 120,
        )
        # Store job data
        key = f"aegisforge:job:{job.job_id}"
        redis_client.set(key, job.model_dump_json(), ex=3600)

        # Create a stale claim (score in the past)
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 600})

        detector = StuckJobDetector(
            redis_client=redis_client,
            config=StuckJobConfig(max_claim_age_seconds=300),
        )
        stuck = detector.scan_for_stuck_jobs()
        stuck_ids = [s.job_id for s in stuck]
        assert "job-stale-claim" in stuck_ids

    def test_recovery_increments_count(self, redis_client: Any) -> None:
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

        # Create job data
        from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

        job = ExecutionJob(
            job_id="job-recovery-count",
            request_id="req-recovery",
            workflow_id="wf-recovery",
            organization_id="org-test",
            status=ExecutionJobStatus.RUNNING,
            max_retries=5,
            submitted_at=time.monotonic(),
        )
        key = f"aegisforge:job:{job.job_id}"
        redis_client.set(key, job.model_dump_json(), ex=3600)
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 600})

        detector = StuckJobDetector(
            redis_client=redis_client,
            config=StuckJobConfig(max_claim_age_seconds=1, max_recoveries=5),
        )

        # First recovery
        stuck = detector.scan_for_stuck_jobs()
        for s in stuck:
            if s.job_id == "job-recovery-count":
                detector.recover_stuck_job(s)

        assert detector.get_recovery_count("job-recovery-count") == 1

    def test_max_recoveries_marks_terminal(self, redis_client: Any) -> None:
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET
        from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

        job = ExecutionJob(
            job_id="job-terminal-recovery",
            request_id="req-terminal",
            workflow_id="wf-terminal",
            organization_id="org-test",
            status=ExecutionJobStatus.RUNNING,
            max_retries=10,
            submitted_at=time.monotonic(),
        )
        key = f"aegisforge:job:{job.job_id}"
        redis_client.set(key, job.model_dump_json(), ex=3600)
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 600})

        detector = StuckJobDetector(
            redis_client=redis_client,
            config=StuckJobConfig(max_claim_age_seconds=1, max_recoveries=2),
        )

        # Simulate multiple stuck detections
        for _ in range(5):
            redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 600})
            stuck = detector.scan_for_stuck_jobs()
            for s in stuck:
                if s.job_id == "job-terminal-recovery":
                    detector.recover_stuck_job(s)

        # After max_recoveries, job should be terminal
        import json

        data = redis_client.get(key)
        assert data is not None
        job_data = json.loads(data)
        assert job_data["status"] == "failed"

    def test_reset_recovery_counts(self, redis_client: Any) -> None:
        detector = StuckJobDetector(
            redis_client=redis_client,
            config=StuckJobConfig(),
        )
        detector._recovery_counts["job-1"] = 3
        detector.reset_recovery_counts()
        assert detector.get_recovery_count("job-1") == 0

    def test_stuck_job_info_fields(self) -> None:
        info = StuckJobInfo(
            job_id="job-test",
            status="running",
            reason="stale claim",
            age_seconds=100.0,
            claim_age_seconds=500.0,
            recovery_count=1,
        )
        assert info.job_id == "job-test"
        assert info.status == "running"
        assert info.claim_age_seconds == 500.0
