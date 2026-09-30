"""Phase 6G — Checkpoint resilience + failure taxonomy + workflow context tests.

Workstream 4/6/8/9 coverage:

Checkpoint store:
1.  Tenant guard: a checkpoint written by another organization is refused
    on resume (no cross-tenant leakage through duplicated workflow ids).
2.  Malformed state_json does not crash resume (treated as empty → re-executes).
3.  Tenant isolation in InMemoryCheckpointStore (parity with DB store).
4.  Sanitization still strips secrets.

Failure taxonomy (Workstream 6):
5.  Every documented class is produced by its inputs.
6.  Permission failures NEVER suggest retry (escalate only).
7.  Configuration/dependency failures never retry (deterministic).
8.  Bounded recovery: retries stop when the budget is exhausted.
9.  Taxonomy metrics are bounded (no raw error text in labels).

Workflow context (Workstream 9):
10. Two concurrent execute_workflow runs do not clobber each other's
    checkpointer (contextvar-scoped context).
"""
from __future__ import annotations

import json
import threading
from typing import Any

from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentType,
    ExecutionPlan,
    ExecutionPlanTask,
    RequestStatus,
)
from aegisforge.workflows.checkpoint import (
    InMemoryCheckpointStore,
    WorkflowCheckpointer,
    _sanitize_state,
)
from aegisforge.workflows.langgraph_workflow import execute_workflow
from aegisforge.workflows.scheduler import MultiAgentExecutor

# ---------------------------------------------------------------------------
# Checkpoint resilience
# ---------------------------------------------------------------------------


class TestCheckpointTenantGuard:
    def test_cross_tenant_checkpoint_refused_on_resume(self) -> None:
        """A checkpoint for org-beta must never be resumed by org-alpha,
        even when both use the same workflow_id (duplication/misconfig)."""
        store = InMemoryCheckpointStore()

        # org-beta writes a checkpoint for workflow id "shared-wf".
        checkpointer_beta = WorkflowCheckpointer(
            workflow_id="shared-wf",
            request_id="req-beta",
            organization_id="org-beta",
            store=store,
        )
        checkpointer_beta.save_after_node(
            "multi_agent_execute",
            {
                "status": "executing",
                "organization_id": "org-beta",
                "task_records": {
                    "t1": {
                        "task_id": "t1",
                        "status": "completed",
                        "output": {"answer": "beta-secret-answer"},
                    }
                },
                "plan": {"tasks": []},
            },
        )

        # org-alpha creates its OWN checkpointer for the same workflow id.
        checkpointer_alpha = WorkflowCheckpointer(
            workflow_id="shared-wf",
            request_id="req-alpha",
            organization_id="org-alpha",
            store=store,
        )
        resumed = checkpointer_alpha.load_resume_state()
        assert resumed is None, "Cross-tenant checkpoint must be refused"

    def test_same_tenant_checkpoint_resumes(self) -> None:
        store = InMemoryCheckpointStore()
        writer = WorkflowCheckpointer(
            workflow_id="wf-ok",
            request_id="req-1",
            organization_id="org-alpha",
            store=store,
        )
        writer.save_after_node("multi_agent_execute", {"status": "executing"})
        reader = WorkflowCheckpointer(
            workflow_id="wf-ok",
            request_id="req-1",
            organization_id="org-alpha",
            store=store,
        )
        assert reader.load_resume_state() is not None

    def test_empty_organization_id_still_resumes(self) -> None:
        """Legacy checkpoints without organization_id are not broken by the guard."""
        store = InMemoryCheckpointStore()
        writer = WorkflowCheckpointer(
            workflow_id="wf-legacy", request_id="req-1", store=store
        )
        writer.save_after_node("plan", {"status": "executing"})
        reader = WorkflowCheckpointer(
            workflow_id="wf-legacy", request_id="req-1", store=store
        )
        assert reader.load_resume_state() is not None


