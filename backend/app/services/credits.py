"""
Credits: what a customer has been given, what they have used, what is left.

ONE source of truth, two append-only tables, no counters:
  * credit_ledger  — plan grants, top-ups, refunds / chargebacks, manual adjustments
  * usage_ledger   — one row per call (unique on the provider call id), with its duration

A balance is a pure function of those rows (`credit_summary`). Because nothing is stored as a
running total, a duplicate or late webhook can never double-charge or double-grant: grants and
top-ups are unique by idempotency key, calls are unique by call id, and a late event simply changes
the inputs the next read is computed from.

Unit: seconds in the database, minutes on screen. A call is charged for exactly the seconds it ran.

Rules (the customer-facing policy, enforced here):
  * Monthly included minutes are granted once per billing period, expire when the period ends and
    never roll over. Calls are charged to the included allowance of the period they happened in.
  * Anything beyond the allowance is charged to the purchased balance. Purchased credits never
    expire. The purchased balance may go NEGATIVE (a call cannot be refused mid-flight, and a refund
    can claw back credits already spent); the next top-up repays that first.
  * Chargeable usage = production calls + test calls made while subscribed. The one promised free
    test call ("free_test") is recorded with its real provider cost but is never charged.
  * Usage from before an organization's first grant is never charged (it pre-dates billing).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.billing import CreditLedgerEntry, Subscription, UsageLedgerEntry
from app.db.models.calls import Call, CallDirection
from app.services.billing_state import ACTIVE_STATUSES, current_subscription
from app.services.plans import PLANS

logger = logging.getLogger("atla.credits")

CHARGEABLE_KINDS = ("production", "test")
INCLUDED, PURCHASED = "included", "purchased"


@dataclass(frozen=True)
class CreditSummary:
    entitled: bool  # has a trialing/active plan right now
    period_start: datetime | None
    period_end: datetime | None
    included_granted_seconds: int
    included_used_seconds: int
    included_remaining_seconds: int
    purchased_balance_seconds: int  # may be negative
    available_seconds: int  # included remaining + purchased balance (may be negative)
    status: str  # "ok" | "low" | "empty"


@dataclass
class _PeriodGrant:
    period_start: datetime
    period_end: datetime
    seconds: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def lock_org_credits(db: Session, organization_id: uuid.UUID) -> None:
    """Serialises credit decisions for one org for the rest of the transaction (released at commit/rollback)."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"credits:{organization_id}"})


# ---------------------------------------------------------------- grants

def _insert_once(db: Session, entry: CreditLedgerEntry) -> bool:
    """Insert unless this idempotency key already exists. True if inserted."""
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
        return True
    except IntegrityError:
        return False


def ensure_plan_grant(db: Session, sub: Subscription, now: datetime | None = None) -> bool:
    """
    Grants the plan's included minutes for the subscription's CURRENT billing period, exactly once
    (idempotency key = subscription + period start). Safe to call from the webhook, the reconciler, or
    anywhere else, any number of times and in any order. Returns True only if it created the grant.
    """
    now = now or _now()
    if sub.status not in ACTIVE_STATUSES or not sub.current_period_start or not sub.current_period_end:
        return False
    if sub.current_period_end <= now:
        return False  # a stale snapshot of a period that is already over grants nothing
    plan = PLANS.get(sub.plan_id)
    if plan is None or plan.included_minutes <= 0:
        return False
    key = _grant_key(sub)
    created = _insert_once(
        db,
        CreditLedgerEntry(
            organization_id=sub.organization_id,
            kind="plan_grant",
            bucket=INCLUDED,
            seconds=plan.included_minutes * 60,
            period_start=sub.current_period_start,
            period_end=sub.current_period_end,
            reference=sub.external_subscription_id,
            idempotency_key=key,
            note=f"{plan.name} monthly included minutes",
        ),
    )
    if created:
        logger.info("Granted %s min [org=%s plan=%s period=%s]", plan.included_minutes, sub.organization_id, plan.id, sub.current_period_start)

    # Mid-period upgrade: top the period's allowance up to the current plan's, once. A downgrade (or
    # repeating an upgrade) finds nothing missing, so nothing is granted and nothing is ever clawed back.
    missing = period_grant_deficit_seconds(db, sub, now)
    topped_up = False
    if missing > 0:
        target = _period_granted_seconds(db, sub) + missing
        topped_up = _insert_once(
            db,
            CreditLedgerEntry(
                organization_id=sub.organization_id,
                kind="plan_grant",
                bucket=INCLUDED,
                seconds=missing,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                reference=sub.external_subscription_id,
                idempotency_key=f"{key}:topup-to:{target}",  # same target => same key: a replay or a race inserts nothing
                note=f"Upgrade to {plan.name}: included minutes topped up for this period",
            ),
        )
        if topped_up:
            logger.info("Upgrade top-up %s s [org=%s plan=%s period=%s]", missing, sub.organization_id, plan.id, sub.current_period_start)
    return created or topped_up


