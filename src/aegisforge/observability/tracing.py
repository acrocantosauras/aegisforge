"""Observability tracing for AegisForge.

Captures latency, status, execution IDs, model metadata, retrieval counts,
tool calls, retries, failures, and approval waits across the full pipeline.

Does NOT log: API keys, passwords, authentication tokens, private content.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """A single trace span representing a unit of work."""

    name: str
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: int = 0
    status: str = "ok"  # ok, error, timeout
    attributes: dict[str, Any] = field(default_factory=dict)
    parent_span_id: str = ""
    span_id: str = ""

    def finish(self, status: str = "ok") -> None:
        self.end_time = time.monotonic()
        self.duration_ms = int((self.end_time - self.start_time) * 1000)
        self.status = status


@dataclass
class TraceContext:
    """Full trace context for a request through the system."""

    request_id: str
    workflow_id: str = ""
    spans: list[Span] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_span(self, span: Span) -> None:
        self.spans.append(span)

    def get_span_by_name(self, name: str) -> Span | None:
        for span in self.spans:
            if span.name == name:
                return span
        return None

    def total_duration_ms(self) -> int:
        if not self.spans:
            return 0
        return sum(s.duration_ms for s in self.spans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "workflow_id": self.workflow_id,
            "span_count": len(self.spans),
            "total_duration_ms": self.total_duration_ms(),
            "spans": [
                {
                    "name": s.name,
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                    "attributes": {
                        k: v
                        for k, v in s.attributes.items()
                        if not any(
                            sensitive in k.lower()
                            for sensitive in ["key", "token", "password", "secret"]
                        )
                    },
                }
                for s in self.spans
            ],
            "metadata": self.metadata,
        }


class Tracer:
    """Request-level tracer that collects spans."""

    def __init__(self, request_id: str, workflow_id: str = "") -> None:
        self.context = TraceContext(
            request_id=request_id,
            workflow_id=workflow_id,
        )

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[Span, None, None]:
        """Context manager for a trace span."""
        span = Span(
            name=name,
            start_time=time.monotonic(),
            attributes=attributes or {},
        )
        self.context.add_span(span)
        try:
            yield span
            span.finish("ok")
        except Exception as exc:
            span.finish("error")
            span.attributes["error"] = str(exc)
            raise
        finally:
            logger.info(
                "Span %s: %d ms [%s] (request=%s)",
                name,
                span.duration_ms,
                span.status,
                self.context.request_id,
            )

    def log_summary(self) -> dict[str, Any]:
        """Log a summary of the trace (without sensitive data)."""
        summary = self.context.to_dict()
        logger.info(
            "Trace complete: request=%s spans=%d duration=%dms",
            summary["request_id"],
            summary["span_count"],
            summary["total_duration_ms"],
        )
        return summary


@dataclass
class ExecutionMetrics:
    """Collected execution metrics for a workflow."""

    request_id: str = ""
    workflow_id: str = ""

    # Latency measurements (ms)
    planner_latency_ms: int = 0
    retrieval_latency_ms: int = 0
    model_latency_ms: int = 0
    tool_latency_ms: int = 0
    workflow_duration_ms: int = 0
    queue_latency_ms: int = 0

    # Counts
    retrieval_count: int = 0
    tool_call_count: int = 0
    retry_count: int = 0
    failure_count: int = 0
    approval_wait_count: int = 0

    # Status
    final_status: str = ""
    planner_type: str = ""

    # Model metadata
    model_name: str = ""
    tokens_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "workflow_id": self.workflow_id,
            "planner_latency_ms": self.planner_latency_ms,
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "model_latency_ms": self.model_latency_ms,
            "tool_latency_ms": self.tool_latency_ms,
            "workflow_duration_ms": self.workflow_duration_ms,
            "queue_latency_ms": self.queue_latency_ms,
            "retrieval_count": self.retrieval_count,
            "tool_call_count": self.tool_call_count,
            "retry_count": self.retry_count,
            "failure_count": self.failure_count,
            "approval_wait_count": self.approval_wait_count,
            "final_status": self.final_status,
            "planner_type": self.planner_type,
            "model_name": self.model_name,
            "tokens_used": self.tokens_used,
        }


class MetricsCollector:
    """Collects and stores execution metrics."""

    def __init__(self) -> None:
        self._metrics: dict[str, ExecutionMetrics] = {}

    def create(self, request_id: str, workflow_id: str = "") -> ExecutionMetrics:
        metrics = ExecutionMetrics(
            request_id=request_id,
            workflow_id=workflow_id,
        )
        self._metrics[request_id] = metrics
        return metrics

    def get(self, request_id: str) -> ExecutionMetrics | None:
        return self._metrics.get(request_id)

    def list_metrics(self, limit: int = 100) -> list[ExecutionMetrics]:
        metrics = list(self._metrics.values())
        metrics.sort(key=lambda m: m.request_id, reverse=True)
        return metrics[:limit]

    def get_summary(self) -> dict[str, Any]:
        """Get aggregate metrics summary."""
        all_metrics = list(self._metrics.values())
        if not all_metrics:
            return {"count": 0}

        return {
            "count": len(all_metrics),
            "avg_workflow_duration_ms": sum(m.workflow_duration_ms for m in all_metrics) // len(all_metrics),
            "avg_planner_latency_ms": sum(m.planner_latency_ms for m in all_metrics) // len(all_metrics),
            "avg_retrieval_latency_ms": sum(m.retrieval_latency_ms for m in all_metrics) // max(1, sum(1 for m in all_metrics if m.retrieval_latency_ms > 0)),
            "total_tool_calls": sum(m.tool_call_count for m in all_metrics),
            "total_retries": sum(m.retry_count for m in all_metrics),
            "total_failures": sum(m.failure_count for m in all_metrics),
            "success_rate": (
                sum(1 for m in all_metrics if m.final_status == "completed") / len(all_metrics)
                if all_metrics else 0
            ),
        }


# Global metrics collector
_default_metrics_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """Return the global metrics collector singleton."""
    global _default_metrics_collector
    if _default_metrics_collector is None:
        _default_metrics_collector = MetricsCollector()
    return _default_metrics_collector


def reset_metrics_collector() -> None:
    """Reset the global metrics collector (for tests)."""
    global _default_metrics_collector
    _default_metrics_collector = None
