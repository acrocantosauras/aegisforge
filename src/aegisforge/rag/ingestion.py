"""Document ingestion pipeline for AegisForge.

Modular pipeline: text extraction → normalization → chunking → embedding → persistence.
Each stage is independently testable.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# --- Text Extraction ---


def extract_text_from_plain(content: bytes, filename: str = "") -> str:
    """Extract text from plain text content."""
    return content.decode("utf-8", errors="replace")


def extract_text_from_markdown(content: bytes, filename: str = "") -> str:
    """Extract text from markdown content."""
    return content.decode("utf-8", errors="replace")


def extract_text_from_pdf(content: bytes, filename: str = "") -> str:
    """Extract text from PDF content.

    Uses a simple fallback approach. For production, integrate with
    a proper PDF library (e.g., pymupdf, pdfplumber).
    """
    try:
        import subprocess
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # Try pdftotext if available
            result = subprocess.run(
                ["pdftotext", tmp_path, "-"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        finally:
            os.unlink(tmp_path)
    except Exception:
        pass

    # Fallback: return a placeholder
    return f"[PDF content from {filename or 'unknown'} — text extraction not available]"


def extract_text_from_docx(content: bytes, filename: str = "") -> str:
    """Extract text from DOCX content."""
    try:
        from docx import Document
        import io

        doc = Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        return f"[DOCX content from {filename or 'unknown'} — python-docx not installed]"
    except Exception:
        return f"[DOCX content from {filename or 'unknown'} — extraction failed]"


EXTRACTORS: dict[str, callable] = {
    "text/plain": extract_text_from_plain,
    "text/markdown": extract_text_from_markdown,
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
}


def extract_text(content: bytes, content_type: str, filename: str = "") -> str:
    """Extract text from content based on content type."""
    extractor = EXTRACTORS.get(content_type)
    if extractor is None:
        raise ValueError(f"Unsupported content type: {content_type}")
    return extractor(content, filename)


# --- Normalization ---


def normalize_text(text: str) -> str:
    """Normalize extracted text for consistent processing."""
    import re

    # Remove excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    # Remove control characters except newlines and tabs
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.strip()


# --- Chunking ---


@dataclass
class Chunk:
    """A text chunk with metadata."""

    chunk_id: str
    content: str
    position: int
    document_id: str = ""
    source: str = ""
    page: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def chunk_text(
    text: str,
    document_id: str = "",
    source: str = "",
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """Split text into overlapping chunks.

    Each chunk retains position information and metadata.
    """
    if not text or not text.strip():
        return []

    chunks: list[Chunk] = []
    start = 0
    position = 0

    while start < len(text):
        end = start + chunk_size

        # Try to break at sentence or paragraph boundary
        if end < len(text):
            # Look for paragraph break
            para_break = text.rfind("\n\n", start, end)
            if para_break > start + chunk_size // 2:
                end = para_break + 2
            else:
                # Look for sentence break
                for sep in [". ", ".\n", "! ", "? ", "\n"]:
                    sent_break = text.rfind(sep, start, end)
                    if sent_break > start + chunk_size // 2:
                        end = sent_break + len(sep)
                        break

        chunk_content = text[start:end].strip()
        if chunk_content:
            chunk_id = f"chunk-{uuid.uuid4().hex[:12]}"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    content=chunk_content,
                    position=position,
                    document_id=document_id,
                    source=source,
                    metadata=metadata or {},
                )
            )
            position += 1

        # Move forward with overlap
        start = end - chunk_overlap if end < len(text) else end

    return chunks


# --- Ingestion Pipeline ---


@dataclass
class IngestionResult:
    """Result of document ingestion."""

    document_id: str
    title: str
    chunks: list[Chunk]
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


def ingest_document(
    content: bytes,
    title: str,
    content_type: str = "text/plain",
    document_id: str = "",
    organization_id: str = "",
    source: str = "",
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    metadata: dict[str, Any] | None = None,
) -> IngestionResult:
    """Run the full ingestion pipeline: extract → normalize → chunk.

    Does NOT handle embedding or persistence — those are separate steps.
    """
    document_id = document_id or f"doc-{uuid.uuid4().hex[:12]}"

    # Validate content size
    if len(content) > 10_485_760:  # 10MB
        raise ValueError("Document exceeds maximum size of 10MB")

    # Step 1: Extract text
    raw_text = extract_text(content, content_type, title)

    # Step 2: Normalize
    normalized = normalize_text(raw_text)

    if not normalized:
        return IngestionResult(
            document_id=document_id,
            title=title,
            chunks=[],
            content_hash=hashlib.sha256(content).hexdigest(),
            metadata=metadata or {},
        )

    # Step 3: Chunk
    enriched_metadata = {
        **(metadata or {}),
        "organization_id": organization_id,
        "content_type": content_type,
    }
    chunks = chunk_text(
        text=normalized,
        document_id=document_id,
        source=source or title,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        metadata=enriched_metadata,
    )

    return IngestionResult(
        document_id=document_id,
        title=title,
        chunks=chunks,
        content_hash=hashlib.sha256(content).hexdigest(),
        metadata=enriched_metadata,
    )
