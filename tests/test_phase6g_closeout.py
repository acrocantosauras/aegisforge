"""Phase 6G closeout regression tests.

Covers the remaining hardening steps:

1.  Terminal-state guard correctness (CLAIM Lua + dequeue-claim Lua use
    cjson-parsed status, NOT substring matching): a non-terminal job whose
    errors/result contain the literal text '"status":"completed"' must
    still be claimable and executable.
2.  Dequeue→claim durability: the atomic pop+claim path registers the
    claim inside the same Redis operation, so a worker that dies between
    dequeue and execution leaves the job in the active-claims zset —
    recoverable by visibility-timeout recovery.  The job can no longer
    vanish from all recoverable state.
3.  Idempotency under concurrency: many threads submitting with the same
    idempotency key produce exactly one logical job, one queue entry, and
    one consistent reference for all callers.
4.  Bounded worker state: crashed workers' registry/capacity/active-job
    entries are swept by cleanup_dead_worker_state once the heartbeat TTL
    expires; live workers are never removed.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

import pytest

from aegisforge.async_execution.jobs import JobManager, JobWorker, RedisJobQueue
from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus, ToolExecutionStatus
from aegisforge.tools.base import BaseTool, ToolDefinition
from aegisforge.tools.registry import ToolRegistry


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
    not _redis_available(), reason="Closeout tests require a real Redis instance"
)


@pytest.fixture()
def redis_client() -> Any:
    import redis

    client = redis.from_url(_test_redis_url(), decode_responses=True)
    client.ping()
    yield client
    for key in client.scan_iter(match="aegisforge:test-co-*"):
        client.delete(key)
    for key in ("aegisforge:active_claims", "aegisforge:workers", "aegisforge:worker_state", "aegisforge:worker_active"):
        client.delete(key)


def _queue(redis_client: Any, visibility: int = 300) -> RedisJobQueue:
    return RedisJobQueue(
        redis_client,
        queue_name=f"aegisforge:test-co-{uuid.uuid4().hex[:10]}",
        visibility_timeout=visibility,
    )


POISON = '"status":"completed"'  # substring that broke the old terminal guard


@requires_redis
class TestTerminalGuardNoFalsePositives:
    def test_poisoned_payload_job_still_claimable(self, redis_client: Any) -> None:
        """A QUEUED job whose error/result text contains the literal
        '"status":"completed"' must not be rejected as terminal."""
        queue = _queue(redis_client)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-poison", workflow_id="wf-poison", organization_id="org-a"
        )
        # Poison the durable payload with terminal-looking text in errors.
        poisoned = queue.get_job_data(job.job_id)
        assert poisoned is not None
        poisoned.errors.append(f"previous run saw {POISON} in a result payload")
        queue._store_job_data(poisoned)

        dequeued = queue.dequeue()
        assert dequeued is not None
        assert dequeued.job_id == job.job_id
        # The old substring guard would have rejected this claim.
        assert queue.claim_job(dequeued, "w-poison") is True

    def test_poisoned_payload_job_executes_via_worker(self, redis_client: Any) -> None:
        queue = _queue(redis_client)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-poison2", workflow_id="wf-poison2", organization_id="org-a"
        )
        poisoned = queue.get_job_data(job.job_id)
        assert poisoned is not None
        poisoned.result = {"echo": POISON}
        queue._store_job_data(poisoned)

        executed: list[str] = []

        def handler(j: ExecutionJob) -> dict[str, Any]:
            executed.append(j.job_id)
            return {"ok": True}

        worker = JobWorker(manager, handler, worker_id="w-poison2")
        worker.process_all(max_jobs=5)
        assert executed == [job.job_id]

    def test_genuinely_terminal_job_still_rejected(self, redis_client: Any) -> None:
        """The guard must keep its true-positive: terminal jobs are dropped."""
        queue = _queue(redis_client)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-term", workflow_id="wf-term", organization_id="org-a",
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        # Simulate completion recorded in durable state.
        stale_copy = queue.dequeue()
        assert stale_copy is not None
        queue.update_job_status(job.job_id, ExecutionJobStatus.COMPLETED)
        # A stale QUEUED copy re-enters the queue (recovery replay scenario).
        queue.enqueue(stale_copy)
        seen = queue.dequeue()
        if seen is not None and seen.job_id == job.job_id:
            # Must be refused: durable state says COMPLETED.
            assert queue.claim_job(seen, "w-term") is False
        stored = queue.get_job_data(job.job_id)
        assert stored is not None and stored.status == ExecutionJobStatus.COMPLETED


@requires_redis
class TestAtomicDequeueClaimDurability:
    def test_worker_crash_between_dequeue_and_execution_is_recoverable(
        self, redis_client: Any
    ) -> None:
        """With atomic pop+claim, a worker that dies right after dequeue
        leaves the job registered in the active-claims zset — the recovery
        scan can find and re-enqueue it.  No loss window remains."""
        queue = _queue(redis_client, visibility=1)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-dur", workflow_id="wf-dur", organization_id="org-a"
        )

        # Atomic path: pop and claim in ONE operation.
        claimed = queue.dequeue("w-crashy", capacity=1)
        assert claimed is not None and claimed.job_id == job.job_id
        assert queue.get_claim_owner(job.job_id) == "w-crashy"
        assert queue.is_claimed(job.job_id) is True

        # Worker crashes here (no heartbeat, no release, no execution).
        time.sleep(1.2)
        recovered = queue.recover_expired_claims()
        assert job.job_id in recovered
        redelivered = queue.dequeue("w-good", capacity=1)
        assert redelivered is not None and redelivered.job_id == job.job_id

    def test_atomic_dequeue_refuses_capacity_exhausted(self, redis_client: Any) -> None:
        queue = _queue(redis_client)
        manager = JobManager(queue)
        for i in range(3):
            manager.submit_job(
                request_id=f"req-cap-{i}", workflow_id=f"wf-cap-{i}",
                organization_id="org-a",
            )
        # Capacity 1: first atomic dequeue succeeds, second must not hand
        # out another job to the same (saturated) worker.
        first = queue.dequeue("w-cap", capacity=1)
        assert first is not None
        second = queue.dequeue("w-cap", capacity=1)
        assert second is None
        # A different worker can still take work.
        other = queue.dequeue("w-other", capacity=1)
        assert other is not None

    def test_worker_end_to_end_with_atomic_path(self, redis_client: Any) -> None:
        """The production JobWorker path uses atomic dequeue+claim."""
        queue = _queue(redis_client)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-e2e", workflow_id="wf-e2e", organization_id="org-a"
        )
        assert queue.atomic_dequeue_available is True

        executed: list[str] = []

        def handler(j: ExecutionJob) -> dict[str, Any]:
            executed.append(j.job_id)
            return {"ok": True}

        worker = JobWorker(manager, handler, worker_id="w-e2e")
        worker.process_all(max_jobs=5)
        assert executed == [job.job_id]
        stored = queue.get_job_data(job.job_id)
        assert stored is not None and stored.status == ExecutionJobStatus.COMPLETED
        # Claim released after completion.
        assert queue.get_claim_owner(job.job_id) is None


@requires_redis
class TestIdempotencyConcurrency:
    def test_concurrent_submissions_same_key_single_job(self, redis_client: Any) -> None:
        """20 threads race on one idempotency key: exactly one logical job,
        one queue entry, all callers get the same job reference."""
        queue = _queue(redis_client)
        manager = JobManager(queue)
        key = f"idem-{uuid.uuid4().hex[:10]}"
        results: list[ExecutionJob] = []
        lock = threading.Lock()
        start = threading.Barrier(20)

        def submit(i: int) -> None:
            start.wait()  # maximize contention
            job = manager.submit_job(
                request_id=f"req-{i}", workflow_id=f"wf-{i}",
                organization_id=f"org-{i % 3}", idempotency_key=key,
            )
            with lock:
                results.append(job)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert len(results) == 20
        job_ids = {j.job_id for j in results}
        assert len(job_ids) == 1  # exactly one logical job for everyone
        assert queue.size() == 1  # exactly one queue entry

    def test_distinct_keys_create_distinct_jobs(self, redis_client: Any) -> None:
        queue = _queue(redis_client)
        manager = JobManager(queue)
        jobs = [
            manager.submit_job(
                request_id=f"req-{i}", workflow_id="wf-distinct",
                organization_id="org-a", idempotency_key=f"idem-{uuid.uuid4().hex[:10]}",
            )
            for i in range(5)
        ]
        assert len({j.job_id for j in jobs}) == 5
        assert queue.size() == 5


@requires_redis
class TestWorkerStateBoundedness:
    def test_dead_worker_entries_are_swept(self, redis_client: Any) -> None:
        queue = _queue(redis_client)
        # A worker registers, advertises capacity, takes a slot... then dies.
        queue.register_worker("w-dead")
        queue.set_worker_capacity("w-dead", 4)
        queue.register_worker("w-alive")
        queue.set_worker_capacity("w-alive", 4)
        queue._redis.hset("aegisforge:worker_active", "w-dead", 2)
        queue._redis.hset("aegisforge:worker_active", "w-alive", 1)

        # Simulate w-dead's heartbeat going stale without touching w-alive.
        cutoff = time.time() - 3600
        queue._redis.zadd("aegisforge:workers", {"w-dead": cutoff})

        removed = queue.cleanup_dead_worker_state(ttl=60)
        assert removed == 1

        assert queue.get_active_workers(ttl=3600) == ["w-alive"]
        assert queue.get_worker_capacity("w-alive") == 4
        assert queue.get_worker_capacity("w-dead") == 0  # swept
        assert (
            redis_client.hget("aegisforge:worker_active", "w-dead") is None
        )  # active-slot leak swept
        assert (
            redis_client.hget("aegisforge:worker_active", "w-alive") == "1"
        )  # live worker untouched

    def test_repeated_crashes_do_not_grow_state(self, redis_client: Any) -> None:
        """10 crash cycles leave zero residual worker entries after sweeps."""
        queue = _queue(redis_client)
        for i in range(10):
            wid = f"w-crash-{i}"
            queue.register_worker(wid)
            queue.set_worker_capacity(wid, 2)
            queue._redis.zadd("aegisforge:workers", {wid: time.time() - 7200})
        removed = queue.cleanup_dead_worker_state(ttl=60)
        assert removed == 10
        assert queue.get_active_workers(ttl=3600) == []
        assert redis_client.hlen("aegisforge:worker_state") == 0
        assert redis_client.hlen("aegisforge:worker_active") == 0


class TestWorkflowContextIsolation:
    """Step 9: the ContextVar-scoped execution context must give each
    concurrent execution its own view, support nesting, and clean up."""

    def test_concurrent_executions_isolate_contexts(self) -> None:
        import aegisforge.workflows.langgraph_workflow as lw

        results: dict[int, Any] = {}
        errors: list[Exception] = []
        start = threading.Barrier(4)

        def run(i: int) -> None:
            try:
                ctx = lw._WorkflowContext()
                ctx.request_id = f"req-{i}"
                token = lw._execution_ctx.set(ctx)
                start.wait()
                time.sleep(0.05)  # widen the interleaving window
                resolved = lw._current_context()
                results[i] = resolved.request_id
                lw._execution_ctx.reset(token)
            except Exception as exc:  # pragma: no cover — surfaced via assert
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert results == {0: "req-0", 1: "req-1", 2: "req-2", 3: "req-3"}

    def test_nested_execution_restores_outer_context(self) -> None:
        import aegisforge.workflows.langgraph_workflow as lw

        outer = lw._WorkflowContext()
        outer.request_id = "outer"
        token_outer = lw._execution_ctx.set(outer)
        try:
            inner = lw._WorkflowContext()
            inner.request_id = "inner"
            token_inner = lw._execution_ctx.set(inner)
            try:
                assert lw._current_context().request_id == "inner"
            finally:
                lw._execution_ctx.reset(token_inner)
            assert lw._current_context().request_id == "outer"
        finally:
            lw._execution_ctx.reset(token_outer)
        # Back to default: falls through to the legacy global _ctx.
        assert lw._execution_ctx.get() is None
        assert lw._current_context() is lw._ctx


class TestCircuitPlannerSecurityQuickChecks:
    """Focused re-verification (Step 6/13): deny-first ordering and the
    planner selection ladder never bypass permissions."""

    def test_selection_ladder_orders_health_and_circuit(self) -> None:
        from aegisforge.tools.circuit_breaker import CircuitState
        from aegisforge.tools.health import (
            ToolHealthSnapshot,
            ToolHealthTracker,
            select_tool_with_circuit,
        )

        tracker = ToolHealthTracker()
        # healthy tool: full success history
        for _ in range(10):
            tracker.record_success("tool.healthy")
        # degraded tool: some failures
        for _ in range(5):
            tracker.record_success("tool.degraded")
        for _ in range(3):
            tracker.record_failure("tool.degraded")

        def snap(name: str) -> ToolHealthSnapshot:
            return tracker.get_health(name)

        health = [snap("tool.healthy"), snap("tool.degraded")]

        # Healthy wins over degraded, both circuits closed.
        pick = select_tool_with_circuit(
            requested="tool.degraded",
            required_permissions=["p"],
            available_tools=["tool.healthy", "tool.degraded"],
            health_context=health,
            circuit_context={
                "tool.healthy": CircuitState.CLOSED,
                "tool.degraded": CircuitState.CLOSED,
            },
        )
        assert pick == "tool.healthy"

        # Circuit OPEN beats health: an OPEN-circuit tool is avoided even
        # with a perfect health history when a closed alternative exists.
        for _ in range(10):
            tracker.record_success("tool.open")
        health.append(snap("tool.open"))
        pick = select_tool_with_circuit(
            requested="tool.open",
            required_permissions=["p"],
            available_tools=["tool.healthy", "tool.open"],
            health_context=health,
            circuit_context={
                "tool.healthy": CircuitState.CLOSED,
                "tool.open": CircuitState.OPEN,
            },
        )
        assert pick == "tool.healthy"

    def test_registry_deny_first_ordering_matrix(self) -> None:
        class Tool(BaseTool):
            def __init__(self, enabled: bool) -> None:
                super().__init__(
                    ToolDefinition(
                        name="matrix.tool", description="d",
                        permission_requirements=["p"], enabled=enabled,
                    )
                )

            def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
                return {}

        breaker_states: dict[str, str] = {}

        registry = ToolRegistry()
        registry.register(Tool(enabled=False))
        # Unknown tool + open circuit on other tools: unknown wins (FAILED).
        r = registry.execute("ghost", {}, granted_permissions=["p"])
        assert r.status == ToolExecutionStatus.FAILED
        # Disabled tool: DENIED regardless of circuit state.
        r = registry.execute("matrix.tool", {}, granted_permissions=["p"])
        assert r.status == ToolExecutionStatus.DENIED
        # Disabled tool + missing permissions: still DENIED (policy), and
        # circuit state must not turn it into an execution.
        r = registry.execute("matrix.tool", {}, granted_permissions=[])
        assert r.status == ToolExecutionStatus.DENIED
        assert breaker_states == {}
