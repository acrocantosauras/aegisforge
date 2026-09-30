"""Phase 6G — Distributed worker correctness hardening tests.

Deterministic regression tests for the races audited in Workstream 2 and the
failure injections of Workstream 11.  Every test simulates the race by
directly manipulating Redis state (claim keys, sorted-set scores, job data)
rather than by sleeping — timing races cannot be reproduced reliably by
wall-clock sleeps.

Races covered (labels match the audit report):

A) dequeue → claim race          — two workers claim one job (already covered
                                    by 6B tests; here with the Lua script).
C) recovery → claim race         — recovery and a fresh claim race on one job.
D) heartbeat → expiration race   — a slow worker cannot extend another
                                    worker's claim (atomic compare-and-expire).
E) retry → idempotency race      — retry state is persisted BEFORE enqueue.
F) worker crash while executing  — claim expires, job recovered once.
G) duplicate delivery            — two queue copies of one job → one execution.
H) terminal job claimed again    — completed/failed/cancelled jobs rejected.
I) stale worker registrations    — utilization snapshot excludes dead workers.
J) Redis restart                 — queue operations fail safely (fail-fast
                                    errors, no crashes, no corrupted state).
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


def _test_redis_url() -> str:
    """Redis URL for tests (honors AEGISFORGE_TEST_REDIS_URL for authed Redis)."""
    return os.environ.get(
        "AEGISFORGE_TEST_REDIS_URL", "redis://localhost:6379/0"
    )

def _redis_available() -> bool:
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
    reason="Worker hardening tests require a real Redis instance",
)


@pytest.fixture()
def redis_client() -> Any:
    import redis

    client = redis.from_url(_test_redis_url(), decode_responses=True)
    client.ping()
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    for key in (
        "aegisforge:active_claims",
        "aegisforge:active_claims:recovery_lock",
        "aegisforge:workers",
        "aegisforge:worker_state",
        "aegisforge:worker_active",
        "aegisforge:stuck_job_recovery_counts",
    ):
        client.delete(key)
    yield client
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    for key in (
        "aegisforge:active_claims",
        "aegisforge:active_claims:recovery_lock",
        "aegisforge:workers",
        "aegisforge:worker_state",
        "aegisforge:worker_active",
        "aegisforge:stuck_job_recovery_counts",
    ):
        client.delete(key)


@pytest.fixture()
def queue_name() -> str:
    return f"aegisforge:test-hard-{uuid.uuid4().hex[:8]}"


def _make_job(
    *,
    job_id: str | None = None,
    status: ExecutionJobStatus = ExecutionJobStatus.QUEUED,
    retry_count: int = 0,
    max_retries: int = 3,
) -> ExecutionJob:
    suffix = uuid.uuid4().hex[:10]
    return ExecutionJob(
        job_id=job_id or f"job-{suffix}",
        request_id=f"req-{suffix}",
        workflow_id=f"wf-{suffix}",
        organization_id="org-test",
        status=status,
        retry_count=retry_count,
        max_retries=max_retries,
        idempotency_key=f"idem-{suffix}",
        submitted_at=time.time(),
    )


# ---------------------------------------------------------------------------
# A/B: claim atomicity with the Lua script
# ---------------------------------------------------------------------------


@requires_redis
class TestClaimAtomicity:
    def test_two_workers_one_job_exactly_one_claim(self, redis_client: Any, queue_name: str) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()
        assert queue.claim_job(job, "worker-1") is True
        assert queue.claim_job(job, "worker-2") is False
        assert queue.get_claim_owner(job.job_id) == "worker-1"

    def test_concurrent_claim_attempts_single_winner(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Stress: 8 threads race to claim the same job — exactly 1 wins."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()
        winners: list[str] = []
        lock = threading.Lock()
        start = threading.Event()

        def try_claim(worker_id: str) -> None:
            start.wait()
            if queue.claim_job(job, worker_id):
                with lock:
                    winners.append(worker_id)

        threads = [
            threading.Thread(target=try_claim, args=(f"w-{i}",)) for i in range(8)
        ]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)
        assert len(winners) == 1

    def test_capacity_blocked_claim_rejected(self, redis_client: Any, queue_name: str) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job1 = _make_job()
        job2 = _make_job()
        assert queue.claim_job(job1, "w-cap", capacity=1) is True
        # Worker at capacity cannot claim another job.
        assert queue.claim_job(job2, "w-cap", capacity=1) is False
        # Another worker with capacity can.
        assert queue.claim_job(job2, "w-other", capacity=1) is True

    def test_capacity_slot_released_after_job(
        self, redis_client: Any, queue_name: str
    ) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()
        queue.claim_job(job, "w-cap", capacity=1)
        assert queue.get_worker_active_jobs("w-cap") == 1
        queue.release_worker_slot("w-cap")
        assert queue.get_worker_active_jobs("w-cap") == 0
        # Slot freed: the same worker can claim again.
        assert queue.claim_job(job, "w-cap", capacity=1) is False  # claim key still exists
        redis_client.delete(f"aegisforge:claim:{job.job_id}")
        assert queue.claim_job(job, "w-cap", capacity=1) is True


# ---------------------------------------------------------------------------
# D: heartbeat → expiration race (atomic compare-and-expire)
# ---------------------------------------------------------------------------


@requires_redis
class TestHeartbeatOwnership:
    def test_heartbeat_cannot_extend_foreign_claim(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Worker A's claim expires and worker B re-claims; A's late heartbeat
        must NOT extend B's claim (atomic compare-and-expire)."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()
        assert queue.claim_job(job, "worker-a") is True
        # Simulate expiration + re-claim by worker-b.
        redis_client.delete(f"aegisforge:claim:{job.job_id}")
        assert queue.claim_job(job, "worker-b") is True
        # Worker-a's heartbeat arrives late.
        extended = queue.heartbeat_job(job.job_id, "worker-a")
        assert extended is False
        # B's claim intact.
        assert queue.get_claim_owner(job.job_id) == "worker-b"

    def test_release_cannot_delete_foreign_claim(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """A slow worker completing late must not delete the new owner's claim."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()
        assert queue.claim_job(job, "worker-a") is True
        redis_client.delete(f"aegisforge:claim:{job.job_id}")
        assert queue.claim_job(job, "worker-b") is True

        queue.release_claim(job.job_id, "worker-a")
        # B still owns the claim — A's release was a no-op.
        assert queue.get_claim_owner(job.job_id) == "worker-b"

        queue.release_claim(job.job_id, "worker-b")
        assert queue.get_claim_owner(job.job_id) is None


