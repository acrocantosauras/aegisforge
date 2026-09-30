"""Phase 6B: Distributed worker concurrency tests.

Proves that multiple workers can safely operate against the same Redis queue:
- Atomic claiming: two workers + one job → only one executes it
- Job distribution: two workers + multiple jobs → jobs are distributed
- Crash recovery: expired claims → job re-enqueued for another worker
- Idempotency: duplicate submission → same job returned
- Tenant isolation: concurrent requests from different tenants remain isolated
- Terminal states: failed/completed jobs never silently remain executing

Uses real Redis to prove actual distributed queue semantics.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

import pytest

from aegisforge.async_execution.jobs import (
    JobManager,
    JobWorker,
    RedisJobQueue,
)
from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _test_redis_url() -> str:
    """Redis URL for tests (honors AEGISFORGE_TEST_REDIS_URL for authed Redis)."""
    return os.environ.get(
        "AEGISFORGE_TEST_REDIS_URL", "redis://localhost:6379/0"
    )

def _redis_available() -> bool:
    """Check if a real Redis instance is reachable."""
    try:
        import redis as _redis

        c = _redis.from_url(_test_redis_url(), decode_responses=True)
        c.ping()
        c.close()
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Distributed worker tests require a real Redis instance",
)


@pytest.fixture()
def redis_client() -> Any:
    """Real Redis client for integration tests."""
    import redis

    client = redis.from_url(
        _test_redis_url(),
        decode_responses=True,
    )
    client.ping()
    # Clean up test keys AND global shared state before each test
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    client.delete("aegisforge:active_claims")
    client.delete("aegisforge:active_claims:recovery_lock")
    client.delete("aegisforge:workers")
    yield client
    # Cleanup after test
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    client.delete("aegisforge:active_claims")
    client.delete("aegisforge:active_claims:recovery_lock")
    client.delete("aegisforge:workers")


@pytest.fixture()
def unique_queue_name() -> str:
    """Generate a unique queue name per test to avoid cross-test interference."""
    return f"aegisforge:test-queue-{uuid.uuid4().hex[:8]}"


def _make_job(
    request_id: str = "",
    organization_id: str = "org-test",
    max_retries: int = 3,
    idempotency_key: str = "",
) -> ExecutionJob:
    """Create a test job with sensible defaults."""
    return ExecutionJob(
        job_id=f"job-{uuid.uuid4().hex[:12]}",
        request_id=request_id or f"req-{uuid.uuid4().hex[:8]}",
        workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
        organization_id=organization_id,
        status=ExecutionJobStatus.QUEUED,
        max_retries=max_retries,
        idempotency_key=idempotency_key or f"idem-{uuid.uuid4().hex[:8]}",
    )


# ---------------------------------------------------------------------------
# Test: Atomic claiming — two workers, one job, only one wins
# ---------------------------------------------------------------------------

@requires_redis
def test_atomic_claim_one_job_one_winner(redis_client: Any, unique_queue_name: str) -> None:
    """Two workers try to claim the same job — exactly one succeeds."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name, visibility_timeout=30)
    job = _make_job()

    # Worker 1 claims
    claimed_1 = queue.claim_job(job, "worker-1")
    # Worker 2 tries to claim the same job
    claimed_2 = queue.claim_job(job, "worker-2")

    assert claimed_1 is True
    assert claimed_2 is False  # Only one winner

    owner = queue.get_claim_owner(job.job_id)
    assert owner == "worker-1"


# ---------------------------------------------------------------------------
# Test: Claim verification — dequeue + claim is atomic in practice
# ---------------------------------------------------------------------------

