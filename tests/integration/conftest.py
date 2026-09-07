"""Shared fixtures for real-infrastructure integration tests (Phase 4.2).

These tests run ONLY when AEGISFORGE_INTEGRATION_TESTS=true and require
real PostgreSQL+pgvector and Redis (e.g. via docker compose up -d
postgres redis). They are never part of the default unit test run.
"""
from __future__ import annotations

import os

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
REDIS_URL = os.environ.get(
    "AEGISFORGE_TEST_REDIS_URL",
    "redis://localhost:6379/0",
)


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
    # Start clean: stale jobs from a previous run must not interfere.
    client.delete("aegisforge:jobs")
    yield client
    # Clean up test keys
    for key in client.keys("aegisforge:jobs"):
        client.delete(key)
    for key in client.keys("ratelimit:*"):
        client.delete(key)