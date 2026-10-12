"""credit ledger, top-up purchases, free test allowances

Revision ID: a8c0e2f41b73
Revises: a7c9e1f53b68

Backfill (the only history that can be reconstructed reliably):
  * every existing usage_ledger row of kind "test" becomes "free_test". Before this revision no test call
    was ever charged to a customer, so none of that history may suddenly count against a balance.
  * production usage needs no backfill: balances are computed from usage_ledger by call time, so calls
    already recorded are attributed to the right billing period as soon as that period's grant exists.
    Run `python -m app.workers.reconcile_billing --apply` after deploying to create the grants for
    subscriptions that are already active.
Not recoverable: what each past test call cost the CUSTOMER (nothing was charged), and any usage that
happened before an organization's first subscription (never charged, so deliberately not debited).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a8c0e2f41b73"
down_revision = "a7c9e1f53b68"
branch_labels = None
depends_on = None


def _uuid():
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "credit_ledger",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("organization_id", _uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("bucket", sa.String(16), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reference", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credit_ledger_organization_id", "credit_ledger", ["organization_id"])

    op.create_table(
        "topup_purchases",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("organization_id", _uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", _uuid(), nullable=True),
        sa.Column("provider_transaction_id", sa.String(255), nullable=True, unique=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("credit_seconds", sa.Integer(), nullable=False),
        sa.Column("cents_per_minute", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_minor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clawed_back_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topup_purchases_organization_id", "topup_purchases", ["organization_id"])

    op.create_table(
        "free_test_allowances",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("organization_id", _uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("retell_call_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "slot", name="uq_free_test_org_slot"),
        sa.UniqueConstraint("user_id", "slot", name="uq_free_test_user_slot"),
    )
    op.create_index("ix_free_test_allowances_organization_id", "free_test_allowances", ["organization_id"])

    op.execute("UPDATE usage_ledger SET kind = 'free_test' WHERE kind = 'test'")
    # Orgs that already spent test calls must not get a second free one: one allowance row per org
    # that has any test usage (attributed to the org's owner when one can be found).
    op.execute(
        """
        INSERT INTO free_test_allowances (id, organization_id, user_id, slot, retell_call_id, created_at, updated_at)
        SELECT gen_random_uuid(), o.organization_id, o.user_id, 0, NULL, now(), now()
        FROM (
            SELECT DISTINCT ON (u.organization_id) u.organization_id, m.user_id
            FROM usage_ledger u
            JOIN organization_members m ON m.organization_id = u.organization_id
            WHERE u.kind = 'free_test'
            ORDER BY u.organization_id, m.created_at ASC
        ) o
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("UPDATE usage_ledger SET kind = 'test' WHERE kind = 'free_test'")
    op.drop_table("free_test_allowances")
    op.drop_table("topup_purchases")
    op.drop_table("credit_ledger")