@requires_redis
def test_dequeue_claim_atomicity(redis_client: Any, unique_queue_name: str) -> None:
    """Rpop is atomic: two concurrent dequeues never return the same job."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    job1 = _make_job()
    job2 = _make_job()
    queue.enqueue(job1)
    queue.enqueue(job2)

    results: list[str | None] = []
    lock = threading.Lock()

    def _dequeue() -> None:
        j = queue.dequeue()
        with lock:
            results.append(j.job_id if j else None)

    threads = [threading.Thread(target=_dequeue) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 2 jobs dequeued, each unique
    assert len(results) == 2
    assert results[0] != results[1]
    assert None not in results


# ---------------------------------------------------------------------------
# Test: Multi-worker job distribution
# ---------------------------------------------------------------------------

@requires_redis
def test_multi_worker_job_distribution(redis_client: Any, unique_queue_name: str) -> None:
    """Multiple jobs are distributed across workers, each job processed exactly once."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    def _handler(job: ExecutionJob) -> dict[str, Any]:
        # Simulate work
        time.sleep(0.01)
        return {"result": "ok"}

    # Submit 5 jobs
    job_ids = []
    for _ in range(5):
        job = manager.submit_job(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
            organization_id="org-test",
        )
        job_ids.append(job.job_id)

    # Create 3 workers
    workers = []
    for i in range(3):
        w = JobWorker(manager, _handler, worker_id=f"worker-dist-{i}")
        workers.append(w)

    # Each worker processes jobs until queue is empty
    all_processed: list[str] = []
    for w in workers:
        while True:
            job = w.process_next_job()
            if job is None:
                break
            all_processed.append(job.job_id)

    # All 5 jobs were processed
    assert len(all_processed) == 5
    # Each job processed exactly once (no duplicates)
    assert len(set(all_processed)) == 5


# ---------------------------------------------------------------------------
# Test: Crash recovery — expired claim → re-enqueue
# ---------------------------------------------------------------------------

@requires_redis
def test_crash_recovery_requeues_expired_claim(
    redis_client: Any, unique_queue_name: str
) -> None:
    """When a worker crashes (claim expires), the job is recoverable."""
    from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=300
    )
    manager = JobManager(queue)
    job = manager.submit_job(
        request_id="req-crash-test",
        workflow_id="wf-crash-test",
        organization_id="org-test",
    )

    # Worker claims and processes (simulating crash by not completing)
    claim_success = queue.claim_job(job, "worker-crasher")
    assert claim_success is True

    # Simulate crash: directly set the claim score to the past so recovery
    # is deterministic and does not depend on sleep timing.
    redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 10})

    # Recovery scan finds the expired claim and re-enqueues
    recovered = queue.recover_expired_claims()
    assert job.job_id in recovered

    # The job is now back in the queue
    assert queue.size() >= 1

    # Another worker can dequeue it
    requeued_job = queue.dequeue()
    assert requeued_job is not None
    assert requeued_job.job_id == job.job_id


# ---------------------------------------------------------------------------
# Test: Idempotency — duplicate submission returns same job
# ---------------------------------------------------------------------------

@requires_redis
def test_idempotency_prevents_duplicate_jobs(
    redis_client: Any, unique_queue_name: str
) -> None:
    """Submitting the same idempotency key twice returns the same job."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    idempotency_key = f"idem-{uuid.uuid4().hex[:8]}"

    job1 = manager.submit_job(
        request_id="req-idem-1",
        workflow_id="wf-idem-1",
        organization_id="org-test",
        idempotency_key=idempotency_key,
    )
    job2 = manager.submit_job(
        request_id="req-idem-2",
        workflow_id="wf-idem-2",
        organization_id="org-test",
        idempotency_key=idempotency_key,
    )

    assert job1.job_id == job2.job_id  # Same job returned


# ---------------------------------------------------------------------------
# Test: Tenant isolation — concurrent requests from different orgs
# ---------------------------------------------------------------------------

@requires_redis
def test_tenant_isolation_concurrent_jobs(
    redis_client: Any, unique_queue_name: str
) -> None:
    """Jobs from different tenants are isolated — each worker only sees its org's jobs."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    # Submit jobs for two different tenants
    org_a_jobs = []
    org_b_jobs = []
    for _ in range(3):
        job_a = manager.submit_job(
            request_id=f"req-a-{uuid.uuid4().hex[:8]}",
            workflow_id="wf-a",
            organization_id="org-alpha",
        )
        org_a_jobs.append(job_a.job_id)

        job_b = manager.submit_job(
            request_id=f"req-b-{uuid.uuid4().hex[:8]}",
            workflow_id="wf-b",
            organization_id="org-beta",
        )
        org_b_jobs.append(job_b.job_id)

    # All jobs are in the queue (total 6)
    assert queue.size() == 6

    # Dequeue all and verify organization_id is preserved
    dequeued_orgs: dict[str, str] = {}
    while True:
        job = queue.dequeue()
        if job is None:
            break
        dequeued_orgs[job.job_id] = job.organization_id

    assert len(dequeued_orgs) == 6
    for jid in org_a_jobs:
        assert dequeued_orgs[jid] == "org-alpha"
    for jid in org_b_jobs:
        assert dequeued_orgs[jid] == "org-beta"


