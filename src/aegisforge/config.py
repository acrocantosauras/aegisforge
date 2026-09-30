from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Placeholder secret keys that must never reach a production process.
_INSECURE_SECRET_KEYS = frozenset(
    {
        "",
        "dev-secret-key-change-me",
        "change-me-in-production",
        "change-me",
        "changeme",
        "secret",
        "test-secret",
    }
)

_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})

# Minimum secret length for production (OWASP credential guidance).
_MIN_PRODUCTION_SECRET_LENGTH = 32


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

    # Phase 6C — Reliability & Observability
    stuck_job_detection_enabled: bool = Field(default=True)
    stuck_job_max_age_seconds: float = Field(default=3600.0)
    stuck_job_max_claim_age_seconds: float = Field(default=600.0)
    stuck_job_max_execution_time_seconds: float = Field(default=1800.0)
    stuck_job_max_recoveries: int = Field(default=5)
    worker_heartbeat_interval_seconds: int = Field(default=30)
    worker_recovery_scan_interval_seconds: int = Field(default=60)

    # Phase 6F — Tool health (bounded evidence windows and thresholds)
    tool_health_window_size: int = Field(default=20)
    tool_health_min_samples: int = Field(default=3)
    tool_health_degraded_failure_rate: float = Field(default=0.34)
    tool_health_unavailable_failure_rate: float = Field(default=0.75)
    tool_health_degraded_consecutive_failures: int = Field(default=2)
    tool_health_unavailable_consecutive_failures: int = Field(default=4)
    tool_health_recovery_samples: int = Field(default=2)

    # Phase 6G — Tool circuit breaker (fast-fail around repeatedly failing tools)
    tool_circuit_failure_threshold: int = Field(default=5)
    tool_circuit_failure_window_seconds: float = Field(default=60.0)
    tool_circuit_cooldown_seconds: float = Field(default=30.0)
    tool_circuit_max_probes_per_open: int = Field(default=3)

    # Phase 6G — Worker capacity awareness (application-level, bounded)
    worker_capacity: int = Field(default=1)
    worker_capacity_stale_after_seconds: int = Field(default=90)

    # Approval Settings
    approval_required_risk_levels: str = Field(default="high,critical")
    approval_timeout_hours: int = Field(default=24)

    # CORS Settings
    cors_origins: str = Field(default="http://localhost:3000")

    # Evaluation Settings
    llm_critic_enabled: bool = Field(default=True)

    # Phase 5 — Multi-agent execution
    workflow_execution_mode: str = Field(default="auto")  # auto | serial | parallel
    max_parallel_tasks: int = Field(default=4)
    task_default_timeout_seconds: int = Field(default=120)
    task_default_max_retries: int = Field(default=2)

    # Phase 5 — Advanced RAG (hybrid retrieval)
    rag_hybrid_enabled: bool = Field(default=False)
    rag_reranker: str = Field(default="deterministic")  # deterministic | llm
    rag_query_expansion_enabled: bool = Field(default=False)
    rag_query_expansion_max: int = Field(default=3)
    rag_fusion_candidates: int = Field(default=60)
    rag_context_max_tokens: int = Field(default=2000)
    rag_lexical_top_k: int = Field(default=10)

    # Phase 5 — MCP ecosystem
    mcp_catalog_json: str = Field(
        default="",
        description="JSON array of MCPServerConfig-like dicts (no secrets). Secrets come from env.",
    )
    mcp_health_check_interval_seconds: int = Field(default=60)

    # Rate Limiting Settings
    rate_limit_enabled: bool = Field(default=False)
    rate_limit_max_requests: int = Field(default=300)
    rate_limit_window_seconds: int = Field(default=60)
    rate_limit_auth_max_requests: int = Field(default=600)
    rate_limit_exempt_paths: str = Field(default="/metrics,/api/v1/health,/docs,/redoc,/openapi.json")

    @model_validator(mode="after")
    def _validate_production_security(self) -> Settings:
        """Fail fast on insecure production configuration (WS4).

        A production process must never start with a placeholder/short
        SECRET_KEY (every JWT, session, and signed value would be forgeable)
        or with debug mode enabled.  Development and test environments keep
        working defaults so the local loop is unaffected.
        """
        environment = (self.environment or "").strip().lower()
        if environment not in _PRODUCTION_ENVIRONMENTS:
            return self

        key = (self.secret_key or "").strip()
        lowered = key.lower()
        placeholder = (
            key in _INSECURE_SECRET_KEYS
            or "change-me" in lowered
            or "changeme" in lowered
            or "change_me" in lowered
            or "dev-secret" in lowered
        )
        if placeholder:
            raise ValueError(
                "Refusing to start: SECRET_KEY is a placeholder/insecure value "
                "while environment=production. Generate one with "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
                "and set SECRET_KEY."
            )
        if len(key) < _MIN_PRODUCTION_SECRET_LENGTH:
            message = (
                f"Refusing to start: SECRET_KEY must be at least "
                f"{_MIN_PRODUCTION_SECRET_LENGTH} characters in production "
                f"(got {len(key)})."
            )
            raise ValueError(message)
        if self.debug:
            raise ValueError(
                "Refusing to start: DEBUG must be false when environment=production."
            )
        if not self.rate_limit_enabled:
            logger.warning(
                "SECURITY: rate limiting is DISABLED in production "
                "(rate_limit_enabled=false) — set RATE_LIMIT_ENABLED=true."
            )
        return self


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
