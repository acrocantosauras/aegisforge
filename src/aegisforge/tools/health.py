"""Tool health: a bounded, deterministic, evidence-derived availability signal.

Phase 6F.  Health is a PLANNING signal, never an execution guarantee: a
"healthy" tool can still fail and an "unavailable" tool can still be selected
when no alternative exists (execution then follows the normal permission,
registry, retry, and recovery paths — nothing here bypasses them).

Design constraints:

- **Bounded evidence**: per-tool outcomes are kept in a fixed-size ring
  (default 20).  No unbounded history, no raw errors, no payloads.
- **Bounded states**: ``unknown | healthy | degraded | unavailable`` only.
- **Deterministic**: the state is a pure function of the window + the
  previous state (for the sticky-unavailable exit rule).  Fully explainable.
- **No single-failure flapping**: a single transient failure cannot mark a
  tool unavailable; thresholds require either repeated consecutive failures
  or a sustained failure rate over a minimum sample count.
- **MCP separation**: tool-level health is independent of MCP *server*
  health.  A failing tool never marks its server unhealthy, and a healthy
  server does not imply every tool on it is healthy (server health remains
  owned exclusively by the MCP lifecycle manager / catalog).
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from aegisforge.observability.metrics import (
    record_tool_health_state_change,
    set_tool_health_states,
)


class ToolHealthState:
    """Bounded availability states (string values, JSON friendly)."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


# Ordering used for "prefer a healthier alternative" decisions.
_STATE_RANK: dict[str, int] = {
    ToolHealthState.UNAVAILABLE: 0,
    ToolHealthState.UNKNOWN: 1,
    ToolHealthState.DEGRADED: 2,
    ToolHealthState.HEALTHY: 3,
}


@dataclass
class ToolHealthConfig:
    """Thresholds for deriving tool health from bounded recent evidence."""

    # Ring size of recent outcomes per tool (bounded history).
    window_size: int = 20
    # Minimum samples before rate-based classification applies.
    min_samples: int = 3
    # Failure rate within the window that marks a tool degraded/unavailable.
    degraded_failure_rate: float = 0.34
    unavailable_failure_rate: float = 0.75
    # Consecutive failures that mark a tool degraded/unavailable.
    degraded_consecutive_failures: int = 2
    unavailable_consecutive_failures: int = 4
    # Consecutive successes required to leave the sticky unavailable state.
    recovery_samples: int = 2

    @classmethod
    def from_settings(cls, settings: Any) -> ToolHealthConfig:
        """Build a config from Settings (falls back to defaults for None)."""
        return cls(
            window_size=int(getattr(settings, "tool_health_window_size", 20)),
            min_samples=int(getattr(settings, "tool_health_min_samples", 3)),
            degraded_failure_rate=float(
                getattr(settings, "tool_health_degraded_failure_rate", 0.34)
            ),
            unavailable_failure_rate=float(
                getattr(settings, "tool_health_unavailable_failure_rate", 0.75)
            ),
            degraded_consecutive_failures=int(
                getattr(settings, "tool_health_degraded_consecutive_failures", 2)
            ),
            unavailable_consecutive_failures=int(
                getattr(settings, "tool_health_unavailable_consecutive_failures", 4)
            ),
            recovery_samples=int(getattr(settings, "tool_health_recovery_samples", 2)),
        )

    def __post_init__(self) -> None:
        if self.window_size < 1:
            raise ValueError("tool health window_size must be >= 1")
        if self.min_samples < 1:
            raise ValueError("tool health min_samples must be >= 1")
        if not 0.0 < self.degraded_failure_rate <= 1.0:
            raise ValueError("degraded_failure_rate must be in (0, 1]")
        if not self.degraded_failure_rate <= self.unavailable_failure_rate <= 1.0:
            raise ValueError("unavailable_failure_rate must be >= degraded_failure_rate")
        if self.degraded_consecutive_failures < 1:
            raise ValueError("degraded_consecutive_failures must be >= 1")
        if self.unavailable_consecutive_failures < self.degraded_consecutive_failures:
            raise ValueError(
                "unavailable_consecutive_failures must be >= degraded_consecutive_failures"
            )
        if self.recovery_samples < 1:
            raise ValueError("recovery_samples must be >= 1")