# ---------------------------------------------------------------------------
# E: retry → idempotency/status race (persist before enqueue)
# ---------------------------------------------------------------------------


@requires_redis
class TestRetryOrdering:
    def test_retry_state_persisted_before_enqueue(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """The retry copy in the queue must carry RETRYING, not stale FAILED."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-retry-order",
            workflow_id="wf-retry-order",
            organization_id="org-test",
            max_retries=3,
        )

        def failing_handler(j: ExecutionJob) -> dict[str, Any]:
            raise RuntimeError("boom")

        worker = JobWorker(manager, failing_handler, worker_id="w-retry")
        result = worker.process_next_job()
        assert result is not None
        assert result.status == ExecutionJobStatus.RETRYING

        # The queued copy must already reflect RETRYING + incremented count.
        queued_raw = redis_client.lindex(queue_name, -1)  # oldest entry (FIFO)
        assert queued_raw is not None
        from aegisforge.domain.models import ExecutionJob as Job

        queued_job = Job.model_validate_json(queued_raw)
        assert queued_job.job_id == job.job_id
        assert queued_job.status == ExecutionJobStatus.RETRYING
        # Redis job data (authoritative) matches the queued copy.
        stored = queue.get_job_data(job.job_id)
        assert stored is not None
        assert stored.status == ExecutionJobStatus.RETRYING
        assert stored.retry_count == 1

    def test_recovered_job_state_persisted_before_enqueue(
        self, redis_client: Any, queue_name: str
    ) -> None:
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

        queue = RedisJobQueue(
            redis_client, queue_name=queue_name, visibility_timeout=300
        )
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-rec-order",
            workflow_id="wf-rec-order",
            organization_id="org-test",
        )
        # Real worker flow: dequeue THEN claim (crash after claim).
        dequeued = queue.dequeue()
        assert dequeued is not None
        queue.claim_job(dequeued, "w-crash")
        # Force the claim to look expired.
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 10})

        recovered = queue.recover_expired_claims()
        assert job.job_id in recovered

        queued_raw = redis_client.lindex(queue_name, -1)
        assert queued_raw is not None
        queued_job = ExecutionJob.model_validate_json(queued_raw)
        assert queued_job.status == ExecutionJobStatus.RETRYING
        assert queued_job.retry_count == 1
        # Authoritative Redis state matches the queued copy.
        stored = queue.get_job_data(job.job_id)
        assert stored is not None
        assert stored.status == ExecutionJobStatus.RETRYING


# ---------------------------------------------------------------------------
# F/C: crash recovery + recovery/claim race
# ---------------------------------------------------------------------------


@requires_redis
class TestRecoveryRaces:
    def test_concurrent_recovery_single_requeue(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Two workers scan simultaneously — the job must be re-enqueued once."""
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

        queue = RedisJobQueue(
            redis_client, queue_name=queue_name, visibility_timeout=300
        )
        job = _make_job()
        queue.enqueue(job)
        # Real worker flow: dequeue THEN claim (crash after claim) so the
        # queue is empty at crash time.
        dequeued = queue.dequeue()
        assert dequeued is not None
        queue.claim_job(dequeued, "w-crash")
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 10})

        results: list[list[str]] = []
        lock = threading.Lock()
        start = threading.Event()

        def scan() -> None:
            start.wait()
            recovered = queue.recover_expired_claims()
            with lock:
                results.append(recovered)

        threads = [threading.Thread(target=scan) for _ in range(4)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)

        total_recovered = sum(len(r) for r in results)
        assert total_recovered == 1
        assert queue.size() == 1  # exactly one copy in the queue

    def test_recovery_does_not_clobber_a_foreign_lock(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """A scan that cannot acquire the lock must leave the holder's lock
        intact — it must never delete another scanner's lock."""
        lock_key = "aegisforge:active_claims:recovery_lock"
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        redis_client.set(lock_key, "other-scanner-token", ex=30)
        assert queue.recover_expired_claims() == []
        # Foreign lock untouched (and unexpired).
        assert redis_client.get(lock_key) == "other-scanner-token"
        assert redis_client.ttl(lock_key) > 0

    def test_terminal_job_not_recovered(
        self, redis_client: Any, queue_name: str
    ) -> None:
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        job = _make_job(status=ExecutionJobStatus.COMPLETED)
        queue._store_job_data(job)
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 10})
        recovered = queue.recover_expired_claims()
        assert recovered == []
        assert queue.size() == 0

    def test_max_retries_exhausted_marks_failed(
        self, redis_client: Any, queue_name: str
    ) -> None:
        from aegisforge.async_execution.jobs import _ACTIVE_CLAIMS_SET

        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        job = _make_job(status=ExecutionJobStatus.RUNNING, retry_count=3, max_retries=3)
        queue._store_job_data(job)
        redis_client.zadd(_ACTIVE_CLAIMS_SET, {job.job_id: time.time() - 10})
        recovered = queue.recover_expired_claims()
        assert recovered == []
        stored = queue.get_job_data(job.job_id)
        assert stored is not None
        assert stored.status == ExecutionJobStatus.FAILED


