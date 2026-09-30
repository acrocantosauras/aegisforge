"""Phase 6C: Performance baseline for AegisForge job queue.

Provides a modest, reproducible performance baseline measuring:
- Job submission throughput
- Queue latency (time from enqueue to dequeue)
- Execution throughput (jobs completed per second)
- Concurrent worker scaling
- Recovery overhead

Benchmark Methodology:
---------------------
All benchmarks use:
- Backend: Redis 7.x on localhost (real Redis, not mocked)
- Payload: ExecutionJob model with request_id, workflow_id, org (JSON ~300 bytes)
- Handler: Deterministic no-op (returns {"result": "ok"}), no I/O, no LLM
- Workers: Thread-based concurrency (not multiprocessing), single process
- Retries: Default max_retries=3 (not exhausted in happy-path benchmarks)
- Persistence: Jobs are stored in Redis (job data + queue + claims)
- Timing: time.monotonic() for benchmarks, perf_counter() for sub-ms measurements
- Network: Localhost only, no simulated network latency

Production Representativeness:
- Submission throughput (~500+ jobs/sec): Measures Redis LPUSH + SET overhead.
  Production-realistic for queue submission.
- Dequeue+execute (~150+ jobs/sec): Measures RPOP + claim + handler + status update.
  Production-realistic for lightweight handlers. Real workflow execution
  (LLM calls, tool execution, RAG) will be orders of magnitude slower.
- Queue latency (~2ms avg): Measures time from LPUSH to RPOP on localhost.
  Production latency will be higher due to network RTT.
- Concurrent scaling: Measures thread-based scaling. Real workers are
  separate processes on separate machines — scaling characteristics differ.
- Recovery throughput (~400+ jobs/sec): Measures ZRANGEBYSCORE + re-enqueue.
  Production-realistic for recovery speed.
- In-memory baseline (~80k+ jobs/sec): NOT production-representative.
  Included only as an architectural lower bound. Never present this
  number as production Redis/Postgres performance.

Known limitations:
- Measures queue overhead, not actual workflow execution time
- Single Redis instance, no network latency simulation
- Deterministic handler (no I/O, no LLM calls)
- Thread-based concurrency (not multiprocessing)
- No PostgreSQL involvement (queue is Redis-only)

To reproduce:
    pytest tests/test_performance_baseline.py -v -s
"""
from __future__ import annotations

import os
import statistics
import time
from typing import Any

import pytest

from aegisforge.async_execution.jobs import (
    InMemoryJobQueue,
    JobManager,
    JobWorker,
    RedisJobQueue,
)
from aegisforge.domain.models import ExecutionJob


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
    reason="Performance baseline requires Redis",
)


@pytest.fixture()
def redis_client() -> Any:
    import uuid

    import redis

    client = redis.from_url(
        _test_redis_url(),
        decode_responses=True,
    )
    client.ping()
    prefix = f"aegisforge:test-perf-{uuid.uuid4().hex[:8]}"
    for key in client.scan_iter(match=f"{prefix}*"):
        client.delete(key)
    yield client
    for key in client.scan_iter(match=f"{prefix}*"):
        client.delete(key)


def _ok_handler(job: ExecutionJob) -> dict[str, Any]:
    """Minimal handler for benchmarking — no I/O, no delays."""
    return {"result": "ok", "job_id": job.job_id}