@dataclass(frozen=True)
class ToolHealthSnapshot:
    """Bounded, planner-safe view of one tool's health.

    Contains ONLY availability evidence — never errors, payloads, tenant
    data, or credentials.  Immutable: the planner cannot mutate health state
    through it.
    """

    tool_name: str
    state: str  # ToolHealthState value
    recent_failure_rate: float  # 0.0..1.0 over the bounded window
    recent_timeout_rate: float  # 0.0..1.0 over the bounded window
    consecutive_failures: int
    sample_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "state": self.state,
            "recent_failure_rate": round(self.recent_failure_rate, 3),
            "recent_timeout_rate": round(self.recent_timeout_rate, 3),
            "consecutive_failures": self.consecutive_failures,
            "sample_count": self.sample_count,
        }


# Outcome kinds recorded in the ring.  ``timeout`` is a failure for
# availability purposes but is also counted separately for timeout_rate.
_SUCCESS = "success"
_FAILURE = "failure"
_TIMEOUT = "timeout"


def _derive_state(
    outcomes: deque[str],
    prior_state: str,
    config: ToolHealthConfig,
) -> tuple[str, float, float, int, int]:
    """Pure derivation of (state, failure_rate, timeout_rate, consecutive_failures, n)."""
    n = len(outcomes)
    if n == 0:
        return ToolHealthState.UNKNOWN, 0.0, 0.0, 0, 0

    failures = sum(1 for o in outcomes if o != _SUCCESS)
    timeouts = sum(1 for o in outcomes if o == _TIMEOUT)
    failure_rate = failures / n
    timeout_rate = timeouts / n

    consecutive_failures = 0
    for outcome in reversed(outcomes):
        if outcome == _SUCCESS:
            break
        consecutive_failures += 1
    consecutive_successes = 0
    for outcome in reversed(outcomes):
        if outcome != _SUCCESS:
            break
        consecutive_successes += 1

    # Sticky unavailable: exit only after bounded recovery evidence.
    if prior_state == ToolHealthState.UNAVAILABLE:
        if consecutive_successes >= config.recovery_samples:
            state = (
                ToolHealthState.HEALTHY
                if failure_rate <= config.degraded_failure_rate
                else ToolHealthState.DEGRADED
            )
        else:
            state = ToolHealthState.UNAVAILABLE
        return state, failure_rate, timeout_rate, consecutive_failures, n

    if (
        consecutive_failures >= config.unavailable_consecutive_failures
        or (n >= config.min_samples and failure_rate >= config.unavailable_failure_rate)
    ):
        state = ToolHealthState.UNAVAILABLE
    elif (
        consecutive_failures >= config.degraded_consecutive_failures
        or (n >= config.min_samples and failure_rate >= config.degraded_failure_rate)
    ):
        state = ToolHealthState.DEGRADED
    elif n >= config.min_samples:
        state = ToolHealthState.HEALTHY
    else:
        state = ToolHealthState.UNKNOWN

    return state, failure_rate, timeout_rate, consecutive_failures, n