class TestMalformedCheckpoint:
    def test_malformed_state_json_treated_as_empty(self) -> None:
        """Corrupt state must not crash resume — the workflow re-executes."""
        store = InMemoryCheckpointStore()
        checkpoint = _make_checkpoint(store)
        # Corrupt the stored state directly (simulates a partial write).
        store._checkpoints[checkpoint.checkpoint_id].state = "not-a-dict"  # type: ignore[assignment]

        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-bad", request_id="req-1", store=store
        )
        # InMemory store returns the raw object; DbCheckpointStore goes
        # through json.loads.  Exercise the DB path's robustness directly.
        state = checkpointer.load_resume_state()
        # In-memory stores the object as-is; the tenant guard passes and
        # state is returned.  The critical DB-path behavior is tested below.
        assert state is None or isinstance(state, dict)

    def test_db_store_malformed_json_returns_empty_state(self) -> None:
        """DbCheckpointStore._model_to_checkpoint survives corrupt state_json."""
        from aegisforge.workflows.checkpoint import DbCheckpointStore

        class FakeModel:
            id = "cp-1"
            workflow_id = "wf-1"
            request_id = "req-1"
            node_name = "plan"
            organization_id = "org-1"
            user_id = "u1"
            created_at = None
            state_json = "{corrupt json!!"

        store = DbCheckpointStore(session_factory=lambda: None)
        checkpoint = store._model_to_checkpoint(FakeModel())
        assert checkpoint.state == {}

    def test_db_store_non_dict_json_returns_empty_state(self) -> None:
        from aegisforge.workflows.checkpoint import DbCheckpointStore

        class FakeModel:
            id = "cp-2"
            workflow_id = "wf-1"
            request_id = "req-1"
            node_name = "plan"
            organization_id = "org-1"
            user_id = "u1"
            created_at = None
            state_json = json.dumps([1, 2, 3])  # valid JSON, wrong shape

        store = DbCheckpointStore(session_factory=lambda: None)
        checkpoint = store._model_to_checkpoint(FakeModel())
        assert checkpoint.state == {}

    def test_checkpoint_sanitization_strips_secrets(self) -> None:
        state = {
            "intent": "research",
            "api_key": "sk-super-secret",
            "password": "hunter2",
            "token": "jwt-token",
            "auth_token": "bearer",
            "secret": "s3cret",
            "task_records": {},
        }
        sanitized = _sanitize_state(state)
        assert "api_key" not in sanitized
        assert "password" not in sanitized
        assert "token" not in sanitized
        assert "auth_token" not in sanitized
        assert "secret" not in sanitized
        assert sanitized["intent"] == "research"


def _make_checkpoint(store: Any) -> Any:
    """Helper: save one checkpoint and return it."""
    checkpointer = WorkflowCheckpointer(
        workflow_id="wf-bad", request_id="req-1", store=store
    )
    cp_id = checkpointer.save_after_node("plan", {"status": "executing"})
    return store.load_checkpoint(cp_id)


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------


