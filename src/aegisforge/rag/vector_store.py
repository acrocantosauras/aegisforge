"""Vector store abstraction for AegisForge.

Provides an abstract VectorStore interface and two implementations:
- InMemoryVectorStore: for testing and development
- PgVectorStore: for production with PostgreSQL + pgvector
"""
from __future__ import annotations

import json
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VectorStoreEntry:
    """A single entry in the vector store."""

    id: str
    content: str
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    """A single search result from the vector store."""

    id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(ABC):
    """Abstract vector store interface."""

    @abstractmethod
    def add(
        self,
        entries: list[VectorStoreEntry],
        organization_id: str = "",
    ) -> None:
        """Add entries to the vector store."""
        ...

    @abstractmethod
    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        organization_id: str = "",
        metadata_filter: dict[str, Any] | None = None,
        similarity_threshold: float = 0.0,
    ) -> list[SearchResult]:
        """Search the vector store by embedding similarity."""
        ...

    @abstractmethod
    def delete_by_document(self, document_id: str, organization_id: str = "") -> int:
        """Delete all entries for a document. Returns count deleted."""
        ...

    @abstractmethod
    def count(self, organization_id: str = "") -> int:
        """Count entries, optionally filtered by organization."""
        ...


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class InMemoryVectorStore(VectorStore):
    """In-memory vector store for testing and development.

    NOT suitable for production — data is lost on restart.
    """

    def __init__(self) -> None:
        self._entries: list[VectorStoreEntry] = []
        self._org_map: dict[str, str] = {}  # entry_id -> organization_id

    def add(self, entries: list[VectorStoreEntry], organization_id: str = "") -> None:
        for entry in entries:
            self._entries.append(entry)
            if organization_id:
                self._org_map[entry.id] = organization_id

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        organization_id: str = "",
        metadata_filter: dict[str, Any] | None = None,
        similarity_threshold: float = 0.0,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []

        for entry in self._entries:
            # Organization filtering
            if organization_id and self._org_map.get(entry.id, "") != organization_id:
                continue

            # Metadata filtering
            if metadata_filter:
                match = True
                for key, value in metadata_filter.items():
                    if entry.metadata.get(key) != value:
                        match = False
                        break
                if not match:
                    continue

            # Compute similarity
            score = _cosine_similarity(query_embedding, entry.embedding)

            if score >= similarity_threshold:
                results.append(
                    SearchResult(
                        id=entry.id,
                        content=entry.content,
                        score=score,
                        metadata=entry.metadata,
                    )
                )

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def delete_by_document(self, document_id: str, organization_id: str = "") -> int:
        original_count = len(self._entries)
        self._entries = [
            e
            for e in self._entries
            if not (
                e.metadata.get("document_id") == document_id
                and (not organization_id or self._org_map.get(e.id, "") == organization_id)
            )
        ]
        return original_count - len(self._entries)

    def count(self, organization_id: str = "") -> int:
        if not organization_id:
            return len(self._entries)
        return sum(1 for eid in self._entries if self._org_map.get(eid.id, "") == organization_id)


