"""refund requests (separate, audited refund workflow)

Revision ID: a9c1e3f52d84
Revises: a8c0e2f41b73
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a9c1e3f52d84"
down_revision = "a8c0e2f41b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refund_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("transaction_id", sa.String(64), nullable=False, unique=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("basis", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_adjustment_id", sa.String(64), nullable=True),
        sa.Column("provider_status", sa.String(32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refund_requests_organization_id", "refund_requests", ["organization_id"])
    op.create_index("ix_refund_requests_provider_adjustment_id", "refund_requests", ["provider_adjustment_id"])


def downgrade() -> None:
    op.drop_table("refund_requests")
