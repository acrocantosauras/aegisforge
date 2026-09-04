"""Tests for LangGraph checkpointing and approval pause/resume.

Tests:
- Checkpoint save/load
- Workflow resumption from checkpoint
- Approval pause and resume
- Sanitization of checkpoint state
- Recovery from failure
"""
from __future__ import annotations

import uuid

from aegisforge.approval.service import ApprovalService, is_approval_required
from aegisforge.domain.models import (
    ApprovalStatus,
    RiskLevel,
    RequestStatus,
)
from aegisforge.workflows.checkpoint import (
    InMemoryCheckpointStore,
    WorkflowCheckpointer,
    WorkflowCheckpoint,
    _sanitize_state,
)


class TestCheckpointStore:
    """Test the InMemoryCheckpointStore."""

    def test_save_and_load(self):
        store = InMemoryCheckpointStore()
        cp = WorkflowCheckpoint(
            checkpoint_id="cp-1",
            workflow_id="wf-1",
            request_id="req-1",
            node_name="plan",
            state={"status": "planning", "intent": "test"},
        )
        store.save_checkpoint(cp)
        loaded = store.load_checkpoint("cp-1")
        assert loaded is not None
        assert loaded.node_name == "plan"
        assert loaded.state["intent"] == "test"

    def test_load_nonexistent(self):
        store = InMemoryCheckpointStore()
        assert store.load_checkpoint("cp-nonexistent") is None

    def test_load_latest_by_workflow(self):
        store = InMemoryCheckpointStore()
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1",
            workflow_id="wf-1",
            request_id="req-1",
            node_name="validate_request",
            state={"step": 1},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-2",
            workflow_id="wf-1",
            request_id="req-1",
            node_name="plan",
            state={"step": 2},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-3",
            workflow_id="wf-other",
            request_id="req-2",
            node_name="execute_agent",
            state={"step": 3},
        ))

        latest = store.load_latest_by_workflow("wf-1")
        assert latest is not None
        assert latest.checkpoint_id == "cp-2"
        assert latest.node_name == "plan"

    def test_list_checkpoints(self):
        store = InMemoryCheckpointStore()
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="r-1",
            node_name="a", state={},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-2", workflow_id="wf-1", request_id="r-1",
            node_name="b", state={},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-3", workflow_id="wf-2", request_id="r-2",
            node_name="c", state={},
        ))

        cps = store.list_checkpoints("wf-1")
        assert len(cps) == 2

    def test_delete_workflow_checkpoints(self):
        store = InMemoryCheckpointStore()
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-1", workflow_id="wf-1", request_id="r-1",
            node_name="a", state={},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-2", workflow_id="wf-1", request_id="r-1",
            node_name="b", state={},
        ))
        store.save_checkpoint(WorkflowCheckpoint(
            checkpoint_id="cp-3", workflow_id="wf-2", request_id="r-2",
            node_name="c", state={},
        ))

        count = store.delete_workflow_checkpoints("wf-1")
        assert count == 2
        assert store.load_checkpoint("cp-1") is None
        assert store.load_checkpoint("cp-3") is not None


class TestWorkflowCheckpointer:
    """Test the WorkflowCheckpointer."""

    def test_save_and_resume(self):
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-test",
            request_id="req-test",
            organization_id="org-1",
        )
        state = {"status": "planning", "intent": "test query", "plan": {}}
        cp_id = checkpointer.save_after_node("validate_request", state)
        assert cp_id.startswith("cp-")

        resumed = checkpointer.load_resume_state()
        assert resumed is not None
        assert resumed["status"] == "planning"
        assert resumed["intent"] == "test query"

    def test_get_current_node(self):
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-test",
            request_id="req-test",
        )
        assert checkpointer.get_current_node() is None

        checkpointer.save_after_node("plan", {"status": "planning"})
        assert checkpointer.get_current_node() == "plan"

        checkpointer.save_after_node("execute_agent", {"status": "executing"})
        assert checkpointer.get_current_node() == "execute_agent"

    def test_multiple_workflows_isolated(self):
        cp1 = WorkflowCheckpointer(
            workflow_id="wf-1", request_id="r-1",
        )
        cp2 = WorkflowCheckpointer(
            workflow_id="wf-2", request_id="r-2",
        )

        cp1.save_after_node("plan", {"intent": "query1"})
        cp2.save_after_node("execute_agent", {"intent": "query2"})

        assert cp1.get_current_node() == "plan"
        assert cp2.get_current_node() == "execute_agent"

    def test_clear_checkpoints(self):
        checkpointer = WorkflowCheckpointer(
            workflow_id="wf-test", request_id="req-test",
        )
        checkpointer.save_after_node("a", {"step": 1})
        checkpointer.save_after_node("b", {"step": 2})

        count = checkpointer.clear_checkpoints()
        assert count == 2
        assert checkpointer.load_resume_state() is None

    def test_execution_id_unique(self):
        cp1 = WorkflowCheckpointer(workflow_id="wf-1", request_id="r-1")
        cp2 = WorkflowCheckpointer(workflow_id="wf-2", request_id="r-2")
        assert cp1.execution_id != cp2.execution_id


