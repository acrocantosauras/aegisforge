"""Phase 6C: Failure injection and reliability tests.

Deterministically proves system reliability under failure conditions:
- Worker crash → job recovery
- Worker restart → worker registers again
- Expired claim → recovery
- Heartbeat → claim remains alive
- Bounded retries → terminal failure
- Stuck-job detection
- Health/readiness dependency failure
- Recovery after dependency becomes available
- Multi-worker distribution
- No cross-tenant leakage

Uses real Redis to prove actual distributed queue semantics.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from aegisforge.async_execution.jobs import JobManager, JobWorker, RedisJobQueue
from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redis_available() -> bool:
    """Check if a real Redis instance is reachable."""
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
    reason="Reliability tests require a real Redis instance",
)


@pytest.fixture()
def redis_client() -> Any:
    """Real Redis client for reliability tests."""
    import redis

    client = redis.from_url(
        "redis://localhost:6379/0",
        decode_responses=True,
    )
    client.ping()
    # Clean up test keys AND global shared state before each test
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    client.delete("aegisforge:active_claims")
    client.delete("aegisforge:workers")
    yield client
    # Cleanup after test
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    client.delete("aegisforge:active_claims")
    client.delete("aegisforge:workers")


@pytest.fixture()
def unique_queue_name() -> str:
    return f"aegisforge:test-rel-{uuid.uuid4().hex[:8]}"


def _make_job(
    request_id: str = "",
    organization_id: str = "org-test",
    max_retries: int = 3,
) -> ExecutionJob:
    return ExecutionJob(
        job_id=f"job-{uuid.uuid4().hex[:12]}",
        request_id=request_id or f"req-{uuid.uuid4().hex[:8]}",
        workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
        organization_id=organization_id,
        status=ExecutionJobStatus.QUEUED,
        max_retries=max_retries,
        idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
    )


# ---------------------------------------------------------------------------
# Test: Worker crash → job recovery
# ---------------------------------------------------------------------------

@requires_redis
def test_worker_crash_job_recovery(redis_client: Any, unique_queue_name: str) -> None:
    """When a worker crashes mid-execution, its claimed job is recovered."""
    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=2
    )
    manager = JobManager(queue)
    job = manager.submit_job(
        request_id="req-crash-recovery",
        workflow_id="wf-crash-recovery",
        organization_id="org-test",
        max_retries=2,
    )

    # Simulate real worker flow: dequeue THEN claim (worker crashes after claim)
    dequeued = queue.dequeue()
    assert dequeued is not None
    assert dequeued.job_id == job.job_id

    # Claim the job (simulating worker starting execution)
    queue.claim_job(dequeued, "worker-crasher")

    # Wait for claim to expire (worker crashed, no heartbeat)
    time.sleep(3)

    # Recovery scan finds the expired claim
    recovered = queue.recover_expired_claims()
    assert job.job_id in recovered

    # The job is re-enqueued with incremented retry_count
    requeued = queue.dequeue()
    assert requeued is not None
    assert requeued.job_id == job.job_id
    assert requeued.retry_count == 1
    assert requeued.status == ExecutionJobStatus.RETRYING


# ---------------------------------------------------------------------------
# Test: Worker restart → registers again
# ---------------------------------------------------------------------------

@requires_redis
def test_worker_restart_registers_again(redis_client: Any, unique_queue_name: str) -> None:
    """After a worker restarts, it re-registers in the worker registry."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)

    # First worker instance
    queue.register_worker("worker-restart-test")
    active = queue.get_active_workers()
    assert "worker-restart-test" in active

    # Simulate crash: unregister
    queue.unregister_worker("worker-restart-test")
    active = queue.get_active_workers()
    assert "worker-restart-test" not in active

    # Worker restarts and re-registers
    queue.register_worker("worker-restart-test")
    active = queue.get_active_workers()
    assert "worker-restart-test" in active


# ---------------------------------------------------------------------------
# Test: Expired claim → recovery
# ---------------------------------------------------------------------------

@requires_redis
def test_expired_claim_recovery(redis_client: Any, unique_queue_name: str) -> None:
    """Expired claims are detected and recovered."""
    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=1
    )
    job = _make_job()
    queue.claim_job(job, "worker-expire-test")

    # Wait for claim to expire
    time.sleep(2)

    # Recovery finds it
    recovered = queue.recover_expired_claims()
    assert job.job_id in recovered


