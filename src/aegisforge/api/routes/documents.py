"""Document management API endpoints for AegisForge.

Provides: upload, list, detail, delete documents with tenant isolation.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aegisforge.config import Settings, get_settings
from aegisforge.db.models import DocumentChunkModel, DocumentModel, UserModel
from aegisforge.db.session import get_db, get_session_factory
from aegisforge.rag.embeddings import get_embedding_provider
from aegisforge.rag.ingestion import ingest_document
from aegisforge.rag.vector_store import VectorStoreEntry, get_vector_store
from aegisforge.security.validation import (
    sanitize_filename,
    validate_document_upload,
)
from aegisforge.services.auth_service import get_current_user

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

    document_id = f"doc-{uuid.uuid4().hex[:12]}"

    # Ingest
    try:
        ingestion_result = ingest_document(
            content=content,
            title=title,
            content_type=content_type,
            document_id=document_id,
            organization_id=user.organization_id,
            source=filename,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Embed and store in vector store
    try:
        embedding_provider = get_embedding_provider(
            settings.embedding_provider,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            batch_size=settings.embedding_batch_size,
        )
        vector_store = get_vector_store(
            "pgvector" if settings.database_url.startswith("postgresql") else "memory",
            db_session_factory=lambda: get_session_factory(settings)(),
            dimension=settings.embedding_dimension,
        )

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
                        "organization_id": user.organization_id,
                    },
                )
                for c, emb in zip(ingestion_result.chunks, embeddings)
            ]
            vector_store.add(entries, organization_id=user.organization_id)
    except Exception as exc:
        logger.warning("Vector storage failed (non-fatal): %s", exc)

    # Persist document metadata
    doc_model = DocumentModel(
        id=document_id,
        organization_id=user.organization_id,
        title=title,
        content_type=content_type,
        source=filename,
        content_hash=ingestion_result.content_hash,
        chunk_count=len(ingestion_result.chunks),
        status="completed",
        doc_metadata=json.dumps(ingestion_result.metadata),
        uploaded_by=user.id,
    )
    db.add(doc_model)

    # Persist chunks
    for chunk in ingestion_result.chunks:
        chunk_model = DocumentChunkModel(
            id=chunk.chunk_id,
            document_id=document_id,
            organization_id=user.organization_id,
            content=chunk.content,
            position=chunk.position,
            source=chunk.source,
            chunk_metadata=json.dumps(chunk.metadata),
        )
        db.add(chunk_model)

    db.commit()

    return DocumentUploadResponse(
        document_id=document_id,
        title=title,
        chunk_count=len(ingestion_result.chunks),
        status="completed",
        message=f"Document ingested with {len(ingestion_result.chunks)} chunk(s)",
    )


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
) -> DocumentListResponse:
    """List documents for the user's organization."""
    query = db.query(DocumentModel).filter(
        DocumentModel.organization_id == user.organization_id
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
    """Get document details with tenant isolation."""
    doc = db.query(DocumentModel).filter(DocumentModel.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return doc


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: str,
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> None:
    """Delete a document and its chunks with tenant isolation."""
    doc = db.query(DocumentModel).filter(DocumentModel.id == document_id).first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.organization_id != user.organization_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Delete chunks
    db.query(DocumentChunkModel).filter(
        DocumentChunkModel.document_id == document_id
    ).delete()

    # Delete from vector store
    try:
        vector_store = get_vector_store(
            "pgvector" if settings.database_url.startswith("postgresql") else "memory",
            db_session_factory=lambda: get_session_factory(settings)(),
            dimension=settings.embedding_dimension,
        )
        vector_store.delete_by_document(document_id, user.organization_id)
    except Exception as exc:
        logger.warning("Failed to delete from vector store: %s", exc)

    # Delete document
    db.delete(doc)
    db.commit()
