from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aegisforge.config import Settings


@lru_cache(maxsize=32)
def get_engine(database_url: str):
    engine_kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        # In-memory SQLite databases are per-connection; use StaticPool
        # so that all sessions share the same underlying connection.
        if ":memory:" in database_url or database_url == "sqlite://":
            engine_kwargs["poolclass"] = StaticPool
    return create_engine(database_url, **engine_kwargs)


def get_session_factory(settings: Settings):
    engine = get_engine(settings.database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """Yield a database session. Can be overridden via app.dependency_overrides."""
    from aegisforge.config import get_settings as _get_settings

    settings = _get_settings()
    SessionLocal = get_session_factory(settings)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