class ToolHealthTracker:
    """Thread-safe, bounded per-tool health evidence store.

    Mutated only by execution paths (the ToolRegistry records every real
    outcome).  Planners receive immutable snapshots and can never write.
    """

    def __init__(self, config: ToolHealthConfig | None = None) -> None:
        self._config = config or ToolHealthConfig()
        self._outcomes: dict[str, deque[str]] = {}
        self._states: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def config(self) -> ToolHealthConfig:
        return self._config

    # ------------------------------------------------------------------
    # Recording (execution paths only)
    # ------------------------------------------------------------------

    def record_success(self, tool_name: str) -> None:
        self._record(tool_name, _SUCCESS)

    def record_failure(self, tool_name: str, *, timeout: bool = False) -> None:
        self._record(tool_name, _TIMEOUT if timeout else _FAILURE)

    def _record(self, tool_name: str, outcome: str) -> None:
        if not tool_name:
            return
        with self._lock:
            ring = self._outcomes.get(tool_name)
            if ring is None:
                ring = deque(maxlen=self._config.window_size)
                self._outcomes[tool_name] = ring
            prior = self._states.get(tool_name, ToolHealthState.UNKNOWN)
            ring.append(outcome)
            state, _frate, _trate, _consec, _n = _derive_state(
                ring, prior, self._config
            )
            self._states[tool_name] = state
            if state != prior:
                try:
                    record_tool_health_state_change(prior, state)
                except Exception:  # noqa: S110 - observability must never break execution
                    pass
            try:
                set_tool_health_states(self._states)
            except Exception:  # noqa: S110 - observability must never break execution
                pass

    # ------------------------------------------------------------------
    # Reading (planner-safe)
    # ------------------------------------------------------------------

    def get_health(self, tool_name: str) -> ToolHealthSnapshot:
        with self._lock:
            ring = self._outcomes.get(tool_name)
            prior = self._states.get(tool_name, ToolHealthState.UNKNOWN)
            if ring is None:
                return ToolHealthSnapshot(
                    tool_name=tool_name,
                    state=ToolHealthState.UNKNOWN,
                    recent_failure_rate=0.0,
                    recent_timeout_rate=0.0,
                    consecutive_failures=0,
                    sample_count=0,
                )
            state, frate, trate, consec, n = _derive_state(
                ring, prior, self._config
            )
            return ToolHealthSnapshot(
                tool_name=tool_name,
                state=state,
                recent_failure_rate=frate,
                recent_timeout_rate=trate,
                consecutive_failures=consec,
                sample_count=n,
            )

    def list_health(self) -> list[ToolHealthSnapshot]:
        """Snapshots for every tool with recorded evidence (bounded by registry size)."""
        with self._lock:
            names = sorted(self._outcomes.keys())
        return [self.get_health(name) for name in names]

    def reset(self) -> None:
        """Clear all evidence (admin/test use — never called by the planner)."""
        with self._lock:
            self._outcomes.clear()
            self._states.clear()
            try:
                set_tool_health_states({})
            except Exception:  # noqa: S110 - observability must never break execution
                pass


# ---------------------------------------------------------------------------
# Health-aware planning helpers (pure functions over snapshots)
# ---------------------------------------------------------------------------


def _snapshot_map(
    health_context: list[dict[str, Any]] | list[ToolHealthSnapshot],
) -> dict[str, ToolHealthSnapshot]:
    result: dict[str, ToolHealthSnapshot] = {}
    for item in health_context:
        if isinstance(item, ToolHealthSnapshot):
            result[item.tool_name] = item
        elif isinstance(item, dict):
            try:
                result[str(item.get("tool_name", ""))] = ToolHealthSnapshot(
                    tool_name=str(item.get("tool_name", "")),
                    state=str(item.get("state", ToolHealthState.UNKNOWN)),
                    recent_failure_rate=float(item.get("recent_failure_rate", 0.0)),
                    recent_timeout_rate=float(item.get("recent_timeout_rate", 0.0)),
                    consecutive_failures=int(item.get("consecutive_failures", 0)),
                    sample_count=int(item.get("sample_count", 0)),
                )
            except (TypeError, ValueError):
                continue
    return result


