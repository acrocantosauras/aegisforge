"""Document ingestion + indexing service.

Single implementation of the ingestion pipeline (extract → normalize → chunk →
embed → persist → index).  The REST upload route
(:mod:`aegisforge.api.routes.documents`) and the demo seeder both call
:func:`ingest_and_store_document` so there is exactly one pipeline, one set of
chunking settings, and one owner-scoping rule.

Tenant and owner scoping are enforced here, not by callers:

* the document row is bound to the uploader's organization **and** owner,
* chunks are written with the same organization id,
* vector rows are written with organization **and** owner predicates so
  retrieval (org + owner scoped, fail-closed) can never widen visibility.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from aegisforge.config import Settings
from aegisforge.db.models import DocumentChunkModel, DocumentModel
from aegisforge.db.session import get_session_factory
from aegisforge.rag.embeddings import get_embedding_provider
from aegisforge.rag.ingestion import ingest_document
from aegisforge.rag.vector_store import VectorStoreEntry, get_vector_store
from aegisforge.security.validation import sanitize_filename

logger = logging.getLogger(__name__)


@dataclass
class IngestedDocument:
    """Outcome of a successful ingestion."""

    document_id: str
    title: str
    chunk_count: int
    content_hash: str
    indexed: bool
    indexing_error: str = ""


def build_vector_store(settings: Settings) -> Any:
    """Build the configured vector store (pgvector on Postgres, memory otherwise)."""
    return get_vector_store(
        "pgvector" if settings.database_url.startswith("postgresql") else "memory",
        db_session_factory=lambda: get_session_factory(settings)(),
        dimension=settings.embedding_dimension,
    )


def ingest_and_store_document(
    db: Session,
    *,
    content: bytes,
    title: str,
    content_type: str,
    source: str,
    organization_id: str,
    owner_id: str,
    settings: Settings,
    document_id: str = "",
    metadata: dict[str, Any] | None = None,
    vector_store: Any | None = None,
) -> IngestedDocument:
    """Run the full ingestion pipeline and persist the document + chunks.

    ``document_id`` may be supplied by a caller that needs deterministic ids
    (the demo seeder uses a content-addressed id so repeated runs are
    idempotent); otherwise a random id is generated.

    Vector indexing is best-effort: a vector-store failure is reported on the
    result but never rolls back the authoritative document/chunk rows.
    """
    document_id = document_id or f"doc-{uuid.uuid4().hex[:12]}"
    safe_source = sanitize_filename(source or title or "document")

    ingestion_result = ingest_document(
        content=content,
        title=title,
        content_type=content_type,
        document_id=document_id,
        organization_id=organization_id,
        source=safe_source,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        metadata=metadata,
    )

    indexed = False
    indexing_error = ""
    try:
        embedding_provider = get_embedding_provider(
            settings.embedding_provider,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            batch_size=settings.embedding_batch_size,
        )
        store = vector_store or build_vector_store(settings)

        if ingestion_result.chunks:
            texts = [c.content for c in ingestion_result.chunks]
            embeddings = embedding_provider.embed_texts(texts)
            entries = [
                VectorStoreEntry(
                    id=c.chunk_id,
                    content=c.content,
                    embedding=emb,
                    metadata={
                        "document_id": c.document_id,
                        "source": c.source,
                        "organization_id": organization_id,
                        "owner_id": owner_id,
                    },
                )
                for c, emb in zip(ingestion_result.chunks, embeddings)
            ]
            # Owner-scoped indexing: retrieval enforces org AND owner, so the
            # write side must carry the same scope.
            store.add(entries, organization_id=organization_id, owner_id=owner_id)
            indexed = True
    except Exception as exc:
        indexing_error = str(exc)
        logger.warning("Vector storage failed (non-fatal): %s", exc)

    db.add(
        DocumentModel(
            id=document_id,
            organization_id=organization_id,
            title=title,
            content_type=content_type,
            source=safe_source,
            content_hash=ingestion_result.content_hash,
            chunk_count=len(ingestion_result.chunks),
            status="completed",
            doc_metadata=json.dumps(ingestion_result.metadata),
            uploaded_by=owner_id,
        )
    )
    for chunk in ingestion_result.chunks:
        db.add(
            DocumentChunkModel(
                id=chunk.chunk_id,
                document_id=document_id,
                organization_id=organization_id,
                content=chunk.content,
                position=chunk.position,
                source=chunk.source,
                chunk_metadata=json.dumps(chunk.metadata),
            )
        )
    db.commit()

    return IngestedDocument(
        document_id=document_id,
        title=title,
        chunk_count=len(ingestion_result.chunks),
        content_hash=ingestion_result.content_hash,
        indexed=indexed,
        indexing_error=indexing_error,
    )