class TestFailureTaxonomy:
    """Workstream 6: bounded taxonomy with strict no-retry-for-policy rules."""

    def _executor(self) -> MultiAgentExecutor:
        return MultiAgentExecutor()

    def test_timeout_classified(self) -> None:
        ex = self._executor()
        assert ex._classify_failure(["Timeout after 30s"], AgentExecutionStatus.TIMEOUT) == "timeout"

    def test_permission_classified_and_never_retried(self) -> None:
        ex = self._executor()
        cls = ex._classify_failure(["Missing required permissions"], AgentExecutionStatus.DENIED)
        assert cls == "permission"
        for retries_left in range(5):
            decision = ex._suggest_recovery(cls, retry_count=0, max_retries=retries_left)
            assert decision["action"] == "escalate"
            assert decision["recoverable"] is False
            assert "retry" not in decision["action"]

    def test_circuit_open_classified_unavailable(self) -> None:
        ex = self._executor()
        cls = ex._classify_failure(
            ["CIRCUIT_OPEN: tool 'x' fast-failed"], AgentExecutionStatus.FAILED
        )
        assert cls == "unavailable"

    def test_transient_classified(self) -> None:
        ex = self._executor()
        assert (
            ex._classify_failure(["connection refused"], AgentExecutionStatus.FAILED)
            == "transient"
        )

    def test_configuration_classified_no_retry(self) -> None:
        ex = self._executor()
        cls = ex._classify_failure(
            ["Tool 'x' not found in registry"], AgentExecutionStatus.FAILED
        )
        assert cls == "configuration"
        decision = ex._suggest_recovery(cls, retry_count=0, max_retries=5)
        assert decision["action"] == "skip"
        assert decision["recoverable"] is False

    def test_dependency_classified_no_retry(self) -> None:
        ex = self._executor()
        cls = ex._classify_failure(
            ["Required dependency 't1' did not complete"], AgentExecutionStatus.FAILED
        )
        assert cls == "dependency"
        decision = ex._suggest_recovery(cls, retry_count=0, max_retries=5)
        assert decision["action"] == "skip"

    def test_model_classified(self) -> None:
        ex = self._executor()
        assert (
            ex._classify_failure(["LLM rate limit exceeded"], AgentExecutionStatus.FAILED)
            == "model"
        )

    def test_unavailable_classified(self) -> None:
        ex = self._executor()
        assert (
            ex._classify_failure(
                ["Service unavailable temporarily"], AgentExecutionStatus.FAILED
            )
            == "unavailable"
        )

    def test_exhausted_retries_abort(self) -> None:
        ex = self._executor()
        decision = ex._suggest_recovery("transient", retry_count=2, max_retries=2)
        assert decision["action"] == "abort"
        assert decision["recoverable"] is False

    def test_bounded_retry_budget_respected(self) -> None:
        ex = self._executor()
        # remaining budget → retry
        assert ex._suggest_recovery("transient", 0, 2)["action"] == "retry"
        # budget exhausted → abort
        assert ex._suggest_recovery("transient", 2, 2)["action"] == "abort"

    def test_taxonomy_metrics_bounded_labels(self) -> None:
        from prometheus_client import REGISTRY

        from aegisforge.observability.metrics import record_failure_classification

        record_failure_classification("transient", "retry")
        record_failure_classification("permission", "escalate")
        for collector in list(REGISTRY.collect()):
            if collector.name == "failure_classifications_total":
                for sample in collector.samples:
                    assert sample.labels["failure_class"] in {
                        "timeout", "transient", "unavailable", "model",
                        "permission", "configuration", "dependency", "unknown",
                    }
                    assert sample.labels["recovery_action"] in {
                        "retry", "retry_with_extended_timeout", "skip",
                        "escalate", "abort",
                    }


# ---------------------------------------------------------------------------
# Concurrent workflow execution (context isolation)
# ---------------------------------------------------------------------------


