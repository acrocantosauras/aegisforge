"""Real Redis integration tests (Phase 4.2 Part 7).

Verifies the production queue path against a REAL Redis server:
enqueue → dequeue → worker execution → retry → idempotency →
cancellation → failure recovery.
"""
from __future__ import annotations

import pytest

from aegisforge.async_execution.jobs import JobManager, JobWorker, RedisJobQueue
from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus

from .conftest import requires_infra


@requires_infra
class TestRedisQueue:
    def test_enqueue_dequeue_roundtrip(self, redis_client):
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        job = ExecutionJob(
            job_id="job-redis-1",
            request_id="req-1",
            workflow_id="wf-1",
            organization_id="org-1",
        )
        assert queue.enqueue(job) is True
        assert queue.size() == 1

        dequeued = queue.dequeue()
        assert dequeued is not None
        assert dequeued.job_id == "job-redis-1"
        assert dequeued.organization_id == "org-1"
        assert queue.size() == 0

    def test_serialization_preserves_metadata(self, redis_client):
        """organization_id + trace_id must survive the Redis roundtrip."""
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        job = ExecutionJob(
            job_id="job-redis-2",
            request_id="req-2",
            workflow_id="wf-2",
            organization_id="org-42",
            trace_id="trace-abc-123",
        )
        queue.enqueue(job)
        dequeued = queue.dequeue()
        assert dequeued is not None
        assert dequeued.organization_id == "org-42"
        assert dequeued.trace_id == "trace-abc-123"

    def test_worker_full_cycle(self, redis_client):
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        manager = JobManager(queue)
        worker = JobWorker(manager, lambda job: {"result": "done", "job": job.job_id})

        manager.submit_job(
            request_id="req-3",
            workflow_id="wf-3",
            organization_id="org-3",
            max_retries=2,
        )
        processed = worker.process_next_job()
        assert processed is not None
        assert processed.status == ExecutionJobStatus.COMPLETED
        assert processed.result["result"] == "done"
        assert queue.size() == 0

    def test_worker_retry_recovers(self, redis_client):
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        manager = JobManager(queue)

        calls = {"n": 0}

        def flaky_handler(job):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("transient failure")
            return {"result": "recovered"}

        worker = JobWorker(manager, flaky_handler)
        manager.submit_job(request_id="req-4", workflow_id="wf-4", organization_id="org-4", max_retries=3)

        first = worker.process_next_job()
        assert first.status == ExecutionJobStatus.RETRYING
        assert queue.size() == 1  # requeued in real Redis

        second = worker.process_next_job()
        assert second.status == ExecutionJobStatus.COMPLETED
        assert second.result["result"] == "recovered"

    def test_idempotent_submission(self, redis_client):
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        manager = JobManager(queue)
        job1 = manager.submit_job(
            request_id="req-5", workflow_id="wf-5", idempotency_key="idem-1"
        )
        job2 = manager.submit_job(
            request_id="req-5", workflow_id="wf-5", idempotency_key="idem-1"
        )
        assert job1.job_id == job2.job_id
        assert queue.size() == 1

    def test_cancellation(self, redis_client):
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        manager = JobManager(queue)
        job = manager.submit_job(request_id="req-6", workflow_id="wf-6")
        cancelled = manager.cancel_job(job.job_id)
        assert cancelled is not None
        assert cancelled.status == ExecutionJobStatus.CANCELLED

    def test_failure_records_error(self, redis_client):
        queue = RedisJobQueue(redis_client, queue_name="aegisforge:jobs")
        manager = JobManager(queue)

        def failing_handler(job):
            raise RuntimeError("permanent failure")

        worker = JobWorker(manager, failing_handler)
        manager.submit_job(request_id="req-7", workflow_id="wf-7", max_retries=0)
        processed = worker.process_next_job()
        assert processed is not None
        assert processed.status == ExecutionJobStatus.FAILED
        assert "permanent failure" in processed.errors


@requires_infra
class TestWorkerProductionBehavior:
    def test_worker_does_not_fall_back_to_memory_in_production(self, monkeypatch):
        """In production, the worker must FAIL clearly without Redis."""
        from aegisforge.config import Settings

        settings = Settings(
            environment="production",
            redis_url="redis://localhost:6399/0",  # nothing listens here
        )
        monkeypatch.setattr("sys.argv", ["worker"])
        with pytest.raises(SystemExit) as excinfo:
            from aegisforge.worker import run_worker

            run_worker(settings)
        assert excinfo.value.code == 1

    def test_worker_uses_redis_queue_in_production(self, redis_client, monkeypatch):
        """With Redis available, production worker must build a RedisJobQueue."""

        import aegisforge.worker as worker_module

        # Monkeypatch redis connection to the real client
        class FakeRedis:
            @staticmethod
            def from_url(url, **kwargs):
                return redis_client

        monkeypatch.setattr("redis.from_url", FakeRedis.from_url)

        # Patch signal handlers so the loop can be interrupted cleanly
        monkeypatch.setattr(worker_module, "_handle_signal", lambda *a: None)

        from aegisforge.config import Settings

        settings = Settings(
            environment="production",
            redis_url="redis://localhost:6379/0",
        )

        # Capture the queue built inside run_worker by intercepting JobManager
        captured: dict = {}

        real_init = worker_module.JobManager.__init__

        def spy_init(self, queue):
            captured["queue"] = queue
            real_init(self, queue)

        monkeypatch.setattr(worker_module.JobManager, "__init__", spy_init)
        # Prevent the infinite loop: make process_next_job raise after first iteration
        monkeypatch.setattr(
            worker_module.JobWorker,
            "process_next_job",
            lambda self: (_ for _ in ()).throw(SystemExit(0)),
        )

        with pytest.raises(SystemExit):
            worker_module.run_worker(settings)

        assert isinstance(captured["queue"], RedisJobQueue)