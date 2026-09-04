"""Tests for async job system.

Covers: job creation, worker execution, retry, failure, status updates,
duplicate execution prevention, and idempotency.
"""
from __future__ import annotations

import pytest

from aegisforge.async_execution.jobs import (
    InMemoryJobQueue,
    JobManager,
    JobWorker,
)
from aegisforge.domain.models import ExecutionJobStatus


# --- Job Queue Tests ---


def test_in_memory_queue_enqueue_dequeue() -> None:
    queue = InMemoryJobQueue()
    from aegisforge.domain.models import ExecutionJob

    job = ExecutionJob(
        job_id="job-1",
        request_id="req-1",
        workflow_id="wf-1",
    )
    assert queue.enqueue(job) is True
    assert queue.size() == 1

    dequeued = queue.dequeue()
    assert dequeued is not None
    assert dequeued.job_id == "job-1"
    assert queue.size() == 0


def test_in_memory_queue_dequeue_empty() -> None:
    queue = InMemoryJobQueue()
    assert queue.dequeue() is None


def test_in_memory_queue_requeue() -> None:
    queue = InMemoryJobQueue()
    from aegisforge.domain.models import ExecutionJob

    job = ExecutionJob(job_id="job-1", request_id="req-1", workflow_id="wf-1")
    queue.enqueue(job)
    queue.dequeue()

    assert queue.requeue(job) is True
    assert queue.size() == 1


# --- Job Manager Tests ---


def test_job_manager_submit_job() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(
        request_id="req-1",
        workflow_id="wf-1",
        organization_id="org-1",
    )

    assert job.job_id
    assert job.status == ExecutionJobStatus.QUEUED
    assert job.request_id == "req-1"


def test_job_manager_get_job() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1")
    retrieved = manager.get_job(job.job_id)
    assert retrieved is not None
    assert retrieved.job_id == job.job_id


def test_job_manager_update_status() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1")
    updated = manager.update_job_status(
        job.job_id,
        ExecutionJobStatus.RUNNING,
    )
    assert updated is not None
    assert updated.status == ExecutionJobStatus.RUNNING


def test_job_manager_idempotency() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job1 = manager.submit_job(
        request_id="req-1",
        workflow_id="wf-1",
        idempotency_key="idem-1",
    )
    job2 = manager.submit_job(
        request_id="req-1",
        workflow_id="wf-1",
        idempotency_key="idem-1",
    )
    assert job1.job_id == job2.job_id
    assert queue.size() == 1  # Only one job in queue


def test_job_manager_should_retry() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1", max_retries=3)
    manager.update_job_status(job.job_id, ExecutionJobStatus.FAILED, error="Temp error")

    assert manager.should_retry(job) is True


def test_job_manager_no_retry_on_permission_error() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1", max_retries=3)
    manager.update_job_status(job.job_id, ExecutionJobStatus.FAILED, error="Permission denied")

    assert manager.should_retry(job) is False


def test_job_manager_no_retry_at_limit() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1", max_retries=1)
    job.retry_count = 1  # At limit
    manager.update_job_status(job.job_id, ExecutionJobStatus.FAILED, error="Error")

    assert manager.should_retry(job) is False


def test_job_manager_requeue_for_retry() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1", max_retries=3)
    manager.update_job_status(job.job_id, ExecutionJobStatus.FAILED, error="Error")

    retried = manager.requeue_for_retry(job.job_id)
    assert retried is not None
    assert retried.retry_count == 1
    assert retried.status == ExecutionJobStatus.RETRYING


def test_job_manager_cancel() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    job = manager.submit_job(request_id="req-1", workflow_id="wf-1")
    cancelled = manager.cancel_job(job.job_id)
    assert cancelled is not None
    assert cancelled.status == ExecutionJobStatus.CANCELLED


def test_job_manager_list_jobs() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    manager.submit_job(request_id="req-1", workflow_id="wf-1")
    manager.submit_job(request_id="req-2", workflow_id="wf-2")

    jobs = manager.list_jobs()
    assert len(jobs) == 2


# --- Job Worker Tests ---


def test_worker_process_successful_job() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    def handler(job):
        return {"result": "success", "job_id": job.job_id}

    worker = JobWorker(manager, handler)
    manager.submit_job(request_id="req-1", workflow_id="wf-1")

    processed = worker.process_next_job()
    assert processed is not None
    assert processed.status == ExecutionJobStatus.COMPLETED
    assert processed.result.get("result") == "success"


def test_worker_process_failing_job() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    def handler(job):
        raise RuntimeError("Job failed")

    worker = JobWorker(manager, handler)
    manager.submit_job(request_id="req-1", workflow_id="wf-1", max_retries=0)  # No retries

    processed = worker.process_next_job()
    assert processed is not None
    assert processed.status == ExecutionJobStatus.FAILED


def test_worker_process_no_jobs() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)
    worker = JobWorker(manager, lambda j: {})

    processed = worker.process_next_job()
    assert processed is None


def test_worker_process_all() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    def handler(job):
        return {"done": True}

    worker = JobWorker(manager, handler)

    for i in range(5):
        manager.submit_job(request_id=f"req-{i}", workflow_id=f"wf-{i}")

    processed = worker.process_all(max_jobs=3)
    assert len(processed) == 3
    assert queue.size() == 2


def test_worker_retries_on_failure() -> None:
    queue = InMemoryJobQueue()
    manager = JobManager(queue)

    call_count = 0

    def handler(job):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RuntimeError("Transient error")
        return {"result": "recovered"}

    worker = JobWorker(manager, handler)
    job = manager.submit_job(request_id="req-1", workflow_id="wf-1", max_retries=3)

    # First attempt fails and gets requeued
    processed = worker.process_next_job()
    assert processed.status == ExecutionJobStatus.RETRYING

    # Should be requeued
    assert queue.size() == 1

    # Second attempt succeeds
    processed = worker.process_next_job()
    assert processed.status == ExecutionJobStatus.COMPLETED
    assert processed.result.get("result") == "recovered"
