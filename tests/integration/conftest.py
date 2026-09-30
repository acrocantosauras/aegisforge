"""Shared fixtures for real-infrastructure integration tests (Phase 4.2).

These tests run ONLY when AEGISFORGE_INTEGRATION_TESTS=true and require
real PostgreSQL+pgvector and Redis (e.g. via docker compose up -d
postgres redis). They are never part of the default unit test run.
"""
from __future__ import annotations

import os
from urllib.parse import quote

import pytest

from aegisforge.db.session import get_engine

REQUIRE_REAL = os.environ.get("AEGISFORGE_INTEGRATION_TESTS", "").lower() == "true"

requires_infra = pytest.mark.skipif(
    not REQUIRE_REAL,
    reason="Real infrastructure tests require AEGISFORGE_INTEGRATION_TESTS=true",
)

PG_URL = os.environ.get(
    "AEGISFORGE_TEST_DATABASE_URL",
    "postgresql+psycopg://aegisforge:aegisforge@localhost:5432/aegisforge",
)


def _resolve_test_redis_url() -> str:
    """Resolve the Redis URL for integration tests without echoing secrets.

    Precedence:
    1. AEGISFORGE_TEST_REDIS_URL — explicit test override, used exactly as-is.
    2. REDIS_URL — shared environment URL (as used by docker compose services).
    3. REDIS_PASSWORD — build an authenticated localhost URL (URL-encoded).
    4. Unauthenticated localhost fallback for plain local development Redis.
    """
    explicit = os.environ.get("AEGISFORGE_TEST_REDIS_URL", "")
    if explicit:
        return explicit
    shared = os.environ.get("REDIS_URL", "")
    if shared:
        return shared
    password = os.environ.get("REDIS_PASSWORD", "")
    if password:
        return f"redis://:{quote(password, safe='')}@localhost:6379/0"
    return "redis://localhost:6379/0"


REDIS_URL = _resolve_test_redis_url()


@pytest.fixture(scope="session")
def pg_engine():
    """Real PostgreSQL engine with schema supplied by migrations."""
    engine = get_engine(PG_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session_factory(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine, autoflush=False, autocommit=False)


@pytest.fixture()
def redis_client():
    import redis as redis_lib

    client = redis_lib.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    client.ping()
    # Start clean: stale keys from previous runs must not interfere.
    for pattern in [
        "aegisforge:jobs",
        "aegisforge:job:*",
        "aegisforge:idempotency:*",
        "aegisforge:active_claims",
        "aegisforge:workers",
        "ratelimit:*",
    ]:
        for key in client.keys(pattern):
            client.delete(key)
    yield client
    # Clean up test keys
    for pattern in [
        "aegisforge:jobs",
        "aegisforge:job:*",
        "aegisforge:idempotency:*",
        "aegisforge:active_claims",
        "aegisforge:workers",
        "ratelimit:*",
    ]:
        for key in client.keys(pattern):
            client.delete(key)