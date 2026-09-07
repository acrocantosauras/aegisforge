"""Embedding provider abstraction for AegisForge.

The embedding provider is replaceable.  Domain code depends only on the
EmbeddingProvider interface, never on a specific provider implementation.
"""
from __future__ import annotations

import logging
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


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens."""
    import re

    return re.findall(r"[a-z0-9]+", text.lower())


def _bow_embedding(
    text: str,
    dimension: int,
    hash_seed: int = 0,
) -> list[float]:
    """Deterministic bag-of-words style embedding.

    Token overlap drives cosine similarity, so this provider produces
    semantically useful similarity ordering for retrieval-quality tests
    without depending on a real embedding model.
    """
    import hashlib
    import math

    vec = [0.0] * dimension
    tokens = _tokenize(text)
    for token in tokens:
        h = int(
            hashlib.sha256(
                f"{token}|{hash_seed}".encode()
            ).hexdigest()[:8],
            16,
        )
        idx = (h + hash_seed) % dimension
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """Deterministic embedding provider for testing and evaluation.

    Uses a bag-of-words style embedding for backward-compatible hash-based
    test vectors, plus an optional neighbor-aware term weighting so the
    evaluation corpus can exercise semantic/paraphrase discrimination without
    depending on a real embedding model.

    NOT suitable for production use.
    """

    def __init__(self, dimension: int = 384) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [_bow_embedding(text, self._dimension, hash_seed=0) for text in texts]


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
