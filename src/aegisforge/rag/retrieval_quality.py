from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import RetrievalResult

# ---------------------------------------------------------------------------
# Deterministic enterprise retrieval-quality corpus
# ---------------------------------------------------------------------------
#
# The goal of this corpus is NOT "lots of content."
# The goal is a small, readable, deterministic set of cases that prove:
#
#  - exact terminology retrieval
#  - semantic/paraphrased retrieval
#  - distractor resistance
#  - near-synonym collisions (similar vocabulary, different meaning)
#  - insufficient-context behavior
#  - tenant isolation
#
# Each query declares which chunk ids should be considered relevant for the
# purpose of retrieval-quality measurement. This is a curated evaluation
# dataset, not a model-training set.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalQualityCase:
    """One deterministic retrieval evaluation query."""

    name: str
    query: str
    query_type: str  # exact | semantic | paraphrased | distractor | insufficient
    organization_id: str
    relevant_chunk_ids: tuple[str, ...]
    relevant_document_ids: tuple[str, ...]
    relevant_concepts: tuple[str, ...]
    top_k: int = 5


# Corpus is intentionally small so the evaluation remains readable and CI-fast.

RETRIEVAL_QUALITY_CORPUS: tuple[RetrievalQualityCase, ...] = (
    # ---- Exact terminology ----
    RetrievalQualityCase(
        name="exact_support_escalation",
        query="support escalation policy",
        query_type="exact",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-support-escalation-000",
            "acme-support-escalation-001",
        ),
        relevant_document_ids=("acme-support-escalation",),
        relevant_concepts=("support escalation", "manager approval", "severity"),
    ),
    RetrievalQualityCase(
        name="exact_incident_response",
        query="incident response procedure",
        query_type="exact",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-incident-response-000",
            "acme-incident-response-001",
        ),
        relevant_document_ids=("acme-incident-response",),
        relevant_concepts=("incident response", "P1", "acknowledgment"),
    ),
    RetrievalQualityCase(
        name="exact_data_access",
        query="data access policy",
        query_type="exact",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-data-access-000",
            "acme-data-access-001",
        ),
        relevant_document_ids=("acme-data-access",),
        relevant_concepts=("data access", "manager approval", "audit"),
    ),

    # ---- Semantic / paraphrased terminology ----
    RetrievalQualityCase(
        name="semantic_bug_severity_workflow",
        query="how should critical bug severity be handled",
        query_type="semantic",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-support-escalation-000",
            "acme-support-escalation-001",
            "acme-incident-response-000",
        ),
        relevant_document_ids=(
            "acme-support-escalation",
            "acme-incident-response",
        ),
        relevant_concepts=("severity", "critical", "escalation", "incident"),
    ),
    RetrievalQualityCase(
        name="paraphrased_customer_issue_urgency",
        query="a customer is reporting a severe service issue, what is the process",
        query_type="paraphrased",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-support-escalation-000",
            "acme-incident-response-000",
            "acme-incident-response-001",
        ),
        relevant_document_ids=(
            "acme-support-escalation",
            "acme-incident-response",
        ),
        relevant_concepts=("customer", "severe", "service issue", "process"),
    ),
    RetrievalQualityCase(
        name="paraphrased_access_request",
        query="someone needs production data access for an investigation",
        query_type="paraphrased",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-data-access-000",
            "acme-data-access-001",
        ),
        relevant_document_ids=("acme-data-access",),
        relevant_concepts=("production data access", "investigation", "approval"),
    ),

    # ---- Distractor-heavy query ----
    RetrievalQualityCase(
        name="distractor_code_review_and_access",
        query="code review and data access requirements",
        query_type="distractor",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-code-review-000",
            "acme-data-access-000",
        ),
        relevant_document_ids=(
            "acme-code-review",
            "acme-data-access",
        ),
        relevant_concepts=("code review", "data access", "requirements"),
    ),

    # ---- Near-synonym collision but different meaning ----
    RetrievalQualityCase(
        name="collision_release_notes_not_policy",
        query="what was released in the latest update",
        query_type="distractor",
        organization_id="org-acme",
        relevant_chunk_ids=(
            "acme-release-notes-000",
            "acme-release-notes-001",
        ),
        relevant_document_ids=("acme-release-notes",),
        relevant_concepts=("release", "latest update", "changelog"),
    ),

    # ---- Insufficient context ----
    RetrievalQualityCase(
        name="insufficient_quantum_compliance",
        query="quantum resistant compliance roadmap for 2027",
        query_type="insufficient",
        organization_id="org-acme",
        relevant_chunk_ids=(),
        relevant_document_ids=(),
        relevant_concepts=("quantum", "compliance", "roadmap"),
    ),

    # ---- Second tenant to prove isolation ----
    RetrievalQualityCase(
        name="tenant_beta_support_escalation",
        query="beta support escalation policy",
        query_type="exact",
        organization_id="org-beta",
        relevant_chunk_ids=(
            "beta-support-escalation-000",
            "beta-support-escalation-001",
        ),
        relevant_document_ids=("beta-support-escalation",),
        relevant_concepts=("support escalation", "manager approval"),
    ),
)


