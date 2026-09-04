"""OpenTelemetry-style tracing verification (Phase 4.2 Part 4).

Verifies the Tracer abstraction is wired into the actual workflow
execution path: spans are recorded for every graph node, failed nodes
produce error spans, the summary is attached to the final state, and
no secrets/sensitive values are stored in spans.
"""
from __future__ import annotations

from aegisforge.domain.models import RequestStatus
from aegisforge.observability.tracing import Tracer
from aegisforge.workflows.langgraph_workflow import execute_workflow


class TestTracerUnit:
    def test_span_records_duration_and_status(self):
        tracer = Tracer(request_id="req-1", workflow_id="wf-1")
        with tracer.span("plan"):
            pass
        summary = tracer.log_summary()
        assert summary["span_count"] == 1
        span = summary["spans"][0]
        assert span["name"] == "plan"
        assert span["status"] == "ok"
        assert span["duration_ms"] >= 0

    def test_failed_span_marks_error(self):
        import pytest as _pytest

        tracer = Tracer(request_id="req-1")
        with _pytest.raises(RuntimeError), tracer.span("execute_agent"):
            raise RuntimeError("boom")
        summary = tracer.log_summary()
        span = summary["spans"][0]
        assert span["status"] == "error"
        assert "boom" in span["attributes"]["error"]

    def test_sensitive_attributes_stripped(self):
        tracer = Tracer(request_id="req-1")
        with tracer.span(
            "llm",
            attributes={"api_key": "sk-secret", "model": "gpt-4o", "token": "abc"},
        ):
            pass
        span = tracer.log_summary()["spans"][0]
        assert "api_key" not in span["attributes"]
        assert "token" not in span["attributes"]
        assert span["attributes"]["model"] == "gpt-4o"


class TestWorkflowTracing:
    def test_workflow_records_node_spans(self):
        result = execute_workflow(
            request_id="req-trace-1",
            intent="Research tracing",
            user_id="user-1",
            organization_id="org-1",
            workflow_id="wf-trace-1",
        )
        assert result["status"] == RequestStatus.COMPLETED.value
        summary = result.get("trace_summary", {})
        assert summary.get("span_count", 0) >= 5  # all 5 graph nodes
        names = {s["name"] for s in summary["spans"]}
        assert {
            "validate_request",
            "plan",
            "execute_agent",
            "evaluate",
            "retry_or_complete",
        } <= names

    def test_trace_contains_no_secrets(self):
        result = execute_workflow(
            request_id="req-trace-2",
            intent="Research secrets",
            user_id="user-1",
            organization_id="org-1",
        )
        import json

        rendered = json.dumps(result.get("trace_summary", {}))
        assert "sk-" not in rendered
        assert "password" not in rendered.lower()
        assert "api_key" not in rendered

    def test_correlation_id_propagates_to_tracer(self):
        result = execute_workflow(
            request_id="req-trace-3",
            intent="Research correlation",
            user_id="user-1",
            organization_id="org-1",
            trace_id="trace-correlation-42",
        )
        summary = result.get("trace_summary", {})
        # The trace summary's request_id carries the correlation id
        assert summary["request_id"] == "trace-correlation-42"