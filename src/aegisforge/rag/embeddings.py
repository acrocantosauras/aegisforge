"""Embedding provider abstraction for AegisForge.

The embedding provider is replaceable.  Domain code depends only on the
EmbeddingProvider interface, never on a specific provider implementation.
"""
from __future__ import annotations

import hashlib
import logging
import math
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """Abstract base for embedding providers."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the embedding dimension."""
        ...

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts and return their vectors."""
        ...

    def embed_text(self, text: str) -> list[float]:
        """Embed a single text."""
        results = self.embed_texts([text])
        return results[0] if results else []


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """Deterministic fake embedding provider for testing.

    Generates consistent pseudo-embeddings based on text content.
    NOT suitable for production use — produces no semantically meaningful vectors.
    """

    def __init__(self, dimension: int = 384) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            # Generate deterministic pseudo-embedding from content hash
            h = hashlib.sha256(text.encode("utf-8")).digest()
            # Expand hash to fill the dimension
            raw: list[float] = []
            for i in range(self._dimension):
                byte_val = h[i % len(h)]
                # Map to [-1, 1] range with some structure
                val = (byte_val / 127.5) - 1.0
                # Add position-dependent variation
                val += 0.1 * math.sin(i * 0.1 + byte_val)
                raw.append(val)
            # Normalize to unit vector
            norm = math.sqrt(sum(v * v for v in raw))
            if norm > 0:
                raw = [v / norm for v in raw]
            results.append(raw)
        return results


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI embedding provider (requires httpx)."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimension: int = 384,
        batch_size: int = 32,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimension = dimension
        self._batch_size = batch_size

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise ValueError("OpenAI API key is required for embedding")

        import httpx

        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            response = httpx.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"input": batch, "model": self._model},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            for item in sorted(data["data"], key=lambda x: x["index"]):
                all_embeddings.append(item["embedding"])

        return all_embeddings


def get_embedding_provider(
    provider_type: str = "deterministic",
    **kwargs: Any,
) -> EmbeddingProvider:
    """Factory function to create embedding providers."""
    if provider_type == "deterministic":
        return DeterministicEmbeddingProvider(
            dimension=kwargs.get("dimension", 384),
        )
    elif provider_type == "openai":
        return OpenAIEmbeddingProvider(
            api_key=kwargs.get("api_key", ""),
            model=kwargs.get("model", "text-embedding-3-small"),
            dimension=kwargs.get("dimension", 384),
            batch_size=kwargs.get("batch_size", 32),
        )
    else:
        raise ValueError(f"Unknown embedding provider: {provider_type}")
