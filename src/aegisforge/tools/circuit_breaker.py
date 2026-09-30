"""Tool circuit breaker: fast-fail around repeatedly failing tools.

Phase 6G (extended reliability sprint), Workstream 1.

A circuit breaker lives AROUND tool execution at the ``ToolRegistry.execute()``
choke point.  It is deliberately NOT a retry system: retries stay owned by the
agents/scheduler; the breaker only decides whether an execution attempt should
be attempted at all.

State machine::

    CLOSED ──(N qualifying failures)──▶ OPEN
    OPEN   ──(cooldown elapsed)─────▶ HALF_OPEN
    HALF_OPEN ──(probe succeeds)────▶ CLOSED
    HALF_OPEN ──(probe fails)───────▶ OPEN

Design rules (from the sprint contract):

- Only *availability* failures may trip the circuit: timeouts, connection
  failures, transient/transport failures.  Policy outcomes (permission,
  policy, approval, disabled, unknown tool, tenant/auth failures) MUST NOT
  count as availability evidence.
- OPEN circuits fast-fail with a structured ``ToolExecutionResult`` whose
  status is ``FAILED`` and whose error is prefixed ``CIRCUIT_OPEN`` — callers
  classify it as transient/unavailable, never as permission.
- HALF_OPEN admits at most ONE concurrent probe; all other callers fast-fail.
  A successful probe closes the circuit; a failed probe re-opens it.
- Bounded state: the breaker keeps at most the last ``N`` outcome timestamps
  per tool (ring), so memory cannot grow unboundedly.
- Thread-safe: all state transitions happen under a single per-breaker lock.
- Process-local by design (same as tool health): circuit state resets on
  process restart.  Documented — no distributed coordination exists today.
- Metrics are bounded: labels are circuit states only, never tool names.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from aegisforge.observability.metrics import (
    record_circuit_fast_fail,
    record_circuit_probe,
    record_circuit_state_change,
)


class CircuitState:
    """Bounded circuit states (string values, JSON friendly)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# Ordering used by planning helpers: closed is best, open is worst.
CIRCUIT_STATE_RANK: dict[str, int] = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}


@dataclass
class CircuitBreakerConfig:
    """Thresholds for the tool circuit breaker.

    Safe defaults: 5 qualifying failures within a 60s window trip the
    circuit; the cooldown before probing recovery is 30s; at most one
    concurrent probe is admitted in HALF_OPEN.
    """

    # Number of qualifying availability failures within the window that
    # trips OPEN.
    failure_threshold: int = 5
    # Size of the sliding window over which failures are counted (seconds).
    failure_window_seconds: float = 60.0
    # How long an OPEN circuit waits before admitting a single probe.
    cooldown_seconds: float = 30.0
    # Number of failed probes admitted per OPEN episode at the base cooldown.
    # Beyond this, the cooldown is exponentially backed off (bounded) so a
    # dead tool is not probed at full rate forever.
    max_probes_per_open: int = 3

    @classmethod
    def from_settings(cls, settings: Any) -> CircuitBreakerConfig:
        """Build a config from Settings (falls back to defaults for None)."""
        return cls(
            failure_threshold=int(
                getattr(settings, "tool_circuit_failure_threshold", 5)
            ),
            failure_window_seconds=float(
                getattr(settings, "tool_circuit_failure_window_seconds", 60.0)
            ),
            cooldown_seconds=float(
                getattr(settings, "tool_circuit_cooldown_seconds", 30.0)
            ),
            max_probes_per_open=int(
                getattr(settings, "tool_circuit_max_probes_per_open", 3)
            ),
        )

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("circuit failure_threshold must be >= 1")
        if self.failure_window_seconds <= 0:
            raise ValueError("circuit failure_window_seconds must be > 0")
        if self.cooldown_seconds < 0:
            raise ValueError("circuit cooldown_seconds must be >= 0")
        if self.max_probes_per_open < 1:
            raise ValueError("circuit max_probes_per_open must be >= 1")


class CircuitOpenError(Exception):
    """Raised internally when an execution is fast-failed by an OPEN circuit."""

    def __init__(self, tool_name: str, cooldown_remaining: float) -> None:
        self.tool_name = tool_name
        self.cooldown_remaining = max(cooldown_remaining, 0.0)
        super().__init__(
            f"Circuit OPEN for tool '{tool_name}': fast-failed "
            f"(retry allowed in {self.cooldown_remaining:.1f}s)"
        )


