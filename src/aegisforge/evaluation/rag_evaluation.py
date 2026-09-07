from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import AgentResult, RetrievalResult
from aegisforge.rag.retrieval_quality import (
    RetrievalQualityCase,
    RetrievalQualityMetrics,
    evaluate_retrieval,
)

# ---------------------------------------------------------------------------
# RAG-side retrieval-quality evaluation (deterministic)
# ---------------------------------------------------------------------------
#
# This complements the existing RAGEvaluationResult by making retrieval
# quality explicit and testable against a deterministic query case.
#
# It is intentionally separate from optional LLM-based criticism.
# ---------------------------------------------------------------------------


@dataclass
class RetrievalEvaluationResult:
    """Deterministic retrieval-quality evaluation for one RAG query case."""

    query_name: str
    query_type: str
    retrieval_metrics: RetrievalQualityMetrics
    has_citations: bool = False
    citation_count: int = 0
    cited_relevant_chunk_ids: tuple[str, ...] = field(default_factory=tuple)
    missing_cited_relevant: tuple[str, ...] = field(default_factory=tuple)
    citation_recall_at_k: float = 0.0
    grounded: bool = False
    insufficient_context: bool = False
    retrieval_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_name": self.query_name,
            "query_type": self.query_type,
            "retrieval": self.retrieval_metrics.to_dict(),
            "has_citations": self.has_citations,
            "citation_count": self.citation_count,
            "cited_relevant_chunk_ids": list(self.cited_relevant_chunk_ids),
            "missing_cited_relevant": list(self.missing_cited_relevant),
            "citation_recall_at_k": round(self.citation_recall_at_k, 4),
            "grounded": self.grounded,
            "insufficient_context": self.insufficient_context,
            "retrieval_score": round(self.retrieval_score, 4),
        }


def evaluate_rag_retrieval(
    case: RetrievalQualityCase,
    retrieval_results: list[RetrievalResult],
    agent_result: AgentResult | None = None,
) -> RetrievalEvaluationResult:
    """Evaluate retrieval plus citation behavior for one deterministic case."""
    metrics = evaluate_retrieval(case, retrieval_results)

    citations: list[dict[str, Any]] = []
    if agent_result is not None:
        result_payload = agent_result.result or {}
        citations = list(result_payload.get("citations", []) or [])
        if not citations and agent_result.evidence:
            citations = [e for e in agent_result.evidence if isinstance(e, dict)]

    citation_chunk_ids = tuple(
        str(c.get("chunk_id") or c.get("id") or "")
        for c in citations
        if c.get("chunk_id") or c.get("id")
    )
    relevant_set = set(case.relevant_chunk_ids)
    cited_relevant = tuple(cid for cid in citation_chunk_ids if cid in relevant_set)
    missing_cited_relevant = tuple(
        rid for rid in case.relevant_chunk_ids if rid not in set(citation_chunk_ids)
    )

    citation_recall_at_k = (
        len(cited_relevant) / max(1, len(relevant_set))
        if relevant_set
        else 0.0
    )

    has_citations = bool(citations)
    citation_count = len(citations)

    retrieval_count = (
        agent_result.result.get("retrieval_count", len(retrieval_results))
        if agent_result is not None
        else len(retrieval_results)
    )
    context_available = bool(
        agent_result.result.get("context_available", retrieval_results != [])
        if agent_result is not None
        else retrieval_results != []
    )

    insufficient_context = (
        retrieval_count == 0 or not context_available
    ) and not case.relevant_chunk_ids

    grounded = bool(
        has_citations and metrics.relevant_retrieved_count > 0
    ) or (not case.relevant_chunk_ids and retrieval_count == 0)

    retrieval_score = round(
        0.5 * metrics.recall_at_k
        + 0.3 * (1.0 if grounded else 0.0)
        + 0.2 * (1.0 if has_citations else 0.0),
        4,
    )

    return RetrievalEvaluationResult(
        query_name=case.name,
        query_type=case.query_type,
        retrieval_metrics=metrics,
        has_citations=has_citations,
        citation_count=citation_count,
        cited_relevant_chunk_ids=cited_relevant,
        missing_cited_relevant=missing_cited_relevant,
        citation_recall_at_k=citation_recall_at_k,
        grounded=grounded,
        insufficient_context=insufficient_context,
        retrieval_score=retrieval_score,
    )


@dataclass
class RetrievalEvaluationReport:
    case_results: list[RetrievalEvaluationResult]
    total_queries: int
    exact_queries: int
    semantic_queries: int
    insufficient_context_cases: int
    average_recall_at_k: float = 0.0
    average_citation_recall_at_k: float = 0.0
    grounded_rate: float = 0.0
    citation_present_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_queries": self.total_queries,
            "exact_queries": self.exact_queries,
            "semantic_queries": self.semantic_queries,
            "insufficient_context_cases": self.insufficient_context_cases,
            "average_recall_at_k": round(self.average_recall_at_k, 4),
            "average_citation_recall_at_k": round(self.average_citation_recall_at_k, 4),
            "grounded_rate": round(self.grounded_rate, 4),
            "citation_present_rate": round(self.citation_present_rate, 4),
            "case_results": [r.to_dict() for r in self.case_results],
        }


def build_retrieval_evaluation_report(
    cases: tuple[RetrievalQualityCase, ...],
    per_case: dict[str, RetrievalEvaluationResult],
) -> RetrievalEvaluationReport:
    results = [per_case[case.name] for case in cases]

    exact_queries = sum(1 for r in results if r.query_type == "exact")
    semantic_queries = sum(
        1 for r in results if r.query_type in ("semantic", "paraphrased", "distractor")
    )
    insufficient_context_cases = sum(
        1 for r in results if r.insufficient_context
    )

    measurable = [r for r in results if r.retrieval_metrics.relevant_chunk_ids]
    if measurable:
        average_recall_at_k = (
            sum(r.retrieval_metrics.recall_at_k for r in measurable) / len(measurable)
        )
        average_citation_recall_at_k = (
            sum(r.citation_recall_at_k for r in measurable) / len(measurable)
        )
    else:
        average_recall_at_k = 0.0
        average_citation_recall_at_k = 0.0

    grounded_rate = (
        sum(1 for r in results if r.grounded) / len(results) if results else 0.0
    )
    citation_present_rate = (
        sum(1 for r in results if r.has_citations) / len(results) if results else 0.0
    )

    return RetrievalEvaluationReport(
        case_results=results,
        total_queries=len(results),
        exact_queries=exact_queries,
        semantic_queries=semantic_queries,
        insufficient_context_cases=insufficient_context_cases,
        average_recall_at_k=average_recall_at_k,
        average_citation_recall_at_k=average_citation_recall_at_k,
        grounded_rate=grounded_rate,
        citation_present_rate=citation_present_rate,
    )
