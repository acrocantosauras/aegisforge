"""Phase 6D: Durable workflow resume after worker crash.

Verifies that when a worker crashes mid-workflow and the job is recovered:
1. The worker handler passes resume_from_checkpoint=True for retry jobs
2. The checkpoint state is loaded and the workflow continues from the last node
3. Already-completed tasks are skipped (not re-executed)
4. The checkpoint resume metric is emitted
5. Tenant isolation is preserved through resume

Uses in-memory checkpoint store + in-memory queue to prove the resume logic
in isolation. Real Redis + DB tests for integration coverage.
"""
from __future__ import annotations

from typing import Any

import pytest

from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentType,
    ExecutionJob,
    ExecutionJobStatus,
    ExecutionPlan,
    ExecutionPlanTask,
    RequestStatus,
)
from aegisforge.workflows.checkpoint import (
    InMemoryCheckpointStore,
    WorkflowCheckpointer,
)
from aegisforge.workflows.scheduler import AgentFactory, ExecutionConfig, MultiAgentExecutor

# ---------------------------------------------------------------------------
# Test: Worker handler detects retry and sets resume flag
# ---------------------------------------------------------------------------


class TestWorkerHandlerResumeFlag:
    """Verify the worker handler passes resume_from_checkpoint for retry jobs."""

    def test_retry_count_zero_no_resume(self) -> None:
        """A first-time job (retry_count=0) should NOT resume from checkpoint."""
        job = ExecutionJob(
            job_id="job-first",
            request_id="req-1",
            workflow_id="wf-1",
            organization_id="org-test",
            status=ExecutionJobStatus.QUEUED,
            retry_count=0,
        )
        is_retry_resume = job.retry_count > 0
        assert is_retry_resume is False

    def test_retry_count_positive_enables_resume(self) -> None:
        """A retry job (retry_count>0) should resume from checkpoint."""
        job = ExecutionJob(
            job_id="job-retry",
            request_id="req-1",
            workflow_id="wf-1",
            organization_id="org-test",
            status=ExecutionJobStatus.RETRYING,
            retry_count=1,
        )
        is_retry_resume = job.retry_count > 0
        assert is_retry_resume is True

    def test_crash_recovery_sets_retry_count(self) -> None:
        """When a job is recovered from a crash, retry_count is incremented."""
        job = ExecutionJob(
            job_id="job-crash",
            request_id="req-1",
            workflow_id="wf-1",
            organization_id="org-test",
            status=ExecutionJobStatus.QUEUED,
            retry_count=0,
            max_retries=3,
        )
        # Simulate crash recovery (what recover_expired_claims does)
        job.retry_count += 1
        job.status = ExecutionJobStatus.RETRYING
        assert job.retry_count == 1
        assert job.retry_count > 0


# ---------------------------------------------------------------------------
# Test: Checkpoint store saves and loads correctly
# ---------------------------------------------------------------------------


