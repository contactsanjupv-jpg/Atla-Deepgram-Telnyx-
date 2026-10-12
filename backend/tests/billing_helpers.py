"""Shared fixtures for the credit / top-up / free-test / transcript tests. Real Postgres, real HTTP layer."""
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models.billing import Subscription, UsageLedgerEntry
from app.db.models.calls import Call, CallDirection
from app.services.credits import ensure_plan_grant
from tests.test_tenant_isolation import _signup


def new_org(prefix="cr"):
    tag = uuid.uuid4().hex[:10]
    return _signup(f"{prefix}-{tag}@credits-test.com", f"Credits Org {tag}")


def now():
    return datetime.now(timezone.utc)


def subscribe(org_id, plan="starter", start=None, end=None, status="active", grant=True, sub_id=None):
    """A paid subscription row (as the Paddle webhook would have left it), with its included-credit grant."""
    start = start or now() - timedelta(days=1)
    end = end or start + timedelta(days=30)
    db = SessionLocal()
    try:
        sub = Subscription(
            organization_id=uuid.UUID(org_id), plan_id=plan, status=status, billing_provider="paddle",
            external_subscription_id=sub_id or f"sub_{uuid.uuid4().hex[:10]}",
            current_period_start=start, current_period_end=end, last_event_at=start, status_changed_at=start,
        )
        db.add(sub)
        db.flush()
        if grant:
            ensure_plan_grant(db, sub, now=start + timedelta(seconds=1))
        db.commit()
        return sub.external_subscription_id
    finally:
        db.close()


def add_usage(org_id, seconds, kind="production", at=None, cost=None, direction=None):
    """One finished call and its usage_ledger row (what call ingestion writes)."""
    at = at or now()
    cid = f"call_{uuid.uuid4().hex[:12]}"
    db = SessionLocal()
    try:
        call = Call(
            organization_id=uuid.UUID(org_id), retell_call_id=cid,
            direction=direction or (CallDirection.test if kind in ("test", "free_test") else CallDirection.inbound),
            status="ended", started_at=at - timedelta(seconds=seconds), ended_at=at, duration_seconds=seconds,
        )
        db.add(call)
        db.flush()
        db.add(
            UsageLedgerEntry(
                organization_id=uuid.UUID(org_id), call_id=call.id, retell_call_id=cid, kind=kind,
                duration_ms=seconds * 1000, billable_seconds=seconds, provider_cost=cost, occurred_at=at,
            )
        )
        db.commit()
        return cid
    finally:
        db.close()


def summary(org_id):
    from app.services.credits import credit_summary

    db = SessionLocal()
    try:
        return credit_summary(db, uuid.UUID(org_id))
    finally:
        db.close()