# ---------------------------------------------------------------------------
# G/H: duplicate delivery + terminal claims
# ---------------------------------------------------------------------------


@requires_redis
class TestDuplicateAndTerminal:
    def test_duplicate_delivery_executes_once(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Two queue copies of the same job → exactly one execution."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-dup",
            workflow_id="wf-dup",
            organization_id="org-test",
        )
        # Simulate duplicate delivery (e.g. recovery + retry raced).
        queue.enqueue(job)
        assert queue.size() == 2

        executions: list[str] = []

        def handler(j: ExecutionJob) -> dict[str, Any]:
            executions.append(j.job_id)
            return {"ok": True}

        worker = JobWorker(manager, handler, worker_id="w-dup")
        processed = worker.process_all(max_jobs=10)
        # First copy executes; the second is rejected by the terminal guard.
        assert len(executions) == 1
        assert processed[0].job_id == job.job_id
        assert processed[0].status == ExecutionJobStatus.COMPLETED

    def test_terminal_job_claim_rejected(
        self, redis_client: Any, queue_name: str
    ) -> None:
        for status in (
            ExecutionJobStatus.COMPLETED,
            ExecutionJobStatus.FAILED,
            ExecutionJobStatus.CANCELLED,
        ):
            job = _make_job(status=status)
            queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
            queue._store_job_data(job)
            assert queue.claim_job(job, "w-zombie") is False, f"status={status}"

    def test_cancelled_job_not_executed(
        self, redis_client: Any, queue_name: str
    ) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-cancel",
            workflow_id="wf-cancel",
            organization_id="org-test",
        )
        manager.cancel_job(job.job_id)
        # Job data now says CANCELLED; a worker dequeuing a stale QUEUED copy
        # must not execute it.
        stale_copy = _make_job(job_id=job.job_id)  # same id, QUEUED status
        queue.enqueue(stale_copy)

        executions: list[str] = []

        def handler(j: ExecutionJob) -> dict[str, Any]:
            executions.append(j.job_id)
            return {"ok": True}

        worker = JobWorker(manager, handler, worker_id="w-cancel")
        worker.process_all(max_jobs=10)
        assert executions == []


