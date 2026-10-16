import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class Subscription(Base, UUIDPKMixin, TimestampMixin):
    """
    Deliberately provider-agnostic: `billing_provider` + `external_subscription_id`
    instead of Stripe-specific columns, so swapping billing providers later
    doesn't require a schema change. See app/providers/billing (Phase 7).
    """
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index(
            "uq_subscriptions_external",
            "billing_provider",
            "external_subscription_id",
            unique=True,
            postgresql_where="external_subscription_id IS NOT NULL",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    billing_provider: Mapped[str] = mapped_column(String(32), default="paddle", nullable=False)
    external_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="trialing", nullable=False)
    current_period_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `occurred_at` of the newest provider event applied. Older events are
    # ignored (webhooks are at-least-once and may arrive out of order).
    last_event_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the customer cancelled and service continues until this date.
    cancel_effective_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When `status` last CHANGED (provider time) — drives grace-period policy.
    status_changed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageLedgerEntry(Base, UUIDPKMixin, TimestampMixin):
    """
    One row per finished call: the auditable record of what was used and what
    the provider says it cost us. Unique on retell_call_id, so a duplicate
    webhook can never double-count. Customer usage and our margin are both
    SUMs over this table — there is no separate counter that can drift.
    """

    __tablename__ = "usage_ledger"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    retell_call_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # "production" (always chargeable) | "test" (a test call made while subscribed: chargeable) |
    # "free_test" (the one promised free call: NOT charged to the customer, cost still recorded)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    billable_seconds: Mapped[int] = mapped_column(Integer, nullable=False)  # duration rounded to the nearest second
    # Retell's call_cost.combined_cost, stored as received. UNIT: CENTS (Retell's API reference:
    # "Combined cost of all individual costs in cents"; matches the real 44 s test call = 12.13).
    # INTERNAL ONLY — never sent to customers.
    provider_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    provider_cost_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditLedgerEntry(Base, UUIDPKMixin, TimestampMixin):
    """
    Append-only record of every credit that is GRANTED, BOUGHT or ADJUSTED. Call usage is not
    copied here: it already lives, once per call, in `usage_ledger`. A customer's balance is a pure
    function of these two tables (services/credits.py), so there is no counter that can drift.

    seconds is signed. bucket "included" = monthly plan allowance (expires with its billing
    period, never rolls over); "purchased" = top-ups (never expire; can go negative when a refund
    claws back credits that were already used, which then blocks new calls until repaid).
    idempotency_key is unique, so a retried or duplicated event can never grant twice.
    """

    __tablename__ = "credit_ledger"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # plan_grant | topup | refund | chargeback | adjustment
    bucket: Mapped[str] = mapped_column(String(16), nullable=False)  # included | purchased
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)  # subscription / transaction / adjustment id
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TopupPurchase(Base, UUIDPKMixin, TimestampMixin):
    """
    One customer-chosen top-up. Created BEFORE the Paddle transaction so an interrupted request
    leaves a record the reconciler can settle. Credits are granted only when Paddle reports the
    transaction completed (never from a browser redirect).
    """

    __tablename__ = "topup_purchases"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)  # USD cents charged (before tax)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    credit_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    cents_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)  # the rate that priced it, kept for audit
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    # created -> completed (credits granted) | failed (checkout expired/canceled/payment failed, no credits)
    # completed -> partially_refunded | refunded | chargeback
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clawed_back_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FreeTestAllowance(Base, UUIDPKMixin, TimestampMixin):
    """
    The promised free test call(s). A row IS the consumption: uniqueness on (organization, slot) and
    on (user, slot) makes the allowance atomic under concurrent requests and impossible to reset by
    refreshing, re-saving the agent, creating another receptionist or signing in again.
    """

    __tablename__ = "free_test_allowances"
    __table_args__ = (
        UniqueConstraint("organization_id", "slot", name="uq_free_test_org_slot"),
        UniqueConstraint("user_id", "slot", name="uq_free_test_user_slot"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    retell_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RefundRequest(Base, UUIDPKMixin, TimestampMixin):
    """
    A refund is its own, explicitly authorized action (services/refunds.py) - never a side effect of cancelling.
    One row per Paddle transaction (unique), written BEFORE the provider call, so a repeat can never refund twice.
    status: requested | submitted | approved | rejected | failed | unconfirmed. Only Paddle's adjustment status moves it to
    approved / rejected; "submitted" means Paddle accepted the request and it awaits approval.
    """

    __tablename__ = "refund_requests"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    basis: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="requested", nullable=False)
    provider_adjustment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
