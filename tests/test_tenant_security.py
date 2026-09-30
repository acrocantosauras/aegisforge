"""Phase 6G — Security / tenant isolation regression tests (Workstream 8).

Adversarial checks across the surfaces touched by this sprint:

1.  Cross-tenant job payload integrity: organization_id flows through
    dequeue → claim → execute unchanged (the queue is shared; payloads are
    never mixed up between tenants).
2.  Idempotency dedup is global by design (dedup, not access control):
    a duplicate submission returns the SAME job instead of creating a
    second one, and the mapping cannot be hijacked by a later submission.
3.  Worker logs never contain Redis credentials.
4.  Circuit/health state carries no tenant or payload data (aggregate
    process-global state is safe: tool names are operator-controlled and
    snapshots contain only bounded evidence fields).
5.  Claim ownership: a worker from "another deployment" (different
    worker_id) cannot release or heartbeat someone else's claim.
6.  Disabled/unknown tools and permission boundaries still hold when the
    circuit is OPEN (deny-first ordering verified adversarially).
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import pytest

from aegisforge.async_execution.jobs import JobManager, JobWorker, RedisJobQueue
from aegisforge.domain.models import ExecutionJob, ExecutionJobStatus, ToolExecutionStatus


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
    reason="Tenant isolation queue tests require a real Redis instance",
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
        "aegisforge:workers",
        "aegisforge:worker_state",
        "aegisforge:worker_active",
    ):
        client.delete(key)
    yield client
    for key in client.scan_iter(match="aegisforge:test-*"):
        client.delete(key)
    for key in (
        "aegisforge:active_claims",
        "aegisforge:workers",
        "aegisforge:worker_state",
        "aegisforge:worker_active",
    ):
        client.delete(key)


@pytest.fixture()
def queue_name() -> str:
    return f"aegisforge:test-sec-{uuid.uuid4().hex[:8]}"


@requires_redis
class TestTenantPayloadIntegrity:
    def test_organization_id_preserved_through_queue_lifecycle(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Jobs from two tenants flow through the same queue without mixing."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)

        alpha_jobs = [
            manager.submit_job(
                request_id=f"req-a-{i}",
                workflow_id=f"wf-a-{i}",
                organization_id="org-alpha",
            )
            for i in range(3)
        ]
        beta_jobs = [
            manager.submit_job(
                request_id=f"req-b-{i}",
                workflow_id=f"wf-b-{i}",
                organization_id="org-beta",
            )
            for i in range(3)
        ]

        seen: dict[str, str] = {}
        while True:
            job = queue.dequeue()
            if job is None:
                break
            seen[job.job_id] = job.organization_id

        for j in alpha_jobs:
            assert seen[j.job_id] == "org-alpha"
        for j in beta_jobs:
            assert seen[j.job_id] == "org-beta"

    def test_worker_executes_with_correct_tenant_context(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """The handler receives the organization the job was submitted with."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        manager.submit_job(
            request_id="req-tenant-ctx",
            workflow_id="wf-tenant-ctx",
            organization_id="org-alpha",
        )

        received: list[str] = []

        def handler(j: ExecutionJob) -> dict[str, Any]:
            received.append(j.organization_id)
            return {"ok": True}

        worker = JobWorker(manager, handler, worker_id="w-tenant")
        worker.process_all(max_jobs=5)
        assert received == ["org-alpha"]


