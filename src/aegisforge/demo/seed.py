"""Deterministic demo seeding (Phase 10).

Seeds the synthetic flagship-demo corpus through the **real** ingestion
pipeline (:func:`aegisforge.services.document_service.ingest_and_store_document`):
same extractor, normalizer, chunker, embedding provider, vector store, and
owner-scoping rules as a document uploaded through ``POST /documents``.

Properties that matter for a reproducible demo:

* **Deterministic** — document ids are derived from the corpus slug, and the
  configured embedding provider is deterministic, so repeated seeds produce
  identical ids, chunk counts, and vectors.
* **Idempotent** — a demo document that already exists for the same
  organization + owner is skipped, never duplicated.  Changed corpus content is
  detected by content hash and re-ingested.
* **Tenant-preserving** — everything is written under the seeding user's own
  organization and owner id.  Retrieval is org + owner scoped and fail-closed,
  so demo knowledge never leaks into another user's RAG results.
* **Reported** — the caller gets an exact created / skipped / updated breakdown
  so a demo run can state what happened rather than asserting success.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from aegisforge.config import Settings
from aegisforge.db.models import DocumentChunkModel, DocumentModel
from aegisforge.demo.scenario import (
    DEMO_DOCUMENTS,
    DemoDocument,
    demo_id_prefix,
    document_id_for,
    read_demo_document,
)
from aegisforge.rag.ingestion import IngestionResult, ingest_document
from aegisforge.services.document_service import (
    build_vector_store,
    ingest_and_store_document,
)

logger = logging.getLogger(__name__)


@dataclass
class SeedOutcome:
    """What a seed run actually did."""

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    chunk_counts: dict[str, int] = field(default_factory=dict)
    #: Document slugs whose vector indexing failed (rows persisted, not indexed).
    indexing_failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.skipped) + len(self.updated)

    def summary(self) -> str:
        return (
            f"{self.total} demo document(s): "
            f"{len(self.created)} created, "
            f"{len(self.updated)} updated, "
            f"{len(self.skipped)} unchanged"
        )


def _existing_document(
    db: Session,
    document_id: str,
    organization_id: str,
    owner_id: str,
) -> DocumentModel | None:
    """Look up a document under the exact org + owner scope."""
    return (
        db.query(DocumentModel)
        .filter(
            DocumentModel.id == document_id,
            DocumentModel.organization_id == organization_id,
            DocumentModel.uploaded_by == owner_id,
        )
        .first()
    )


def _expected_content_hash(
    document: DemoDocument,
    settings: Settings,
    document_id: str,
) -> str:
    """Hash the demo file the same way the ingestion pipeline would."""
    content = read_demo_document(document)
    result: IngestionResult = ingest_document(
        content=content,
        title=document.title,
        content_type="text/markdown",
        document_id=document_id,
        organization_id="",
        source=document.slug,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    return result.content_hash


def delete_demo_documents(
    db: Session,
    *,
    organization_id: str,
    owner_id: str,
    settings: Settings,
    vector_store: Any | None = None,
) -> int:
    """Remove every seeded demo document for one owner.

    Uses the same authorization predicate as the REST delete endpoint (org +
    owner) and removes chunk rows and vector rows, so a reset leaves no
    orphaned embeddings behind.
    """
    documents = (
        db.query(DocumentModel)
        .filter(
            DocumentModel.organization_id == organization_id,
            DocumentModel.uploaded_by == owner_id,
            DocumentModel.id.like(f"{demo_id_prefix()}%"),
        )
        .all()
    )
    if not documents:
        return 0

    store = vector_store or build_vector_store(settings)
    removed = 0
    for document in documents:
        try:
            store.delete_by_document(
                document.id,
                organization_id=organization_id,
                owner_id=owner_id,
            )
        except Exception as exc:  # noqa: BLE001 - reset must still clear rows
            logger.warning(
                "Vector cleanup failed for demo document %s: %s", document.id, exc
            )
        db.query(DocumentChunkModel).filter(
            DocumentChunkModel.document_id == document.id,
            DocumentChunkModel.organization_id == organization_id,
        ).delete(synchronize_session=False)
        db.delete(document)
        removed += 1
    db.commit()
    return removed


def seed_demo_documents(
    db: Session,
    *,
    organization_id: str,
    owner_id: str,
    settings: Settings,
    documents: tuple[DemoDocument, ...] | list[DemoDocument] = DEMO_DOCUMENTS,
    vector_store: Any | None = None,
    reset: bool = False,
) -> SeedOutcome:
    """Seed the demo corpus for one user, idempotently.

    Args:
        db: active database session.
        organization_id: the seeding user's organization (never overridden).
        owner_id: the seeding user's id — every document and embedding is bound
            to this owner so retrieval stays owner-scoped.
        settings: active application settings (chunking + embedding provider).
        documents: corpus subset (defaults to the full manifest).
        vector_store: injectable store (tests pass an in-memory store).
        reset: delete previously seeded demo documents first.
    """
    outcome = SeedOutcome()
    if not organization_id or not owner_id:
        raise ValueError(
            "Demo seeding requires an organization_id and owner_id; "
            "demo documents are always owner-scoped."
        )

    store = vector_store or build_vector_store(settings)

    if reset:
        delete_demo_documents(
            db,
            organization_id=organization_id,
            owner_id=owner_id,
            settings=settings,
            vector_store=store,
        )

    for document in documents:
        document_id = document_id_for(document.slug, owner_id)
        content = read_demo_document(document)
        expected_hash = _expected_content_hash(document, settings, document_id)

        existing = _existing_document(db, document_id, organization_id, owner_id)
        if existing is not None and existing.content_hash == expected_hash:
            outcome.skipped.append(document.slug)
            outcome.chunk_counts[document.slug] = existing.chunk_count
            continue

        if existing is not None:
            # Corpus changed (or a partial previous run): clear the old rows
            # and embeddings before re-ingesting under the same id.
            try:
                store.delete_by_document(
                    document_id,
                    organization_id=organization_id,
                    owner_id=owner_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Vector cleanup failed for demo document %s: %s", document_id, exc
                )
            db.query(DocumentChunkModel).filter(
                DocumentChunkModel.document_id == document_id,
                DocumentChunkModel.organization_id == organization_id,
            ).delete(synchronize_session=False)
            db.delete(existing)
            db.commit()

        ingested = ingest_and_store_document(
            db,
            content=content,
            title=document.title,
            content_type="text/markdown",
            source=f"{document.slug}.md",
            organization_id=organization_id,
            owner_id=owner_id,
            settings=settings,
            document_id=document_id,
            metadata={"demo": True, "demo_slug": document.slug, "category": document.category},
            vector_store=store,
        )

        outcome.chunk_counts[document.slug] = ingested.chunk_count
        if not ingested.indexed:
            outcome.indexing_failures.append(document.slug)
        if existing is None:
            outcome.created.append(document.slug)
        else:
            outcome.updated.append(document.slug)

    return outcome