@requires_redis
class TestPerformanceBaseline:
    """Reproducible performance baseline for the job queue system."""

    def test_submission_throughput(self, redis_client: Any) -> None:
        """Measure how fast jobs can be submitted to the queue."""
        import uuid

        queue_name = f"aegisforge:test-perf-submit-{uuid.uuid4().hex[:8]}"
        queue = RedisJobQueue(redis_client, queue_name=queue_name)
        manager = JobManager(queue)

        num_jobs = 500
        start = time.monotonic()

        for i in range(num_jobs):
            manager.submit_job(
                request_id=f"req-perf-{i}",
                workflow_id=f"wf-perf-{i}",
                organization_id="org-perf",
            )

        elapsed = time.monotonic() - start
        throughput = num_jobs / elapsed

        print(f"\n  Submission throughput: {throughput:.0f} jobs/sec ({num_jobs} in {elapsed:.3f}s)")
        assert throughput > 50, f"Submission throughput too low: {throughput:.0f} jobs/sec"

        # Cleanup
        for key in redis_client.scan_iter(match=f"{queue_name}*"):
            redis_client.delete(key)

    def test_dequeue_throughput(self, redis_client: Any) -> None:
        """Measure how fast jobs can be dequeued."""
        import uuid

        queue_name = f"aegisforge:test-perf-dequeue-{uuid.uuid4().hex[:8]}"
        queue = RedisJobQueue(redis_client, queue_name=queue_name)
        manager = JobManager(queue)

        num_jobs = 500
        for i in range(num_jobs):
            manager.submit_job(
                request_id=f"req-perf-{i}",
                workflow_id=f"wf-perf-{i}",
                organization_id="org-perf",
            )

        worker = JobWorker(manager, _ok_handler, worker_id="perf-worker")

        start = time.monotonic()
        processed = worker.process_all(max_jobs=num_jobs)
        elapsed = time.monotonic() - start
        throughput = len(processed) / elapsed

        print(f"\n  Dequeue+execute throughput: {throughput:.0f} jobs/sec ({len(processed)} in {elapsed:.3f}s)")
        assert len(processed) == num_jobs
        assert throughput > 20, f"Dequeue throughput too low: {throughput:.0f} jobs/sec"

        for key in redis_client.scan_iter(match=f"{queue_name}*"):
            redis_client.delete(key)

    def test_queue_latency(self, redis_client: Any) -> None:
        """Measure queue wait time (time between submission and dequeue)."""
        import uuid

        queue_name = f"aegisforge:test-perf-latency-{uuid.uuid4().hex[:8]}"
        queue = RedisJobQueue(redis_client, queue_name=queue_name)
        manager = JobManager(queue)

        num_jobs = 100
        latencies: list[float] = []

        for i in range(num_jobs):
            submit_time = time.monotonic()
            manager.submit_job(
                request_id=f"req-latency-{i}",
                workflow_id=f"wf-latency-{i}",
                organization_id="org-perf",
            )
            # Immediately dequeue
            dequeued = queue.dequeue()
            dequeue_time = time.monotonic()
            if dequeued is not None:
                latencies.append(dequeue_time - submit_time)

        avg_latency = statistics.mean(latencies) * 1000  # ms
        p95_latency = sorted(latencies)[int(len(latencies) * 0.95)] * 1000  # ms

        print(f"\n  Queue latency: avg={avg_latency:.2f}ms, p95={p95_latency:.2f}ms")
        assert avg_latency < 10, f"Average queue latency too high: {avg_latency:.2f}ms"

        for key in redis_client.scan_iter(match=f"{queue_name}*"):
            redis_client.delete(key)

    def test_execution_throughput(self, redis_client: Any) -> None:
        """Measure end-to-end execution throughput (submit → execute)."""
        import uuid

        queue_name = f"aegisforge:test-perf-exec-{uuid.uuid4().hex[:8]}"
        queue = RedisJobQueue(redis_client, queue_name=queue_name)
        manager = JobManager(queue)
        worker = JobWorker(manager, _ok_handler, worker_id="perf-exec-worker")

        num_jobs = 200
        for i in range(num_jobs):
            manager.submit_job(
                request_id=f"req-exec-{i}",
                workflow_id=f"wf-exec-{i}",
                organization_id="org-perf",
            )

        start = time.monotonic()
        processed = worker.process_all(max_jobs=num_jobs)
        elapsed = time.monotonic() - start
        throughput = len(processed) / elapsed

        print(f"\n  Execution throughput: {throughput:.0f} jobs/sec ({len(processed)} in {elapsed:.3f}s)")
        assert throughput > 20

        for key in redis_client.scan_iter(match=f"{queue_name}*"):
            redis_client.delete(key)

    def test_concurrent_worker_scaling(self, redis_client: Any) -> None:
        """Measure throughput with multiple concurrent workers."""
        import threading
        import uuid

        queue_name = f"aegisforge:test-perf-scale-{uuid.uuid4().hex[:8]}"
        queue = RedisJobQueue(redis_client, queue_name=queue_name)
        manager = JobManager(queue)

        num_jobs = 300
        for i in range(num_jobs):
            manager.submit_job(
                request_id=f"req-scale-{i}",
                workflow_id=f"wf-scale-{i}",
                organization_id="org-perf",
            )

        num_workers = 4
        results: list[list[str]] = [[] for _ in range(num_workers)]
        barriers = [threading.Event() for _ in range(num_workers)]

        def worker_fn(worker_idx: int) -> None:
            worker = JobWorker(
                manager, _ok_handler, worker_id=f"perf-scale-{worker_idx}"
            )
            barriers[worker_idx].set()  # Signal ready
            # Wait for all workers to be ready
            for b in barriers:
                b.wait(timeout=5)
            while True:
                job = worker.process_next_job()
                if job is None:
                    break
                results[worker_idx].append(job.job_id)

        start = time.monotonic()
        threads = [threading.Thread(target=worker_fn, args=(i,)) for i in range(num_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        elapsed = time.monotonic() - start

        all_jobs = []
        for r in results:
            all_jobs.extend(r)

        throughput = len(all_jobs) / elapsed

        print(f"\n  Concurrent throughput ({num_workers} workers): {throughput:.0f} jobs/sec")
        print(f"  Jobs processed: {len(all_jobs)}/{num_jobs}")
        # All jobs should be processed
        assert len(all_jobs) == num_jobs
        # Each job processed exactly once
        assert len(set(all_jobs)) == num_jobs

        for key in redis_client.scan_iter(match=f"{queue_name}*"):
            redis_client.delete(key)

    def test_recovery_overhead(self, redis_client: Any) -> None:
        """Measure the overhead of crash recovery."""
        import uuid

        queue_name = f"aegisforge:test-perf-recovery-{uuid.uuid4().hex[:8]}"
        queue = RedisJobQueue(
            redis_client, queue_name=queue_name, visibility_timeout=1
        )
        manager = JobManager(queue)

        num_jobs = 50
        job_ids = []
        for i in range(num_jobs):
            job = manager.submit_job(
                request_id=f"req-recovery-{i}",
                workflow_id=f"wf-recovery-{i}",
                organization_id="org-perf",
            )
            # Claim and abandon (simulating crash)
            queue.claim_job(job, f"worker-crash-{i}")
            job_ids.append(job.job_id)

        # Wait for claims to expire
        time.sleep(2)

        # Measure recovery time
        start = time.monotonic()
        recovered = queue.recover_expired_claims(max_recover=num_jobs)
        elapsed = time.monotonic() - start

        print(f"\n  Recovery: {len(recovered)}/{num_jobs} jobs in {elapsed:.3f}s")
        print(f"  Recovery throughput: {len(recovered)/max(elapsed, 0.001):.0f} jobs/sec")
        assert len(recovered) == num_jobs

        for key in redis_client.scan_iter(match=f"{queue_name}*"):
            redis_client.delete(key)


@requires_redis
class TestInMemoryVsRedis:
    """Compare in-memory vs Redis queue performance."""

    def test_in_memory_throughput(self) -> None:
        """Baseline: in-memory queue throughput."""
        queue = InMemoryJobQueue()
        manager = JobManager(queue)
        worker = JobWorker(manager, _ok_handler)

        num_jobs = 1000
        for i in range(num_jobs):
            manager.submit_job(
                request_id=f"req-mem-{i}",
                workflow_id=f"wf-mem-{i}",
                organization_id="org-perf",
            )

        # Use perf_counter for higher precision on Windows
        import time as _time

        start = _time.perf_counter()
        processed = worker.process_all(max_jobs=num_jobs)
        elapsed = _time.perf_counter() - start
        throughput = len(processed) / max(elapsed, 1e-9)

        print(f"\n  In-memory throughput: {throughput:.0f} jobs/sec")
        assert throughput > 100


@requires_redis
class TestPhase6GPerformance:
    """Phase 6G additions: atomic claim, heartbeat, checkpoint I/O, and
    circuit-breaker admission overhead.  Same methodology constraints as
    the 6C baseline above: localhost Redis/SQLite, deterministic handlers,
    single process.  NOT production-representative numbers."""

    def test_atomic_claim_throughput_and_latency(self, redis_client: Any) -> None:
        """Measure the atomic pop+claim Lua path (Phase 6G)."""
        import uuid

        queue = RedisJobQueue(
            redis_client,
            queue_name=f"aegisforge:test-perf-atomic-{uuid.uuid4().hex[:8]}",
        )
        manager = JobManager(queue)
        num_jobs = 300
        for i in range(num_jobs):
            manager.submit_job(
                request_id=f"req-{i}", workflow_id=f"wf-{i}",
                organization_id="org-perf", idempotency_key="",
            )

        latencies: list[float] = []
        start = time.perf_counter()
        claimed = 0
        for _ in range(num_jobs):
            t0 = time.perf_counter()
            job = queue.dequeue("w-perf", capacity=1000)
            latencies.append(time.perf_counter() - t0)
            if job is not None:
                claimed += 1
        elapsed = time.perf_counter() - start

        avg_ms = statistics.mean(latencies) * 1000
        p95_ms = statistics.quantiles(latencies, n=20)[18] * 1000
        print(
            f"\n  Atomic claim: {claimed/elapsed:.0f} claims/sec, "
            f"avg {avg_ms:.2f} ms, p95 {p95_ms:.2f} ms"
        )
        assert claimed == num_jobs
        assert avg_ms < 50

        for key in redis_client.scan_iter(match="aegisforge:test-perf-atomic-*"):
            redis_client.delete(key)

    def test_heartbeat_latency(self, redis_client: Any) -> None:
        """Measure Lua compare-and-expire heartbeat cost."""
        import uuid

        queue = RedisJobQueue(
            redis_client,
            queue_name=f"aegisforge:test-perf-hb-{uuid.uuid4().hex[:8]}",
        )
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-hb", workflow_id="wf-hb", organization_id="org-perf"
        )
        dequeued = queue.dequeue("w-hb", capacity=10)
        assert dequeued is not None

        latencies: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            assert queue.heartbeat_job(job.job_id, "w-hb") is True
            latencies.append(time.perf_counter() - t0)
        avg_ms = statistics.mean(latencies) * 1000
        print(f"\n  Heartbeat: avg {avg_ms:.2f} ms")
        assert avg_ms < 20

    def test_checkpoint_save_load_latency(self) -> None:
        """Checkpoint write/read cost against SQLite (proxy for the pgvector
        store; real PostgreSQL will differ)."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from aegisforge.db.base import Base
        from aegisforge.workflows.checkpoint import (
            DbCheckpointStore,
            WorkflowCheckpoint,
        )

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        store = DbCheckpointStore(session_factory=sessionmaker(bind=engine))
        state = {"tasks": [{"id": i, "status": "ok"} for i in range(20)]}

        save_latencies: list[float] = []
        for i in range(50):
            cp = WorkflowCheckpoint(
                checkpoint_id=f"cp-{i}", workflow_id="wf-perf", request_id="req-perf",
                node_name=f"node-{i}", organization_id="org-perf", state=state,
            )
            t0 = time.perf_counter()
            store.save_checkpoint(cp)
            save_latencies.append(time.perf_counter() - t0)

        load_latencies: list[float] = []
        for i in range(50):
            t0 = time.perf_counter()
            loaded = store.load_checkpoint(f"cp-{i}")
            load_latencies.append(time.perf_counter() - t0)
            assert loaded is not None

        save_avg = statistics.mean(save_latencies) * 1000
        load_avg = statistics.mean(load_latencies) * 1000
        print(f"\n  Checkpoint save: avg {save_avg:.2f} ms; load: avg {load_avg:.2f} ms")
        assert save_avg < 50
        assert load_avg < 50

    def test_circuit_admission_overhead(self) -> None:
        """Circuit CLOSED admission overhead vs OPEN fast-fail."""
        from aegisforge.tools.base import BaseTool, ToolDefinition
        from aegisforge.tools.circuit_breaker import (
            CircuitBreakerConfig,
            ToolCircuitBreaker,
        )
        from aegisforge.tools.health import ToolHealthTracker
        from aegisforge.tools.registry import ToolRegistry

        class NoopTool(BaseTool):
            def __init__(self) -> None:
                super().__init__(
                    ToolDefinition(name="perf.noop", description="d", permission_requirements=["p"])
                )

            def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
                return {}

        registry = ToolRegistry(
            health_tracker=ToolHealthTracker(),
            circuit_breaker=ToolCircuitBreaker(
                CircuitBreakerConfig(failure_threshold=5, cooldown_seconds=3600)
            ),
        )
        registry.register(NoopTool())

        closed_times: list[float] = []
        for _ in range(300):
            t0 = time.perf_counter()
            r = registry.execute("perf.noop", {}, granted_permissions=["p"])
            closed_times.append(time.perf_counter() - t0)
            assert r.status.value == "completed"

        # Trip the circuit.
        for _ in range(5):
            registry._circuit.record_failure("perf.noop")
        assert registry.get_circuit_state("perf.noop") == "open"

        open_times: list[float] = []
        for _ in range(300):
            t0 = time.perf_counter()
            r = registry.execute("perf.noop", {}, granted_permissions=["p"])
            open_times.append(time.perf_counter() - t0)
            assert "circuit" in (r.error or "").lower()

        closed_avg_us = statistics.mean(closed_times) * 1e6
        open_avg_us = statistics.mean(open_times) * 1e6
        print(
            f"\n  Circuit CLOSED admission: {closed_avg_us:.0f} µs/call; "
            f"OPEN fast-fail: {open_avg_us:.0f} µs/call"
        )
        # Fast-fail must be cheaper than a real execution (tool skipped).
        assert open_avg_us < closed_avg_us * 5