class TestSanitizeState:
    """Test checkpoint state sanitization."""

    def test_removes_sensitive_keys(self):
        state = {
            "status": "planning",
            "intent": "test",
            "api_key": "sk-secret-key",
            "password": "hunter2",
            "token": "abc123",
            "secret": "hidden",
        }
        sanitized = _sanitize_state(state)
        assert "api_key" not in sanitized
        assert "password" not in sanitized
        assert "token" not in sanitized
        assert "secret" not in sanitized
        assert sanitized["status"] == "planning"
        assert sanitized["intent"] == "test"

    def test_preserves_safe_keys(self):
        state = {
            "request_id": "req-1",
            "workflow_id": "wf-1",
            "status": "executing",
            "plan": {"tasks": []},
            "tool_calls": [],
            "errors": [],
        }
        sanitized = _sanitize_state(state)
        assert sanitized["request_id"] == "req-1"
        assert sanitized["workflow_id"] == "wf-1"


class TestApprovalIntegration:
    """Test approval pause/resume workflow integration."""

    def test_approval_required_high_risk(self):
        assert is_approval_required(RiskLevel.HIGH) is True
        assert is_approval_required(RiskLevel.CRITICAL) is True

    def test_approval_not_required_low_risk(self):
        assert is_approval_required(RiskLevel.LOW) is False
        assert is_approval_required(RiskLevel.MEDIUM) is False

    def test_approval_service_lifecycle(self):
        service = ApprovalService()

        # Create
        approval = service.create_approval_request(
            job_id="job-1",
            request_id="req-1",
            workflow_id="wf-1",
            action_description="Deploy config change",
            requested_by="user-1",
            organization_id="org-1",
            risk_level=RiskLevel.HIGH,
        )
        assert approval.status == ApprovalStatus.PENDING

        # Get
        fetched = service.get_approval(approval.approval_id)
        assert fetched is not None
        assert fetched.approval_id == approval.approval_id

        # Approve
        result = service.approve(
            approval.approval_id,
            reviewer_id="admin-1",
            decision_reason="Approved for testing",
        )
        assert result is not None
        assert result.status == ApprovalStatus.APPROVED
        assert result.reviewer_id == "admin-1"

        # Cannot re-approve
        result2 = service.approve(approval.approval_id, reviewer_id="admin-2")
        assert result2 is None

    def test_approval_rejection(self):
        service = ApprovalService()

        approval = service.create_approval_request(
            job_id="job-2",
            request_id="req-2",
            workflow_id="wf-2",
            action_description="Destructive action",
            requested_by="user-1",
            risk_level=RiskLevel.CRITICAL,
        )

        result = service.reject(
            approval.approval_id,
            reviewer_id="admin-1",
            decision_reason="Too risky",
        )
        assert result is not None
        assert result.status == ApprovalStatus.REJECTED

    def test_approval_expiry(self):
        from datetime import UTC, datetime, timedelta

        service = ApprovalService(approval_timeout_hours=-1)  # Already expired

        approval = service.create_approval_request(
            job_id="job-3",
            request_id="req-3",
            workflow_id="wf-3",
            action_description="Expiring action",
            requested_by="user-1",
            risk_level=RiskLevel.HIGH,
        )

        # Should be expired
        result = service.approve(
            approval.approval_id,
            reviewer_id="admin-1",
        )
        assert result is None
        assert service.get_approval(approval.approval_id).status == ApprovalStatus.EXPIRED

    def test_approval_not_found(self):
        service = ApprovalService()
        assert service.approve("nonexistent", "admin-1") is None
        assert service.reject("nonexistent", "admin-1") is None