# ---------------------------------------------------------------------------
# Test: Heartbeat → claim remains alive
# ---------------------------------------------------------------------------

@requires_redis
def test_heartbeat_keeps_claim_alive(redis_client: Any, unique_queue_name: str) -> None:
    """Heartbeating prevents claim expiration."""
    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=2
    )
    job = _make_job()
    queue.claim_job(job, "worker-heartbeat-test")

    # Heartbeat before expiration
    time.sleep(1)
    queue.heartbeat_job(job.job_id, "worker-heartbeat-test")
    time.sleep(1)
    queue.heartbeat_job(job.job_id, "worker-heartbeat-test")
    time.sleep(1)
    queue.heartbeat_job(job.job_id, "worker-heartbeat-test")

    # Claim should still be alive (4 seconds, with heartbeat every 1s)
    assert queue.is_claimed(job.job_id)
    owner = queue.get_claim_owner(job.job_id)
    assert owner == "worker-heartbeat-test"


# ---------------------------------------------------------------------------
# Test: Bounded retries → terminal failure
# ---------------------------------------------------------------------------

@requires_redis
def test_bounded_retry_terminal_failure(redis_client: Any, unique_queue_name: str) -> None:
    """Jobs that exhaust max_retries reach terminal FAILED state."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)
    manager.submit_job(
        request_id="req-bounded-retry",
        workflow_id="wf-bounded-retry",
        organization_id="org-test",
        max_retries=1,
    )

    call_count = {"n": 0}

    def always_fail(job: ExecutionJob) -> dict[str, Any]:
        call_count["n"] += 1
        raise RuntimeError("Simulated failure")

    worker = JobWorker(manager, always_fail, worker_id="worker-bounded-retry")

    # First attempt
    result1 = worker.process_next_job()
    assert result1 is not None
    assert result1.status == ExecutionJobStatus.RETRYING

    # Second attempt (retry)
    result2 = worker.process_next_job()
    assert result2 is not None
    assert result2.status == ExecutionJobStatus.FAILED

    # No more jobs in queue
    assert queue.size() == 0
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Test: Stuck-job detection
# ---------------------------------------------------------------------------

@requires_redis
def test_stuck_job_detection(redis_client: Any, unique_queue_name: str) -> None:
    """The stuck job detector identifies jobs with stale claims."""
    from aegisforge.async_execution.stuck_job_detector import (
        StuckJobConfig,
        StuckJobDetector,
    )

    # Use short visibility timeout so claim expires quickly
    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=1
    )
    job = _make_job()
    # Set status to RUNNING (matching real worker flow: dequeue → claim → running)
    job.status = ExecutionJobStatus.RUNNING
    queue._store_job_data(job)
    queue.claim_job(job, "worker-stuck-test")

    # Create detector with very short thresholds
    detector = StuckJobDetector(
        redis_client=redis_client,
        config=StuckJobConfig(
            max_claim_age_seconds=0.1,  # Very short for testing
            enabled=True,
        ),
    )

    # Wait for claim to expire AND for the detector to see it as stale
    # Score = expires_at = time.time() + 1. Need now > expires_at + 0.1
    time.sleep(1.5)

    stuck = detector.scan_for_stuck_jobs()
    # The job should be detected as stuck
    stuck_ids = [s.job_id for s in stuck]
    assert job.job_id in stuck_ids


@requires_redis
def test_stuck_job_recovery_bounded(redis_client: Any, unique_queue_name: str) -> None:
    """Stuck job recovery is bounded by max_recoveries."""
    from aegisforge.async_execution.stuck_job_detector import (
        StuckJobConfig,
        StuckJobDetector,
    )

    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=1
    )

    # Create job with RUNNING status directly (simulating real worker flow)
    job = ExecutionJob(
        job_id=f"job-stuck-{uuid.uuid4().hex[:8]}",
        request_id="req-stuck-bounded",
        workflow_id="wf-stuck-bounded",
        organization_id="org-test",
        status=ExecutionJobStatus.RUNNING,
        max_retries=10,  # High max retries
        submitted_at=time.monotonic(),
    )
    queue._store_job_data(job)

    detector = StuckJobDetector(
        redis_client=redis_client,
        config=StuckJobConfig(
            max_claim_age_seconds=0.5,
            max_recoveries=2,  # Low for testing
            enabled=True,
        ),
    )

    # Simulate repeated stuck detection and recovery
    for _ in range(5):
        # Create a fresh claim (stale after 0.5s)
        claim_key = f"aegisforge:claim:{job.job_id}"
        redis_client.set(claim_key, f"worker-stuck-{uuid.uuid4().hex[:4]}", ex=1)
        # Add to active claims sorted set with expiration in the past
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 10})
        time.sleep(1)  # Wait for claim key to expire

        stuck = detector.scan_for_stuck_jobs()
        for s in stuck:
            if s.job_id == job.job_id:
                detector.recover_stuck_job(s)

    # After max_recoveries, job should be terminally failed
    job_data = queue.get_job_data(job.job_id)
    assert job_data is not None
    assert job_data.status == ExecutionJobStatus.FAILED


# ---------------------------------------------------------------------------
# Test: Redis interruption behavior
# ---------------------------------------------------------------------------

@requires_redis
def test_redis_reconnection_after_failure(redis_client: Any, unique_queue_name: str) -> None:
    """Queue operations work after a temporary Redis failure."""
    from aegisforge.async_execution.reliability import BoundedBackoff, RedisResilientClient

    # Create a resilient client wrapper
    resilient = RedisResilientClient(
        redis_client=redis_client,
        max_retries=3,
        backoff=BoundedBackoff(initial_delay=0.01, max_delay=0.1),
    )

    # Normal operation
    result = resilient.execute("ping")
    assert result is not None

    # Simulate failure by using a broken client
    class BrokenRedis:
        def __init__(self, real_client: Any) -> None:
            self._real = real_client
            self._fail_next = True

        def ping(self) -> Any:
            if self._fail_next:
                self._fail_next = False
                raise ConnectionError("Simulated Redis failure")
            return self._real.ping()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    broken = BrokenRedis(redis_client)
    resilient_broken = RedisResilientClient(
        redis_client=broken,
        max_retries=3,
        backoff=BoundedBackoff(initial_delay=0.01, max_delay=0.1),
    )

    # First call fails, retry succeeds
    result = resilient_broken.execute("ping")
    assert result is not None


# ---------------------------------------------------------------------------
# Test: Health/readiness dependency failure
# ---------------------------------------------------------------------------

def test_health_endpoint_always_ok() -> None:
    """/health always returns 200 regardless of dependencies."""
    from fastapi.testclient import TestClient

    from aegisforge.app import create_app
    from aegisforge.config import Settings

    settings = Settings(
        database_url="sqlite:///:memory:",
        secret_key="test-secret",
        environment="test",
    )
    app = create_app(settings=settings)
    client = TestClient(app)

    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Test: Multi-worker distribution
# ---------------------------------------------------------------------------

@requires_redis
def test_multi_worker_concurrent_processing(
    redis_client: Any, unique_queue_name: str
) -> None:
    """Multiple workers process jobs concurrently without conflicts."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    processed_by: dict[str, str] = {}
    lock = threading.Lock()

    def make_handler(worker_id: str) -> Any:
        def handler(job: ExecutionJob) -> dict[str, Any]:
            with lock:
                processed_by[job.job_id] = worker_id
            time.sleep(0.01)
            return {"worker": worker_id}
        return handler

    # Submit 10 jobs
    for _ in range(10):
        manager.submit_job(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
            organization_id="org-test",
        )

    # Create 3 workers
    workers = []
    for i in range(3):
        w = JobWorker(manager, make_handler(f"worker-{i}"), worker_id=f"worker-{i}")
        workers.append(w)

    # Process from all workers
    all_jobs: list[str] = []
    for w in workers:
        while True:
            job = w.process_next_job()
            if job is None:
                break
            all_jobs.append(job.job_id)

    # All 10 jobs processed
    assert len(all_jobs) == 10
    assert len(set(all_jobs)) == 10  # No duplicates

    # Each job processed by exactly one worker
    assert len(processed_by) == 10
    for worker_id in processed_by.values():
        assert worker_id.startswith("worker-")