# ---------------------------------------------------------------------------
# I/J: stale workers + Redis failure injection
# ---------------------------------------------------------------------------


@requires_redis
class TestWorkerRegistryAndFailures:
    def test_stale_worker_excluded_from_utilization(
        self, redis_client: Any, queue_name: str
    ) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        queue.register_worker("w-live")
        queue.set_worker_capacity("w-live", 4)
        # w-dead: advertise capacity first (which registers a fresh heartbeat),
        # THEN let the heartbeat go stale.
        queue.set_worker_capacity("w-dead", 4)
        import time as _t

        redis_client.zadd("aegisforge:workers", {"w-dead": _t.time() - 10_000})

        utilization = queue.list_worker_utilization()
        ids = [u["worker_id"] for u in utilization]
        assert "w-live" in ids
        assert "w-dead" not in ids
        live = next(u for u in utilization if u["worker_id"] == "w-live")
        assert live["capacity"] == 4
        assert live["available"] == 4

    def test_graceful_shutdown_cleans_worker_state(
        self, redis_client: Any, queue_name: str
    ) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        manager = JobManager(queue)
        worker = JobWorker(manager, lambda j: {"ok": True}, worker_id="w-shutdown")
        worker.register_in_registry()
        assert queue.get_worker_capacity("w-shutdown") == 1
        worker.unregister_from_registry()
        assert queue.get_worker_capacity("w-shutdown") == 0
        assert "w-shutdown" not in queue.get_active_workers()

    def test_redis_unavailable_fails_safely(self, redis_client: Any, queue_name: str) -> None:
        """Failure injection J: kill the connection — operations fail fast,
        return sane defaults, and never raise out of the queue API."""
        RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()

        class BrokenClient:
            """Simulates Redis being unreachable (connection refused)."""

            def __getattr__(self, name: str) -> Any:
                def _fail(*args: Any, **kwargs: Any) -> Any:
                    raise ConnectionError("Redis is down")

                return _fail

            def register_script(self, script: str) -> Any:
                def _fail(*args: Any, **kwargs: Any) -> Any:
                    raise ConnectionError("Redis is down")

                return _fail

        broken_queue = RedisJobQueue(BrokenClient(), queue_name=queue_name)
        # All queue operations degrade to safe outcomes (no exceptions).
        assert broken_queue.enqueue(job) is False
        assert broken_queue.dequeue() is None
        assert broken_queue.size() == 0
        assert broken_queue.claim_job(job, "w") is False
        assert broken_queue.heartbeat_job(job.job_id, "w") is False
        assert broken_queue.recover_expired_claims() == []
        assert broken_queue.get_active_workers() == []

    def test_job_data_ttl_bounds_redis_state(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Bounded Redis state: job data has a TTL (no unbounded key growth)."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=30)
        job = _make_job()
        queue._store_job_data(job)
        ttl = redis_client.ttl(f"aegisforge:job:{job.job_id}")
        assert ttl > 0  # bounded lifetime


@requires_redis
class TestWorkerCapacityEndToEnd:
    def test_capacity_aware_worker_loop(self, redis_client: Any, queue_name: str) -> None:
        """A capacity-1 worker claims and releases exactly one slot per job."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        for i in range(3):
            manager.submit_job(
                request_id=f"req-cap-{i}",
                workflow_id=f"wf-cap-{i}",
                organization_id="org-test",
            )

        worker = JobWorker(
            manager, lambda j: {"ok": True}, worker_id="w-cap-e2e", capacity=1
        )
        worker.register_in_registry()
        assert queue.get_worker_capacity("w-cap-e2e") == 1

        processed = worker.process_all(max_jobs=5)
        assert len(processed) == 3
        # All slots released after processing.
        assert queue.get_worker_active_jobs("w-cap-e2e") == 0

        utilization = queue.list_worker_utilization()
        entry = next(u for u in utilization if u["worker_id"] == "w-cap-e2e")
        assert entry["capacity"] == 1
        assert entry["active"] == 0
        assert entry["available"] == 1

        worker.unregister_from_registry()


@requires_redis
class TestIdempotencyConcurrency:
    def test_concurrent_submissions_single_logical_job(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Task 4: N simultaneous submissions with one idempotency key must
        yield ONE logical job and exactly ONE queue entry.  Regression against
        the previous check-then-create race where concurrent submitters both
        observed the ``pending`` marker and each enqueued a job."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        key = f"idem-conc-{uuid.uuid4().hex[:10]}"

        results: list[ExecutionJob] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        start = threading.Event()

        def submit() -> None:
            start.wait()
            try:
                job = manager.submit_job(
                    request_id=f"req-{uuid.uuid4().hex[:6]}",
                    workflow_id="wf-conc",
                    organization_id="org-test",
                    idempotency_key=key,
                )
                with lock:
                    results.append(job)
            except BaseException as exc:  # surfaced in assertion
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(10)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert len(results) == 10
        # One logical job identity across all submitter views.
        assert len({j.job_id for j in results}) == 1
        # Exactly one queue entry and one authoritative job-data record.
        assert queue.size() == 1
        winner = results[0].job_id
        stored = queue.get_job_data(winner)
        assert stored is not None
        assert stored.idempotency_key == key
        # The mapping points at the winner, never a stale 'pending' marker.
        assert queue.check_idempotency(key) == winner

    def test_atomic_enqueue_binds_key_and_queue_together(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """The idempotency binding and the queue entry appear atomically: a
        bound key always has its job queued (no bound-but-not-queued window)."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        key = f"idem-atomic-{uuid.uuid4().hex[:10]}"
        job = manager.submit_job(
            request_id="req-atomic",
            workflow_id="wf-atomic",
            organization_id="org-test",
            idempotency_key=key,
        )
        assert queue.check_idempotency(key) == job.job_id
        assert queue.size() == 1
        assert queue.get_job_data(job.job_id) is not None