def _grant_key(sub: Subscription) -> str:
    start = sub.current_period_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    return f"grant:{sub.external_subscription_id or sub.id}:{start.isoformat()}"

def _period_granted_seconds(db: Session, sub: Subscription) -> int:
    """Included seconds already granted for this billing period."""
    return int(
        db.query(func.coalesce(func.sum(CreditLedgerEntry.seconds), 0))
        .filter(
            CreditLedgerEntry.organization_id == sub.organization_id,
            CreditLedgerEntry.kind == "plan_grant",
            CreditLedgerEntry.period_start == sub.current_period_start,
        )
        .scalar()
    )


def period_grant_deficit_seconds(db: Session, sub: Subscription, now: datetime | None = None) -> int:
    """Seconds the current plan allows this period beyond what was already granted (0 if none / not eligible)."""
    now = now or _now()
    if sub.status not in ACTIVE_STATUSES or not sub.current_period_start or not sub.current_period_end:
        return 0
    if sub.current_period_end <= now:
        return 0
    plan = PLANS.get(sub.plan_id)
    if plan is None or plan.included_minutes <= 0:
        return 0
    granted = _period_granted_seconds(db, sub)
    return max(0, plan.included_minutes * 60 - granted)


def ensure_current_grant(db: Session, organization_id: uuid.UUID) -> bool:
    sub = current_subscription(db, organization_id)
    return ensure_plan_grant(db, sub) if sub is not None else False


def add_ledger_entry(
    db: Session,
    *,
    organization_id: uuid.UUID,
    kind: str,
    seconds: int,
    idempotency_key: str,
    reference: str | None = None,
    note: str | None = None,
    bucket: str = PURCHASED,
) -> bool:
    """Top-ups, refunds, chargebacks, manual adjustments. Idempotent by key."""
    return _insert_once(
        db,
        CreditLedgerEntry(
            organization_id=organization_id,
            kind=kind,
            bucket=bucket,
            seconds=seconds,
            reference=reference,
            idempotency_key=idempotency_key,
            note=note,
        ),
    )


# ---------------------------------------------------------------- balance

def _chargeable_seconds(db: Session, organization_id: uuid.UUID, start=None, end=None) -> int:
    q = db.query(func.coalesce(func.sum(UsageLedgerEntry.billable_seconds), 0)).filter(
        UsageLedgerEntry.organization_id == organization_id, UsageLedgerEntry.kind.in_(CHARGEABLE_KINDS)
    )
    if start is not None:
        q = q.filter(UsageLedgerEntry.occurred_at >= start)
    if end is not None:
        q = q.filter(UsageLedgerEntry.occurred_at < end)
    return int(q.scalar())


