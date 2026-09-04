from __future__ import annotations

from typing import Any

from aegisforge.tools.base import BaseTool, ToolDefinition

# --- Curated knowledge base (deterministic, no LLM required) ---

_KNOWLEDGE_BASE: dict[str, dict[str, str]] = {
    "support escalation": {
        "topic": "Support Escalation Policy",
        "summary": (
            "Customer support issues must be triaged within 4 business hours. "
            "Severity 1 (critical business impact) requires immediate escalation to the on-call engineering lead. "
            "Severity 2 issues are escalated to the team lead within 24 hours. "
            "All escalations must include a ticket ID, reproduction steps, and impact assessment."
        ),
        "source": "internal-policy/support-escalation-v2.1",
        "last_reviewed": "2026-08-15",
    },
    "incident response": {
        "topic": "Incident Response Procedure",
        "summary": (
            "Incidents are classified as P1 (service down), P2 (degraded), P3 (minor). "
            "P1 incidents require acknowledgment within 15 minutes and a status page update within 30 minutes. "
            "A post-incident review must be completed within 48 hours of resolution."
        ),
        "source": "internal-policy/incident-response-v3.0",
        "last_reviewed": "2026-07-20",
    },
    "data access": {
        "topic": "Data Access Policy",
        "summary": (
            "Access to customer data requires manager approval and a documented business justification. "
            "Production database access is restricted to the SRE team during active incident resolution. "
            "All data access is logged and audited quarterly."
        ),
        "source": "internal-policy/data-access-v1.4",
        "last_reviewed": "2026-06-01",
    },
    "code review": {
        "topic": "Code Review Standards",
        "summary": (
            "All production code changes require at least one peer review. "
            "Security-sensitive changes require review from the security team. "
            "Automated tests must pass before merge. Branch protection rules enforce these requirements."
        ),
        "source": "engineering/code-review-v2.0",
        "last_reviewed": "2026-08-01",
    },
}


class KnowledgeSearchTool(BaseTool):
    """Searches approved internal knowledge sources.

    This is a safe, deterministic tool that returns curated information.
    No network access, no file system access, no LLM calls.
    """

    def __init__(self) -> None:
        definition = ToolDefinition(
            name="knowledge.search",
            description="Search approved internal knowledge sources for policy and procedural information.",
            version="1.0",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "summary": {"type": "string"},
                    "source": {"type": "string"},
                    "last_reviewed": {"type": "string"},
                },
            },
            permission_requirements=["knowledge.search"],
            timeout_seconds=10,
        )
        super().__init__(definition)
        self._kb = _KNOWLEDGE_BASE

    def _execute(self, input_data: dict[str, Any]) -> dict[str, str]:
        query = str(input_data.get("query", "")).lower().strip()
        if not query:
            return {"topic": "", "summary": "Empty query provided.", "source": "", "last_reviewed": ""}

        # Simple keyword matching against curated knowledge base
        best_match: dict[str, str] | None = None
        best_score = 0
        for key, entry in self._kb.items():
            score = sum(1 for word in key.split() if word in query)
            if score > best_score:
                best_score = score
                best_match = entry

        if best_match is None:
            # Fallback: return a generic "no match" result
            return {
                "topic": "No direct match found",
                "summary": (
                    f"No approved internal knowledge source directly matched the query '{query}'. "
                    "Consider refining the query or consulting the knowledge management team."
                ),
                "source": "knowledge.search/no-match",
                "last_reviewed": "",
            }

        return best_match
