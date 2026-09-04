from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = Field(default="aegisforge")
    environment: str = Field(default="development")
    debug: bool = Field(default=False)
    api_prefix: str = Field(default="/api/v1")
    database_url: str = Field(default="sqlite:///./aegisforge.db")
    redis_url: str = Field(default="redis://localhost:6379/0")
    secret_key: str = Field(default="dev-secret-key-change-me")
    access_token_expire_minutes: int = Field(default=60)

    # LLM Provider Settings
    llm_provider: str = Field(default="openai")
    llm_model: str = Field(default="gpt-4o-mini")
    llm_api_key: str = Field(default="")
    llm_temperature: float = Field(default=0.0)
    llm_max_tokens: int = Field(default=4096)
    llm_timeout_seconds: int = Field(default=60)
    llm_max_retries: int = Field(default=3)

    # Embedding Settings
    embedding_provider: str = Field(default="deterministic")
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_api_key: str = Field(default="")
    embedding_dimension: int = Field(default=384)
    embedding_batch_size: int = Field(default=32)

    # Document Ingestion Settings
    max_document_size_bytes: int = Field(default=10_485_760)  # 10MB
    allowed_content_types: str = Field(default="text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    chunk_size: int = Field(default=512)
    chunk_overlap: int = Field(default=50)

    # MCP Settings
    mcp_enabled: bool = Field(default=False)
    mcp_timeout_seconds: int = Field(default=30)

    # Async Execution Settings
    job_queue_enabled: bool = Field(default=False)
    job_max_retries: int = Field(default=3)
    job_timeout_seconds: int = Field(default=300)

    # Approval Settings
    approval_required_risk_levels: str = Field(default="high,critical")
    approval_timeout_hours: int = Field(default=24)

    # CORS Settings
    cors_origins: str = Field(default="http://localhost:3000")

    # Evaluation Settings
    llm_critic_enabled: bool = Field(default=True)

    # Rate Limiting Settings
    rate_limit_enabled: bool = Field(default=False)
    rate_limit_max_requests: int = Field(default=300)
    rate_limit_window_seconds: int = Field(default=60)
    rate_limit_auth_max_requests: int = Field(default=600)
    rate_limit_exempt_paths: str = Field(default="/metrics,/api/v1/health,/docs,/redoc,/openapi.json")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_model_provider_from_settings(settings: Settings | None = None):  # type: ignore[no-untyped-def]
    """Create a ModelProvider from settings. Returns None if not configured for real LLM."""
    from aegisforge.llm.providers import get_model_provider

    settings = settings or get_settings()

    # Only create a real provider if an API key is configured
    if settings.llm_provider == "deterministic" or not settings.llm_api_key:
        return None

    try:
        return get_model_provider(
            settings.llm_provider,
            api_key=settings.llm_api_key,
        )
    except Exception as exc:
        logger.warning("Failed to create model provider: %s", exc)
        return None
