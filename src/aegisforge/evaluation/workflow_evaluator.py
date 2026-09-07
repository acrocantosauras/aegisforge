"""Workflow-level evaluation for multi-agent runs (Phase 5).

Expands evaluation from final-output checking into four measured domains:

- Planning: decomposition quality, dependency quality, agent selection.
- Execution: task success, retry efficiency, tool-call economy, latency.
- Collaboration: information passed correctly, conflicting outputs,
  missing evidence, redundant work.
- Final response: groundedness, completeness, citation quality,
  failure transparency.

All numbers are derived from the actual run records — never fabricated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import (
    AgentExecutionStatus,
    ExecutionPlan,
)
from aegisforge.evaluation.evaluators import (
    PlanEvaluationResult,
    evaluate_plan,
)

logger = logging.getLogger(__name__)


def _val(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _status_ok(status: Any) -> bool:
    return _val(status) == AgentExecutionStatus.COMPLETED.value


@dataclass
class CollaborationEvaluation:
    """How well agents exchanged information."""

    total_tasks: int = 0
    tasks_with_references: int = 0
    references_resolved: int = 0
    references_failed: int = 0
    conflicting_outputs: int = 0
    missing_evidence_tasks: int = 0
    redundant_tasks: int = 0
    information_passed_ratio: float = 0.0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "tasks_with_references": self.tasks_with_references,
            "references_resolved": self.references_resolved,
            "references_failed": self.references_failed,
            "conflicting_outputs": self.conflicting_outputs,
            "missing_evidence_tasks": self.missing_evidence_tasks,
            "redundant_tasks": self.redundant_tasks,
            "information_passed_ratio": round(self.information_passed_ratio, 4),
            "score": round(self.score, 4),
        }


@dataclass
class FinalResponseEvaluation:
    """Evaluation of the workflow's final answer."""

    produced: bool = False
    grounded: bool = False
    citation_count: int = 0
    complete: bool = False
    failed_upstream_count: int = 0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "produced": self.produced,
            "grounded": self.grounded,
            "citation_count": self.citation_count,
            "complete": self.complete,
            "failed_upstream_count": self.failed_upstream_count,
            "score": round(self.score, 4),
        }


@dataclass
class WorkflowEvaluation:
    """Aggregated evaluation of a full workflow run."""

    planning: PlanEvaluationResult = field(default_factory=PlanEvaluationResult)
    collaboration: CollaborationEvaluation = field(default_factory=CollaborationEvaluation)
    final_response: FinalResponseEvaluation = field(default_factory=FinalResponseEvaluation)
    overall_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "planning": self.planning.to_dict(),
            "collaboration": self.collaboration.to_dict(),
            "final_response": self.final_response.to_dict(),
            "overall_score": round(self.overall_score, 4),
        }


def _collect_references(plan: ExecutionPlan) -> list[tuple[str, list[str]]]:
    """Return (task_id, referenced_dependency_ids) for tasks with references."""
    refs: list[tuple[str, list[str]]] = []
    for task in plan.tasks:
        dep_ids: list[str] = []
        if task.input_references:
            dep_ids += [ref.split(".", 1)[0] for ref in task.input_references.values()]
        for key in ("evidence_from", "agent_outputs_from"):
            for dep in (task.input_data.get(key) or []):
                if isinstance(dep, str):
                    dep_ids.append(dep)
        if dep_ids:
            refs.append((task.task_id, dep_ids))
    return refs


def evaluate_workflow_run(
    plan: ExecutionPlan,
    records: dict[str, dict[str, Any]],
    final_output: dict[str, Any] | None = None,
    plan_validation_errors: list[str] | None = None,
) -> WorkflowEvaluation:
    """Evaluate a completed multi-agent run from its serialized records."""
    evaluation = WorkflowEvaluation()

    # --- Planning domain ---
    planning = evaluate_plan(plan, plan_validation_errors or [])
    evaluation.planning = planning

    # --- Collaboration domain ---
    collab = CollaborationEvaluation(total_tasks=len(plan.tasks))
    referenced = _collect_references(plan)
    collab.tasks_with_references = len(referenced)

    for task_id, dep_ids in referenced:
        for dep_id in dep_ids:
            dep_record = records.get(dep_id)
            dep_ok = dep_record is not None and _status_ok(dep_record.get("status"))
            output = (dep_record or {}).get("output") or {}
            value_present = bool(output)
            if dep_ok and value_present:
                collab.references_resolved += 1
            else:
                collab.references_failed += 1

    # Conflicting outputs: surfaced conflicts from analysis agents.
    for record in records.values():
        output = record.get("output") or {}
        if isinstance(output.get("conflicts"), list):
            collab.conflicting_outputs += len(output["conflicts"])

    # Missing evidence: completed critical agents that produced no citations
    # and no evidence at all.
    for record in records.values():
        if not _status_ok(record.get("status")):
            continue
        agent_type = _val(record.get("agent_type"))
        evidence = record.get("evidence") or []
        output = record.get("output") or {}
        citations = output.get("citations") or []
        if agent_type in ("rag", "analysis", "synthesis") and not evidence and not citations:
            collab.missing_evidence_tasks += 1

    # Redundant work: two completed tasks of the same agent type with
    # identical source citations (same evidence provenance).
    seen_sources: dict[str, list[str]] = {}
    for record in records.values():
        if not _status_ok(record.get("status")):
            continue
        output = record.get("output") or {}
        citations = output.get("citations") or []
        for citation in citations:
            key = str(citation.get("source") or citation.get("chunk_id") or "")
            if not key:
                continue
            for earlier in seen_sources.setdefault(key, []):
                if earlier != record.get("task_id"):
                    collab.redundant_tasks += 1
            seen_sources[key].append(str(record.get("task_id")))

    total_refs = collab.references_resolved + collab.references_failed
    collab.information_passed_ratio = (
        collab.references_resolved / total_refs if total_refs else 1.0
    )
    collab.score = max(
        0.0,
        min(
            1.0,
            0.4 * collab.information_passed_ratio
            + 0.3 * (1.0 - min(collab.conflicting_outputs / max(len(records), 1), 1.0))
            + 0.3 * (1.0 - min(collab.missing_evidence_tasks / max(len(records), 1), 1.0)),
        ),
    )
    evaluation.collaboration = collab

    # --- Final response domain ---
    final = FinalResponseEvaluation()
    if final_output:
        answer = str(final_output.get("answer") or "")
        citations = final_output.get("citations") or []
        failed_upstream = final_output.get("failed_upstream") or []
        complete = bool(final_output.get("complete", not failed_upstream))
        final.produced = bool(answer)
        final.grounded = bool(citations) or bool(final_output.get("evidence_count"))
        final.citation_count = len(citations)
        final.complete = complete
        final.failed_upstream_count = len(failed_upstream)
        final.score = max(
            0.0,
            min(
                1.0,
                0.3 * (1.0 if final.produced else 0.0)
                + 0.3 * (1.0 if final.grounded else 0.0)
                + 0.2 * (1.0 if final.complete else 0.0)
                + 0.2 * (1.0 if final.failed_upstream_count == 0 else 0.5),
            ),
        )
    evaluation.final_response = final

    # --- Overall ---
    scores = [
        planning.overall_score,
        collab.score,
        final.score if final.produced else 0.5,
    ]
    evaluation.overall_score = sum(scores) / len(scores) if scores else 0.0
    return evaluation
