"""WS4: production configuration fail-fast tests.

A production process must refuse to start with a placeholder or short
SECRET_KEY (JWT forgery risk) or with debug mode enabled.  Development
and test environments keep their working defaults.

Evidence type: UNIT (STATIC/CONFIG in the handoff report).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegisforge.config import Settings

STRONG_KEY = "x9Q" + "a" * 45 + "-production-unique-secret-2026-OK"


def _prod(**kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("environment", "production")
    kwargs.setdefault("database_url", "sqlite:///:memory:")
    kwargs.setdefault("secret_key", STRONG_KEY)
    return Settings(**kwargs)


class TestProductionSecretKeyFailFast:
    def test_default_dev_secret_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            _prod(secret_key="dev-secret-key-change-me")

    def test_compose_placeholder_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            _prod(secret_key="change-me-in-production")

    def test_empty_secret_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            _prod(secret_key="")

    def test_short_secret_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="at least 32"):
            _prod(secret_key="short-but-not-placeholder-key")

    def test_debug_true_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="DEBUG"):
            _prod(debug=True)

    def test_strong_secret_and_no_debug_starts(self) -> None:
        settings = _prod()
        assert settings.environment == "production"
        assert settings.secret_key == STRONG_KEY

    def test_prod_alias_accepted(self) -> None:
        settings = _prod(environment="prod")
        assert settings.environment == "prod"


class TestNonProductionUnaffected:
    def test_development_keeps_default_secret(self) -> None:
        settings = Settings(
            environment="development",
            database_url="sqlite:///:memory:",
            secret_key="dev-secret-key-change-me",
        )
        assert settings.secret_key == "dev-secret-key-change-me"

    def test_test_environment_keeps_weak_secret(self) -> None:
        settings = Settings(
            environment="test",
            database_url="sqlite:///:memory:",
            secret_key="test-secret",
        )
        assert settings.secret_key == "test-secret"

    def test_production_rate_limit_warning_does_not_block(self, caplog) -> None:  # type: ignore[no-untyped-def]
        import logging

        with caplog.at_level(logging.WARNING, logger="aegisforge.config"):
            _prod(rate_limit_enabled=False)
        assert any("rate limiting" in r.message.lower() for r in caplog.records)