class PgVectorStore(VectorStore):
    """PostgreSQL + pgvector implementation.

    Requires: CREATE EXTENSION IF NOT EXISTS vector;
    """

    def __init__(self, db_session_factory: Any, dimension: int = 384) -> None:
        self._session_factory = db_session_factory
        self._dimension = dimension
        self._initialized = False

    def _ensure_table(self) -> None:
        """Create the vector store table if it doesn't exist.

        Raises on failure: in production the caller fails clearly instead
        of silently degrading to an in-memory vector store.
        """
        if self._initialized:
            return
        from sqlalchemy import text

        session = self._session_factory()
        try:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            session.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS vector_embeddings (
                        id VARCHAR(64) PRIMARY KEY,
                        organization_id VARCHAR(64) NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        embedding vector({self._dimension}) NOT NULL,
                        metadata JSONB DEFAULT '{{}}'::jsonb,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """
                )
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_vector_embeddings_org "
                    "ON vector_embeddings (organization_id)"
                )
            )
            session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_vector_embeddings_embedding "
                    "ON vector_embeddings USING hnsw (embedding vector_cosine_ops)"
                )
            )
            session.commit()
            self._initialized = True
        except Exception as exc:
            logger.warning("Could not initialize pgvector table: %s", exc)
            raise RuntimeError(f"pgvector initialization failed: {exc}") from exc
        finally:
            if session is not None:
                session.close()

    def add(self, entries: list[VectorStoreEntry], organization_id: str = "") -> None:
        self._ensure_table()
        if not entries:
            return
        try:
            from sqlalchemy import text

            session = self._session_factory()
            try:
                for entry in entries:
                    embedding_str = "[" + ",".join(str(v) for v in entry.embedding) + "]"
                    session.execute(
                        text(
                            "INSERT INTO vector_embeddings (id, organization_id, content, embedding, metadata) "
                            "VALUES (:id, :org_id, :content, CAST(:embedding AS vector), "
                            "CAST(:metadata AS jsonb))"
                        ),
                        {
                            "id": entry.id,
                            "org_id": organization_id,
                            "content": entry.content,
                            "embedding": embedding_str,
                            "metadata": json.dumps(entry.metadata),
                        },
                    )
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            logger.exception("Failed to add embeddings to pgvector")
            raise RuntimeError(f"pgvector insert failed: {exc}") from exc

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        organization_id: str = "",
        metadata_filter: dict[str, Any] | None = None,
        similarity_threshold: float = 0.0,
    ) -> list[SearchResult]:
        self._ensure_table()
        try:
            from sqlalchemy import text

            session = self._session_factory()
            try:
                embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

                where_clauses = ["1=1"]
                params: dict[str, Any] = {
                    "query_embedding": embedding_str,
                    "top_k": top_k,
                }

                if organization_id:
                    where_clauses.append("organization_id = :org_id")
                    params["org_id"] = organization_id

                if similarity_threshold > 0:
                    where_clauses.append(
                        "1 - (embedding <=> CAST(:query_embedding AS vector)) >= :threshold"
                    )
                    params["threshold"] = similarity_threshold

                where_sql = " AND ".join(where_clauses)

                query = text(
                    f"SELECT id, content, 1 - (embedding <=> CAST(:query_embedding AS vector)) as score, metadata "
                    f"FROM vector_embeddings "
                    f"WHERE {where_sql} "
                    f"ORDER BY embedding <=> CAST(:query_embedding AS vector) "
                    f"LIMIT :top_k"
                )

                rows = session.execute(query, params).fetchall()
                results = []
                for row in rows:
                    # psycopg3 returns JSONB as a dict; older drivers as str.
                    meta = row.metadata
                    if isinstance(meta, str):
                        meta = json.loads(meta) if meta else {}
                    meta = meta or {}
                    # Apply metadata filter post-query if needed
                    if metadata_filter:
                        match = all(meta.get(k) == v for k, v in metadata_filter.items())
                        if not match:
                            continue
                    results.append(
                        SearchResult(
                            id=row.id,
                            content=row.content,
                            score=float(row.score),
                            metadata=meta,
                        )
                    )
                return results
            finally:
                session.close()
        except Exception:
            logger.exception("Failed to search pgvector")
            return []

    def delete_by_document(self, document_id: str, organization_id: str = "") -> int:
        self._ensure_table()
        try:
            from sqlalchemy import text

            session = self._session_factory()
            try:
                where_clauses = ["metadata->>'document_id' = :doc_id"]
                params: dict[str, Any] = {"doc_id": document_id}

                if organization_id:
                    where_clauses.append("organization_id = :org_id")
                    params["org_id"] = organization_id

                where_sql = " AND ".join(where_clauses)
                result = session.execute(
                    text(f"DELETE FROM vector_embeddings WHERE {where_sql}"), params
                )
                session.commit()
                return result.rowcount
            finally:
                session.close()
        except Exception:
            logger.exception("Failed to delete from pgvector")
            return 0

    def count(self, organization_id: str = "") -> int:
        self._ensure_table()
        try:
            from sqlalchemy import text

            session = self._session_factory()
            try:
                if organization_id:
                    row = session.execute(
                        text("SELECT COUNT(*) FROM vector_embeddings WHERE organization_id = :org_id"),
                        {"org_id": organization_id},
                    ).fetchone()
                else:
                    row = session.execute(text("SELECT COUNT(*) FROM vector_embeddings")).fetchone()
                return row[0] if row else 0
            finally:
                session.close()
        except Exception:
            return 0


def get_vector_store(
    store_type: str = "memory",
    **kwargs: Any,
) -> VectorStore:
    """Factory function to create vector stores."""
    if store_type == "memory":
        return InMemoryVectorStore()
    elif store_type == "pgvector":
        return PgVectorStore(
            db_session_factory=kwargs.get("db_session_factory"),
            dimension=kwargs.get("dimension", 384),
        )
    else:
        raise ValueError(f"Unknown vector store type: {store_type}")