def credit_summary(db: Session, organization_id: uuid.UUID, now: datetime | None = None) -> CreditSummary:
    now = now or _now()
    settings = get_settings()
    rows = (
        db.query(CreditLedgerEntry)
        .filter(CreditLedgerEntry.organization_id == organization_id, CreditLedgerEntry.kind == "plan_grant")
        .order_by(CreditLedgerEntry.period_start.asc(), CreditLedgerEntry.created_at.asc())
        .all()
    )
    # Rows sharing a period_start are ONE allowance (base grant + upgrade top-ups): merge them so the
    # period is attributed usage once, not once per row, and "the newer grant owns the overlap" below
    # only ever applies between genuinely different periods.
    merged: dict[datetime, _PeriodGrant] = {}
    for r in rows:
        if r.period_start in merged:
            merged[r.period_start].seconds += r.seconds
        else:
            merged[r.period_start] = _PeriodGrant(r.period_start, r.period_end, r.seconds)
    grants = list(merged.values())

    attributed = 0
    overage = 0
    current = None  # (grant, used, effective_end)
    for i, g in enumerate(grants):
        effective_end = g.period_end
        if i + 1 < len(grants) and grants[i + 1].period_start < effective_end:
            effective_end = grants[i + 1].period_start  # overlapping grants: the newer one owns the overlap
        used = _chargeable_seconds(db, organization_id, g.period_start, effective_end)
        attributed += used
        overage += max(0, used - g.seconds)
        if g.period_start <= now < effective_end:
            current = (g, used, effective_end)

    unattributed = 0
    if grants:
        after_first = _chargeable_seconds(db, organization_id, start=grants[0].period_start)
        unattributed = max(0, after_first - attributed)  # after billing began, outside any paid period

    purchased_rows = int(
        db.query(func.coalesce(func.sum(CreditLedgerEntry.seconds), 0))
        .filter(CreditLedgerEntry.organization_id == organization_id, CreditLedgerEntry.bucket == PURCHASED)
        .scalar()
    )
    purchased_balance = purchased_rows - overage - unattributed

    if current is not None:
        g, used, _end = current
        granted, included_used = g.seconds, min(used, g.seconds)
        remaining = max(0, g.seconds - used)
        period_start, period_end = g.period_start, g.period_end
    else:
        granted = included_used = remaining = 0
        period_start = period_end = None

    available = remaining + purchased_balance
    entitled = current_subscription(db, organization_id) is not None and _is_entitled(db, organization_id)

    if available <= 0:
        status = "empty"
    else:
        floor = max(granted * settings.LOW_CREDIT_PERCENT // 100, 10 * 60) if granted else 10 * 60
        status = "low" if available < floor else "ok"

    return CreditSummary(
        entitled=entitled,
        period_start=period_start,
        period_end=period_end,
        included_granted_seconds=granted,
        included_used_seconds=included_used,
        included_remaining_seconds=remaining,
        purchased_balance_seconds=purchased_balance,
        available_seconds=available,
        status=status,
    )


def _is_entitled(db: Session, organization_id: uuid.UUID) -> bool:
    sub = current_subscription(db, organization_id)
    return sub is not None and sub.status in ACTIVE_STATUSES


def credits_exhausted_beyond_grace(db: Session, organization_id: uuid.UUID, now: datetime | None = None) -> bool:
    """True once an entitled org has run further below zero than the configured grace allows."""
    s = credit_summary(db, organization_id, now)
    return s.entitled and s.available_seconds < -get_settings().OVERAGE_GRACE_MINUTES * 60


# ---------------------------------------------------------------- test-call admission

def open_test_reservations(db: Session, organization_id: uuid.UUID, now: datetime | None = None) -> int:
    """Test calls started but not yet finished: each may still consume up to TEST_CALL_MAX_SECONDS."""
    now = now or _now()
    window = timedelta(seconds=get_settings().TEST_CALL_MAX_SECONDS + 120)
    return (
        db.query(func.count(Call.id))
        .filter(
            Call.organization_id == organization_id,
            Call.direction == CallDirection.test,
            Call.status.in_(("registered", "ongoing")),
            Call.created_at >= now - window,
        )
        .scalar()
    )


def can_cover_paid_test(db: Session, organization_id: uuid.UUID, now: datetime | None = None) -> bool:
    """Call inside the org's credit lock. Counts calls already in flight, so two simultaneous
    requests cannot both spend the same last minutes."""
    summary = credit_summary(db, organization_id, now)
    needed = get_settings().TEST_CALL_MAX_SECONDS
    in_flight = open_test_reservations(db, organization_id, now) * needed
    return summary.available_seconds - in_flight >= needed


# ---------------------------------------------------------------- reporting

def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def daily_usage(db: Session, organization_id: uuid.UUID, days: int, tz_name: str | None, now: datetime | None = None) -> list[dict]:
    """
    One entry per calendar day in the caller's timezone, oldest first, for the last `days` days
    (today included). A day with no calls is a real zero. calls = production calls handled;
    credit_seconds = chargeable seconds (production + paid tests).
    """
    tz = resolve_timezone(tz_name)
    now = now or _now()
    today = now.astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    start_utc = datetime.combine(first, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    day = func.date(func.timezone(tz.key, UsageLedgerEntry.occurred_at)).label("day")
    rows = (
        db.query(
            day,
            func.count().filter(UsageLedgerEntry.kind == "production"),
            func.coalesce(func.sum(UsageLedgerEntry.billable_seconds).filter(UsageLedgerEntry.kind.in_(CHARGEABLE_KINDS)), 0),
        )
        .filter(UsageLedgerEntry.organization_id == organization_id, UsageLedgerEntry.occurred_at >= start_utc)
        .group_by(day)
        .all()
    )
    by_day: dict[date, tuple[int, int]] = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    out = []
    for i in range(days):
        d = first + timedelta(days=i)
        calls, secs = by_day.get(d, (0, 0))
        out.append({"date": d.isoformat(), "calls": calls, "credit_seconds": secs})
    return out
