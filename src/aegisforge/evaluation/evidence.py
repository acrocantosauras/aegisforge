"""Evidence quality classification and structured evidence-claim relationships.

Phase 5.3C: Evidence-Aware Reasoning

Provides deterministic classification of evidence quality and structured
relationships between claims and their supporting/contradicting evidence.
Downstream reasoning (analysis, synthesis, evaluation) uses this to:

- Distinguish supported vs unsupported claims
- Identify conflicting evidence
- Assess weak/insufficient evidence
- Request more evidence when gaps are detected
- Qualify answers with confidence levels

All classification is deterministic and auditable — no LLM calls required.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EvidenceQuality(str, Enum):
    """Quality classification for a single evidence item."""

    STRONG = "strong"          # High relevance, clear source, recent
    MODERATE = "moderate"      # Moderate relevance, valid source
    WEAK = "weak"              # Low relevance or stale
    CONFLICTING = "conflicting"  # Contradicts other evidence
    INSUFFICIENT = "insufficient"  # Too sparse to evaluate


class ClaimSupport(str, Enum):
    """How well a claim is supported by evidence."""

    FULLY_SUPPORTED = "fully_supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    WEAKLY_SUPPORTED = "weakly_supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"


@dataclass
class EvidenceItem:
    """A single piece of evidence with quality metadata."""

    content: str
    source: str = ""
    task_id: str = ""
    relevance_score: float = 0.0
    quality: EvidenceQuality = EvidenceQuality.MODERATE
    recency: str = ""  # e.g. "2026-08-01" or ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content[:500],
            "source": self.source,
            "task_id": self.task_id,
            "relevance_score": round(self.relevance_score, 3),
            "quality": self.quality.value,
            "recency": self.recency,
        }


@dataclass
class ClaimEvidence:
    """Tracks the evidence supporting or contradicting a specific claim."""

    claim_text: str
    support_status: ClaimSupport = ClaimSupport.UNKNOWN
    supporting_evidence: list[EvidenceItem] = field(default_factory=list)
    contradicting_evidence: list[EvidenceItem] = field(default_factory=list)
    confidence: float = 0.0
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim_text[:300],
            "support_status": self.support_status.value,
            "supporting_count": len(self.supporting_evidence),
            "contradicting_count": len(self.contradicting_evidence),
            "confidence": round(self.confidence, 3),
            "gaps": self.gaps,
        }


@dataclass
class EvidenceAssessment:
    """Aggregate assessment of evidence quality for an evidence set."""

    total_items: int = 0
    strong_count: int = 0
    moderate_count: int = 0
    weak_count: int = 0
    conflicting_count: int = 0
    insufficient_count: int = 0
    unique_sources: int = 0
    overall_quality: float = 0.0
    evidence_sufficient: bool = False
    items: list[EvidenceItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_items": self.total_items,
            "strong_count": self.strong_count,
            "moderate_count": self.moderate_count,
            "weak_count": self.weak_count,
            "conflicting_count": self.conflicting_count,
            "insufficient_count": self.insufficient_count,
            "unique_sources": self.unique_sources,
            "overall_quality": round(self.overall_quality, 3),
            "evidence_sufficient": self.evidence_sufficient,
        }


# ---- Keyword-based polarity detection (deterministic) ----

_NEGATIVE_MARKERS = frozenset([
    "not allowed", "prohibited", "denied", "must not", "cannot",
    "is not", "does not", "will not", "no ", "never", "forbidden",
    "restricted", "not permitted", "not authorized", "blocked",
])

_POSITIVE_MARKERS = frozenset([
    "allowed", "permitted", "required", "must", "approved",
    "is allowed", "does allow", "will", "always", "encouraged",
    "recommended", "mandated", "authorized",
])


def _text_polarity(text: str) -> str:
    """Determine polarity of a text fragment (negative/positive/neutral)."""
    lower = text.lower()
    has_neg = any(m in lower for m in _NEGATIVE_MARKERS)
    has_pos = any(m in lower for m in _POSITIVE_MARKERS)
    if has_neg and not has_pos:
        return "negative"
    if has_pos and not has_neg:
        return "positive"
    return "neutral"


def _token_overlap(query: str, content: str) -> float:
    """Lightweight relevance: fraction of query tokens found in content."""
    query_tokens = {t for t in re.sub(r"[^a-z0-9 ]", " ", query.lower()).split() if len(t) > 2}
    content_tokens = set(re.sub(r"[^a-z0-9 ]", " ", content.lower()).split())
    if not query_tokens:
        return 0.0
    return len(query_tokens & content_tokens) / len(query_tokens)


# ---- Public API ----


def classify_evidence_quality(
    evidence_items: list[dict[str, Any]],
    query: str = "",
) -> EvidenceAssessment:
    """Classify the quality of a set of evidence items.

    Each item is a dict with at least ``content`` and optionally ``source``,
    ``score``/``relevance``, ``recency``.  Deterministic — no LLM calls.
    """
    assessment = EvidenceAssessment()
    items: list[EvidenceItem] = []

    for raw in evidence_items:
        content = str(raw.get("content", raw.get("summary", "")))
        if not content.strip():
            continue

        source = str(raw.get("source", raw.get("chunk_id", "")))
        task_id = str(raw.get("task_id", raw.get("agent", "")))
        relevance = float(raw.get("score", raw.get("relevance", 0.0)))
        if relevance == 0.0 and query:
            relevance = _token_overlap(query, content)
        recency = str(raw.get("last_reviewed", raw.get("recency", "")))

        # Determine quality
        if relevance >= 0.7:
            quality = EvidenceQuality.STRONG
        elif relevance >= 0.4:
            quality = EvidenceQuality.MODERATE
        elif relevance >= 0.2:
            quality = EvidenceQuality.WEAK
        else:
            quality = EvidenceQuality.INSUFFICIENT

        item = EvidenceItem(
            content=content,
            source=source,
            task_id=task_id,
            relevance_score=relevance,
            quality=quality,
            recency=recency,
            metadata=raw.get("metadata", {}),
        )
        items.append(item)

    # Conflict detection across evidence items
    polarities: list[tuple[str, str, str]] = []  # (source, task_id, polarity)
    for item in items:
        pol = _text_polarity(item.content)
        polarities.append((item.source, item.task_id, pol))

    conflict_pairs: set[tuple[int, int]] = set()
    for i in range(len(polarities)):
        for j in range(i + 1, len(polarities)):
            _, _, pi = polarities[i]
            _, _, pj = polarities[j]
            if pi != "neutral" and pj != "neutral" and pi != pj:
                conflict_pairs.add((i, j))

    # Mark conflicting items
    conflicting_indices: set[int] = set()
    for i, j in conflict_pairs:
        conflicting_indices.add(i)
        conflicting_indices.add(j)
    for idx in conflicting_indices:
        if idx < len(items):
            items[idx].quality = EvidenceQuality.CONFLICTING

    # Aggregate
    assessment.items = items
    assessment.total_items = len(items)
    sources: set[str] = set()
    for item in items:
        sources.add(item.source or item.task_id or "unknown")
        if item.quality == EvidenceQuality.STRONG:
            assessment.strong_count += 1
        elif item.quality == EvidenceQuality.MODERATE:
            assessment.moderate_count += 1
        elif item.quality == EvidenceQuality.WEAK:
            assessment.weak_count += 1
        elif item.quality == EvidenceQuality.CONFLICTING:
            assessment.conflicting_count += 1
        elif item.quality == EvidenceQuality.INSUFFICIENT:
            assessment.insufficient_count += 1

    assessment.unique_sources = len(sources)

    # Overall quality score (0.0 - 1.0)
    if assessment.total_items == 0:
        assessment.overall_quality = 0.0
        assessment.evidence_sufficient = False
    else:
        quality_score = (
            0.4 * (assessment.strong_count / assessment.total_items)
            + 0.25 * (assessment.moderate_count / assessment.total_items)
            + 0.1 * (assessment.weak_count / assessment.total_items)
            - 0.15 * (assessment.conflicting_count / assessment.total_items)
        )
        # Bonus for source diversity
        diversity_bonus = min(0.15, 0.05 * min(assessment.unique_sources, 3))
        assessment.overall_quality = max(0.0, min(1.0, quality_score + diversity_bonus))
        # Sufficient: at least 1 strong or 2 moderate items from >= 2 sources
        assessment.evidence_sufficient = (
            (assessment.strong_count >= 1 or assessment.moderate_count >= 2)
            and assessment.unique_sources >= 2
        )

    return assessment


def assess_claim_support(
    claim: str,
    evidence: list[dict[str, Any]],
    threshold: float = 0.3,
) -> ClaimEvidence:
    """Assess how well a claim is supported by the given evidence.

    Deterministic keyword-overlap heuristic — no LLM calls.
    """
    claim_result = ClaimEvidence(claim_text=claim)

    if not evidence:
        claim_result.support_status = ClaimSupport.UNKNOWN
        claim_result.confidence = 0.0
        claim_result.gaps.append("No evidence provided")
        return claim_result

    supporting: list[EvidenceItem] = []
    contradicting: list[EvidenceItem] = []

    for raw in evidence:
        content = str(raw.get("content", raw.get("summary", "")))
        if not content.strip():
            continue

        overlap = _token_overlap(claim, content)
        polarity = _text_polarity(content)
        source = str(raw.get("source", raw.get("task_id", "")))

        item = EvidenceItem(
            content=content,
            source=source,
            relevance_score=overlap,
        )

        if overlap >= threshold:
            # Check polarity alignment with claim
            claim_polarity = _text_polarity(claim)
            if claim_polarity != "neutral" and polarity != "neutral" and claim_polarity != polarity:
                contradicting.append(item)
            else:
                supporting.append(item)

    claim_result.supporting_evidence = supporting
    claim_result.contradicting_evidence = contradicting

    # Determine support status
    if contradicting and supporting:
        claim_result.support_status = ClaimSupport.CONTRADICTED
        claim_result.confidence = max(0.0, 0.3 - 0.1 * len(contradicting))
    elif supporting and len(supporting) >= 2:
        claim_result.support_status = ClaimSupport.FULLY_SUPPORTED
        claim_result.confidence = min(1.0, 0.5 + 0.15 * len(supporting))
    elif supporting:
        claim_result.support_status = ClaimSupport.PARTIALLY_SUPPORTED
        claim_result.confidence = min(0.7, 0.3 + 0.15 * len(supporting))
    elif not supporting and not contradicting:
        # Some evidence exists but nothing relevant enough
        claim_result.support_status = ClaimSupport.WEAKLY_SUPPORTED
        claim_result.confidence = 0.1
        claim_result.gaps.append("Evidence exists but relevance is below threshold")
    else:
        claim_result.support_status = ClaimSupport.UNSUPPORTED
        claim_result.confidence = 0.0
        claim_result.gaps.append("No supporting evidence found")

    # Check for source diversity
    supporting_sources = {e.source for e in supporting if e.source}
    if len(supporting_sources) < 2 and claim_result.support_status in (
        ClaimSupport.FULLY_SUPPORTED, ClaimSupport.PARTIALLY_SUPPORTED
    ):
        claim_result.gaps.append("Single-source support — may need corroboration")

    return claim_result


def merge_evidence_assessments(
    *assessments: EvidenceAssessment,
) -> EvidenceAssessment:
    """Merge multiple evidence assessments (e.g. from different agents) into one."""
    merged = EvidenceAssessment()
    all_items: list[EvidenceItem] = []
    all_sources: set[str] = set()

    for a in assessments:
        all_items.extend(a.items)
        all_sources.add(str(a.unique_sources))  # approximate tracking

    merged.items = all_items
    merged.total_items = len(all_items)
    merged.unique_sources = len({i.source or i.task_id for i in all_items if i.source or i.task_id})

    for item in all_items:
        if item.quality == EvidenceQuality.STRONG:
            merged.strong_count += 1
        elif item.quality == EvidenceQuality.MODERATE:
            merged.moderate_count += 1
        elif item.quality == EvidenceQuality.WEAK:
            merged.weak_count += 1
        elif item.quality == EvidenceQuality.CONFLICTING:
            merged.conflicting_count += 1
        elif item.quality == EvidenceQuality.INSUFFICIENT:
            merged.insufficient_count += 1

    if merged.total_items > 0:
        quality_score = (
            0.4 * (merged.strong_count / merged.total_items)
            + 0.25 * (merged.moderate_count / merged.total_items)
            + 0.1 * (merged.weak_count / merged.total_items)
            - 0.15 * (merged.conflicting_count / merged.total_items)
        )
        diversity_bonus = min(0.15, 0.05 * min(merged.unique_sources, 3))
        merged.overall_quality = max(0.0, min(1.0, quality_score + diversity_bonus))
        merged.evidence_sufficient = (
            (merged.strong_count >= 1 or merged.moderate_count >= 2)
            and merged.unique_sources >= 2
        )

    return merged
