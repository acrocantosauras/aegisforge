"""RAG / Knowledge Agent for AegisForge.

Retrieves authorized content, builds grounded context, and produces
structured responses with citations.  Handles insufficient context
gracefully rather than hallucinating.
"""
from __future__ import annotations

import logging
from typing import Any

from aegisforge.agents.base import AgentExecutionContext, BaseAgent, PermissionSpec
from aegisforge.domain.models import (
    AgentExecutionStatus,
    AgentResult,
    AgentType,
)
from aegisforge.rag.retrieval import RetrievalService

logger = logging.getLogger(__name__)


class RAGAgent(BaseAgent):
    """RAG / Knowledge Agent.

    Workflow:
        Task → Retrieve relevant chunks → Build grounded context →
        Produce structured response → Citations/evidence

    The agent:
    - Retrieves only authorized content (tenant-scoped)
    - Provides source references (citations)
    - Distinguishes retrieved evidence from generated reasoning
    - Handles insufficient context gracefully
    """

    def __init__(
        self,
        name: str = "rag-agent",
        description: str = "Retrieves authorized knowledge and produces grounded responses.",
        permissions: list[PermissionSpec] | None = None,
        retrieval_service: RetrievalService | None = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type=AgentType.RAG,
            description=description,
            permissions=permissions or [],
        )
        self._retrieval_service = retrieval_service

    def _execute(self, input_data: dict[str, Any], context: AgentExecutionContext) -> AgentResult:
        query = input_data.get("query", "")
        if not query:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="No query provided for RAG execution",
                errors=["Query is required for RAG execution"],
            )

        if self._retrieval_service is None:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.FAILED,
                summary="RAG retrieval service not configured",
                errors=["Retrieval service is required for RAG execution"],
            )

        # Build retrieval query with tenant scoping
        from aegisforge.domain.models import RetrievalQuery

        top_k = input_data.get("top_k", 5)
        similarity_threshold = input_data.get("similarity_threshold", 0.3)

        retrieval_query = RetrievalQuery(
            query=query,
            organization_id=context.organization_id,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )

        # Retrieve authorized content
        retrieval_results = self._retrieval_service.retrieve(retrieval_query)

        # Build citations
        citations: list[dict[str, Any]] = []
        evidence_texts: list[str] = []

        for result in retrieval_results:
            citation = {
                "chunk_id": result.chunk_id,
                "document_id": result.document_id,
                "source": result.source,
                "score": result.score,
                "snippet": result.content[:200] + "..." if len(result.content) > 200 else result.content,
            }
            citations.append(citation)
            evidence_texts.append(result.content)

        # Build grounded context
        context_text = self._retrieval_service.build_context(retrieval_results)

        # Handle insufficient context
        if not retrieval_results:
            return AgentResult(
                agent_name=self.name,
                agent_type=self.agent_type,
                status=AgentExecutionStatus.COMPLETED,
                summary=(
                    f"No relevant knowledge found for query: '{query}'. "
                    "The organization may need to ingest documents covering this topic."
                ),
                result={
                    "query": query,
                    "answer": "Insufficient context available to answer this question.",
                    "citations": [],
                    "context_available": False,
                    "retrieval_count": 0,
                },
                evidence=[],
                confidence=0.0,
                tool_calls=[],
            )

        # Build structured response with evidence
        answer_parts: list[str] = []
        answer_parts.append(f"Based on {len(retrieval_results)} retrieved document(s):\n")

        for i, result in enumerate(retrieval_results, 1):
            source = result.source or f"Document {result.document_id}"
            answer_parts.append(f"{i}. [{source}] (relevance: {result.score:.2f})")
            answer_parts.append(f"   {result.content[:300]}")
            answer_parts.append("")

        # Distinguish evidence from reasoning
        answer_parts.append("---")
        answer_parts.append(
            "Note: The above is based on retrieved document evidence. "
            "It reflects the content of indexed documents and should be verified "
            "against the original sources."
        )

        summary = f"Retrieved {len(retrieval_results)} relevant chunk(s) for: {query}"

        return AgentResult(
            agent_name=self.name,
            agent_type=self.agent_type,
            status=AgentExecutionStatus.COMPLETED,
            summary=summary,
            result={
                "query": query,
                "answer": "\n".join(answer_parts),
                "citations": citations,
                "context_available": True,
                "retrieval_count": len(retrieval_results),
                "grounded_context_preview": context_text[:500] if context_text else "",
            },
            evidence=citations,
            confidence=min(1.0, max(r.score for r in retrieval_results)) if retrieval_results else 0.0,
            tool_calls=[],
        )
