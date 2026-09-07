"""Advanced RAG: hybrid retrieval, fusion, reranking, query expansion,
and context management (Phase 5).

Pipeline (all pieces individually testable and deterministic by default):

    Semantic (vector) search   +   Lexical (PostgreSQL tsvector) search
                        \\                /
                     Candidate fusion (reciprocal rank)
                              |
                          Reranking (pluggable)
                              |
                    Context assembly (budgeted, deduped,
                    source-diverse, citation-mapped)
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from aegisforge.domain.models import RetrievalQuery, RetrievalResult
from aegisforge.observability.metrics import record_rag_hybrid_retrieval
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.rag.vector_store import VectorStore, _tokenize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reciprocal-rank fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    semantic: list[RetrievalResult],
    lexical: list[RetrievalResult],
    top_k: int = 5,
    rrf_constant: int = 60,
) -> list[RetrievalResult]:
    """Fuse two ranked lists using Reciprocal Rank Fusion (RRF).

    Deterministic and parameter-light; does not require score normalization
    across retrieval systems.
    """
    fused: dict[str, float] = {}
    by_id: dict[str, RetrievalResult] = {}

    for rank, result in enumerate(semantic):
        fused[result.chunk_id] = fused.get(result.chunk_id, 0.0) + 1.0 / (rrf_constant + rank + 1)
        by_id[result.chunk_id] = result
    for rank, result in enumerate(lexical):
        fused[result.chunk_id] = fused.get(result.chunk_id, 0.0) + 1.0 / (rrf_constant + rank + 1)
        by_id[result.chunk_id] = result

    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
    return [by_id[cid] for cid, _ in ordered[:top_k]]


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------


@dataclass
class RerankStats:
    reranker: str = "none"
    candidates: int = 0
    kept: int = 0
    timeout: bool = False
    fell_back: bool = False
    latency_ms: int = 0


class Reranker(ABC):
    """Pluggable reranker: given candidate results + query, return reranked."""

    @abstractmethod
    def rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        top_k: int = 5,
    ) -> tuple[list[RetrievalResult], RerankStats]:
        ...


def _normalized_text(text: str) -> str:
    return " ".join(text.lower().split())


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class DeterministicReranker(Reranker):
    """Score re-balancing + near-duplicate removal + query overlap boost.

    score' = 0.6 * retrieval_score + 0.4 * query_token_overlap
    Chunks that are near-duplicates of a higher-ranked chunk are dropped so
    the final context is not dominated by one redundant passage.
    """

    def __init__(self, duplicate_threshold: float = 0.85) -> None:
        self._duplicate_threshold = duplicate_threshold

    def rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        top_k: int = 5,
    ) -> tuple[list[RetrievalResult], RerankStats]:
        start = time.monotonic()
        query_tokens = set(_tokenize(query))
        scored: list[tuple[float, RetrievalResult]] = []
        for result in results:
            content_tokens = set(_tokenize(result.content))
            overlap = (
                len(query_tokens & content_tokens) / len(query_tokens)
                if query_tokens and content_tokens
                else 0.0
            )
            new_score = 0.6 * result.score + 0.4 * overlap
            scored.append((new_score, result))
        scored.sort(key=lambda item: item[0], reverse=True)

        kept: list[RetrievalResult] = []
        for _, result in scored:
            duplicate = False
            for existing in kept:
                if _jaccard(
                    _normalized_text(result.content),
                    _normalized_text(existing.content),
                ) >= self._duplicate_threshold:
                    duplicate = True
                    break
            if duplicate:
                continue
            kept.append(result)
            if len(kept) >= top_k:
                break

        stats = RerankStats(
            reranker="deterministic",
            candidates=len(results),
            kept=len(kept),
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        return kept, stats


class LLMReranker(Reranker):
    """Optional LLM cross-encoder-style reranker.

    Never required for normal tests: when no model provider is configured the
    reranker falls back to the deterministic implementation.  Timeouts and
    malformed responses also fall back rather than failing the request.
    """

    def __init__(
        self,
        model_provider: Any | None = None,
        deterministic_fallback: Reranker | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        self._provider = model_provider
        self._fallback = deterministic_fallback or DeterministicReranker()
        self._timeout = timeout_seconds

    def rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        top_k: int = 5,
    ) -> tuple[list[RetrievalResult], RerankStats]:
        start = time.monotonic()
        if self._provider is None or not results:
            fallback_results, stats = self._fallback.rerank(query, results, top_k)
            stats.reranker = "llm"
            stats.fell_back = True
            stats.latency_ms = int((time.monotonic() - start) * 1000)
            return fallback_results, stats

        candidates = results[: max(top_k * 3, top_k)]
        payload = {
            "query": query,
            "chunks": [
                {"id": r.chunk_id, "content": r.content[:800]} for r in candidates
            ],
            "top_k": top_k,
        }
        try:
            response = self._provider.generate_structured(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "You are a retrieval reranker. Given the query and candidate "
                            "chunks, return ONLY JSON: {\"ranked_ids\": [\"chunk_id\", ...]} "
                            "ordered best-first with exactly the most relevant ids.\n\n"
                            + json.dumps(payload)
                        ),
                    }
                ],
                temperature=0.0,
                max_tokens=512,
            )
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            data = json.loads(content)
            ranked_ids = [str(i) for i in data.get("ranked_ids", [])]
            by_id = {r.chunk_id: r for r in results}
            reranked = [by_id[cid] for cid in ranked_ids if cid in by_id]
            if not reranked:
                raise ValueError("LLM reranker returned no known ids")
            stats = RerankStats(
                reranker="llm",
                candidates=len(results),
                kept=len(reranked[:top_k]),
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            return reranked[:top_k], stats
        except Exception as exc:
            logger.warning("LLM reranker failed (%s); falling back to deterministic", exc)
            fallback_results, stats = self._fallback.rerank(query, results, top_k)
            stats.reranker = "llm"
            stats.fell_back = True
            stats.timeout = True
            stats.latency_ms = int((time.monotonic() - start) * 1000)
            return fallback_results, stats


def get_reranker(
    reranker_type: str = "deterministic",
    model_provider: Any | None = None,
) -> Reranker:
    """Factory for rerankers (deterministic by default)."""
    if reranker_type == "deterministic":
        return DeterministicReranker()
    if reranker_type == "llm":
        return LLMReranker(model_provider=model_provider)
    raise ValueError(f"Unknown reranker type: {reranker_type}")


# ---------------------------------------------------------------------------
# Query expansion
# ---------------------------------------------------------------------------


class QueryExpander:
    """Produces a small, bounded set of retrieval queries for one user query.

    Deterministic expansions (keyword n-grams) always run; optional LLM-based
    expansions are attempted only when a provider is configured.  Expansion
    count is hard-capped by ``max_expansions``.
    """

    def __init__(
        self,
        max_expansions: int = 3,
        model_provider: Any | None = None,
    ) -> None:
        self._max = max_expansions
        self._provider = model_provider

    def expand(self, query: str) -> list[str]:
        if not query or not query.strip():
            return []
        expansions: list[str] = [query]
        tokens = _tokenize(query)
        if len(tokens) >= 2:
            expansions.append(" ".join(tokens[:4]))
        if len(tokens) >= 4:
            expansions.append(" ".join(tokens[1:5]))
        # Term-phrase expansion helps enterprise terminology (e.g. "support
        # escalation" stays as a phrase; bigrams surface both orders).
        if len(tokens) >= 2:
            expansions.append(" ".join(reversed(tokens[:2])))

        # Optional LLM-based terminology expansion (capped, best-effort).
        if self._provider is not None:
            try:
                response = self._provider.generate_structured(
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "Rewrite this enterprise knowledge query into up to "
                                f"{self._max} alternate phrasings that capture synonyms, "
                                "acronyms, and internal terminology. Return ONLY JSON: "
                                "{\"queries\": [\"...\"]}.\n\nQuery: " + query
                            ),
                        }
                    ],
                    temperature=0.0,
                    max_tokens=256,
                )
                content = response.content.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
                data = json.loads(content)
                for q in data.get("queries", []):
                    if len(expansions) >= self._max + 1:
                        break
                    if isinstance(q, str) and q.strip() and q not in expansions:
                        expansions.append(q.strip())
            except Exception as exc:
                logger.debug("LLM query expansion failed (non-fatal): %s", exc)

        return expansions[: self._max + 1]


# ---------------------------------------------------------------------------
# Context assembly / management
# ---------------------------------------------------------------------------


@dataclass
class AssembledContext:
    context_text: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    used_chunk_ids: list[str] = field(default_factory=list)
    dropped_redundant: int = 0
    dropped_for_budget: int = 0
    truncated: bool = False


class ContextAssembler:
    """Builds a final prompt context with safeguards:

    - hard token/character budget,
    - duplicate / near-duplicate chunk removal,
    - source diversity (per-source cap),
    - citation mapping so every included chunk is citeable.
    """

    def __init__(
        self,
        max_tokens: int = 2000,
        max_per_source: int = 3,
        duplicate_threshold: float = 0.8,
    ) -> None:
        # Approximate tokens as characters/4 (deterministic, no tokenizer dep).
        self._max_chars = max_tokens * 4
        self._max_per_source = max_per_source
        self._duplicate_threshold = duplicate_threshold

    def assemble(
        self,
        results: list[RetrievalResult],
    ) -> AssembledContext:
        if not results:
            return AssembledContext(context_text="")

        selected: list[RetrievalResult] = []
        per_source: dict[str, int] = {}
        budget = self._max_chars

        for result in results:
            source = result.source or result.document_id or "unknown"
            if per_source.get(source, 0) >= self._max_per_source:
                continue
            duplicate = False
            norm = _normalized_text(result.content)
            for existing in selected:
                if _jaccard(norm, _normalized_text(existing.content)) >= self._duplicate_threshold:
                    duplicate = True
                    break
            if duplicate:
                continue
            if len(result.content) > budget:
                if not selected:
                    # Nothing fits — include a truncated first chunk.
                    selected.append(result)
                else:
                    # No budget left for this (or any later) chunk.
                    break
                budget = 0
                continue
            selected.append(result)
            per_source[source] = per_source.get(source, 0) + 1
            budget -= len(result.content)

        assembled = AssembledContext(
            context_text="",
            used_chunk_ids=[r.chunk_id for r in selected],
            dropped_redundant=len(results) - len(selected),
            truncated=budget <= 0 and len(selected) < len(results),
        )

        parts: list[str] = []
        citations: list[dict[str, Any]] = []
        for index, result in enumerate(selected, start=1):
            source_label = result.source or f"Document {result.document_id}"
            marker = f"[{index}]"
            parts.append(f"{marker} {source_label} (score: {result.score:.3f})\n{result.content}")
            citations.append(
                {
                    "marker": index,
                    "chunk_id": result.chunk_id,
                    "document_id": result.document_id,
                    "source": result.source,
                }
            )
        if parts:
            header = "Retrieved evidence (citations refer to the numbered sources):\n\n"
            assembled.context_text = header + "\n\n---\n\n".join(parts)
        assembled.citations = citations
        return assembled


# ---------------------------------------------------------------------------
# Hybrid retrieval service
# ---------------------------------------------------------------------------


@dataclass
class HybridRetrievalResponse:
    query: str
    results: list[RetrievalResult] = field(default_factory=list)
    semantic_results: list[RetrievalResult] = field(default_factory=list)
    lexical_results: list[RetrievalResult] = field(default_factory=list)
    expansions: list[str] = field(default_factory=list)
    context: AssembledContext | None = None
    rerank_stats: RerankStats = field(default_factory=RerankStats)
    latency_ms: int = 0


class HybridRetrievalService:
    """Composes semantic + lexical retrieval with fusion → rerank → context.

    ``semantic`` is the existing ``RetrievalService``; lexical search runs on
    the same ``vector_store`` used by semantic retrieval so PostgreSQL-native
    tsvector search is reused for production.
    """

    def __init__(
        self,
        semantic: RetrievalService,
        vector_store: VectorStore,
        reranker: Reranker | None = None,
        query_expander: QueryExpander | None = None,
        context_assembler: ContextAssembler | None = None,
        fusion_candidates: int = 60,
        lexical_top_k: int = 10,
    ) -> None:
        self._semantic = semantic
        self._vector_store = vector_store
        self._reranker = reranker or DeterministicReranker()
        self._expander = query_expander or QueryExpander(max_expansions=0)
        self._assembler = context_assembler or ContextAssembler()
        self._fusion_candidates = max(fusion_candidates, 10)
        self._lexical_top_k = lexical_top_k
        self._last_query: str = ""
        self._last_org: str = ""
        self._last_top_k: int = 5
        self._last_threshold: float = 0.0
        self._last_use_expansion: bool = False

    def retrieve(
        self,
        query: RetrievalQuery,
        use_query_expansion: bool = False,
    ) -> HybridRetrievalResponse:
        """Run hybrid retrieval and return structured results + context."""
        if not query.query or not query.query.strip():
            return HybridRetrievalResponse(query=query.query)

        start = time.monotonic()
        expansions = self._expander.expand(query.query) if use_query_expansion else [query.query]

        # Semantic candidates (a larger pool than the final top_k).
        pool = max(query.top_k * 2, min(self._fusion_candidates, 40))
        semantic_results: list[RetrievalResult] = []
        for expansion in expansions[:3]:
            semantic_results.extend(
                self._semantic.retrieve(
                    RetrievalQuery(
                        query=expansion,
                        organization_id=query.organization_id,
                        top_k=pool,
                        similarity_threshold=query.similarity_threshold,
                        metadata_filter=query.metadata_filter,
                    )
                )
            )

        lexical_results: list[RetrievalResult] = []
        try:
            lexical_hits = self._vector_store.lexical_search(
                query=query.query,
                top_k=self._lexical_top_k,
                organization_id=query.organization_id,
                metadata_filter=query.metadata_filter or None,
            )
            for hit in lexical_hits:
                doc_id = hit.metadata.get("document_id", "")
                lexical_results.append(
                    RetrievalResult(
                        chunk_id=hit.id,
                        document_id=doc_id,
                        content=hit.content,
                        score=hit.score,
                        source=hit.metadata.get("source", ""),
                        metadata=hit.metadata,
                    )
                )
        except NotImplementedError:
            lexical_results = []
        except Exception as exc:
            logger.warning("Lexical search failed (non-fatal): %s", exc)
            lexical_results = []

        if not semantic_results and not lexical_results:
            logger.debug(
                "Hybrid retrieval returned no candidates for query %r in org %r",
                query.query,
                query.organization_id,
            )

        # Fusion.
        fused = reciprocal_rank_fusion(
            semantic_results,
            lexical_results,
            top_k=pool,
        )

        # Rerank.
        reranked, rerank_stats = self._reranker.rerank(
            query.query,
            fused,
            top_k=query.top_k,
        )

        # Remember the last retrieval parameters so the adapter can rebuild
        # the final optimized context without re-deriving query intent.
        self._last_query = query.query
        self._last_org = query.organization_id
        self._last_top_k = query.top_k
        self._last_threshold = query.similarity_threshold
        self._last_use_expansion = use_query_expansion

        # Context management.
        context = self._assembler.assemble(reranked)

        response = HybridRetrievalResponse(
            query=query.query,
            results=reranked,
            semantic_results=semantic_results,
            lexical_results=lexical_results,
            expansions=expansions,
            context=context,
            rerank_stats=rerank_stats,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        try:
            record_rag_hybrid_retrieval(
                response,
                semantic_candidates=len(semantic_results),
                lexical_candidates=len(lexical_results),
            )
        except Exception:  # noqa: S110 - observability must never break retrieval
            pass
        return response


class RAGHybridAdapter(RetrievalService):
    """Presents hybrid retrieval through the RetrievalService interface used
    by RAGAgent (``retrieve`` → list, ``build_context`` → str).

    When hybrid mode is enabled, RAG tasks transparently benefit from
    fusion + reranking + managed context without changing agent code.

    The adapter exposes the hybrid assembler's final context so the final
    evidence set returned to the agent reflects reranking and context
    optimization rather than raw retrieval results.
    """

    is_hybrid: bool = True

    def __init__(
        self,
        hybrid: HybridRetrievalService,
        context_assembler: ContextAssembler | None = None,
    ) -> None:
        self._hybrid = hybrid
        self._assembler = context_assembler or ContextAssembler()

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        response = self._hybrid.retrieve(query)
        return response.results

    def build_context(
        self,
        results: list[RetrievalResult] | None = None,
        max_context_length: int = 4000,
    ) -> str:
        """Build final grounded context from hybrid-optimized results.

        If results are provided, they are used directly. If not, the adapter
        re-runs the hybrid retrieval for the most recent query so the final
        context reflects the optimizer's selected and deduped evidence.
        """
        if results is None:
            from aegisforge.domain.models import RetrievalQuery

            query_text = self._hybrid._last_query or ""
            if not query_text:
                return ""
            response = self._hybrid.retrieve(
                RetrievalQuery(
                    query=query_text,
                    organization_id=self._hybrid._last_org or "",
                    top_k=self._hybrid._last_top_k,
                    similarity_threshold=self._hybrid._last_threshold,
                ),
                use_query_expansion=self._hybrid._last_use_expansion,
            )
            results = response.results

        assembled = self._assembler.assemble(results)
        return assembled.context_text

    def get_document_count(self, organization_id: str = "") -> int:
        return self._hybrid._semantic.get_document_count(organization_id)  # type: ignore[attr-defined]

    def delete_document(self, document_id: str, organization_id: str = "") -> int:
        return self._hybrid._semantic.delete_document(document_id, organization_id)  # type: ignore[attr-defined]


def build_hybrid_retrieval_adapter(
    semantic: RetrievalService,
    vector_store: VectorStore,
    *,
    reranker_type: str = "deterministic",
    model_provider: Any | None = None,
    query_expansion_enabled: bool = False,
    query_expansion_max: int = 0,
    fusion_candidates: int = 60,
    lexical_top_k: int = 10,
    context_max_tokens: int = 2000,
) -> RAGHybridAdapter:
    """Build a hybrid retrieval adapter from existing pieces."""
    hybrid = HybridRetrievalService(
        semantic=semantic,
        vector_store=vector_store,
        reranker=get_reranker(reranker_type, model_provider),
        query_expander=QueryExpander(
            max_expansions=query_expansion_max if query_expansion_enabled else 0,
            model_provider=model_provider if query_expansion_enabled else None,
        ),
        context_assembler=ContextAssembler(max_tokens=context_max_tokens),
        fusion_candidates=fusion_candidates,
        lexical_top_k=lexical_top_k,
    )
    return RAGHybridAdapter(hybrid=hybrid)
