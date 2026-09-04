"""phase 4.2 hardening — workflow checkpoint persistence

Revision ID: 20260904_phase42
Revises: 20260903_phase3
Create Date: 2026-09-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260904_phase42"
down_revision = "20260903_phase3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflow_checkpoints_id"), "workflow_checkpoints", ["id"], unique=False)
    op.create_index(op.f("ix_workflow_checkpoints_workflow_id"), "workflow_checkpoints", ["workflow_id"], unique=False)
    op.create_index(op.f("ix_workflow_checkpoints_request_id"), "workflow_checkpoints", ["request_id"], unique=False)
    op.create_index(op.f("ix_workflow_checkpoints_organization_id"), "workflow_checkpoints", ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_table("workflow_checkpoints")