"""phase3 rag, mcp, async, approval tables

Revision ID: 20260903_phase3
Revises: 20260902_phase1
Create Date: 2026-09-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260903_phase3"
down_revision = "20260902_phase1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Documents ---
    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_id"), "documents", ["id"], unique=False)
    op.create_index(op.f("ix_documents_organization_id"), "documents", ["organization_id"], unique=False)

    # --- Document Chunks ---
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=256), nullable=True),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_document_chunks_id"), "document_chunks", ["id"], unique=False)
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False)
    op.create_index(op.f("ix_document_chunks_organization_id"), "document_chunks", ["organization_id"], unique=False)

    # --- Execution Jobs ---
    op.create_table(
        "execution_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("errors", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_execution_jobs_id"), "execution_jobs", ["id"], unique=False)
    op.create_index(op.f("ix_execution_jobs_request_id"), "execution_jobs", ["request_id"], unique=False)
    op.create_index(op.f("ix_execution_jobs_workflow_id"), "execution_jobs", ["workflow_id"], unique=False)
    op.create_index(op.f("ix_execution_jobs_organization_id"), "execution_jobs", ["organization_id"], unique=False)
    op.create_index(op.f("ix_execution_jobs_idempotency_key"), "execution_jobs", ["idempotency_key"], unique=False)

    # --- Approval Requests ---
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("action_description", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("reviewer_id", sa.String(length=64), nullable=True),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["execution_jobs.id"]),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approval_requests_id"), "approval_requests", ["id"], unique=False)
    op.create_index(op.f("ix_approval_requests_job_id"), "approval_requests", ["job_id"], unique=False)
    op.create_index(op.f("ix_approval_requests_request_id"), "approval_requests", ["request_id"], unique=False)
    op.create_index(op.f("ix_approval_requests_organization_id"), "approval_requests", ["organization_id"], unique=False)

    # --- Vector Embeddings (pgvector) ---
    # This is conditional — only runs on PostgreSQL with pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        CREATE TABLE IF NOT EXISTS vector_embeddings (
            id VARCHAR(64) PRIMARY KEY,
            organization_id VARCHAR(64) NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            embedding vector(384) NOT NULL,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_vector_embeddings_org
        ON vector_embeddings (organization_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vector_embeddings")
    op.drop_table("approval_requests")
    op.drop_table("execution_jobs")
    op.drop_table("document_chunks")
    op.drop_table("documents")
