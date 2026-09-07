"""Synthesis Agent for AegisForge.

Combines validated outputs from multiple agents into a final answer.  The
synthesis stage knows which agents ran, which succeeded, and which failed;
it never invents missing agent results.  When critical upstream tasks failed
(or never produced usable evidence), the synthesized answer explicitly flags
the incomplete coverage instead of producing a falsely confident response.
"""
from __future__ import annotations

import logging
from typing import Any

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.domain.models import AgentExecutionStatus, AgentResult, AgentType

logger = logging.getLogger(__name__)

# Agent types whose output is critical for a grounded final answer.
_CRITICAL_AGENT_TYPES = {AgentType.RAG, AgentType.RESEARCH, AgentType.ANALYSIS}


def synthesize_results(
    query: str,
    agent_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically synthesize a final answer from agent outputs.

    ``agent_outputs`` is a list of dicts (each a serialized AgentResult or
    TaskExecutionRecord view) with at least: ``agent_type``/``agent``,
    ``status``, ``summary``, ``result``/``output``, and optional ``evidence``.
    """
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
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
                    citations.append(citation)
        for item in out.get("evidence", []) if isinstance(out.get("evidence"), list) else []:
            if isinstance(item, dict):
                key = str(item.get("chunk_id", item.get("source", "")))
                if key and key not in evidence_seen:
                    evidence_seen.add(key)
                    citations.append(item)

    critical_failed = [
        f for f in failed
        if str(f.get("agent", "")).lower() in {t.value for t in _CRITICAL_AGENT_TYPES}
        or any(t.value in str(f.get("agent", "")).lower() for t in _CRITICAL_AGENT_TYPES)
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

    if not complete:
        answer += (
            "\n\nCoverage warning: the response above is partial — the following "
            "upstream task(s) did not complete successfully: "
            + ", ".join(f"{f['agent']} ({f['task_id']})" for f in failed)
            + "."
        )

    evidence_count = len(evidence_seen)
    confidence = 0.9 if complete and evidence_count else (0.5 if evidence_count else 0.0)
    # Do not claim completeness when critical upstream agents failed.
    if critical_failed:
        confidence = min(confidence, 0.4)
        complete = False

    return {
        "query": query,
        "answer": answer,
        "citations": citations,
        "synthesized_from": [str(o.get("agent", "")) for o in succeeded],
        "failed_upstream": failed,
        "complete": complete,
        "evidence_count": evidence_count,
        "confidence": round(confidence, 3),
    }


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
            },
            evidence=synthesis["citations"],
            confidence=synthesis["confidence"],
            tool_calls=[],
        )