def select_preferred_tool(
    requested: str,
    required_permissions: list[str],
    available_tools: list[str],
    health_context: list[dict[str, Any]] | list[ToolHealthSnapshot],
    permission_checker: Any = None,
) -> str:
    """Pick the healthiest tool for a capability, deterministically.

    Rules (in order):

    1. If the requested tool is unknown to the health context, healthy, or
       ``unknown`` — keep it.  Absence of evidence never changes planning.
    2. If the requested tool is degraded/unavailable, evaluate registered
       alternatives that (a) are in ``available_tools``, (b) are not the
       requested tool, (c) satisfy ``required_permissions`` (checked via
       ``permission_checker`` when provided — the registry's permission
       logic stays authoritative), and (d) have a strictly better health
       state.
    3. Rank candidates by (state rank, failure rate, name) — the name
       tie-break keeps the decision deterministic.
    4. If no strictly better alternative exists, KEEP the requested tool:
       execution still goes through the normal permission/registry/retry
       paths, and the existing failure/recovery semantics stay authoritative.

    This is a planning signal only — it can never bypass permissions, and it
    can never select a tool that is not in the operator-controlled registry.
    """
    if not requested:
        return requested
    snapshots = _snapshot_map(health_context)
    requested_health = snapshots.get(requested)
    if requested_health is None:
        return requested
    if requested_health.state in (ToolHealthState.HEALTHY, ToolHealthState.UNKNOWN):
        return requested

    requested_rank = _STATE_RANK.get(requested_health.state, 1)
    best: tuple[int, float, str] | None = None
    best_name = requested
    for candidate in available_tools:
        if candidate == requested:
            continue
        if permission_checker is not None and not permission_checker(
            candidate, required_permissions
        ):
            continue
        snap = snapshots.get(candidate)
        # Unknown-health candidates are never preferred over the requested
        # tool: absence of evidence is not proof of health.
        if snap is None:
            continue
        cand_rank = _STATE_RANK.get(snap.state, 1)
        if cand_rank <= requested_rank:
            continue  # not strictly better than what was requested
        key = (cand_rank, snap.recent_failure_rate, candidate)
        if best is None or key < best:
            best = key
            best_name = candidate
    return best_name if best is not None else requested


def select_tool_with_circuit(
    requested: str,
    required_permissions: list[str],
    available_tools: list[str],
    health_context: list[dict[str, Any]] | list[ToolHealthSnapshot],
    circuit_context: dict[str, str] | None = None,
    permission_checker: Any = None,
) -> str:
    """Health-aware selection that additionally avoids OPEN circuits.

    Extends :func:`select_preferred_tool` with circuit state (Phase 6G):

    - OPEN circuits are avoided exactly like ``unavailable`` health — the
      tool is still *selectable* when no better alternative exists, but a
      healthier candidate wins.
    - HALF_OPEN counts as mildly degraded (a probe may succeed).
    - Circuit state can NEVER bypass permissions: the permission checker is
      applied exactly as in ``select_preferred_tool``.
    - Unknown-health candidates are never preferred over the requested tool
      (absence of evidence is not proof of health) — same rule as 6F.

    Combined score (lower is better) merges the two bounded signals:

      score = (4 - health_rank) + circuit_penalty

    where health_rank ∈ {1=unavailable, 2=unknown, 3=degraded, 4=healthy}
    and circuit_penalty ∈ {0=closed, 1=half_open, 4=open}.  An OPEN circuit
    therefore makes even a healthy tool strictly worse than a closed
    degraded tool, matching the operational reality that an OPEN circuit
    will fast-fail the next call anyway.

    Falls back to plain health-based selection when no circuit context is
    provided, so existing callers behave identically.
    """
    if not requested:
        return requested
    circuits = circuit_context or {}
    if not circuits:
        return select_preferred_tool(
            requested=requested,
            required_permissions=required_permissions,
            available_tools=available_tools,
            health_context=health_context,
            permission_checker=permission_checker,
        )

    from aegisforge.tools.circuit_breaker import CircuitState

    snapshots = _snapshot_map(health_context)
    _CIRCUIT_PENALTY = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 1,
        CircuitState.OPEN: 4,
    }

    def _score(candidate: str) -> int | None:
        """Combined lower-is-better score; None when unknown-health."""
        snap = snapshots.get(candidate)
        if snap is None:
            return None
        health_rank = _STATE_RANK.get(snap.state, 1)  # 0..3, higher=better
        circuit = circuits.get(candidate, CircuitState.CLOSED)
        penalty = _CIRCUIT_PENALTY.get(circuit, 0)
        return (4 - health_rank) + penalty

    # Phase 6G: circuit-only avoidance — a tool with NO health evidence but an
    # OPEN circuit must still be avoidable when a strictly better alternative
    # exists (score treats missing health as 'unknown' health).
    requested_score = _score(requested)
    if requested_score is None:
        requested_circuit = circuits.get(requested, CircuitState.CLOSED)
        if requested_circuit == CircuitState.CLOSED:
            # No evidence at all about the requested tool → keep it.
            return requested
        requested_score = 2 + _CIRCUIT_PENALTY.get(requested_circuit, 0)

    best_score = requested_score
    best_name = requested
    for candidate in available_tools:
        if candidate == requested:
            continue
        if permission_checker is not None and not permission_checker(
            candidate, required_permissions
        ):
            continue
        cand_score = _score(candidate)
        if cand_score is None:
            # Unknown-health candidates: never preferred over a CLOSED
            # requested tool, but allowed to beat an OPEN/HALF_OPEN one when
            # they carry closed circuits and unknown health (score 2).  A
            # closed+unknown candidate (2) only wins when the requested score
            # is strictly worse (3+).
            cand_circuit = circuits.get(candidate, CircuitState.CLOSED)
            if cand_circuit != CircuitState.CLOSED:
                continue
            cand_score = 2
        if cand_score < best_score:
            best_score = cand_score
            best_name = candidate
    return best_name


