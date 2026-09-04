"""LLM model provider abstraction for AegisForge.

Provides an abstract ModelProvider interface so the platform can support
different LLM vendors.  Application/domain logic never depends on a
specific model vendor.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from aegisforge.observability.metrics import track_llm_request

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""

    def __init__(self, message: str, provider: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class LLMTimeoutError(LLMProviderError):
    """Raised when an LLM call times out."""

    def __init__(self, message: str = "LLM call timed out", provider: str = "") -> None:
        super().__init__(message, provider=provider, retryable=True)


class LLMRateLimitError(LLMProviderError):
    """Raised when rate limited by the LLM provider."""

    def __init__(self, message: str = "Rate limited", provider: str = "", retry_after: float = 0) -> None:
        super().__init__(message, provider=provider, retryable=True)
        self.retry_after = retry_after


class LLMAuthError(LLMProviderError):
    """Raised on authentication failure."""

    def __init__(self, message: str = "Authentication failed", provider: str = "") -> None:
        super().__init__(message, provider=provider, retryable=False)


class LLMMalformedOutputError(LLMProviderError):
    """Raised when the LLM returns output that can't be parsed."""

    def __init__(self, message: str = "Malformed LLM output", provider: str = "") -> None:
        super().__init__(message, provider=provider, retryable=True)


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str
    model: str = ""
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryConfig:
    """Configuration for LLM retry behavior."""

    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    backoff_factor: float = 2.0
    retryable_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)


