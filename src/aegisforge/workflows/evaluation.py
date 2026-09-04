from __future__ import annotations

import logging

from aegisforge.domain.models import (
    AgentResult,
    EvaluationResult,
    EvaluationVerdict,
)

logger = logging.getLogger(__name__)


class ResultEvaluator:
    """Evaluates whether an agent result satisfies task requirements.

    At this stage the evaluation covers:
    - schema validity (required fields present)
    - task completion (status == completed)
    - required fields in result
    - tool execution success
    - obvious failure conditions

    A more sophisticated semantic evaluation system will be built later.
    """

    def evaluate(
        self,
        agent_result: AgentResult,
        expected_fields: list[str] | None = None,
    ) -> EvaluationResult:
        """Evaluate an agent result and return a structured verdict."""
        reasons: list[str] = []
        score = 1.0
        retryable = False

        # Check 1: Agent must have completed
        if agent_result.status.value != "completed":
            reasons.append(f"Agent status is '{agent_result.status.value}', expected 'completed'")
            score *= 0.0
            retryable = agent_result.status.value in ("failed", "timeout")

        # Check 2: Result must not be empty
        if not agent_result.result:
            reasons.append("Agent result is empty")
            score *= 0.5

        # Check 3: Summary must be present
        if not agent_result.summary:
            reasons.append("Agent summary is missing")
            score *= 0.8

        # Check 4: No errors should be present for a completed result
        if agent_result.errors:
            reasons.append(f"Agent reported {len(agent_result.errors)} error(s)")
            score *= 0.5
            retryable = True

        # Check 5: Required fields in result
        if expected_fields:
            for field_name in expected_fields:
                if field_name not in agent_result.result:
                    reasons.append(f"Required field '{field_name}' missing from result")
                    score *= 0.7

        # Check 6: Tool calls should exist for research agents
        if agent_result.tool_calls and any(
            tc.get("status") != "completed" for tc in agent_result.tool_calls
        ):
            reasons.append("One or more tool calls did not complete successfully")
            score *= 0.6
            retryable = True

        # Determine verdict
        if score >= 0.7:
            verdict = EvaluationVerdict.PASSED
        elif retryable and score > 0.0:
            verdict = EvaluationVerdict.RETRY
        else:
            verdict = EvaluationVerdict.FAILED

        # Clamp score
        score = max(0.0, min(1.0, score))

        if not reasons:
            reasons.append("All checks passed")

        return EvaluationResult(
            verdict=verdict,
            score=score,
            reasons=reasons,
            retryable=retryable,
            details={
                "agent_name": agent_result.agent_name,
                "agent_status": agent_result.status.value,
                "has_tool_calls": bool(agent_result.tool_calls),
                "confidence": agent_result.confidence,
            },
        )
