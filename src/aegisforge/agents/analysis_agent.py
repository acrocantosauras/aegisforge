"""Analysis Agent for AegisForge.

Responsible for structured reasoning over *supplied evidence* (previous task
results).  It never retrieves on its own and never invokes tools; it consumes
only what upstream tasks declare and pass to it.

The analysis is deterministic and audit-friendly: findings reference the exact
evidence items (source + originating task) they were derived from, conflicts
between sources are surfaced instead of hidden, and gaps in the evidence are
reported explicitly so downstream synthesis can avoid over-claiming.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.domain.models import AgentExecutionStatus, AgentResult, AgentType

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Lowercase + tokenize text for lightweight relevance checks."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


def _token_overlap(query: str, content: str) -> float:
    query_tokens = {t for t in _normalize(query).split() if len(t) > 2}
    content_tokens = set(_normalize(content).split())
    if not query_tokens:
        return 0.0
    overlap = query_tokens & content_tokens
    return len(overlap) / len(query_tokens)


def analyze_evidence(query: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic structured analysis over a list of evidence items.

    Each evidence item is a dict with at least ``content`` and ideally
    ``source`` / ``task_id``.  Returns findings, conflicts, gaps, and a
    confidence score — all derived from the supplied evidence only.
    """
    findings: list[dict[str, Any]] = []
    relevant_items: list[dict[str, Any]] = []

    for item in evidence:
        content = str(item.get("content", item.get("summary", "")))
        if not content.strip():
            continue
        source = item.get("source", "")
        task_id = item.get("task_id", item.get("agent", ""))
        relevance = item.get("relevance", _token_overlap(query, content))
        if relevance >= 0.3:
            relevant_items.append(item)
        findings.append(
            {
                "content": content[:500],
                "source": source,
                "task_id": task_id,
                "relevance": round(relevance, 3),
            }
        )

    # Conflict detection: statements that disagree on key magnitudes/directions.
    conflicts: list[dict[str, Any]] = []
    negative_markers = ("not allowed", "prohibited", "denied", "no ", "must not", "cannot")
    positive_markers = ("allowed", "permitted", "required", "must", "approved")
    polarity: list[dict[str, Any]] = []
    for item in evidence:
        content = str(item.get("content", "")).lower()
        neg = any(m in content for m in negative_markers)
        pos = any(m in content for m in positive_markers)
        polarity.append(
            {
                "source": item.get("source", ""),
                "task_id": item.get("task_id", item.get("agent", "")),
                "polarity": "negative" if neg and not pos else ("positive" if pos else "neutral"),
            }
        )
    for i in range(len(polarity)):
        for j in range(i + 1, len(polarity)):
            a, b = polarity[i], polarity[j]
            if a["polarity"] != "neutral" and b["polarity"] != "neutral" and a["polarity"] != b["polarity"]:
                conflicts.append(
                    {
                        "type": "polarity_conflict",
                        "left": a,
                        "right": b,
                    }
                )

    # Gap detection: insufficient evidence to answer a substantive query.
    gaps: list[str] = []
    if not relevant_items:
        gaps.append("No evidence above the relevance threshold was supplied")
    if len(relevant_items) < 2:
        gaps.append("Fewer than two corroborating sources available")

    total = max(len(evidence), 1)
    confidence = round(
        min(1.0, 0.3 + 0.5 * (len(relevant_items) / total) - 0.1 * len(conflicts)),
        3,
    )
    confidence = max(0.0, confidence)

    return {
        "query": query,
        "findings": findings,
        "conflicts": conflicts[:10],
        "gaps": gaps,
        "evidence_reviewed": len(evidence),
        "relevant_evidence_count": len(relevant_items),
        "confidence": confidence,
    }


class AnalysisAgent(BaseAgent):
    """Performs structured reasoning over evidence supplied by other agents."""

    def __init__(
        self,
        name: str = "analysis-agent",
        description: str = "Performs structured reasoning over supplied evidence from other agents.",
        permissions: list[PermissionSpec] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type=AgentType.ANALYSIS,
            description=description,
            permissions=permissions or [],
        )

    def _execute(self, input_data: dict[str, Any], context: AgentExecutionContext) -> AgentResult:
        query = str(input_data.get("query", ""))
        evidence = input_data.get("evidence", [])
        if not query:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="No query provided for analysis",
                errors=["Query is required for analysis"],
            )

        if not isinstance(evidence, list) or not evidence:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.COMPLETED,
                summary="Analysis completed with no evidence to review",
                result=analyze_evidence(query, []),
                evidence=[],
                confidence=0.0,
            )

        analysis = analyze_evidence(query, evidence)

        summary = (
            f"Analyzed {analysis['evidence_reviewed']} evidence item(s): "
            f"{analysis['relevant_evidence_count']} relevant, "
            f"{len(analysis['conflicts'])} conflict(s), {len(analysis['gaps'])} gap(s)"
        )
        return AgentResult(
            agent_name=self.name,
            agent_type=self.agent_type,
            status=AgentExecutionStatus.COMPLETED,
            summary=summary,
            result={
                "query": query,
                "answer": summary,
                **analysis,
            },
            evidence=[e for e in evidence if isinstance(e, dict)][:20],
            confidence=analysis["confidence"],
            tool_calls=[],
        )