@requires_redis
class TestIdempotencySecurity:
    def test_duplicate_submission_returns_same_job(
        self, redis_client: Any, queue_name: str
    ) -> None:
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        key = f"idem-{uuid.uuid4().hex[:10]}"
        job1 = manager.submit_job(
            request_id="req-idem-1",
            workflow_id="wf-idem-1",
            organization_id="org-alpha",
            idempotency_key=key,
        )
        job2 = manager.submit_job(
            request_id="req-idem-2",
            workflow_id="wf-idem-2",
            organization_id="org-alpha",
            idempotency_key=key,
        )
        assert job1.job_id == job2.job_id
        # Exactly one job in the queue.
        assert queue.size() == 1

    def test_idempotency_binding_cannot_be_hijacked(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Once bound, the key maps to the ORIGINAL job id; a second submitter
        gets the original job back, never a fresh one under the same key."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        key = f"idem-{uuid.uuid4().hex[:10]}"
        first = manager.submit_job(
            request_id="req-first",
            workflow_id="wf-first",
            organization_id="org-alpha",
            idempotency_key=key,
        )
        # Attacker/buggy caller re-uses the key with a different tenant.
        second = manager.submit_job(
            request_id="req-second",
            workflow_id="wf-second",
            organization_id="org-beta",
            idempotency_key=key,
        )
        assert second.job_id == first.job_id
        stored = queue.get_job_data(first.job_id)
        assert stored is not None
        assert stored.organization_id == "org-alpha"  # original payload intact

    def test_failed_job_allows_new_submission_same_key(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """A FAILED job's key does not block a legitimate resubmission."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        key = f"idem-{uuid.uuid4().hex[:10]}"
        job = manager.submit_job(
            request_id="req-fail",
            workflow_id="wf-fail",
            organization_id="org-alpha",
            idempotency_key=key,
            max_retries=0,
        )

        def failing_handler(j: ExecutionJob) -> dict[str, Any]:
            raise RuntimeError("permanent failure")

        worker = JobWorker(manager, failing_handler, worker_id="w-fail")
        worker.process_all(max_jobs=5)
        stored = queue.get_job_data(job.job_id)
        assert stored is not None and stored.status == ExecutionJobStatus.FAILED

        # Resubmission with the same key is allowed after terminal failure.
        job2 = manager.submit_job(
            request_id="req-fail-2",
            workflow_id="wf-fail-2",
            organization_id="org-alpha",
            idempotency_key=key,
        )
        assert job2.job_id != job.job_id


class TestSecretHygiene:
    def test_worker_logs_never_contain_redis_credentials(self, caplog: Any) -> None:
        """Redis URL credentials must be masked in worker logs."""
        from aegisforge.worker import _sanitize_redis_url

        secret_url = "redis://admin:supersecret@redis-host:6379/2"
        masked = _sanitize_redis_url(secret_url)
        assert "supersecret" not in masked
        assert "redis-host:6379/2" in masked

        plain_url = "redis://redis-host:6379/2"
        assert _sanitize_redis_url(plain_url) == plain_url

        # Malformed input does not raise.
        assert _sanitize_redis_url("::::") is not None

    def test_no_credential_logging_via_caplog(self, caplog: Any) -> None:
        from aegisforge.worker import _sanitize_redis_url

        with caplog.at_level(logging.INFO, logger="aegisforge.worker"):
            logging.getLogger("aegisforge.worker").info(
                "Redis URL: %s", _sanitize_redis_url(
                    "redis://user:topsecret@h:6379/0"
                )
            )
        assert "topsecret" not in caplog.text


class TestAggregateStateLeakage:
    def test_circuit_and_health_snapshots_contain_no_tenant_data(self) -> None:
        """Aggregate health/circuit state is tenant-free by construction."""
        from aegisforge.tools.base import BaseTool, ToolDefinition
        from aegisforge.tools.circuit_breaker import (
            CircuitBreakerConfig,
            ToolCircuitBreaker,
        )
        from aegisforge.tools.registry import ToolRegistry

        class SecretInputTool(BaseTool):
            def __init__(self) -> None:
                super().__init__(
                    ToolDefinition(
                        name="leak.check",
                        description="d",
                        permission_requirements=["p"],
                    )
                )

            def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
                return {"echo": input_data}

        breaker = ToolCircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=300)
        )
        registry = ToolRegistry(circuit_breaker=breaker)
        registry.register(SecretInputTool())
        payload = {"query": "CONFIDENTIAL tenant payload must never leak"}
        result = registry.execute("leak.check", payload, granted_permissions=["p"])
        assert result.status == ToolExecutionStatus.COMPLETED

        # Health snapshots: bounded fields only.
        snap = registry.get_tool_health("leak.check")
        assert set(snap.to_dict().keys()) == {
            "tool_name", "state", "recent_failure_rate",
            "recent_timeout_rate", "consecutive_failures", "sample_count",
        }
        assert "CONFIDENTIAL" not in str(snap.to_dict())

        # Circuit state map records TRANSITIONS only (bounded): a healthy
        # tool that never left CLOSED is absent; after a trip it appears.
        states = registry.list_circuit_states()
        assert states == {}
        breaker.record_failure("leak.check")
        tripped = registry.list_circuit_states()
        assert tripped.get("leak.check") == "open"
        assert "CONFIDENTIAL" not in str(tripped)


class TestDenyFirstUnderOpenCircuit:
    def test_disabled_tool_denied_even_when_circuit_open(self) -> None:
        from aegisforge.tools.base import BaseTool, ToolDefinition
        from aegisforge.tools.circuit_breaker import (
            CircuitBreakerConfig,
            CircuitState,
            ToolCircuitBreaker,
        )
        from aegisforge.tools.registry import ToolRegistry

        class Tool(BaseTool):
            def __init__(self, enabled: bool) -> None:
                super().__init__(
                    ToolDefinition(
                        name="deny.check", description="d",
                        permission_requirements=["p"], enabled=enabled,
                    )
                )

            def _execute(self, input_data: dict[str, Any], context: Any = None) -> dict[str, Any]:
                return {}

        breaker = ToolCircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        registry = ToolRegistry(circuit_breaker=breaker)
        disabled = Tool(enabled=False)
        registry.register(disabled)
        # Force the circuit OPEN (simulating prior failures).
        breaker.record_failure("deny.check")
        assert registry.get_circuit_state("deny.check") == CircuitState.OPEN
        result = registry.execute("deny.check", {}, granted_permissions=["p"])
        assert result.status == ToolExecutionStatus.DENIED  # policy first
        assert "disabled" in (result.error or "")

    def test_unknown_tool_failed_even_when_circuit_would_open(self) -> None:
        from aegisforge.tools.circuit_breaker import (
            CircuitBreakerConfig,
            ToolCircuitBreaker,
        )
        from aegisforge.tools.registry import ToolRegistry

        breaker = ToolCircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        registry = ToolRegistry(circuit_breaker=breaker)
        for _ in range(10):
            result = registry.execute("ghost.tool", {}, granted_permissions=["p"])
            assert result.status == ToolExecutionStatus.FAILED
            assert "not found in registry" in (result.error or "")
        # Unknown tools never created circuit state.
        assert registry.get_circuit_state("ghost.tool") == "closed"


class TestJobListingTenantIsolation:
    def test_list_jobs_filters_by_organization(self) -> None:
        """list_jobs must never return another organization's jobs.

        Regression: organization_id was accepted but ignored, so a caller
        filtering by tenant still received every tenant's jobs.
        """
        from aegisforge.async_execution.jobs import InMemoryJobQueue

        manager = JobManager(InMemoryJobQueue())
        alpha = manager.submit_job(
            request_id="req-a", workflow_id="wf-a", organization_id="org-alpha"
        )
        beta = manager.submit_job(
            request_id="req-b", workflow_id="wf-b", organization_id="org-beta"
        )

        alpha_jobs = manager.list_jobs(organization_id="org-alpha")
        assert [j.job_id for j in alpha_jobs] == [alpha.job_id]
        assert beta.job_id not in {j.job_id for j in alpha_jobs}

        beta_jobs = manager.list_jobs(organization_id="org-beta")
        assert [j.job_id for j in beta_jobs] == [beta.job_id]

        # No organization filter returns everything (admin path).
        assert {j.job_id for j in manager.list_jobs()} == {alpha.job_id, beta.job_id}

    def test_list_jobs_combines_tenant_and_status_filters(self) -> None:
        from aegisforge.async_execution.jobs import InMemoryJobQueue

        manager = JobManager(InMemoryJobQueue())
        a1 = manager.submit_job(
            request_id="req-a1", workflow_id="wf-a1", organization_id="org-alpha"
        )
        manager.submit_job(
            request_id="req-a2", workflow_id="wf-a2", organization_id="org-alpha"
        )
        manager.cancel_job(a1.job_id)

        cancelled = manager.list_jobs(
            organization_id="org-alpha", status=ExecutionJobStatus.CANCELLED
        )
        assert [j.job_id for j in cancelled] == [a1.job_id]
        assert manager.list_jobs(
            organization_id="org-beta", status=ExecutionJobStatus.CANCELLED
        ) == []


@requires_redis
class TestClaimOwnershipBoundary:
    def test_claim_operations_require_matching_worker_id(
        self, redis_client: Any, queue_name: str
    ) -> None:
        """Heartbeat/release from a foreign worker are rejected."""
        queue = RedisJobQueue(redis_client, queue_name=queue_name, visibility_timeout=300)
        manager = JobManager(queue)
        job = manager.submit_job(
            request_id="req-own",
            workflow_id="wf-own",
            organization_id="org-alpha",
        )
        dequeued = queue.dequeue()
        assert dequeued is not None
        assert queue.claim_job(dequeued, "worker-owner") is True

        assert queue.heartbeat_job(job.job_id, "worker-foreign") is False
        queue.release_claim(job.job_id, "worker-foreign")
        assert queue.get_claim_owner(job.job_id) == "worker-owner"

        assert queue.heartbeat_job(job.job_id, "worker-owner") is True
        queue.release_claim(job.job_id, "worker-owner")
        assert queue.get_claim_owner(job.job_id) is None