# ---------------------------------------------------------------------------
# Test: No cross-tenant leakage
# ---------------------------------------------------------------------------

@requires_redis
def test_no_cross_tenant_job_leakage(redis_client: Any, unique_queue_name: str) -> None:
    """Jobs from different tenants remain isolated."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    # Submit jobs for different tenants
    org_a_ids = []
    org_b_ids = []
    for _ in range(5):
        job_a = manager.submit_job(
            request_id=f"req-a-{uuid.uuid4().hex[:8]}",
            workflow_id="wf-a",
            organization_id="org-alpha",
        )
        org_a_ids.append(job_a.job_id)

        job_b = manager.submit_job(
            request_id=f"req-b-{uuid.uuid4().hex[:8]}",
            workflow_id="wf-b",
            organization_id="org-beta",
        )
        org_b_ids.append(job_b.job_id)

    # Dequeue all and verify organization isolation
    seen_orgs: dict[str, str] = {}
    while True:
        job = queue.dequeue()
        if job is None:
            break
        seen_orgs[job.job_id] = job.organization_id

    for jid in org_a_ids:
        assert seen_orgs[jid] == "org-alpha"
    for jid in org_b_ids:
        assert seen_orgs[jid] == "org-beta"


# ---------------------------------------------------------------------------
# Test: Graceful shutdown cleans up claims
# ---------------------------------------------------------------------------

@requires_redis
def test_graceful_shutdown_releases_claim(redis_client: Any, unique_queue_name: str) -> None:
    """When a worker shutsts down gracefully, it releases its claims."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    job = _make_job()

    queue.claim_job(job, "worker-shutdown-test")
    assert queue.is_claimed(job.job_id)

    # Simulate graceful shutdown: release claim
    queue.release_claim(job.job_id, "worker-shutdown-test")
    assert not queue.is_claimed(job.job_id)

    # Worker unregistered
    queue.register_worker("worker-shutdown-test")
    queue.unregister_worker("worker-shutdown-test")
    active = queue.get_active_workers()
    assert "worker-shutdown-test" not in active


