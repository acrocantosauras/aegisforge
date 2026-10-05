"""Document management API endpoints for AegisForge.

Provides: upload, list, detail, delete documents with tenant isolation.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_settings
from aegisforge.db.models import DocumentChunkModel, DocumentModel, UserModel
from aegisforge.db.session import get_db
from aegisforge.security.validation import (
    sanitize_filename,
    validate_document_upload,
)
from aegisforge.services.auth_service import get_current_user
from aegisforge.services.document_service import (
    build_vector_store,
    ingest_and_store_document,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentRead(BaseModel):
    id: str
    organization_id: str
    title: str
    content_type: str
    source: str
    chunk_count: int
    status: str
    uploaded_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentUploadResponse(BaseModel):
    document_id: str
    title: str
    chunk_count: int
    status: str
    message: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentRead]
    total: int
    page: int
    page_size: int


@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    title: str = "",
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> DocumentUploadResponse:
    """Upload and ingest a document."""
    filename = sanitize_filename(file.filename or "unnamed")
    content = await file.read()
    content_type = file.content_type or "text/plain"
    if not title:
        title = filename

    # Security validation
    validation = validate_document_upload(
        filename=filename,
        content_type=content_type,
        content_size=len(content),
        max_size=settings.max_document_size_bytes,
        allowed_types=set(settings.allowed_content_types.split(",")),
    )
    if not validation.passed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"violations": validation.violations},
        )

    # Shared ingestion pipeline (extract → normalize → chunk → embed →
    # persist → index). Owner-scoped: the document, its chunks, and its
    # vector rows are all bound to the uploading user.
    try:
        ingested = ingest_and_store_document(
            db,
            content=content,
            title=title,
            content_type=content_type,
            source=filename,
            organization_id=user.organization_id,
            owner_id=user.id,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return DocumentUploadResponse(
        document_id=ingested.document_id,
        title=ingested.title,
        chunk_count=ingested.chunk_count,
        status="completed",
        message=f"Document ingested with {ingested.chunk_count} chunk(s)",
    )


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> DocumentListResponse:
    """List the caller's documents (org + owner scoped).

    SECURITY: org-only listing would expose every user's document titles and
    sources to the shared default organization.  Users see their own uploads.
    """
    query = db.query(DocumentModel).filter(
        DocumentModel.organization_id == user.organization_id,
        DocumentModel.uploaded_by == user.id,
    )
    total = query.count()
    documents = (
        query.order_by(DocumentModel.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return DocumentListResponse(
        documents=[DocumentRead.model_validate(d) for d in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(
    document_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> DocumentModel:
    """Get document details (org + owner scoped; 404 — no existence oracle)."""
    doc = db.query(DocumentModel).filter(DocumentModel.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.organization_id != user.organization_id or doc.uploaded_by != user.id:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> None:
    """Delete a document and its chunks (org + owner scoped)."""
    doc = db.query(DocumentModel).filter(DocumentModel.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.organization_id != user.organization_id or doc.uploaded_by != user.id:
        # Same-org users must not be able to delete each other's documents.
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete chunks (org-scoped for defense in depth; ownership is already
    # proven via the parent document row above)
    db.query(DocumentChunkModel).filter(
        DocumentChunkModel.document_id == document_id,
        DocumentChunkModel.organization_id == user.organization_id,
    ).delete()

    # Delete from vector store (org + owner scoped — same authorization
    # predicate as retrieval, so a delete can never touch another user's
    # chunks even if document metadata rows were manipulated).
    try:
        vector_store = build_vector_store(settings)
        vector_store.delete_by_document(
            document_id, user.organization_id, owner_id=user.id
        )
    except Exception as exc:
        logger.warning("Failed to delete from vector store: %s", exc)

    # Delete document
    db.delete(doc)
    db.commit()