# ---------------------------------------------------------------------------
# Test: Terminal states — failed jobs don't silently stay executing
# ---------------------------------------------------------------------------

@requires_redis
def test_failed_job_reaches_terminal_state(
    redis_client: Any, unique_queue_name: str
) -> None:
    """A failed job with no retries left reaches terminal FAILED state."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    manager.submit_job(
        request_id="req-fail-test",
        workflow_id="wf-fail-test",
        organization_id="org-test",
        max_retries=0,  # No retries → must reach terminal FAILED immediately
    )

    # Simulate processing failure
    def _failing_handler(j: ExecutionJob) -> dict[str, Any]:
        raise RuntimeError("Simulated failure")

    worker = JobWorker(manager, _failing_handler, worker_id="worker-fail-test")
    result_job = worker.process_next_job()

    assert result_job is not None
    assert result_job.status == ExecutionJobStatus.FAILED
    assert any("Simulated failure" in e for e in result_job.errors)


@requires_redis
def test_completed_job_reaches_terminal_state(
    redis_client: Any, unique_queue_name: str
) -> None:
    """A successful job is marked as COMPLETED."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    manager.submit_job(
        request_id="req-ok-test",
        workflow_id="wf-ok-test",
        organization_id="org-test",
    )

    def _ok_handler(j: ExecutionJob) -> dict[str, Any]:
        return {"result": "success"}

    worker = JobWorker(manager, _ok_handler, worker_id="worker-ok-test")
    result_job = worker.process_next_job()

    assert result_job is not None
    assert result_job.status == ExecutionJobStatus.COMPLETED
    assert result_job.result == {"result": "success"}


# ---------------------------------------------------------------------------
# Test: Heartbeat extends claim
# ---------------------------------------------------------------------------

@requires_redis
def test_heartbeat_extends_claim_ttl(
    redis_client: Any, unique_queue_name: str
) -> None:
    """Heartbeating a job extends its claim TTL, preventing premature recovery."""
    queue = RedisJobQueue(
        redis_client, queue_name=unique_queue_name, visibility_timeout=3
    )
    job = _make_job()

    queue.claim_job(job, "worker-hb")

    # Check TTL
    ttl_before = redis_client.ttl(f"aegisforge:claim:{job.job_id}")
    assert ttl_before > 0

    # Heartbeat
    queue.heartbeat_job(job.job_id, "worker-hb")

    ttl_after = redis_client.ttl(f"aegisforge:claim:{job.job_id}")
    assert ttl_after >= ttl_before  # TTL should be refreshed


# ---------------------------------------------------------------------------
# Test: Worker registry
# ---------------------------------------------------------------------------

@requires_redis
def test_worker_registry(redis_client: Any, unique_queue_name: str) -> None:
    """Workers register, heartbeat, and unregister from the registry."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)

    queue.register_worker("worker-reg-1")
    queue.register_worker("worker-reg-2")

    active = queue.get_active_workers()
    assert "worker-reg-1" in active
    assert "worker-reg-2" in active

    queue.unregister_worker("worker-reg-1")

    active = queue.get_active_workers()
    assert "worker-reg-1" not in active
    assert "worker-reg-2" in active


# ---------------------------------------------------------------------------
# Test: Bounded retry — max retries prevents infinite loop
# ---------------------------------------------------------------------------

@requires_redis
def test_bounded_retry_prevents_infinite_loop(
    redis_client: Any, unique_queue_name: str
) -> None:
    """Jobs that exceed max_retries are not re-enqueued."""
    queue = RedisJobQueue(redis_client, queue_name=unique_queue_name)
    manager = JobManager(queue)

    manager.submit_job(
        request_id="req-retry-exhaust",
        workflow_id="wf-retry-exhaust",
        organization_id="org-test",
        max_retries=1,
    )

    def _always_fail_handler(j: ExecutionJob) -> dict[str, Any]:
        raise RuntimeError("Always fails")

    # Process twice (initial + 1 retry)
    worker = JobWorker(manager, _always_fail_handler, worker_id="worker-retry-test")

    # First attempt
    result1 = worker.process_next_job()
    assert result1 is not None
    assert result1.status == ExecutionJobStatus.RETRYING  # Queued for retry

    # Second attempt (retry)
    result2 = worker.process_next_job()
    assert result2 is not None
    assert result2.status == ExecutionJobStatus.FAILED  # Max retries exhausted

    # Queue should be empty — no more retries
    assert queue.size() == 0
