"""
Customer-facing credits and usage. Calls and minutes only: provider cost, margin and the cost basis
never appear here. Every route is scoped to the caller's organization (current_membership).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import current_membership
from app.config import get_settings
from app.db.base import get_db
from app.db.models.billing import CreditLedgerEntry, TopupPurchase
from app.db.models.tenancy import OrganizationMember
from app.services import free_test
from app.services.billing_state import current_subscription
from app.services.credits import credit_summary, daily_usage, ensure_current_grant
from app.services.plans import PLANS, when_credits_run_out
from app.services.usage import usage_summary

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/usage", tags=["usage"])


class UsageOut(BaseModel):
    period_start: datetime | None
    period_end: datetime | None
    calls_count: int
    billable_seconds: int


@router.get("", response_model=UsageOut)
def get_usage(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    """Only what the customer should see: calls and seconds. No provider cost."""
    return usage_summary(db, membership.organization_id)


class CreditsOut(BaseModel):
    entitled: bool
    plan_name: str | None
    period_start: datetime | None
    period_end: datetime | None
    included_granted_seconds: int
    included_used_seconds: int
    included_remaining_seconds: int
    purchased_balance_seconds: int  # negative = owed after a refund; shown as zero with an explanation
    available_seconds: int
    status: str  # "ok" | "low" | "empty"
    calls_handled: int  # production calls in the current period
    topup_min_minor: int
    topup_max_minor: int
    topup_cents_per_minute: int
    free_test_calls_remaining: int
    when_credits_run_out: str


@router.get("/credits", response_model=CreditsOut)
def get_credits(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    org_id = membership.organization_id
    if ensure_current_grant(db, org_id):  # idempotent self-heal for a subscription that pre-dates credits
        db.commit()
    s = credit_summary(db, org_id)
    sub = current_subscription(db, org_id)
    plan = PLANS.get(sub.plan_id) if sub is not None else None
    cfg = get_settings()
    return CreditsOut(
        entitled=s.entitled,
        plan_name=plan.name if plan else None,
        period_start=s.period_start,
        period_end=s.period_end,
        included_granted_seconds=s.included_granted_seconds,
        included_used_seconds=s.included_used_seconds,
        included_remaining_seconds=s.included_remaining_seconds,
        purchased_balance_seconds=s.purchased_balance_seconds,
        available_seconds=s.available_seconds,
        status=s.status,
        calls_handled=usage_summary(db, org_id)["calls_count"],
        topup_min_minor=cfg.TOPUP_MIN_CENTS,
        topup_max_minor=cfg.TOPUP_MAX_CENTS,
        topup_cents_per_minute=cfg.TOPUP_CENTS_PER_MINUTE,
        free_test_calls_remaining=free_test.remaining(db, org_id, membership.user_id),
        when_credits_run_out=when_credits_run_out(),
    )


class DailyPoint(BaseModel):
    date: str  # YYYY-MM-DD in the requested timezone
    calls: int
    credit_seconds: int


@router.get("/daily", response_model=list[DailyPoint])
def get_daily(
    days: int = Query(default=7, ge=1, le=90),
    tz: str | None = Query(default=None, max_length=64),  # IANA name from the browser; unknown -> UTC
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """One real row per calendar day (a day with no calls is a true zero). days=1 is today."""
    return daily_usage(db, membership.organization_id, days, tz)


class CreditEventOut(BaseModel):
    id: str
    kind: str  # plan_grant | topup | refund | chargeback | adjustment
    seconds: int
    note: str | None
    occurred_at: datetime


@router.get("/events", response_model=list[CreditEventOut])
def get_credit_events(
    limit: int = Query(default=25, ge=1, le=100),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """Grants, top-ups, refunds and adjustments, newest first (the credit side of usage history)."""
    rows = (
        db.query(CreditLedgerEntry)
        .filter(CreditLedgerEntry.organization_id == membership.organization_id)
        .order_by(CreditLedgerEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    return [CreditEventOut(id=str(r.id), kind=r.kind, seconds=r.seconds, note=r.note, occurred_at=r.created_at) for r in rows]
