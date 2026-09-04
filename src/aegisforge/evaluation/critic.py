"""LLM-based critic for AegisForge.

Optional LLM critic that evaluates quality of RAG responses, plans,
and final answers.  Combined with deterministic evaluation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from aegisforge.domain.models import AgentResult, ExecutionPlan, RetrievalResult
from aegisforge.llm.providers import ModelProvider

logger = logging.getLogger(__name__)


class CriticResult(BaseModel):
    """Structured result from the LLM critic."""

    score: float = 0.0
    verdict: str = "pending"  # passed, failed, partial
    criteria: dict[str, float] = field(default_factory=dict)
    reasoning: str = ""
    failures: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    evaluator_type: str = "llm_critic"


class RAGCriticSchema(BaseModel):
    """Expected schema for RAG evaluation output."""

    relevance: float = 0.0
    groundedness: float = 0.0
    citation_correctness: float = 0.0
    completeness: float = 0.0
    hallucination_risk: float = 0.0
    overall_score: float = 0.0
    reasoning: str = ""
    failures: list[str] = field(default_factory=list)


class PlanCriticSchema(BaseModel):
    """Expected schema for plan evaluation output."""

    decomposition_quality: float = 0.0
    unnecessary_tasks: int = 0
    dependency_correctness: float = 0.0
    agent_selection: float = 0.0
    plan_efficiency: float = 0.0
    overall_score: float = 0.0
    reasoning: str = ""
    failures: list[str] = field(default_factory=list)


class ResponseCriticSchema(BaseModel):
    """Expected schema for response evaluation output."""

    correctness: float = 0.0
    relevance: float = 0.0
    completeness: float = 0.0
    clarity: float = 0.0
    unsupported_claims: int = 0
    overall_score: float = 0.0
    reasoning: str = ""
    failures: list[str] = field(default_factory=list)


RAG_CRITIC_PROMPT = """You are an evaluation critic for a RAG (Retrieval-Augmented Generation) system.

Evaluate the following retrieval results and agent response.

RETRIEVAL RESULTS:
{retrieval_context}

AGENT RESPONSE:
{agent_response}

QUERY:
{query}

Rate each criterion from 0.0 to 1.0:
- relevance: How relevant are the retrieved chunks to the query?
- groundedness: How well is the response grounded in the retrieved evidence?
- citation_correctness: Are the citations/references accurate?
- completeness: Does the response adequately cover the topic?
- hallucination_risk: Risk of fabricated information (0=none, 1=high risk)

Respond with valid JSON matching this schema:
{{
    "relevance": 0.0,
    "groundedness": 0.0,
    "citation_correctness": 0.0,
    "completeness": 0.0,
    "hallucination_risk": 0.0,
    "overall_score": 0.0,
    "reasoning": "brief explanation",
    "failures": ["list of issues found"]
}}
"""

PLAN_CRITIC_PROMPT = """You are an evaluation critic for an execution plan.

Evaluate the following execution plan.

PLAN:
{plan_json}

Rate each criterion from 0.0 to 1.0:
- decomposition_quality: How well is the task decomposed?
- dependency_correctness: Are dependencies correct and non-circular?
- agent_selection: Are the right agents assigned to tasks?
- plan_efficiency: Is the plan efficient (no unnecessary steps)?

Respond with valid JSON matching this schema:
{{
    "decomposition_quality": 0.0,
    "unnecessary_tasks": 0,
    "dependency_correctness": 0.0,
    "agent_selection": 0.0,
    "plan_efficiency": 0.0,
    "overall_score": 0.0,
    "reasoning": "brief explanation",
    "failures": ["list of issues found"]
}}
"""

RESPONSE_CRITIC_PROMPT = """You are an evaluation critic for an AI response.

Evaluate the following response to the given query.

QUERY:
{query}

RESPONSE:
{response}

EVIDENCE:
{evidence}

Rate each criterion from 0.0 to 1.0:
- correctness: Is the information correct?
- relevance: Is the response relevant to the query?
- completeness: Does it adequately address the query?
- clarity: Is it clear and well-structured?

Count any unsupported claims (claims not backed by evidence).