class ModelProvider(ABC):
    """Abstract base for LLM providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name."""
        ...

    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate a completion from messages."""
        ...

    def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate a structured (JSON) completion.

        Default implementation calls generate and parses JSON from the response.
        Providers can override for native structured output support.
        """
        response = self.generate(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

        # Try to extract JSON from the response
        try:
            content = response.content.strip()
            # Handle markdown code blocks
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            json.loads(content)
        except (json.JSONDecodeError, ValueError):
            logger.warning("LLM response is not valid JSON, returning as-is")

        return response

    def validate_output(
        self,
        response: LLMResponse,
        schema: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        """Validate LLM output against a Pydantic schema if provided.

        Returns the parsed dict. Raises LLMMalformedOutputError on failure.
        """
        if schema is None:
            return {"content": response.content}

        try:
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
            data = json.loads(content)
            validated = schema.model_validate(data)
            return validated.model_dump()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMMalformedOutputError(
                f"Failed to parse LLM output as JSON: {exc}",
                provider=self.provider_name,
            ) from exc
        except ValidationError as exc:
            raise LLMMalformedOutputError(
                f"LLM output failed schema validation: {exc}",
                provider=self.provider_name,
            ) from exc


def _retry_with_backoff(
    func: Any,
    retry_config: RetryConfig,
    provider_name: str = "",
) -> Any:
    """Execute a function with exponential backoff retry."""
    last_exception: Exception | None = None

    for attempt in range(retry_config.max_retries + 1):
        try:
            return func()
        except LLMProviderError as exc:
            last_exception = exc
            if not exc.retryable or attempt >= retry_config.max_retries:
                raise
            delay = min(
                retry_config.base_delay_seconds * (retry_config.backoff_factor ** attempt),
                retry_config.max_delay_seconds,
            )
            if isinstance(exc, LLMRateLimitError) and exc.retry_after > 0:
                delay = max(delay, exc.retry_after)
            logger.warning(
                "LLM provider %s attempt %d/%d failed (retryable): %s. Retrying in %.1fs",
                provider_name,
                attempt + 1,
                retry_config.max_retries + 1,
                exc,
                delay,
            )
            time.sleep(delay)
        except Exception as exc:
            raise LLMProviderError(
                f"Unexpected LLM error: {exc}",
                provider=provider_name,
                retryable=False,
            ) from exc

    raise last_exception or LLMProviderError("Max retries exceeded", provider=provider_name)


class DeterministicModelProvider(ModelProvider):
    """Deterministic fake model provider for testing.

    Returns structured responses without making any API calls.
    NOT suitable for production use.
    """

    def __init__(self, response_map: dict[str, str] | None = None) -> None:
        self._response_map = response_map or {}

    @property
    def provider_name(self) -> str:
        return "deterministic"

    def generate(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        # Build a deterministic response based on message content
        last_message = messages[-1]["content"] if messages else ""

        resolved_model = model or "deterministic-fake"

        with track_llm_request(self.provider_name, resolved_model) as meta:
            # Check response map
            for key, response in self._response_map.items():
                if key.lower() in last_message.lower():
                    llm_response = LLMResponse(
                        content=response,
                        model=resolved_model,
                        tokens_used=len(response.split()),
                        latency_ms=10,
                        finish_reason="stop",
                    )
                    meta["tokens"] = llm_response.tokens_used
                    return llm_response

            # Default response: echo the intent with a structured plan
            response = json.dumps(
                {
                    "plan_id": f"plan-{uuid.uuid4().hex[:12]}",
                    "tasks": [
                        {
                            "task_id": f"task-{uuid.uuid4().hex[:12]}",
                            "description": f"Investigate and address: {last_message[:200]}",
                            "assigned_agent_type": "research",
                            "dependencies": [],
                            "expected_output_description": "Investigation result",
                            "tool_permissions_required": ["knowledge.search"],
                        }
                    ],
                },
                indent=2,
            )

            llm_response = LLMResponse(
                content=response,
                model=resolved_model,
                tokens_used=len(response.split()),
                latency_ms=10,
                finish_reason="stop",
            )
            meta["tokens"] = llm_response.tokens_used
            return llm_response


class OpenAIModelProvider(ModelProvider):
    """OpenAI API model provider with retry, timeout, and structured output."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        retry_config: RetryConfig | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._retry_config = retry_config or RetryConfig()

    @property
    def provider_name(self) -> str:
        return "openai"

    def generate(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMAuthError("OpenAI API key is required", provider="openai")

        def _call() -> LLMResponse:
            import httpx

            start = time.monotonic()
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model or "gpt-4o-mini",
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=float(timeout_seconds),
            )

            if response.status_code == 401:
                raise LLMAuthError("OpenAI authentication failed", provider="openai")
            if response.status_code == 429:
                retry_after = float(response.headers.get("retry-after", "0"))
                raise LLMRateLimitError(
                    "OpenAI rate limit exceeded", provider="openai", retry_after=retry_after
                )
            if response.status_code >= 500:
                raise LLMProviderError(
                    f"OpenAI server error: {response.status_code}",
                    provider="openai",
                    retryable=True,
                )

            response.raise_for_status()
            data = response.json()
            elapsed_ms = int((time.monotonic() - start) * 1000)

            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})

            return LLMResponse(
                content=content,
                model=data.get("model", model),
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=elapsed_ms,
                finish_reason=choice.get("finish_reason", ""),
                raw=data,
            )

        resolved_model = model or "gpt-4o-mini"
        with track_llm_request(self.provider_name, resolved_model) as meta:
            result = _retry_with_backoff(_call, self._retry_config, "openai")
            meta["tokens"] = result.tokens_used
            return result

    def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate with JSON response format."""
        import httpx

        if not self._api_key:
            raise LLMAuthError("OpenAI API key is required", provider="openai")

        def _call() -> LLMResponse:
            start = time.monotonic()
            body: dict[str, Any] = {
                "model": model or "gpt-4o-mini",
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=float(timeout_seconds),
            )

            if response.status_code == 401:
                raise LLMAuthError("OpenAI authentication failed", provider="openai")
            if response.status_code == 429:
                retry_after = float(response.headers.get("retry-after", "0"))
                raise LLMRateLimitError(
                    "OpenAI rate limit exceeded", provider="openai", retry_after=retry_after
                )
            if response.status_code >= 500:
                raise LLMProviderError(
                    f"OpenAI server error: {response.status_code}", provider="openai", retryable=True
                )

            response.raise_for_status()
            data = response.json()
            elapsed_ms = int((time.monotonic() - start) * 1000)

            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})

            return LLMResponse(
                content=content,
                model=data.get("model", model),
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_ms=elapsed_ms,
                finish_reason=choice.get("finish_reason", ""),
                raw=data,
            )

        resolved_model = model or "gpt-4o-mini"
        with track_llm_request(self.provider_name, resolved_model) as meta:
            result = _retry_with_backoff(_call, self._retry_config, "openai")
            meta["tokens"] = result.tokens_used
            return result


class AnthropicModelProvider(ModelProvider):
    """Anthropic Claude API model provider with retry and structured output."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        retry_config: RetryConfig | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._retry_config = retry_config or RetryConfig()

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def generate(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMAuthError("Anthropic API key is required", provider="anthropic")

        def _call() -> LLMResponse:
            import httpx

            # Separate system message from user/assistant messages
            system_text = ""
            api_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_text = msg["content"]
                else:
                    api_messages.append(msg)

            body: dict[str, Any] = {
                "model": model or "claude-sonnet-4-20250514",
                "messages": api_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if system_text:
                body["system"] = system_text

            start = time.monotonic()
            response = httpx.post(
                f"{self._base_url}/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=float(timeout_seconds),
            )

            if response.status_code == 401:
                raise LLMAuthError("Anthropic authentication failed", provider="anthropic")
            if response.status_code == 429:
                retry_after = float(response.headers.get("retry-after", "0"))
                raise LLMRateLimitError(
                    "Anthropic rate limit exceeded", provider="anthropic", retry_after=retry_after
                )
            if response.status_code >= 500:
                raise LLMProviderError(
                    f"Anthropic server error: {response.status_code}",
                    provider="anthropic",
                    retryable=True,
                )

            response.raise_for_status()
            data = response.json()
            elapsed_ms = int((time.monotonic() - start) * 1000)

            content_blocks = data.get("content", [])
            content = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            usage = data.get("usage", {})

            return LLMResponse(
                content=content,
                model=data.get("model", model),
                tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                latency_ms=elapsed_ms,
                finish_reason=data.get("stop_reason", ""),
                raw=data,
            )

        resolved_model = model or "claude-sonnet-4-20250514"
        with track_llm_request(self.provider_name, resolved_model) as meta:
            result = _retry_with_backoff(_call, self._retry_config, "anthropic")
            meta["tokens"] = result.tokens_used
            return result

    def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate with JSON prompt instructions for structured output."""
        # Add JSON instruction to system prompt
        json_instruction = (
            "\n\nYou must respond with valid JSON only. "
            "No markdown, no explanation, just the JSON object."
        )
        enhanced_messages = []
        for msg in messages:
            if msg["role"] == "system":
                enhanced_messages.append({
                    "role": "system",
                    "content": msg["content"] + json_instruction,
                })
            else:
                enhanced_messages.append(msg)

        if not any(m["role"] == "system" for m in enhanced_messages):
            enhanced_messages.insert(0, {
                "role": "system",
                "content": "You must respond with valid JSON only." + json_instruction,
            })

        return self.generate(
            messages=enhanced_messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )


def get_model_provider(
    provider_type: str = "deterministic",
    **kwargs: Any,
) -> ModelProvider:
    """Factory function to create model providers."""
    if provider_type == "deterministic":
        return DeterministicModelProvider(
            response_map=kwargs.get("response_map"),
        )
    elif provider_type == "openai":
        return OpenAIModelProvider(
            api_key=kwargs.get("api_key", ""),
            base_url=kwargs.get("base_url", "https://api.openai.com/v1"),
            retry_config=kwargs.get("retry_config"),
        )
    elif provider_type == "anthropic":
        return AnthropicModelProvider(
            api_key=kwargs.get("api_key", ""),
            base_url=kwargs.get("base_url", "https://api.anthropic.com"),
            retry_config=kwargs.get("retry_config"),
        )
    else:
        raise ValueError(f"Unknown model provider: {provider_type}")