class TestConcurrentWorkflowContext:
    def test_concurrent_workflows_do_not_clobber_checkpointer(self) -> None:
        """Two synchronous execute_workflow calls in one process must keep
        separate checkpoints (Phase 6G contextvar fix)."""
        from aegisforge.workflows.checkpoint import InMemoryCheckpointStore

        stores = {"a": InMemoryCheckpointStore(), "b": InMemoryCheckpointStore()}
        results: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        def run(tag: str) -> None:
            try:
                checkpointer = WorkflowCheckpointer(
                    workflow_id=f"wf-concurrent-{tag}",
                    request_id=f"req-concurrent-{tag}",
                    organization_id=f"org-{tag}",
                    store=stores[tag],
                )
                final = execute_workflow(
                    request_id=f"req-concurrent-{tag}",
                    intent="Research AI agents thoroughly",
                    user_id=f"user-{tag}",
                    organization_id=f"org-{tag}",
                    workflow_id=f"wf-concurrent-{tag}",
                    checkpointer=checkpointer,
                )
                results[tag] = final
            except Exception as exc:
                errors.append(f"{tag}: {exc}")

        threads = [
            threading.Thread(target=run, args=("a",)),
            threading.Thread(target=run, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert errors == []
        assert set(results) == {"a", "b"}
        # Each workflow checkpointed into ITS OWN store.
        for tag in ("a", "b"):
            workflow_id = f"wf-concurrent-{tag}"
            cps = stores[tag].list_checkpoints(workflow_id)
            assert len(cps) > 0, f"workflow {tag} produced no checkpoints"
            # All checkpoints belong to the right tenant.
            for cp in cps:
                assert cp.organization_id == f"org-{tag}"
        # Cross-contamination check: store 'a' holds no org-b checkpoints.
        for cp in stores["a"].list_checkpoints("wf-concurrent-a"):
            assert cp.organization_id == "org-a"
            assert "org-b" not in json.dumps(cp.state)


class TestWorkflowResumeEndToEnd:
    def test_crash_resume_full_cycle(self) -> None:
        """WS4 acceptance: A completes → crash → B resumes → final result."""
        store = InMemoryCheckpointStore()
        workflow_id = "wf-e2e-recovery"

        # Phase 1: first worker persists a checkpoint showing t1 completed.
        plan = ExecutionPlan(
            plan_id="plan-e2e",
            request_id="req-e2e",
            tasks=[
                ExecutionPlanTask(
                    task_id="t1",
                    description="Task 1",
                    assigned_agent_type=AgentType.RESEARCH,
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
                ExecutionPlanTask(
                    task_id="t2",
                    description="Task 2",
                    assigned_agent_type=AgentType.ANALYSIS,
                    dependencies=["t1"],
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
            ],
        )
        crash_records = {
            "t1": {
                "task_id": "t1",
                "description": "Task 1",
                "agent_type": "research",
                "status": "completed",
                "output": {"answer": "t1-answer"},
                "summary": "Done",
                "errors": [],
                "retry_count": 0,
                "max_retries": 0,
                "timeout_seconds": 5.0,
                "failure_policy": "continue_with_partial_results",
                "dependencies": [],
            }
        }
        checkpointer = WorkflowCheckpointer(
            workflow_id=workflow_id,
            request_id="req-e2e",
            organization_id="org-test",
            store=store,
        )
        checkpointer.save_after_node(
            "multi_agent_execute",
            {
                "status": "executing",
                "organization_id": "org-test",
                "plan": plan.model_dump(mode="json"),
                "task_records": crash_records,
            },
        )

        # Phase 2: "new worker" resumes from the checkpoint.
        resumed = checkpointer.load_resume_state()
        assert resumed is not None
        assert resumed["task_records"]["t1"]["status"] == "completed"

        executed: list[str] = []

        class TrackingAgent:
            name = "tracking"
            agent_type = AgentType.ANALYSIS

            def execute(self, input_data: dict, context: Any) -> Any:
                from aegisforge.domain.models import AgentResult

                executed.append(context.task_id)
                return AgentResult(
                    agent_name="tracking",
                    agent_type=AgentType.ANALYSIS,
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Completed {context.task_id}",
                    result={"answer": f"answer-{context.task_id}"},
                )

        class TrackingFactory:
            @property
            def registry(self):
                from aegisforge.tools.registry import ToolRegistry

                return ToolRegistry()

            def build(self, task, run_id):
                return TrackingAgent()

        executor = MultiAgentExecutor(
            agent_factory=TrackingFactory(),
        )
        outcome = executor.execute(
            plan,
            request_id="req-e2e",
            workflow_id=workflow_id,
            organization_id="org-test",
            preexisting_records=resumed["task_records"],
        )

        assert outcome.status == RequestStatus.COMPLETED
        assert executed == ["t2"], "t1 must NOT be re-executed after crash"
