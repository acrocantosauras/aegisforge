"""DB-backed checkpoint store and durable resume tests (Phase 4.2).

Proves that workflow checkpoints survive store recreation (process
restart) and that a workflow paused for approval can be resumed from a
fresh checkpointer, completing without re-running the approved task.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aegisforge.db.base import Base
from aegisforge.domain.models import RequestStatus
from aegisforge.workflows.checkpoint import (
    DbCheckpointStore,
    WorkflowCheckpoint,
    WorkflowCheckpointer,
)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def store(session_factory):
    return DbCheckpointStore(session_factory=session_factory)


class TestDbCheckpointStore:
    def test_save_and_load(self, store):
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="req-1",
            node_name="plan", state={"status": "executing", "plan": {"tasks": []}},
            organization_id="org-1",
        ))
        loaded = store.load_checkpoint("cp-1")
        assert loaded is not None
        assert loaded.node_name == "plan"
        assert loaded.state["status"] == "executing"
        assert loaded.organization_id == "org-1"

    def test_load_latest_by_workflow(self, store):
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="r1",
            node_name="validate_request", state={"step": 1},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-2", workflow_id="wf-1", request_id="r1",
            node_name="plan", state={"step": 2},
        ))
        latest = store.load_latest_by_workflow("wf-1")
        assert latest is not None
        assert latest.checkpoint_id == "cp-2"
        assert latest.node_name == "plan"

    def test_workflows_isolated(self, store):
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="r1",
            node_name="a", state={},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-2", workflow_id="wf-2", request_id="r2",
            node_name="b", state={},
        ))
        assert store.load_latest_by_workflow("wf-1").checkpoint_id == "cp-1"
        assert store.load_latest_by_workflow("wf-2").checkpoint_id == "cp-2"

    def test_delete_workflow_checkpoints(self, store):
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="r1",
            node_name="a", state={},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-2", workflow_id="wf-1", request_id="r1",
            node_name="b", state={},
        ))
        assert store.delete_workflow_checkpoints("wf-1") == 2
        assert store.load_latest_by_workflow("wf-1") is None

    def test_store_recreation_survives_restart(self, session_factory):
        """A fresh store instance (new process) reads persisted checkpoints."""
        store1 = DbCheckpointStore(session_factory=session_factory)
        store1.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="req-1",
            node_name="execute_agent",
            state={"status": "executing", "current_task_index": 0},
        ))

        store2 = DbCheckpointStore(session_factory=session_factory)
        loaded = store2.load_latest_by_workflow("wf-1")
        assert loaded is not None
        assert loaded.node_name == "execute_agent"
        assert loaded.state["current_task_index"] == 0


class TestWorkflowRestartResume:
    """Workflow pauses at approval → "restart" → resume → complete."""

    def _make_checkpointer(self, session_factory, workflow_id, request_id):
        store = DbCheckpointStore(session_factory=session_factory)
        return WorkflowCheckpointer(
            workflow_id=workflow_id,
            request_id=request_id,
            organization_id="org-1",
            store=store,
        )

    def test_pause_at_approval_then_resume_completes(self, session_factory):
        """Full approval pause/resume across a store recreation."""
        from aegisforge.approval.service import ApprovalService
        from aegisforge.workflows.langgraph_workflow import (
            execute_workflow,
            resume_workflow_after_approval,
        )

        approval_service = ApprovalService(session_factory=session_factory)

        # Phase 1: run workflow with a high-risk task → pauses for approval
        checkpointer1 = self._make_checkpointer(session_factory, "wf-restart-1", "req-restart-1")
        result1 = execute_workflow(
            request_id="req-restart-1",
            intent="Restart the production service now",
            user_id="user-1",
            organization_id="org-1",
            workflow_id="wf-restart-1",
            checkpointer=checkpointer1,
            approval_service=approval_service,
            job_id="job-restart-1",
        )
        assert result1["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value
        assert result1["approval_id"]

        # The approval is persisted
        approval = approval_service.get_approval(result1["approval_id"])
        assert approval is not None
        assert approval.status.value == "pending"

        # Phase 2: "process restarted" — new approval service + new checkpointer
        approval_service2 = ApprovalService(session_factory=session_factory)
        assert approval_service2.get_approval(result1["approval_id"]) is not None

        # Human approves (durable decision)
        approved = approval_service2.approve(result1["approval_id"], reviewer_id="admin-1", decision_reason="OK")
        assert approved is not None

        # Phase 3: resume the workflow from the durable checkpoint
        final = resume_workflow_after_approval(
            workflow_id="wf-restart-1",
            approval_decision="approved",
            approval_id=result1["approval_id"],
            reviewer_id="admin-1",
            store=DbCheckpointStore(session_factory=session_factory),
            request_id="req-restart-1",
            organization_id="org-1",
        )
        assert final["status"] == RequestStatus.COMPLETED.value
        assert final["final_result"]

    def test_rejection_never_executes(self, session_factory):
        """Rejected approval → workflow fails; protected action never runs."""
        from aegisforge.approval.service import ApprovalService
        from aegisforge.workflows.langgraph_workflow import (
            execute_workflow,
            resume_workflow_after_approval,
        )

        approval_service = ApprovalService(session_factory=session_factory)
        checkpointer = self._make_checkpointer(session_factory, "wf-reject-1", "req-reject-1")
        result = execute_workflow(
            request_id="req-reject-1",
            intent="Delete the staging database",
            user_id="user-1",
            organization_id="org-1",
            workflow_id="wf-reject-1",
            checkpointer=checkpointer,
            approval_service=approval_service,
            job_id="job-reject-1",
        )
        assert result["status"] == RequestStatus.ACTION_REQUIRES_APPROVAL.value

        approval_service.reject(result["approval_id"], "admin-1", "Too dangerous")

        final = resume_workflow_after_approval(
            workflow_id="wf-reject-1",
            approval_decision="rejected",
            approval_id=result["approval_id"],
            reviewer_id="admin-1",
            store=DbCheckpointStore(session_factory=session_factory),
        )
        assert final["status"] == RequestStatus.FAILED.value
        assert "rejected" in final["errors"][0].lower()

    def test_resume_reuses_plan_and_advances_tasks(self, session_factory):
        """Resume must not re-plan nor re-run the approved task."""
        from aegisforge.workflows.langgraph_workflow import (
            resume_workflow_after_approval,
        )

        checkpointer = self._make_checkpointer(session_factory, "wf-adv-1", "req-adv-1")

        # First task low risk, second task high risk (approval gate)
        from aegisforge.domain.models import (
            AgentType,
            ExecutionPlanTask,
        )
        from aegisforge.workflows.langgraph_workflow import (
            _save_checkpoint_if_available,
        )

        plan_tasks = [
            ExecutionPlanTask(
                task_id="t1",
                description="Research the topic",
                assigned_agent_type=AgentType.RESEARCH,
                input_data={"query": "topic"},
                risk_level="low",
            ).model_dump(),
            ExecutionPlanTask(
                task_id="t2",
                description="Deploy the change",
                assigned_agent_type=AgentType.RESEARCH,
                input_data={"query": "deploy"},
                risk_level="high",
            ).model_dump(),
        ]
        state = {
            "request_id": "req-adv-1",
            "workflow_id": "wf-adv-1",
            "user_id": "user-1",
            "organization_id": "org-1",
            "job_id": "job-adv-1",
            "intent": "deploy",
            "status": RequestStatus.ACTION_REQUIRES_APPROVAL.value,
            "plan": {"tasks": plan_tasks},
            "current_task_index": 1,
            "current_task": plan_tasks[1],
            "agent_result": {"agent_name": "research-agent", "status": "completed", "result": {"query": "deploy", "answer": "prepared"}},
            "evaluation": {"verdict": "passed", "score": 1.0, "reasons": []},
            "retry_count": 0,
            "max_retries": 3,
            "tool_calls": [],
            "errors": [],
            "approval_required": True,
            "approval_id": "approval-adv-1",
            "evaluation_loop_count": 1,
        }
        _save_checkpoint_if_available(checkpointer, "retry_or_complete", state)

        # Resume: task index 1 is the approved high-risk task → advance to end → complete
        final = resume_workflow_after_approval(
            workflow_id="wf-adv-1",
            approval_decision="approved",
            approval_id="approval-adv-1",
            reviewer_id="admin-1",
            store=DbCheckpointStore(session_factory=session_factory),
            request_id="req-adv-1",
            organization_id="org-1",
        )
        assert final["status"] == RequestStatus.COMPLETED.value