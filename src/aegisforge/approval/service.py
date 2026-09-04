"""Human approval service for AegisForge.

Manages approval requests, decisions, and workflow integration.
Authorization is enforced server-side.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from aegisforge.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)

logger = logging.getLogger(__name__)


class ApprovalService:
    """Manages human approval requests and decisions."""

    def __init__(self, approval_timeout_hours: int = 24) -> None:
        self._approvals: dict[str, ApprovalRequest] = {}
        self._timeout_hours = approval_timeout_hours

    def create_approval_request(
        self,
        job_id: str,
        request_id: str,
        workflow_id: str,
        action_description: str,
        requested_by: str,
        organization_id: str = "",
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        reason: str = "",
    ) -> ApprovalRequest:
        """Create a new approval request."""
        approval = ApprovalRequest(
            approval_id=f"approval-{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            request_id=request_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            action_description=action_description,
            requested_by=requested_by,
            risk_level=risk_level,
            status=ApprovalStatus.PENDING,
            reason=reason,
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=self._timeout_hours),
        )

        self._approvals[approval.approval_id] = approval

        logger.info(
            "Created approval request %s for job %s (risk: %s)",
            approval.approval_id,
            job_id,
            risk_level.value,
        )
        return approval

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        """Get an approval request by ID."""
        return self._approvals.get(approval_id)

    def get_pending_approvals(
        self,
        organization_id: str = "",
        reviewer_id: str | None = None,
    ) -> list[ApprovalRequest]:
        """Get all pending approval requests."""
        results = []
        for approval in self._approvals.values():
            if approval.status != ApprovalStatus.PENDING:
                continue
            # F14: Tenant isolation — filter by organization_id
            if organization_id and approval.organization_id and approval.organization_id != organization_id:
                continue
            results.append(approval)
        return results

    def approve(
        self,
        approval_id: str,
        reviewer_id: str,
        decision_reason: str = "",
    ) -> ApprovalRequest | None:
        """Approve an approval request.

        Only authorized users can approve.
        """
        approval = self._approvals.get(approval_id)
        if approval is None:
            logger.warning("Approval %s not found", approval_id)
            return None

        if approval.status != ApprovalStatus.PENDING:
            logger.warning(
                "Cannot approve %s: current status is %s",
                approval_id,
                approval.status.value,
            )
            return None

        # Check expiry
        if approval.expires_at and datetime.now(UTC) > approval.expires_at:
            approval.status = ApprovalStatus.EXPIRED
            logger.warning("Approval %s has expired", approval_id)
            return None

        # The reviewer cannot be the requester (unless admin — handled at API layer)
        # For now, allow self-approval but log it
        if approval.requested_by == reviewer_id:
            logger.info("Self-approval detected for %s by %s", approval_id, reviewer_id)

        approval.status = ApprovalStatus.APPROVED
        approval.reviewer_id = reviewer_id
        approval.decision_reason = decision_reason
        approval.decided_at = datetime.now(UTC)

        logger.info(
            "Approval %s approved by %s",
            approval_id,
            reviewer_id,
        )
        return approval

    def reject(
        self,
        approval_id: str,
        reviewer_id: str,
        decision_reason: str = "",
    ) -> ApprovalRequest | None:
        """Reject an approval request."""
        approval = self._approvals.get(approval_id)
        if approval is None:
            return None

        if approval.status != ApprovalStatus.PENDING:
            return None

        approval.status = ApprovalStatus.REJECTED
        approval.reviewer_id = reviewer_id
        approval.decision_reason = decision_reason
        approval.decided_at = datetime.now(UTC)

        logger.info(
            "Approval %s rejected by %s (reason: %s)",
            approval_id,
            reviewer_id,
            decision_reason,
        )
        return approval

    def cancel(self, approval_id: str) -> ApprovalRequest | None:
        """Cancel an approval request (e.g., when the job is cancelled)."""
        approval = self._approvals.get(approval_id)
        if approval is None:
            return None

        if approval.status != ApprovalStatus.PENDING:
            return None

        approval.status = ApprovalStatus.CANCELLED
        approval.decided_at = datetime.now(UTC)
        return approval

    def check_expired(self) -> list[ApprovalRequest]:
        """Check for and expire any overdue approval requests."""
        expired: list[ApprovalRequest] = []
        now = datetime.now(UTC)

        for approval in self._approvals.values():
            if approval.status == ApprovalStatus.PENDING and approval.expires_at:
                if now > approval.expires_at:
                    approval.status = ApprovalStatus.EXPIRED
                    expired.append(approval)

        return expired


# Safe action definitions for simulation
SAFE_ACTIONS: dict[str, dict[str, Any]] = {
    "simulated_service_restart": {
        "description": "Simulated restart of a service (no real action taken)",
        "risk_level": "low",
    },
    "simulated_config_change": {
        "description": "Simulated configuration change (no real action taken)",
        "risk_level": "medium",
    },
    "simulated_workflow_operation": {
        "description": "Simulated workflow operation (no real action taken)",
        "risk_level": "medium",
    },
}


def is_approval_required(risk_level: RiskLevel, required_levels: str = "high,critical") -> bool:
    """Determine if a given risk level requires approval."""
    required = {level.strip().lower() for level in required_levels.split(",")}
    return risk_level.value in required


def execute_safe_action(action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Execute a safe/simulated action.

    No real-world effects. Architecture should later support real actions
    only behind explicit permissions and approval.
    """
    action_def = SAFE_ACTIONS.get(action_type)
    if action_def is None:
        return {
            "status": "failed",
            "error": f"Unknown safe action type: {action_type}",
        }

    return {
        "status": "completed",
        "action_type": action_type,
        "description": action_def["description"],
        "parameters": parameters,
        "real_world_effect": False,
        "note": "This is a simulated action with no real-world effects.",
    }