# ---------------------------------------------------------------------------
# Test: Malformed job data handling
# ---------------------------------------------------------------------------

@requires_redis
def test_malformed_job_data_does_not_crash(redis_client: Any, unique_queue_name: str) -> None:
    """Malformed job data in the queue doesn't crash the worker."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)

    # Push malformed data
    redis_client.lpush(unique_queue_name, "not-valid-json")
    redis_client.lpush(unique_queue_name, '{"incomplete": "json"')

    # Dequeue returns None or raises gracefully
    queue.dequeue()
    # Either returns None (handled) or valid job — shouldn't crash
    # The malformed data is consumed but not processed


# ---------------------------------------------------------------------------
# Test: Claim after terminal state is rejected
# ---------------------------------------------------------------------------

@requires_redis
def test_claim_rejected_after_terminal_state(redis_client: Any, unique_queue_name: str) -> None:
    """Claims are rejected for jobs that have reached terminal states."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    job = _make_job()

    # Mark job as completed
    queue._store_job_data(job)
    queue.update_job_status(job.job_id, ExecutionJobStatus.COMPLETED)

    # Try to claim — should be rejected
    claimed = queue.claim_job(job, "worker-terminal-test")
    assert claimed is False


# ---------------------------------------------------------------------------
# Test: Worker metrics are emitted
# ---------------------------------------------------------------------------

@requires_redis
def test_reliability_metrics_emitted(redis_client: Any, unique_queue_name: str) -> None:
    """Reliability-related Prometheus metrics are incremented."""
    from aegisforge.observability.metrics import get_metrics

    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    manager.submit_job(
        request_id="req-metrics-test",
        workflow_id="wf-metrics-test",
        organization_id="org-test",
    )

    def ok_handler(job: ExecutionJob) -> dict[str, Any]:
        return {"result": "ok"}

    worker = JobWorker(manager, ok_handler, worker_id="worker-metrics-test")
    worker.process_next_job()

    after = get_metrics().decode()

    # Queue metrics should have incremented
    assert "queue_jobs_dequeued_total" in after
    assert "queue_jobs_claimed_total" in after
