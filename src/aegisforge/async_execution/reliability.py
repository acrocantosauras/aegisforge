"""Reliability utilities for AegisForge Phase 6C.

Provides bounded execution timeout, Redis reconnection resilience,
and backoff strategies for the distributed job system.

Key properties:
- No infinite retry loops
- Bounded backoff with jitter
- Predictable recovery
- Clear failure observability
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def execute_with_timeout(
    func: Callable[..., Any],
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    timeout_seconds: float = 300.0,
    default: Any = None,
) -> Any:
    """Execute a function with a timeout using a background thread.

    If the function doesn't complete within timeout_seconds, returns
    the default value. Note: the function thread is NOT killed (Python
    limitation), but the caller proceeds with the default.

    This is used to bound job execution time. The handler itself should
    implement cooperative cancellation via its own timeout mechanisms.
    """
    import threading

    if kwargs is None:
        kwargs = {}

    result: list[Any] = [default]
    exception: list[BaseException | None] = [None]

    def _target() -> None:
        try:
            result[0] = func(*args, **kwargs)
        except BaseException as exc:
            exception[0] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        logger.warning(
            "Function %s timed out after %.1fs",
            func.__name__ if hasattr(func, "__name__") else str(func),
            timeout_seconds,
        )
        return default

    if exception[0] is not None:
        raise exception[0]  # type: ignore[misc]

    return result[0]


class BoundedBackoff:
    """Bounded exponential backoff with jitter.

    Prevents thundering-herd problems while ensuring bounded retry.
    """

    def __init__(
        self,
        initial_delay: float = 0.1,
        max_delay: float = 30.0,
        multiplier: float = 2.0,
        jitter: bool = True,
    ) -> None:
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._multiplier = multiplier
        self._jitter = jitter
        self._attempt = 0

    def next_delay(self) -> float:
        """Calculate the next backoff delay."""
        delay = self._initial_delay * (self._multiplier ** self._attempt)
        delay = min(delay, self._max_delay)
        if self._jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        self._attempt += 1
        return delay

    def reset(self) -> None:
        """Reset the backoff to initial state."""
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt


class RedisResilientClient:
    """Wrapper around Redis client with reconnection resilience.

    Catches transient Redis failures and provides bounded retry with
    backoff. Does NOT silently swallow errors — logs them and re-raises
    after exhausting retries.
    """

    def __init__(
        self,
        redis_client: Any,
        max_retries: int = 3,
        backoff: BoundedBackoff | None = None,
    ) -> None:
        self._client = redis_client
        self._max_retries = max_retries
        self._backoff = backoff or BoundedBackoff(
            initial_delay=0.1, max_delay=5.0, multiplier=2.0
        )

    def execute(
        self,
        operation: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a Redis operation with bounded retry on transient errors."""
        last_error: BaseException | None = None

        for attempt in range(self._max_retries):
            try:
                result = getattr(self._client, operation)(*args, **kwargs)
                if attempt > 0:
                    logger.info(
                        "Redis operation '%s' succeeded after %d retries",
                        operation,
                        attempt,
                    )
                return result
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_error = exc
                delay = self._backoff.next_delay()
                logger.warning(
                    "Redis operation '%s' failed (attempt %d/%d): %s. "
                    "Retrying in %.2fs",
                    operation,
                    attempt + 1,
                    self._max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
            except Exception:
                # Non-transient errors — don't retry
                logger.exception(
                    "Redis operation '%s' failed with non-transient error",
                    operation,
                )
                raise

        # Exhausted retries
        logger.error(
            "Redis operation '%s' failed after %d retries",
            operation,
            self._max_retries,
        )
        if last_error is not None:
            raise last_error
        return None

    @property
    def client(self) -> Any:
        """Access the underlying Redis client directly (for operations
        that don't need resilience wrapping)."""
        return self._client


def is_terminal_status(status: str) -> bool:
    """Check if a job status is terminal (no further transitions expected)."""
    return status in ("completed", "failed", "cancelled")


def is_recoverable_status(status: str) -> bool:
    """Check if a job status indicates it could be recovered."""
    return status in ("running", "retrying", "queued")


def bounded_delay(delay: float, max_delay: float = 60.0) -> float:
    """Ensure a delay is within bounds."""
    return max(0.0, min(delay, max_delay))