def build_planner_health_context(
    snapshots: list[ToolHealthSnapshot],
    max_tools: int = 30,
) -> str:
    """Render a compact, bounded health block for LLM planner prompts.

    One line per tool, rounded rates, no errors/payloads/tenant data.
    Capped at ``max_tools`` lines (alphabetical) so prompt size stays bounded
    regardless of registry size.
    """
    if not snapshots:
        return ""
    lines: list[str] = []
    for snap in sorted(snapshots, key=lambda s: s.tool_name)[:max_tools]:
        lines.append(
            f"- {snap.tool_name}: availability={snap.state}, "
            f"recent_failure_rate={round(snap.recent_failure_rate, 2)}, "
            f"recent_timeout_rate={round(snap.recent_timeout_rate, 2)}"
        )
    header = (
        "Tool health (planning signal only — not an execution guarantee). "
        "Prefer healthy tools when equivalents exist; avoid unavailable tools "
        "unless nothing else can do the job:"
    )
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Process-wide default tracker
# ---------------------------------------------------------------------------


_default_tracker: ToolHealthTracker | None = None


def get_default_tool_health_tracker() -> ToolHealthTracker:
    """Return the process-wide tracker shared by every ToolRegistry.

    Health evidence must accumulate across workflow nodes (registries are
    built per node), so all registries share one tracker unless one is
    explicitly injected.  Tool identity comes from the operator-controlled
    registry, so the key space stays bounded and operator-owned.
    Thresholds come from Settings (Phase 6F fields, with safe defaults).
    """
    global _default_tracker
    if _default_tracker is None:
        config: ToolHealthConfig | None = None
        try:
            from aegisforge.config import get_settings

            config = ToolHealthConfig.from_settings(get_settings())
        except Exception:  # settings must never break health
            config = None
        _default_tracker = ToolHealthTracker(config)
    return _default_tracker


def reset_default_tool_health_tracker() -> None:
    """Discard the process-wide tracker (test/admin use only)."""
    global _default_tracker
    if _default_tracker is not None:
        _default_tracker.reset()
    _default_tracker = None
