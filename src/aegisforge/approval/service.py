"""Human approval service for AegisForge.

Manages approval requests, decisions, and workflow integration.
Authorization is enforced server-side.

The production service is backed by PostgreSQL via SQLAlchemy
(``ApprovalRequestModel``).  When constructed without a ``session_factory``
the service uses an in-memory dictionary — this path exists ONLY as a test
double for unit tests and is never used by the API, worker, or workflow.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aegisforge.db.models import ApprovalRequestModel
from aegisforge.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)
from aegisforge.observability.metrics import record_approval_event

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware (SQLite returns naive)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _model_to_domain(model: ApprovalRequestModel) -> ApprovalRequest:
    """Convert a DB model to the domain ApprovalRequest."""
    created_at = _as_utc(model.created_at)
    assert created_at is not None  # non-nullable column
    return ApprovalRequest(
        approval_id=model.id,
        job_id=model.job_id,
        request_id=model.request_id,
        workflow_id=model.workflow_id,
        organization_id=model.organization_id,
        action_description=model.action_description,
        requested_by=model.requested_by,
        reviewer_id=model.reviewer_id,
        risk_level=RiskLevel(model.risk_level),
        status=ApprovalStatus(model.status),
        reason=model.reason,
        decision_reason=model.decision_reason,
        created_at=created_at,
        decided_at=_as_utc(model.decided_at),
        expires_at=_as_utc(model.expires_at),
    )


class ApprovalService:
    """Manages human approval requests and decisions.

    ``session_factory`` is a zero-argument callable returning a SQLAlchemy
    Session.  When ``None``, an in-memory store is used (tests only).
    """

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        approval_timeout_hours: int = 24,
    ) -> None:
        self._session_factory = session_factory
        self._timeout_hours = approval_timeout_hours
        # In-memory test double storage — never used by production paths.
        self._approvals: dict[str, ApprovalRequest] = {}
        self._db_backed = session_factory is not None

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    def _session(self) -> Any:
        if self._session_factory is None:
            raise RuntimeError("ApprovalService has no session factory (in-memory test double)")
        return self._session_factory()

    def _query_approval(self, approval_id: str, organization_id: str = "", for_update: bool = False) -> ApprovalRequestModel | None:
        """Load an approval model by id with optional tenant + row-lock."""
        session = self._session()
        try:
            stmt = select(ApprovalRequestModel).where(ApprovalRequestModel.id == approval_id)
            if organization_id:
                stmt = stmt.where(ApprovalRequestModel.organization_id == organization_id)
            if for_update:
                stmt = stmt.with_for_update()
            return session.execute(stmt).scalar_one_or_none()
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

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
        if not self._db_backed:
            return self._create_in_memory(
                job_id=job_id,
                request_id=request_id,
                workflow_id=workflow_id,
                action_description=action_description,
                requested_by=requested_by,
                organization_id=organization_id,
                risk_level=risk_level,
                reason=reason,
            )

        now = datetime.now(UTC)
        approval_id = f"approval-{uuid.uuid4().hex[:12]}"
        session = self._session()
        try:
            model = ApprovalRequestModel(
                id=approval_id,
                job_id=job_id,
                request_id=request_id,
                workflow_id=workflow_id,
                organization_id=organization_id,
                action_description=action_description,
                requested_by=requested_by,
                risk_level=risk_level.value,
                status=ApprovalStatus.PENDING.value,
                reason=reason,
                created_at=now,
                expires_at=now + timedelta(hours=self._timeout_hours),
            )
            session.add(model)
            session.commit()
            session.refresh(model)
        finally:
            session.close()

        record_approval_event(risk_level.value, "requested")

        logger.info(
            "Created approval request %s for job %s (risk: %s)",
            approval_id,
            job_id,
            risk_level.value,
        )
        return _model_to_domain(model)

    def _create_in_memory(
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
        """In-memory (test double) approval creation."""
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
        record_approval_event(risk_level.value, "requested")
        logger.info(
            "Created approval request %s for job %s (risk: %s)",
            approval.approval_id,
            job_id,
            risk_level.value,
        )
        return approval

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        """Get an approval request by ID."""
        if not self._db_backed:
            return self._approvals.get(approval_id)
        model = self._query_approval(approval_id)
        if model is None:
            return None
        return _model_to_domain(model)

    def get_pending_approvals(
        self,
        organization_id: str = "",
        reviewer_id: str | None = None,
    ) -> list[ApprovalRequest]:
        """Get all pending approval requests."""
        if not self._db_backed:
            results = []
            for approval in self._approvals.values():
                if approval.status != ApprovalStatus.PENDING:
                    continue
                if organization_id and approval.organization_id and approval.organization_id != organization_id:
                    continue
                results.append(approval)
            return results

        session = self._session()
        try:
            stmt = select(ApprovalRequestModel).where(
                ApprovalRequestModel.status == ApprovalStatus.PENDING.value
            )
            if organization_id:
                stmt = stmt.where(ApprovalRequestModel.organization_id == organization_id)
            stmt = stmt.order_by(ApprovalRequestModel.created_at.asc())
            models = session.execute(stmt).scalars().all()
        finally:
            session.close()
        return [_model_to_domain(m) for m in models]

    def list_approvals(
        self,
        organization_id: str = "",
        limit: int = 50,
    ) -> list[ApprovalRequest]:
        """List recent approvals (any status) for an organization."""
        if not self._db_backed:
            results = [
                a
                for a in self._approvals.values()
                if not organization_id or not a.organization_id or a.organization_id == organization_id
            ]
            results.sort(key=lambda a: a.created_at, reverse=True)
            return results[:limit]

        session = self._session()
        try:
            stmt = select(ApprovalRequestModel)
            if organization_id:
                stmt = stmt.where(ApprovalRequestModel.organization_id == organization_id)
            stmt = stmt.order_by(ApprovalRequestModel.created_at.desc()).limit(limit)
            models = session.execute(stmt).scalars().all()
        finally:
            session.close()
        return [_model_to_domain(m) for m in models]

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def approve(
        self,
        approval_id: str,
        reviewer_id: str,
        decision_reason: str = "",
    ) -> ApprovalRequest | None:
        """Approve an approval request.

        Only authorized users can approve.  Duplicate decisions are
        prevented: only PENDING approvals can be decided, and the pending
        check runs inside a row lock on PostgreSQL.
        """
        if not self._db_backed:
            return self._approve_in_memory(approval_id, reviewer_id, decision_reason)

        session = self._session()
        try:
            stmt = (
                select(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == approval_id)
                .with_for_update()
            )
            model = session.execute(stmt).scalar_one_or_none()
            if model is None:
                logger.warning("Approval %s not found", approval_id)
                return None

            if model.status != ApprovalStatus.PENDING.value:
                logger.warning(
                    "Cannot approve %s: current status is %s",
                    approval_id,
                    model.status,
                )
                return None

            # Check expiry
            expires_at = _as_utc(model.expires_at)
            created_at = _as_utc(model.created_at)
            if expires_at is not None and datetime.now(UTC) > expires_at:
                model.status = ApprovalStatus.EXPIRED.value
                model.decided_at = datetime.now(UTC)
                session.commit()
                record_approval_event(
                    model.risk_level,
                    "expired",
                    wait_seconds=(
                        datetime.now(UTC) - (created_at if created_at is not None else datetime.now(UTC))
                    ).total_seconds(),
                )
                logger.warning("Approval %s has expired", approval_id)
                return None

            if model.requested_by == reviewer_id:
                logger.info("Self-approval detected for %s by %s", approval_id, reviewer_id)

            model.status = ApprovalStatus.APPROVED.value
            model.reviewer_id = reviewer_id
            model.decision_reason = decision_reason
            model.decided_at = datetime.now(UTC)
            session.commit()
            session.refresh(model)
        finally:
            session.close()

        decided_at = _as_utc(model.decided_at)
        created_at = _as_utc(model.created_at)
        record_approval_event(
            model.risk_level,
            "approved",
            wait_seconds=(
                (decided_at if decided_at is not None else datetime.now(UTC))
                - (created_at if created_at is not None else datetime.now(UTC))
            ).total_seconds(),
        )
        logger.info("Approval %s approved by %s", approval_id, reviewer_id)
        return _model_to_domain(model)

    def _approve_in_memory(self, approval_id: str, reviewer_id: str, decision_reason: str = "") -> ApprovalRequest | None:
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
            approval.decided_at = datetime.now(UTC)
            record_approval_event(
                approval.risk_level.value,
                "expired",
                wait_seconds=(approval.decided_at - approval.created_at).total_seconds(),
            )
            logger.warning("Approval %s has expired", approval_id)
            return None

        if approval.requested_by == reviewer_id:
            logger.info("Self-approval detected for %s by %s", approval_id, reviewer_id)

        approval.status = ApprovalStatus.APPROVED
        approval.reviewer_id = reviewer_id
        approval.decision_reason = decision_reason
        approval.decided_at = datetime.now(UTC)
        record_approval_event(
            approval.risk_level.value,
            "approved",
            wait_seconds=(approval.decided_at - approval.created_at).total_seconds(),
        )
        logger.info("Approval %s approved by %s", approval_id, reviewer_id)
        return approval

    def reject(
        self,
        approval_id: str,
        reviewer_id: str,
        decision_reason: str = "",
    ) -> ApprovalRequest | None:
        """Reject an approval request."""
        if not self._db_backed:
            return self._reject_in_memory(approval_id, reviewer_id, decision_reason)

        session = self._session()
        try:
            stmt = (
                select(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == approval_id)
                .with_for_update()
            )
            model = session.execute(stmt).scalar_one_or_none()
            if model is None:
                return None
            if model.status != ApprovalStatus.PENDING.value:
                return None

            model.status = ApprovalStatus.REJECTED.value
            model.reviewer_id = reviewer_id
            model.decision_reason = decision_reason
            model.decided_at = datetime.now(UTC)
            session.commit()
            session.refresh(model)
        finally:
            session.close()

        decided_at = _as_utc(model.decided_at)
        created_at = _as_utc(model.created_at)
        record_approval_event(
            model.risk_level,
            "rejected",
            wait_seconds=(
                (decided_at if decided_at is not None else datetime.now(UTC))
                - (created_at if created_at is not None else datetime.now(UTC))
            ).total_seconds(),
        )
        logger.info(
            "Approval %s rejected by %s (reason: %s)",
            approval_id,
            reviewer_id,
            decision_reason,
        )
        return _model_to_domain(model)

    def _reject_in_memory(self, approval_id: str, reviewer_id: str, decision_reason: str = "") -> ApprovalRequest | None:
        approval = self._approvals.get(approval_id)
        if approval is None:
            return None
        if approval.status != ApprovalStatus.PENDING:
            return None

        approval.status = ApprovalStatus.REJECTED
        approval.reviewer_id = reviewer_id
        approval.decision_reason = decision_reason
        approval.decided_at = datetime.now(UTC)
        record_approval_event(
            approval.risk_level.value,
            "rejected",
            wait_seconds=(approval.decided_at - approval.created_at).total_seconds(),
        )
        logger.info(
            "Approval %s rejected by %s (reason: %s)",
            approval_id,
            reviewer_id,
            decision_reason,
        )
        return approval

    def cancel(self, approval_id: str) -> ApprovalRequest | None:
        """Cancel an approval request (e.g., when the job is cancelled)."""
        if not self._db_backed:
            approval = self._approvals.get(approval_id)
            if approval is None:
                return None
            if approval.status != ApprovalStatus.PENDING:
                return None
            approval.status = ApprovalStatus.CANCELLED
            approval.decided_at = datetime.now(UTC)
            record_approval_event(approval.risk_level.value, "cancelled")
            return approval

        session = self._session()
        try:
            stmt = (
                select(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == approval_id)
                .with_for_update()
            )
            model = session.execute(stmt).scalar_one_or_none()
            if model is None:
                return None
            if model.status != ApprovalStatus.PENDING.value:
                return None
            model.status = ApprovalStatus.CANCELLED.value
            model.decided_at = datetime.now(UTC)
            session.commit()
            session.refresh(model)
        finally:
            session.close()
        record_approval_event(model.risk_level, "cancelled")
        return _model_to_domain(model)

    def check_expired(self) -> list[ApprovalRequest]:
        """Check for and expire any overdue approval requests."""
        if not self._db_backed:
            expired: list[ApprovalRequest] = []
            now = datetime.now(UTC)
            for approval in self._approvals.values():
                if (
                    approval.status == ApprovalStatus.PENDING
                    and approval.expires_at
                    and now > approval.expires_at
                ):
                        approval.status = ApprovalStatus.EXPIRED
                        approval.decided_at = now
                        record_approval_event(
                            approval.risk_level.value,
                            "expired",
                            wait_seconds=(now - approval.created_at).total_seconds(),
                        )
                        expired.append(approval)
            return expired

        session = self._session()
        now = datetime.now(UTC)
        try:
            stmt = select(ApprovalRequestModel).where(
                ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                ApprovalRequestModel.expires_at.is_not(None),
                ApprovalRequestModel.expires_at < now,
            )
            models = list(session.execute(stmt).scalars().all())
            for model in models:
                model.status = ApprovalStatus.EXPIRED.value
                model.decided_at = now
            if models:
                session.commit()
            # Capture values while the session is still open
            snapshot = [
                (
                    m.risk_level,
                    _as_utc(m.decided_at),
                    _as_utc(m.created_at),
                )
                for m in models
            ]
        finally:
            session.close()

        for risk_level, decided_at, created_at in snapshot:
            end = decided_at if decided_at is not None else datetime.now(UTC)
            start = created_at if created_at is not None else end
            record_approval_event(
                risk_level,
                "expired",
                wait_seconds=(end - start).total_seconds(),
            )
        return [_model_to_domain(m) for m in models]


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