# ---------------------------------------------------------------------------
# Small curated evidence corpus used by retrieval-quality tests
# ---------------------------------------------------------------------------
#
# This is separate from the evaluation queries. It is the "document store"
# the retrieval system is measured against.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityEvidence:
    chunk_id: str
    document_id: str
    content: str
    organization_id: str
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def quality_evidence_corpus() -> tuple[QualityEvidence, ...]:
    """Return the deterministic evidence corpus used for retrieval-quality tests."""
    return (
        # org-acme: support escalation
        QualityEvidence(
            chunk_id="acme-support-escalation-000",
            document_id="acme-support-escalation",
            organization_id="org-acme",
            source="internal-policy/support-escalation-v2.1",
            content=(
                "Support escalation policy: customer support issues must be triaged "
                "within 4 business hours. Severity 1 critical business impact requires "
                "immediate escalation to the on-call engineering lead. Severity 2 issues "
                "are escalated to the team lead within 24 hours. All escalations must "
                "include a ticket ID, reproduction steps, and impact assessment."
            ),
        ),
        QualityEvidence(
            chunk_id="acme-support-escalation-001",
            document_id="acme-support-escalation",
            organization_id="org-acme",
            source="internal-policy/support-escalation-v2.1",
            content=(
                "Escalation workflow: when a critical bug severity is confirmed, the "
                "support lead notifies the on-call engineering lead and opens an incident "
                "ticket. Manager approval is required before any external status-page update "
                "for Severity 1 issues."
            ),
        ),
        # org-acme: incident response
        QualityEvidence(
            chunk_id="acme-incident-response-000",
            document_id="acme-incident-response",
            organization_id="org-acme",
            source="internal-policy/incident-response-v3.0",
            content=(
                "Incident response procedure: incidents are classified as P1 service down, "
                "P2 degraded, P3 minor. P1 incidents require acknowledgment within 15 "
                "minutes and a status page update within 30 minutes. A post-incident review "
                "must be completed within 48 hours of resolution."
            ),
        ),
        QualityEvidence(
            chunk_id="acme-incident-response-001",
            document_id="acme-incident-response",
            organization_id="org-acme",
            source="internal-policy/incident-response-v3.0",
            content=(
                "During incident response, customer-facing communication is owned by the "
                "incident commander. Critical bugs that affect customers must be escalated "
                "through the support escalation path before public statements are made."
            ),
        ),
        # org-acme: data access
        QualityEvidence(
            chunk_id="acme-data-access-000",
            document_id="acme-data-access",
            organization_id="org-acme",
            source="internal-policy/data-access-v1.4",
            content=(
                "Data access policy: access to customer data requires manager approval and a "
                "documented business justification. Production database access is restricted "
                "to the SRE team during active incident resolution. All data access is logged "
                "and audited quarterly."
            ),
        ),
        QualityEvidence(
            chunk_id="acme-data-access-001",
            document_id="acme-data-access",
            organization_id="org-acme",
            source="internal-policy/data-access-v1.4",
            content=(
                "Investigation data access: when an incident investigation requires customer "
                "data, the investigator must submit a ticket with business justification and "
                "obtain manager approval. Access is time-boxed and revoked after the incident "
                "is resolved."
            ),
        ),
        # org-acme: code review
        QualityEvidence(
            chunk_id="acme-code-review-000",
            document_id="acme-code-review",
            organization_id="org-acme",
            source="engineering/code-review-v2.0",
            content=(
                "Code review standards: all production code changes require at least one peer "
                "review. Security-sensitive changes require review from the security team. "
                "Automated tests must pass before merge. Branch protection rules enforce these "
                "requirements."
            ),
        ),
        # org-acme: release notes (distractor for policy queries)
        QualityEvidence(
            chunk_id="acme-release-notes-000",
            document_id="acme-release-notes",
            organization_id="org-acme",
            source="releases/v2026.08.0",
            content=(
                "Release notes v2026.08.0: this update includes improved dashboard loading "
                "performance, a new export format for reports, and bug fixes for the support "
                "ticket sidebar. The internal escalation workflow was not changed in this release."
            ),
        ),
        QualityEvidence(
            chunk_id="acme-release-notes-001",
            document_id="acme-release-notes",
            organization_id="org-acme",
            source="releases/v2026.08.0",
            content=(
                "Additional changes in v2026.08.0: updated dependencies, minor UI copy edits, "
                "and restored access to the legacy reporting endpoint. No changes to incident "
                "response or data access policy were made."
            ),
        ),
        # org-beta: support escalation (different tenant, similar terminology)
        QualityEvidence(
            chunk_id="beta-support-escalation-000",
            document_id="beta-support-escalation",
            organization_id="org-beta",
            source="beta/policy/support-escalation-v1.0",
            content=(
                "Beta support escalation: beta program support issues must be triaged within "
                "1 business day. Critical beta issues are escalated to the beta program lead. "
                "This policy applies only to beta participants."
            ),
        ),
        QualityEvidence(
            chunk_id="beta-support-escalation-001",
            document_id="beta-support-escalation",
            organization_id="org-beta",
            source="beta/policy/support-escalation-v1.0",
            content=(
                "Beta escalations do not use the standard on-call engineering lead path. "
                "Beta support escalation is handled by the beta program team, not the "
                "production incident response team."
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Retrieval-quality measurement helpers
# ---------------------------------------------------------------------------


@dataclass
class RetrievalQualityMetrics:
    query_name: str
    query_type: str
    top_k: int
    retrieved_chunk_ids: tuple[str, ...]
    relevant_chunk_ids: tuple[str, ...]
    relevant_retrieved_count: int = 0
    relevant_retrieved: tuple[str, ...] = field(default_factory=tuple)
    missing_relevant: tuple[str, ...] = field(default_factory=tuple)
    precision_at_k: float = 0.0
    recall_at_k: float = 0.0
    relevant_retrieval_rate: float = 0.0
    retrieved_from_relevant_documents: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_name": self.query_name,
            "query_type": self.query_type,
            "top_k": self.top_k,
            "retrieved_chunk_ids": list(self.retrieved_chunk_ids),
            "relevant_chunk_ids": list(self.relevant_chunk_ids),
            "relevant_retrieved_count": self.relevant_retrieved_count,
            "relevant_retrieved": list(self.relevant_retrieved),
            "missing_relevant": list(self.missing_relevant),
            "precision_at_k": round(self.precision_at_k, 4),
            "recall_at_k": round(self.recall_at_k, 4),
            "relevant_retrieval_rate": round(self.relevant_retrieval_rate, 4),
            "retrieved_from_relevant_documents": self.retrieved_from_relevant_documents,
        }


def evaluate_retrieval(
    query_case: RetrievalQualityCase,
    results: list[RetrievalResult],
) -> RetrievalQualityMetrics:
    """Evaluate a single retrieval result set against a deterministic query case."""
    retrieved_ids = tuple(r.chunk_id for r in results)
    relevant_set = set(query_case.relevant_chunk_ids)
    retrieved_set = set(retrieved_ids)

    relevant_retrieved = tuple(
        rid for rid in retrieved_ids if rid in relevant_set
    )
    missing_relevant = tuple(
        rid for rid in query_case.relevant_chunk_ids if rid not in retrieved_set
    )

    relevant_count = len(relevant_set) or 1  # avoid division by zero for insufficient-context cases
    precision_at_k = (
        len(relevant_retrieved) / len(retrieved_ids) if retrieved_ids else 0.0
    )
    recall_at_k = len(relevant_retrieved) / relevant_count
    relevant_retrieval_rate = (
        len(relevant_retrieved) / relevant_count
    )

    retrieved_from_relevant_documents = sum(
        1 for r in results if r.document_id in set(query_case.relevant_document_ids)
    )

    return RetrievalQualityMetrics(
        query_name=query_case.name,
        query_type=query_case.query_type,
        top_k=query_case.top_k,
        retrieved_chunk_ids=retrieved_ids,
        relevant_chunk_ids=query_case.relevant_chunk_ids,
        relevant_retrieved_count=len(relevant_retrieved),
        relevant_retrieved=relevant_retrieved,
        missing_relevant=missing_relevant,
        precision_at_k=precision_at_k,
        recall_at_k=recall_at_k,
        relevant_retrieval_rate=relevant_retrieval_rate,
        retrieved_from_relevant_documents=retrieved_from_relevant_documents,
    )


@dataclass
class RetrievalQualityReport:
    case_metrics: list[RetrievalQualityMetrics]
    total_queries: int
    exact_queries: int
    semantic_queries: int
    insufficient_context_cases: int
    relevant_retrieval_rate_weighted: float = 0.0
    average_recall_at_k: float = 0.0
    average_precision_at_k: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_queries": self.total_queries,
            "exact_queries": self.exact_queries,
            "semantic_queries": self.semantic_queries,
            "insufficient_context_cases": self.insufficient_context_cases,
            "average_recall_at_k": round(self.average_recall_at_k, 4),
            "average_precision_at_k": round(self.average_precision_at_k, 4),
            "relevant_retrieval_rate_weighted": round(
                self.relevant_retrieval_rate_weighted, 4
            ),
            "case_metrics": [m.to_dict() for m in self.case_metrics],
        }


def build_retrieval_quality_report(
    cases: tuple[RetrievalQualityCase, ...],
    case_results: dict[str, list[RetrievalResult]],
) -> RetrievalQualityReport:
    """Build a retrieval-quality report from per-case retrieval results."""
    metrics_list = [
        evaluate_retrieval(case, case_results.get(case.name, []))
        for case in cases
    ]

    exact_queries = sum(1 for m in metrics_list if m.query_type == "exact")
    semantic_queries = sum(
        1 for m in metrics_list if m.query_type in ("semantic", "paraphrased", "distractor")
    )
    insufficient_context_cases = sum(
        1 for c in cases if c.query_type == "insufficient"
    )

    measurable = [m for m in metrics_list if m.relevant_chunk_ids]
    if measurable:
        average_recall_at_k = sum(m.recall_at_k for m in measurable) / len(measurable)
        average_precision_at_k = sum(m.precision_at_k for m in measurable) / len(measurable)
        relevant_retrieval_rate_weighted = sum(
            m.relevant_retrieval_rate for m in measurable
        ) / len(measurable)
    else:
        average_recall_at_k = 0.0
        average_precision_at_k = 0.0
        relevant_retrieval_rate_weighted = 0.0

    return RetrievalQualityReport(
        case_metrics=metrics_list,
        total_queries=len(cases),
        exact_queries=exact_queries,
        semantic_queries=semantic_queries,
        insufficient_context_cases=insufficient_context_cases,
        average_recall_at_k=average_recall_at_k,
        average_precision_at_k=average_precision_at_k,
        relevant_retrieval_rate_weighted=relevant_retrieval_rate_weighted,
    )


def hybrid_outperforms_baselines(
    baseline_report: RetrievalQualityReport,
    hybrid_report: RetrievalQualityReport,
    *,
    min_recall_delta: float = 0.0,
    min_relevant_rate_delta: float = 0.0,
) -> tuple[bool, dict[str, Any]]:
    """Return whether hybrid wins on the evaluation corpus and why.

    This is intentionally simple and deterministic. The gate is not a single
    flaky micro-metric; it is directional improvement on the corpus that the
    dataset is designed to discriminate.

    The comparison is computed only over cases whose relevant_chunk_ids are
    non-empty, so insufficient-context cases do not distort the directional
    improvement signal.
    """
    baseline_measurable = [
        m for m in baseline_report.case_metrics if m.relevant_chunk_ids
    ]
    hybrid_measurable = [
        m for m in hybrid_report.case_metrics if m.relevant_chunk_ids
    ]

    baseline_recall = (
        sum(m.recall_at_k for m in baseline_measurable) / len(baseline_measurable)
        if baseline_measurable
        else 0.0
    )
    hybrid_recall = (
        sum(m.recall_at_k for m in hybrid_measurable) / len(hybrid_measurable)
        if hybrid_measurable
        else 0.0
    )
    baseline_rate = (
        sum(m.relevant_retrieval_rate for m in baseline_measurable)
        / len(baseline_measurable)
        if baseline_measurable
        else 0.0
    )
    hybrid_rate = (
        sum(m.relevant_retrieval_rate for m in hybrid_measurable)
        / len(hybrid_measurable)
        if hybrid_measurable
        else 0.0
    )

    recall_delta = hybrid_recall - baseline_recall
    relevant_rate_delta = hybrid_rate - baseline_rate

    improved = (
        recall_delta >= min_recall_delta
        and relevant_rate_delta >= min_relevant_rate_delta
    )

    return improved, {
        "baseline_recall_at_k": round(baseline_recall, 4),
        "hybrid_recall_at_k": round(hybrid_recall, 4),
        "recall_delta": round(recall_delta, 4),
        "baseline_relevant_retrieval_rate": round(baseline_rate, 4),
        "hybrid_relevant_retrieval_rate": round(hybrid_rate, 4),
        "relevant_rate_delta": round(relevant_rate_delta, 4),
        "hybrid_wins": improved,
    }
