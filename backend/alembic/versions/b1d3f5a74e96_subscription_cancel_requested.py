"""subscription: durable 'cancel requested, awaiting confirmation' marker

Revision ID: b1d3f5a74e96
Revises: a9c1e3f52d84
"""
import sqlalchemy as sa
from alembic import op

revision = "b1d3f5a74e96"
down_revision = "a9c1e3f52d84"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "cancel_requested_at")