class ToolCircuitBreaker:
    """Thread-safe, bounded per-tool circuit breaker registry.

    One breaker per tool name.  Tool identity comes from the
    operator-controlled registry, so the key space is bounded and
    operator-owned (never user input).

    Mutated only by execution paths (the ToolRegistry records every real
    outcome).  Planners receive read-only snapshots and can never mutate
    circuit state.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._lock = threading.Lock()
        # tool_name -> deque[float] of recent qualifying failure timestamps
        self._failures: dict[str, deque[float]] = {}
        # tool_name -> state string
        self._states: dict[str, str] = {}
        # tool_name -> monotonic time when OPEN cooldown elapses
        self._open_since: dict[str, float] = {}
        # tool_name -> number of failed probes since last OPEN transition
        self._failed_probes: dict[str, int] = {}
        # tool_name -> whether a HALF_OPEN probe is currently admitted
        self._probe_in_flight: dict[str, bool] = {}

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    # ------------------------------------------------------------------
    # Admission (execution paths)
    # ------------------------------------------------------------------

    def allow(self, tool_name: str) -> bool:
        """Decide whether an execution attempt may proceed.

        Returns False when the call must be fast-failed (OPEN circuit, or
        HALF_OPEN with a probe already admitted by someone else).
        """
        with self._lock:
            state = self._states.get(tool_name, CircuitState.CLOSED)
            if state == CircuitState.CLOSED:
                return True
            now = time.monotonic()
            if state == CircuitState.OPEN:
                open_since = self._open_since.get(tool_name, now)
                if now - open_since >= self._effective_cooldown_seconds_locked(tool_name):
                    # Cooldown elapsed → transition to HALF_OPEN and admit
                    # this caller as THE single probe.
                    self._transition_locked(tool_name, CircuitState.HALF_OPEN)
                    self._probe_in_flight[tool_name] = True
                    return True
                return False
            # HALF_OPEN: only one probe may execute concurrently.
            if self._probe_in_flight.get(tool_name, False):
                return False
            self._probe_in_flight[tool_name] = True
            return True

    # Upper bound on cooldown multiplication so a dead tool is still probed
    # eventually (never permanently stuck).
    _MAX_COOLDOWN_MULTIPLIER = 64

    def _effective_cooldown_seconds_locked(self, tool_name: str) -> float:
        """Cooldown for an OPEN circuit, backed off after repeated failed probes.

        The first ``max_probes_per_open`` failed probes use the base cooldown;
        each further failed probe doubles it (capped at 64x).  This bounds
        probe churn on a dead tool while guaranteeing eventual recovery.
        """
        base = self._config.cooldown_seconds
        failed = self._failed_probes.get(tool_name, 0)
        if failed <= self._config.max_probes_per_open:
            return base
        multiplier = 2 ** (failed - self._config.max_probes_per_open)
        multiplier = min(multiplier, self._MAX_COOLDOWN_MULTIPLIER)
        return base * multiplier

    def probe_cooldown_remaining(self, tool_name: str) -> float:
        """Seconds remaining before an OPEN circuit admits a probe (0 if not OPEN)."""
        with self._lock:
            if self._states.get(tool_name) != CircuitState.OPEN:
                return 0.0
            open_since = self._open_since.get(tool_name, time.monotonic())
            remaining = (
                self._effective_cooldown_seconds_locked(tool_name)
                - (time.monotonic() - open_since)
            )
            return max(remaining, 0.0)

    # ------------------------------------------------------------------
    # Outcome recording (execution paths only)
    # ------------------------------------------------------------------

    def record_success(self, tool_name: str) -> None:
        """Record a successful execution.

        In HALF_OPEN this closes the circuit (the probe succeeded).  In
        CLOSED it clears failure history (evidence of recovery).
        """
        with self._lock:
            state = self._states.get(tool_name, CircuitState.CLOSED)
            self._failures.pop(tool_name, None)
            self._probe_in_flight.pop(tool_name, None)
            if state == CircuitState.HALF_OPEN:
                self._transition_locked(tool_name, CircuitState.CLOSED)
            elif state == CircuitState.OPEN:
                # A success observed while OPEN (e.g. from a straggler call
                # that was admitted before the trip) closes the circuit.
                self._transition_locked(tool_name, CircuitState.CLOSED)

    def record_failure(self, tool_name: str) -> None:
        """Record a qualifying availability failure (timeout/connection/transient).

        CLOSED: appends to the sliding window and trips OPEN when the
        threshold is reached.  HALF_OPEN: the failed probe re-opens the
        circuit (bounded number of failed probes per OPEN episode).
        """
        with self._lock:
            state = self._states.get(tool_name, CircuitState.CLOSED)
            now = time.monotonic()
            self._probe_in_flight.pop(tool_name, None)

            if state == CircuitState.HALF_OPEN:
                failed = self._failed_probes.get(tool_name, 0) + 1
                self._failed_probes[tool_name] = failed
                self._transition_locked(tool_name, CircuitState.OPEN)
                return

            # CLOSED (or unexpected OPEN): slide the window.
            ring = self._failures.get(tool_name)
            if ring is None:
                ring = deque()
                self._failures[tool_name] = ring
            ring.append(now)
            cutoff = now - self._config.failure_window_seconds
            while ring and ring[0] < cutoff:
                ring.popleft()

            if len(ring) >= self._config.failure_threshold:
                self._transition_locked(tool_name, CircuitState.OPEN)

    def _transition_locked(self, tool_name: str, new_state: str) -> None:
        """Transition state (caller must hold the lock)."""
        prior = self._states.get(tool_name, CircuitState.CLOSED)
        if prior == new_state:
            return
        now = time.monotonic()
        self._states[tool_name] = new_state
        if new_state == CircuitState.OPEN:
            self._open_since[tool_name] = now
            self._failures.pop(tool_name, None)
            # Entering OPEN from a settled (CLOSED) state starts a fresh OPEN
            # episode; a failed probe (from HALF_OPEN) keeps the running
            # count so backoff accumulates.
            if prior != CircuitState.HALF_OPEN:
                self._failed_probes.pop(tool_name, None)
        elif new_state == CircuitState.CLOSED:
            self._open_since.pop(tool_name, None)
            self._failed_probes.pop(tool_name, None)
            self._failures.pop(tool_name, None)
            self._probe_in_flight.pop(tool_name, None)
        # HALF_OPEN keeps open_since cleared; probe admission is flagged by
        # the caller via _probe_in_flight.
        if new_state == CircuitState.HALF_OPEN:
            self._open_since.pop(tool_name, None)
        try:
            record_circuit_state_change(prior, new_state)
        except Exception:  # noqa: S110 - observability must never break execution
            pass

    # ------------------------------------------------------------------
    # Reading (planner-safe)
    # ------------------------------------------------------------------

    def get_state(self, tool_name: str) -> str:
        with self._lock:
            return self._states.get(tool_name, CircuitState.CLOSED)

    def list_states(self) -> dict[str, str]:
        """Snapshot of per-tool circuit states (bounded by registry size)."""
        with self._lock:
            return dict(self._states)

    def reset(self) -> None:
        """Clear all circuit state (admin/test use — never called by planners)."""
        with self._lock:
            self._failures.clear()
            self._states.clear()
            self._open_since.clear()
            self._failed_probes.clear()
            self._probe_in_flight.clear()

    def fast_fail(
        self, tool_name: str, execution_id: str = ""
    ) -> tuple[str, float]:
        """Record a fast-fail metric and return (message, cooldown_remaining)."""
        remaining = self.probe_cooldown_remaining(tool_name)
        try:
            record_circuit_fast_fail()
        except Exception:  # noqa: S110 - observability must never break execution
            pass
        message = (
            f"CIRCUIT_OPEN: tool '{tool_name}' is temporarily unavailable "
            "(repeated failures); fast-failed without execution"
        )
        return (message, remaining)


def record_probe_outcome(tool_name: str, success: bool) -> None:
    """Record probe metrics (bounded labels only)."""
    try:
        record_circuit_probe(success)
    except Exception:  # noqa: S110 - observability must never break execution
        pass


# ---------------------------------------------------------------------------
# Process-wide default breaker
# ---------------------------------------------------------------------------


_default_breaker: ToolCircuitBreaker | None = None


def get_default_circuit_breaker() -> ToolCircuitBreaker:
    """Return the process-wide breaker shared by every ToolRegistry.

    Circuit evidence must accumulate across workflow nodes (registries are
    built per node), so all registries share one breaker unless one is
    explicitly injected.  Thresholds come from Settings (with safe defaults).
    """
    global _default_breaker
    if _default_breaker is None:
        config: CircuitBreakerConfig | None = None
        try:
            from aegisforge.config import get_settings

            config = CircuitBreakerConfig.from_settings(get_settings())
        except Exception:  # settings must never break circuit breaking
            config = None
        _default_breaker = ToolCircuitBreaker(config)
    return _default_breaker


def reset_default_circuit_breaker() -> None:
    """Discard the process-wide breaker (test/admin use only)."""
    global _default_breaker
    if _default_breaker is not None:
        _default_breaker.reset()
    _default_breaker = None
