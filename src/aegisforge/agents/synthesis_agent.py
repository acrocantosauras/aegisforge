"""Synthesis Agent for AegisForge.

Combines validated outputs from multiple agents into a final answer.  The
synthesis stage knows which agents ran, which succeeded, and which failed;
it never invents missing agent results.  When critical upstream tasks failed
(or never produced usable evidence), the synthesized answer explicitly flags
the incomplete coverage instead of producing a falsely confident response.

Phase 5.3F enhancements:
- Evidence quality assessment from 5.3C integrated into synthesis
- Contradictions surfaced explicitly with source attribution
- Citations deduplicated and ranked by evidence quality
- Confidence derived from evidence quality + completeness
- Unresolved uncertainty explicitly flagged
"""
from __future__ import annotations

import logging
from typing import Any

from aegisforge.agents.analysis_agent import evidence_polarity
from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.domain.models import AgentExecutionStatus, AgentResult, AgentType
from aegisforge.evaluation.evidence import (
    assess_claim_support,
    classify_evidence_quality,
)

logger = logging.getLogger(__name__)

# Agent types whose output is critical for a grounded final answer.
_CRITICAL_AGENT_TYPES = {AgentType.RAG, AgentType.RESEARCH, AgentType.ANALYSIS}


def synthesize_results(
    query: str,
    agent_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically synthesize a final answer from agent outputs.

    Phase 5.3F: Now incorporates evidence quality assessment, contradiction
    detection, and structured citation ranking.

    ``agent_outputs`` is a list of dicts (each a serialized AgentResult or
    TaskExecutionRecord view) with at least: ``agent_type``/`agent``,
    ``status``, ``summary``, ``result``/``output``, and optional ``evidence``.
    """
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    all_citations: list[dict[str, Any]] = []
    evidence_items: list[dict[str, Any]] = []
    evidence_seen: set[str] = set()

    for out in agent_outputs:
        agent = str(out.get("agent", out.get("agent_name", out.get("agent_type", "agent"))))
        status = str(out.get("status", "unknown"))
        ok = status in (AgentExecutionStatus.COMPLETED.value, "completed", "success")
        if not ok:
            failed.append(
                {
                    "agent": agent,
                    "task_id": out.get("task_id", ""),
                    "status": status,
                    "error": (out.get("errors") or [""])[0] if isinstance(out.get("errors"), list) else str(out.get("errors", "")),
                }
            )
            continue
        succeeded.append(out)
        result = out.get("result") or out.get("output") or {}

        # Gather citations from the result and its evidence without duplicates.
        for citation in result.get("citations", []) if isinstance(result, dict) else []:
            if isinstance(citation, dict):
                key = str(citation.get("chunk_id", citation.get("source", "")))
                if key and key not in evidence_seen:
                    evidence_seen.add(key)
                    all_citations.append(citation)
        for item in out.get("evidence", []) if isinstance(out.get("evidence"), list) else []:
            if isinstance(item, dict):
                key = str(item.get("chunk_id", item.get("source", "")))
                if key and key not in evidence_seen:
                    evidence_seen.add(key)
                    all_citations.append(item)
                # Collect evidence items for quality assessment
                if item.get("content") or item.get("summary"):
                    evidence_items.append(item)

    # ---- Phase 5.3C: Evidence quality assessment ----
    evidence_assessment = classify_evidence_quality(evidence_items, query=query)

    # ---- Phase 5.3F: Contradiction detection across agent outputs ----
    contradictions = _detect_contradictions(succeeded)

    # ---- Phase 5.3F: Claim-level support assessment ----
    claim_assessments: list[dict[str, Any]] = []
    answer_sections = _collect_answer_sections(succeeded)
    if answer_sections:
        combined_answer = "\n\n".join(answer_sections)
        # Split into rough claim sentences for support assessment
        rough_claims = _extract_claims(combined_answer)
        for claim in rough_claims[:10]:  # bound to prevent excessive work
            assessment = assess_claim_support(claim, evidence_items)
            claim_assessments.append(assessment.to_dict())

    # ---- Build final synthesis ----
    critical_failed = [
        f for f in failed
        if any(
            t.value in str(f.get("agent", "")).lower()
            for t in _CRITICAL_AGENT_TYPES
        )
    ]

    sections: list[str] = []
    for out in succeeded:
        agent = str(out.get("agent", out.get("agent_name", out.get("agent_type", ""))))
        summary = str(out.get("summary", ""))
        result = out.get("result") or out.get("output") or {}
        text = summary or (result.get("answer") if isinstance(result, dict) else "") or ""
        if text:
            sections.append(f"[{agent}] {text}")

    complete = len(failed) == 0
    if sections:
        answer = "\n\n".join(sections)
    elif failed:
        answer = (
            "The workflow could not assemble a complete grounded answer because "
            f"{len(failed)} upstream task(s) failed or produced no usable output."
        )
        complete = False
    else:
        answer = "No agent produced output to synthesize."
        complete = False

    # ---- Phase 5.3F: Append evidence quality summary ----
    evidence_summary_parts: list[str] = []
    if evidence_assessment.total_items > 0:
        evidence_summary_parts.append(
            f"Evidence quality: {evidence_assessment.strong_count} strong, "
            f"{evidence_assessment.moderate_count} moderate, "
            f"{evidence_assessment.weak_count} weak, "
            f"{evidence_assessment.conflicting_count} conflicting "
            f"(overall: {evidence_assessment.overall_quality:.2f})"
        )
        if not evidence_assessment.evidence_sufficient:
            evidence_summary_parts.append(
                "Note: Evidence is insufficient for high-confidence conclusions."
            )
        if contradictions:
            evidence_summary_parts.append(
                f"Contradictions detected: {len(contradictions)} — see unresolved findings."
            )
    if claim_assessments:
        unsupported = [
            c for c in claim_assessments
            if c.get("support_status") in ("unsupported", "contradicted")
        ]
        if unsupported:
            evidence_summary_parts.append(
                f"{len(unsupported)} claim(s) lack adequate evidence support."
            )

    if evidence_summary_parts:
        answer += "\n\n--- Evidence Assessment ---\n" + "\n".join(evidence_summary_parts)

    # ---- Phase 5.3F: Rank citations by evidence quality ----
    ranked_citations = _rank_citations(all_citations, evidence_items)

    # ---- Structured decision sections, derived only from measured run data ----
    structured = _build_report_sections(
        succeeded=succeeded,
        ranked_citations=ranked_citations,
        evidence_assessment=evidence_assessment,
        contradictions=contradictions,
    )
    if structured:
        answer += "\n\n" + structured

    if not complete:
        answer += (
            "\n\nCoverage warning: the response above is partial — the following "
            "upstream task(s) did not complete successfully: "
            + ", ".join(f"{f['agent']} ({f['task_id']})" for f in failed)
            + "."
        )

    evidence_count = len(evidence_seen)
    # Confidence: derived from evidence quality + completeness + contradiction count
    if evidence_assessment.total_items > 0:
        base_confidence = evidence_assessment.overall_quality
    else:
        base_confidence = 0.5 if complete else 0.0
    contradiction_penalty = min(0.3, 0.1 * len(contradictions))
    confidence = max(0.0, min(1.0, base_confidence * (0.9 if complete else 0.5) - contradiction_penalty))
    # Do not claim completeness when critical upstream agents failed.
    if critical_failed:
        confidence = min(confidence, 0.4)
        complete = False

    return {
        "query": query,
        "answer": answer,
        "citations": ranked_citations,
        "synthesized_from": [str(o.get("agent", "")) for o in succeeded],
        "failed_upstream": failed,
        "complete": complete,
        "evidence_count": evidence_count,
        "confidence": round(confidence, 3),
        # Phase 5.3F: Structured quality metadata
        "evidence_assessment": evidence_assessment.to_dict(),
        "contradictions": contradictions,
        "claim_assessments": claim_assessments,
    }


def _collect_analysis_findings(
    succeeded: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect findings produced by analysis agents in this run."""
    findings: list[dict[str, Any]] = []
    for out in succeeded:
        result = out.get("result") or out.get("output") or {}
        if not isinstance(result, dict):
            continue
        agent = str(out.get("agent", out.get("agent_name", out.get("agent_type", ""))))
        if "analysis" not in agent.lower():
            continue
        for finding in result.get("findings", []) or []:
            if isinstance(finding, dict) and finding.get("content"):
                findings.append(finding)
    return findings


def _collect_analysis_conflicts(succeeded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect conflicts the analysis stage detected between individual sources."""
    conflicts: list[dict[str, Any]] = []
    for out in succeeded:
        result = out.get("result") or out.get("output") or {}
        if not isinstance(result, dict):
            continue
        agent = str(out.get("agent", out.get("agent_name", out.get("agent_type", ""))))
        if "analysis" not in agent.lower():
            continue
        for conflict in result.get("conflicts", []) or []:
            if isinstance(conflict, dict):
                conflicts.append(conflict)
    return conflicts


def _excerpt(text: str) -> str:
    """Render a retrieved excerpt readably.

    Chunks start mid-sentence by design, so an excerpt that does not begin at a
    sentence boundary is prefixed with an ellipsis — the reader is told it is a
    fragment instead of being shown a broken first word.
    """
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return ""
    starts_mid_sentence = collapsed[0].islower() or collapsed[0] in ");,-—"
    return f"…{collapsed}" if starts_mid_sentence else collapsed


def _collect_analysis_gaps(succeeded: list[dict[str, Any]]) -> list[str]:
    """Collect evidence gaps reported by analysis agents in this run."""
    gaps: list[str] = []
    for out in succeeded:
        result = out.get("result") or out.get("output") or {}
        if not isinstance(result, dict):
            continue
        agent = str(out.get("agent", out.get("agent_name", out.get("agent_type", ""))))
        if "analysis" not in agent.lower():
            continue
        for gap in result.get("gaps", []) or []:
            if gap and str(gap) not in gaps:
                gaps.append(str(gap))
    return gaps


def _build_report_sections(
    *,
    succeeded: list[dict[str, Any]],
    ranked_citations: list[dict[str, Any]],
    evidence_assessment: Any,
    contradictions: list[dict[str, Any]],
) -> str:
    """Render the structured decision sections of the final answer.

    Every line is derived from data the run actually produced: findings and
    gaps from the analysis task's output, conflicts from cross-agent polarity
    analysis, sources from retrieved citations.  Nothing is invented and no
    field is emitted when the run produced no evidence for it — the absence of
    a section is itself the honest signal.
    """
    sections: list[str] = []

    findings = _collect_analysis_findings(succeeded)
    if findings:
        lines = ["## Key findings"]
        for finding in findings[:8]:
            content = _excerpt(str(finding.get("content", "")))[:320]
            source = str(finding.get("source") or "unsourced")
            lines.append(f"- {content} ({source})")
        sections.append("\n".join(lines))

    source_conflicts = _collect_analysis_conflicts(succeeded)
    all_conflicts = source_conflicts + list(contradictions)
    if all_conflicts:
        lines = ["## Conflicts detected between sources"]
        for conflict in all_conflicts[:6]:
            left = conflict.get("left") or {}
            right = conflict.get("right") or {}
            left_source = left.get("source") or conflict.get("left_agent") or "source A"
            right_source = right.get("source") or conflict.get("right_agent") or "source B"
            lines.append(
                f"- {left_source} and {right_source} state opposing positions "
                f"({conflict.get('type', 'conflict')})."
            )
        sections.append("\n".join(lines))

    gaps = _collect_analysis_gaps(succeeded)
    if gaps:
        lines = ["## Unknowns and evidence gaps"]
        lines.extend(f"- {gap}" for gap in gaps[:6])
        sections.append("\n".join(lines))

    if ranked_citations:
        lines = ["## Sources"]
        for citation in ranked_citations[:12]:
            source = str(citation.get("source") or citation.get("document_id") or "unknown")
            score = citation.get("score", citation.get("quality_score"))
            suffix = f" (relevance {float(score):.2f})" if isinstance(score, (int, float)) else ""
            lines.append(f"- {source}{suffix}")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    footer = (
        f"Evidence quality {getattr(evidence_assessment, 'overall_quality', 0.0):.2f} · "
        f"items {getattr(evidence_assessment, 'total_items', 0)} "
        f"(strong {getattr(evidence_assessment, 'strong_count', 0)}, "
        f"moderate {getattr(evidence_assessment, 'moderate_count', 0)}, "
        f"weak {getattr(evidence_assessment, 'weak_count', 0)}, "
        f"conflicting {getattr(evidence_assessment, 'conflicting_count', 0)})"
    )
    return "\n\n".join(sections) + "\n\n---\n" + footer


def _collect_answer_sections(succeeded: list[dict[str, Any]]) -> list[str]:
    """Extract text answer sections from succeeded agent outputs."""
    sections: list[str] = []
    for out in succeeded:
        result = out.get("result") or out.get("output") or {}
        if isinstance(result, dict):
            text = result.get("answer", "")
            if text:
                sections.append(str(text))
    return sections


def _extract_claims(text: str) -> list[str]:
    """Extract rough claim sentences from text for support assessment."""
    # Split on sentence boundaries
    sentences = []
    for part in text.replace("\n", " ").split("."):
        cleaned = part.strip()
        if len(cleaned) > 20 and not cleaned.startswith("["):
            sentences.append(cleaned + ".")
    return sentences[:15]  # bound


def _detect_contradictions(succeeded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect contradictions between agent outputs.

    Uses the same stance classifier as the analysis agent, so a claim judged a
    conflict there is not re-judged as consistent here.
    """
    contradictions: list[dict[str, Any]] = []
    outputs: list[tuple[str, str, str]] = []  # (agent, text, polarity)

    for out in succeeded:
        agent = str(out.get("agent", out.get("agent_name", out.get("agent_type", ""))))
        result = out.get("result") or out.get("output") or {}
        text = ""
        if isinstance(result, dict):
            text = result.get("answer", out.get("summary", ""))
        else:
            text = out.get("summary", "")

        if not text or len(text) < 10:
            continue

        outputs.append((agent, text[:300], evidence_polarity(str(text))))

    for i in range(len(outputs)):
        for j in range(i + 1, len(outputs)):
            ai, ti, pi = outputs[i]
            aj, tj, pj = outputs[j]
            if pi != "neutral" and pj != "neutral" and pi != pj:
                contradictions.append({
                    "type": "polarity_conflict",
                    "left_agent": ai,
                    "left_text": ti[:200],
                    "right_agent": aj,
                    "right_text": tj[:200],
                    "polarity_left": pi,
                    "polarity_right": pj,
                })

    return contradictions


def _rank_citations(
    citations: list[dict[str, Any]],
    evidence_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank citations by evidence quality, deduplicated."""
    # Build quality lookup
    quality_map: dict[str, float] = {}
    for item in evidence_items:
        key = str(item.get("source", item.get("chunk_id", "")))
        if key:
            score = float(item.get("score", item.get("relevance", 0.5)))
            # Keep best score per source
            if key not in quality_map or score > quality_map[key]:
                quality_map[key] = score

    ranked = []
    for citation in citations:
        key = str(citation.get("source", citation.get("chunk_id", "")))
        quality_score = quality_map.get(key, 0.5)
        citation_copy = dict(citation)
        citation_copy["quality_score"] = round(quality_score, 3)
        ranked.append(citation_copy)

    # Sort by quality score descending
    ranked.sort(key=lambda c: c.get("quality_score", 0), reverse=True)
    return ranked


class SynthesisAgent(BaseAgent):
    """Combines validated agent outputs into the final workflow answer."""

    def __init__(
        self,
        name: str = "synthesis-agent",
        description: str = "Combines validated outputs from multiple agents into a final answer.",
        permissions: list[PermissionSpec] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type=AgentType.SYNTHESIS,
            description=description,
            permissions=permissions or [],
        )

    def _execute(self, input_data: dict[str, Any], context: AgentExecutionContext) -> AgentResult:
        query = str(input_data.get("query", ""))
        agent_outputs = input_data.get("agent_outputs", [])
        if not query:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="No query provided for synthesis",
                errors=["Query is required for synthesis"],
            )
        if not isinstance(agent_outputs, list):
            agent_outputs = []

        synthesis = synthesize_results(query, agent_outputs)

        status = AgentExecutionStatus.COMPLETED  # partial results are still a valid (flagged) output
        return AgentResult(
            agent_name=self.name,
            agent_type=self.agent_type,
            status=status,
            summary=(
                f"Synthesized final answer from {len(synthesis['synthesized_from'])} agent(s) "
                f"with {synthesis['evidence_count']} citation(s)"
            ),
            result={
                "query": query,
                "answer": synthesis["answer"],
                "citations": synthesis["citations"],
                "complete": synthesis["complete"],
                "evidence_count": synthesis["evidence_count"],
                "failed_upstream": synthesis["failed_upstream"],
                "evidence_assessment": synthesis["evidence_assessment"],
                "contradictions": synthesis["contradictions"],
                "claim_assessments": synthesis["claim_assessments"],
                "confidence": synthesis["confidence"],
            },
            evidence=synthesis["citations"],
            confidence=synthesis["confidence"],
            tool_calls=[],
        )