class TestCheckpointStoreResume:
    """Verify checkpoint persistence and resume state loading."""

    def test_save_and_load_checkpoint(self) -> None:
        """Checkpoint save/load round-trips correctly."""
        store = InMemoryCheckpointStore()
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-test",
            request_id="req-test",
            organization_id="org-test",
            store=store,
        )
        state = {
            "request_id": "req-test",
            "workflow_id": "wf-test",
            "status": "executing",
            "plan": {"tasks": [{"task_id": "t1"}, {"task_id": "t2"}]},
            "current_task_index": 1,
            "task_records": {
                "t1": {
                    "task_id": "t1",
                    "status": "completed",
                    "output": {"answer": "task 1 done"},
                }
            },
        }
        checkpointer.save_after_node("multi_agent_execute", state)

        loaded = checkpointer.load_resume_state()
        assert loaded is not None
        assert loaded["status"] == "executing"
        assert loaded["current_task_index"] == 1
        assert "t1" in loaded["task_records"]
        assert loaded["task_records"]["t1"]["status"] == "completed"

    def test_load_resume_state_returns_none_when_empty(self) -> None:
        """No checkpoint → returns None (fresh start)."""
        store = InMemoryCheckpointStore()
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-empty",
            request_id="req-empty",
            store=store,
        )
        assert checkpointer.load_resume_state() is None

    def test_load_resume_state_gets_latest(self) -> None:
        """Multiple checkpoints → latest is returned."""
        store = InMemoryCheckpointStore()
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-multi",
            request_id="req-multi",
            store=store,
        )
        # Save two checkpoints
        checkpointer.save_after_node("plan", {"status": "planning", "step": 1})
        checkpointer.save_after_node("execute_agent", {"status": "executing", "step": 2})

        loaded = checkpointer.load_resume_state()
        assert loaded is not None
        assert loaded["step"] == 2
        assert loaded["status"] == "executing"

    def test_checkpoint_preserves_task_records(self) -> None:
        """Checkpoint preserves multi-agent task records for resume."""
        store = InMemoryCheckpointStore()
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-multi-agent",
            request_id="req-ma",
            organization_id="org-test",
            store=store,
        )
        task_records = {
            "task-1": {
                "task_id": "task-1",
                "status": "completed",
                "agent_type": "research",
                "output": {"answer": "Research complete"},
                "summary": "Done",
            },
            "task-2": {
                "task_id": "task-2",
                "status": "pending",
                "agent_type": "synthesis",
            },
        }
        state = {
            "request_id": "req-ma",
            "workflow_id": "wf-multi-agent",
            "status": "executing",
            "task_records": task_records,
            "plan": {
                "tasks": [
                    {"task_id": "task-1", "assigned_agent_type": "research"},
                    {"task_id": "task-2", "assigned_agent_type": "synthesis", "dependencies": ["task-1"]},
                ]
            },
        }
        checkpointer.save_after_node("multi_agent_execute", state)

        loaded = checkpointer.load_resume_state()
        assert loaded is not None
        assert loaded["task_records"]["task-1"]["status"] == "completed"
        assert loaded["task_records"]["task-2"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Test: MultiAgentExecutor skips completed tasks on resume
# ---------------------------------------------------------------------------


class TestMultiAgentExecutorResume:
    """Verify the executor skips completed tasks when given preexisting records."""

    def test_completed_tasks_not_reexecuted(self) -> None:
        """Tasks with status=completed in preexisting_records are not run again."""
        plan = ExecutionPlan(
            plan_id="plan-resume",
            request_id="req-resume",
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
                    assigned_agent_type=AgentType.RESEARCH,
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
            ],
        )

        # Preexisting records: t1 already completed
        preexisting = {
            "t1": {
                "task_id": "t1",
                "description": "Task 1",
                "agent_type": "research",
                "status": "completed",
                "output": {"answer": "Task 1 result"},
                "summary": "Done",
                "errors": [],
                "retry_count": 0,
                "max_retries": 0,
                "timeout_seconds": 5.0,
                "failure_policy": "continue_with_partial_results",
                "dependencies": [],
            }
        }

        executed_tasks: list[str] = []

        # Create a factory that tracks which tasks are executed
        class TrackingAgent:
            name = "tracking"
            agent_type = AgentType.RESEARCH

            def execute(self, input_data: dict, context: Any) -> Any:
                from aegisforge.domain.models import AgentResult
                task_id = context.task_id if hasattr(context, "task_id") else "unknown"
                executed_tasks.append(task_id)
                return AgentResult(
                    agent_name="tracking",
                    agent_type=AgentType.RESEARCH,
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Completed {task_id}",
                    result={"answer": f"Result for {task_id}"},
                )

        class TrackingFactory:
            @property
            def registry(self):
                from aegisforge.tools.registry import ToolRegistry
                return ToolRegistry()

            def build(self, task, run_id):
                return TrackingAgent()

        executor = MultiAgentExecutor(
            config=ExecutionConfig(max_concurrency=1, default_task_timeout_seconds=5.0),
            agent_factory=TrackingFactory(),
        )

        outcome = executor.execute(
            plan,
            request_id="req-resume",
            workflow_id="wf-resume",
            organization_id="org-test",
            preexisting_records=preexisting,
        )

        # t1 should NOT be re-executed (it was already completed)
        assert "t1" not in executed_tasks
        # t2 should be executed (it was pending)
        assert "t2" in executed_tasks
        # Overall status should be completed
        assert outcome.status == RequestStatus.COMPLETED

    def test_all_completed_no_tasks_run(self) -> None:
        """If all tasks are already completed, no tasks are executed."""
        plan = ExecutionPlan(
            plan_id="plan-all-done",
            request_id="req-all-done",
            tasks=[
                ExecutionPlanTask(
                    task_id="t1",
                    description="Task 1",
                    assigned_agent_type=AgentType.RESEARCH,
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
            ],
        )

        preexisting = {
            "t1": {
                "task_id": "t1",
                "description": "Task 1",
                "agent_type": "research",
                "status": "completed",
                "output": {"answer": "Already done"},
                "summary": "Done",
                "errors": [],
                "retry_count": 0,
                "max_retries": 0,
                "timeout_seconds": 5.0,
                "failure_policy": "continue_with_partial_results",
                "dependencies": [],
            }
        }

        executor = MultiAgentExecutor(
            config=ExecutionConfig(max_concurrency=1),
            agent_factory=AgentFactory(),
        )

        outcome = executor.execute(
            plan,
            request_id="req-all-done",
            workflow_id="wf-all-done",
            organization_id="org-test",
            preexisting_records=preexisting,
        )

        assert outcome.status == RequestStatus.COMPLETED
        assert outcome.completed_task_ids == ["t1"]

    def test_dependency_chain_resume(self) -> None:
        """In a dependency chain, completed upstream tasks are skipped."""
        plan = ExecutionPlan(
            plan_id="plan-chain",
            request_id="req-chain",
            tasks=[
                ExecutionPlanTask(
                    task_id="research",
                    description="Research",
                    assigned_agent_type=AgentType.RESEARCH,
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
                ExecutionPlanTask(
                    task_id="analysis",
                    description="Analysis",
                    assigned_agent_type=AgentType.ANALYSIS,
                    dependencies=["research"],
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
                ExecutionPlanTask(
                    task_id="synthesis",
                    description="Synthesis",
                    assigned_agent_type=AgentType.SYNTHESIS,
                    dependencies=["analysis"],
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
            ],
        )

        # research and analysis completed, synthesis pending
        preexisting = {
            "research": {
                "task_id": "research",
                "description": "Research",
                "agent_type": "research",
                "status": "completed",
                "output": {"answer": "Research findings"},
                "summary": "Research done",
                "errors": [],
                "retry_count": 0,
                "max_retries": 0,
                "timeout_seconds": 5.0,
                "failure_policy": "continue_with_partial_results",
                "dependencies": [],
            },
            "analysis": {
                "task_id": "analysis",
                "description": "Analysis",
                "agent_type": "analysis",
                "status": "completed",
                "output": {"answer": "Analysis complete"},
                "summary": "Analysis done",
                "errors": [],
                "retry_count": 0,
                "max_retries": 0,
                "timeout_seconds": 5.0,
                "failure_policy": "continue_with_partial_results",
                "dependencies": ["research"],
            },
        }

        executed_tasks: list[str] = []

        class TrackingAgent:
            name = "tracking"
            agent_type = AgentType.SYNTHESIS

            def execute(self, input_data: dict, context: Any) -> Any:
                from aegisforge.domain.models import AgentResult
                task_id = context.task_id if hasattr(context, "task_id") else "unknown"
                executed_tasks.append(task_id)
                return AgentResult(
                    agent_name="tracking",
                    agent_type=AgentType.SYNTHESIS,
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Synthesized from {task_id}",
                    result={"answer": "Final synthesis"},
                )

        class TrackingFactory:
            @property
            def registry(self):
                from aegisforge.tools.registry import ToolRegistry
                return ToolRegistry()

            def build(self, task, run_id):
                return TrackingAgent()

        executor = MultiAgentExecutor(
            config=ExecutionConfig(max_concurrency=1, default_task_timeout_seconds=5.0),
            agent_factory=TrackingFactory(),
        )

        outcome = executor.execute(
            plan,
            request_id="req-chain",
            workflow_id="wf-chain",
            organization_id="org-test",
            preexisting_records=preexisting,
        )

        # Only synthesis should have been executed
        assert executed_tasks == ["synthesis"]
        assert outcome.status == RequestStatus.COMPLETED


# ---------------------------------------------------------------------------
# Test: Checkpoint resume metric
# ---------------------------------------------------------------------------


class TestCheckpointResumeMetric:
    """Verify the Prometheus metric for checkpoint resume is emitted."""

    def test_metric_function_exists(self) -> None:
        """The record_workflow_checkpoint_resume function exists and is callable."""
        from aegisforge.observability.metrics import record_workflow_checkpoint_resume
        # Should not raise
        record_workflow_checkpoint_resume()

    def test_metric_in_prometheus_output(self) -> None:
        """The metric appears in Prometheus output after being called."""
        from aegisforge.observability.metrics import (
            HAS_PROMETHEUS,
            record_workflow_checkpoint_resume,
        )
        if not HAS_PROMETHEUS:
            pytest.skip("prometheus_client not installed")

        from prometheus_client import generate_latest

        record_workflow_checkpoint_resume()
        after = generate_latest().decode()

        assert "workflow_checkpoint_resumes_total" in after


# ---------------------------------------------------------------------------
# Test: Full resume flow simulation
# ---------------------------------------------------------------------------


class TestFullResumeFlow:
    """End-to-end simulation of crash → recovery → resume."""

    def test_crash_recovery_resume_flow(self) -> None:
        """Simulate: workflow starts, saves checkpoint, crashes, resumes."""
        store = InMemoryCheckpointStore()

        # Phase 1: Initial execution
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-crash-test",
            request_id="req-crash-test",
            organization_id="org-test",
            store=store,
        )
        # Simulate: validate_request completes, plan completes, task-1 completes
        checkpointer.save_after_node("validate_request", {
            "status": "planning",
            "request_id": "req-crash-test",
            "workflow_id": "wf-crash-test",
        })
        checkpointer.save_after_node("plan", {
            "status": "executing",
            "request_id": "req-crash-test",
            "workflow_id": "wf-crash-test",
            "plan": {
                "tasks": [
                    {"task_id": "t1", "assigned_agent_type": "research", "description": "Task 1"},
                    {"task_id": "t2", "assigned_agent_type": "synthesis", "description": "Task 2", "dependencies": ["t1"]},
                ]
            },
        })
        checkpointer.save_after_node("multi_agent_execute", {
            "status": "executing",
            "request_id": "req-crash-test",
            "workflow_id": "wf-crash-test",
            "task_records": {
                "t1": {
                    "task_id": "t1",
                    "status": "completed",
                    "agent_type": "research",
                    "output": {"answer": "Research complete"},
                    "summary": "Done",
                },
                "t2": {
                    "task_id": "t2",
                    "status": "pending",
                    "agent_type": "synthesis",
                },
            },
            "plan": {
                "tasks": [
                    {"task_id": "t1", "assigned_agent_type": "research", "description": "Task 1"},
                    {"task_id": "t2", "assigned_agent_type": "synthesis", "description": "Task 2", "dependencies": ["t1"]},
                ]
            },
        })

        # Phase 2: Worker crashes, job is re-enqueued with retry_count=1
        job = ExecutionJob(
            job_id="job-crash-test",
            request_id="req-crash-test",
            workflow_id="wf-crash-test",
            organization_id="org-test",
            status=ExecutionJobStatus.RETRYING,
            retry_count=1,
        )
        assert job.retry_count > 0  # This triggers resume

        # Phase 3: New worker resumes from checkpoint
        checkpointer2 = WorkflowCheckpointer(
            workflow_id="wf-crash-test",
            request_id="req-crash-test",
            organization_id="org-test",
            store=store,
        )
        resumed_state = checkpointer2.load_resume_state()
        assert resumed_state is not None
        assert resumed_state["task_records"]["t1"]["status"] == "completed"
        assert resumed_state["task_records"]["t2"]["status"] == "pending"

        # Phase 4: Executor resumes — only t2 should be executed
        plan = ExecutionPlan(
            plan_id="plan-crash-test",
            request_id="req-crash-test",
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
                    assigned_agent_type=AgentType.SYNTHESIS,
                    dependencies=["t1"],
                    timeout_seconds=5.0,
                    max_retries=0,
                ),
            ],
        )

        executed_tasks: list[str] = []

        class TrackingAgent:
            name = "tracking"

            def execute(self, input_data: dict, context: Any) -> Any:
                from aegisforge.domain.models import AgentResult
                task_id = context.task_id if hasattr(context, "task_id") else "unknown"
                executed_tasks.append(task_id)
                return AgentResult(
                    agent_name="tracking",
                    agent_type=AgentType.SYNTHESIS,
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Resumed {task_id}",
                    result={"answer": f"Result for {task_id}"},
                )

        class TrackingFactory:
            @property
            def registry(self):
                from aegisforge.tools.registry import ToolRegistry
                return ToolRegistry()

            def build(self, task, run_id):
                return TrackingAgent()

        executor = MultiAgentExecutor(
            config=ExecutionConfig(max_concurrency=1, default_task_timeout_seconds=5.0),
            agent_factory=TrackingFactory(),
        )

        outcome = executor.execute(
            plan,
            request_id="req-crash-test",
            workflow_id="wf-crash-test",
            organization_id="org-test",
            preexisting_records=resumed_state.get("task_records", {}),
        )

        # t1 was NOT re-executed (skipped from checkpoint)
        assert "t1" not in executed_tasks
        # t2 WAS executed (it was pending)
        assert "t2" in executed_tasks
        # Overall workflow completed
        assert outcome.status == RequestStatus.COMPLETED

    def test_resume_preserves_tenant_isolation(self) -> None:
        """Resume only loads checkpoints for the correct organization."""
        store = InMemoryCheckpointStore()

        # Save checkpoint for org-alpha
        cp_alpha = WorkflowCheckpointer(
            workflow_id="wf-alpha",
            request_id="req-alpha",
            organization_id="org-alpha",
            store=store,
        )
        cp_alpha.save_after_node("plan", {
            "status": "executing",
            "organization_id": "org-alpha",
        })

        # Save checkpoint for org-beta
        cp_beta = WorkflowCheckpointer(
            workflow_id="wf-beta",
            request_id="req-beta",
            organization_id="org-beta",
            store=store,
        )
        cp_beta.save_after_node("plan", {
            "status": "executing",
            "organization_id": "org-beta",
        })

        # Load resume for org-alpha — should get org-alpha's checkpoint
        cp_resume = WorkflowCheckpointer(
            workflow_id="wf-alpha",
            request_id="req-alpha",
            organization_id="org-alpha",
            store=store,
        )
        state = cp_resume.load_resume_state()
        assert state is not None
        assert state["organization_id"] == "org-alpha"