Respond with valid JSON matching this schema:
{{
    "correctness": 0.0,
    "relevance": 0.0,
    "completeness": 0.0,
    "clarity": 0.0,
    "unsupported_claims": 0,
    "overall_score": 0.0,
    "reasoning": "brief explanation",
    "failures": ["list of issues found"]
}}
"""


class LLMCritic:
    """LLM-based critic for evaluating RAG, planning, and response quality.

    The critic must NOT automatically declare success based only on its own
    reasoning.  It returns structured scores for combination with
    deterministic evaluation.
    """

    def __init__(self, model_provider: ModelProvider | None = None) -> None:
        self._model_provider = model_provider

    def evaluate_rag(
        self,
        query: str,
        agent_result: AgentResult,
        retrieval_results: list[RetrievalResult] | None = None,
    ) -> CriticResult:
        """Evaluate RAG quality using LLM critic."""
        if self._model_provider is None:
            return CriticResult(
                score=0.5,
                verdict="skipped",
                reasoning="No LLM provider configured for critic",
                evaluator_type="llm_critic",
            )

        # Build retrieval context
        retrieval_context = ""
        if retrieval_results:
            for i, r in enumerate(retrieval_results, 1):
                retrieval_context += f"\n[Source {i}] (score: {r.score:.2f})\n{r.content[:500]}\n"
        else:
            retrieval_context = "No retrieval results available."

        agent_response = agent_result.result.get("answer", agent_result.summary, )

        prompt = RAG_CRITIC_PROMPT.format(
            retrieval_context=retrieval_context or "No context",
            agent_response=agent_response or "No response",
            query=query,
        )

        try:
            response = self._model_provider.generate(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=1024,
            )

            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

            data = json.loads(content)
            validated = RAGCriticSchema.model_validate(data)

            return CriticResult(
                score=validated.overall_score,
                verdict="passed" if validated.overall_score >= 0.7 else "failed",
                criteria={
                    "relevance": validated.relevance,
                    "groundedness": validated.groundedness,
                    "citation_correctness": validated.citation_correctness,
                    "completeness": validated.completeness,
                    "hallucination_risk": validated.hallucination_risk,
                },
                reasoning=validated.reasoning,
                failures=validated.failures,
                evaluator_type="llm_critic",
            )
        except Exception as exc:
            logger.warning("RAG critic evaluation failed: %s", exc)
            return CriticResult(
                score=0.5,
                verdict="error",
                reasoning=f"Critic evaluation failed: {exc}",
                evaluator_type="llm_critic",
            )

    def evaluate_plan(self, plan: ExecutionPlan) -> CriticResult:
        """Evaluate plan quality using LLM critic."""
        if self._model_provider is None:
            return CriticResult(
                score=0.5,
                verdict="skipped",
                reasoning="No LLM provider configured for critic",
                evaluator_type="llm_critic",
            )

        plan_json = json.dumps(
            {
                "plan_id": plan.plan_id,
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "description": t.description,
                        "agent_type": t.assigned_agent_type.value,
                        "dependencies": t.dependencies,
                    }
                    for t in plan.tasks
                ],
            },
            indent=2,
        )

        prompt = PLAN_CRITIC_PROMPT.format(plan_json=plan_json)

        try:
            response = self._model_provider.generate(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=1024,
            )

            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

            data = json.loads(content)
            validated = PlanCriticSchema.model_validate(data)

            return CriticResult(
                score=validated.overall_score,
                verdict="passed" if validated.overall_score >= 0.7 else "failed",
                criteria={
                    "decomposition_quality": validated.decomposition_quality,
                    "dependency_correctness": validated.dependency_correctness,
                    "agent_selection": validated.agent_selection,
                    "plan_efficiency": validated.plan_efficiency,
                },
                reasoning=validated.reasoning,
                failures=validated.failures,
                evaluator_type="llm_critic",
            )
        except Exception as exc:
            logger.warning("Plan critic evaluation failed: %s", exc)
            return CriticResult(
                score=0.5,
                verdict="error",
                reasoning=f"Critic evaluation failed: {exc}",
                evaluator_type="llm_critic",
            )

    def evaluate_response(
        self,
        query: str,
        response_text: str,
        evidence: str = "",
    ) -> CriticResult:
        """Evaluate response quality using LLM critic."""
        if self._model_provider is None:
            return CriticResult(
                score=0.5,
                verdict="skipped",
                reasoning="No LLM provider configured for critic",
                evaluator_type="llm_critic",
            )

        prompt = RESPONSE_CRITIC_PROMPT.format(
            query=query,
            response=response_text[:3000],
            evidence=evidence[:2000] or "No evidence provided",
        )

        try:
            response = self._model_provider.generate(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=1024,
            )

            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

            data = json.loads(content)
            validated = ResponseCriticSchema.model_validate(data)

            return CriticResult(
                score=validated.overall_score,
                verdict="passed" if validated.overall_score >= 0.7 else "failed",
                criteria={
                    "correctness": validated.correctness,
                    "relevance": validated.relevance,
                    "completeness": validated.completeness,
                    "clarity": validated.clarity,
                },
                reasoning=validated.reasoning,
                failures=validated.failures + (
                    [f"{validated.unsupported_claims} unsupported claim(s)"]
                    if validated.unsupported_claims > 0
                    else []
                ),
                evaluator_type="llm_critic",
            )
        except Exception as exc:
            logger.warning("Response critic evaluation failed: %s", exc)
            return CriticResult(
                score=0.5,
                verdict="error",
                reasoning=f"Critic evaluation failed: {exc}",
                evaluator_type="llm_critic",
            )
