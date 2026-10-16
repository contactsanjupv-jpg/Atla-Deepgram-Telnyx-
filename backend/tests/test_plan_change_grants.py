"""
Mid-period plan changes and the monthly included allowance.

Policy (Fix 3):
  * Upgrade inside a billing period: the period's included allowance is topped up to the new plan's
    (Starter 180 -> Growth 480 = +300 min, once). Minutes already used stay used.
  * Downgrade inside a period: no claw-back; the allowance stays until renewal.
  * A genuine new period grants the new plan's allowance; nothing rolls over.
  * Replays, reconciliation, concurrency and upgrade/downgrade cycles never grant a top-up twice.
  * Purchased credits are a separate bucket and are never written by a plan change.
"""
import threading
import uuid
from datetime import timedelta

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry, Subscription
from app.services import credits
from app.services.billing_reconcile import ReconcileReport, _ensure_grants
from app.services.credits import credit_summary
from app.services.plans import PLANS
from app.services.subscription_sync import sync_subscription
from tests.billing_helpers import add_usage, new_org, now

MIN = 60


@pytest.fixture(autouse=True)
def _paddle_prices(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


STARTER = PLANS["starter"].included_minutes * MIN
GROWTH = PLANS["growth"].included_minutes * MIN


class Period:
    """One org + one Paddle subscription whose snapshots we apply, like the webhook / reconciler do."""

    def __init__(self, start=None):
        _c, self.org = new_org()
        self.sub_id = f"sub_{uuid.uuid4().hex[:10]}"
        self.start = start or now() - timedelta(days=1)
        self.end = self.start + timedelta(days=30)
        self.tick = 0

    def apply(self, plan, start=None, end=None):
        """Apply a snapshot for `plan` (the same period unless start/end are given)."""
        if start is not None:
            self.start, self.end = start, end or start + timedelta(days=30)
        self.tick += 1
        price = {"starter": "pri_starter", "growth": "pri_growth"}[plan]
        snap = {
            "id": self.sub_id, "status": "active", "custom_data": {"organization_id": self.org},
            "items": [{"status": "active", "quantity": 1, "price": {"id": price}}],
            "current_billing_period": {"starts_at": self.start.isoformat(), "ends_at": self.end.isoformat()},
        }
        db = SessionLocal()
        try:
            applied = sync_subscription(db, snap, self.start + timedelta(minutes=self.tick))
            db.commit()
            return applied
        finally:
            db.close()

    def rows(self):
        db = SessionLocal()
        try:
            return db.query(CreditLedgerEntry).filter_by(organization_id=uuid.UUID(self.org), kind="plan_grant").all()
        finally:
            db.close()

    def summary(self, at=None):
        db = SessionLocal()
        try:
            return credit_summary(db, uuid.UUID(self.org), at)
        finally:
            db.close()


def _topup(org, minutes, key):
    db = SessionLocal()
    try:
        credits.add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=minutes * MIN, idempotency_key=key)
        db.commit()
    finally:
        db.close()


# ---------------- upgrade ----------------

def test_upgrade_in_the_same_period_tops_the_allowance_up_to_the_new_plan():
    p = Period()
    p.apply("starter")
    assert p.summary().included_remaining_seconds == STARTER
    p.apply("growth")
    s = p.summary()
    assert s.included_granted_seconds == GROWTH
    assert s.included_remaining_seconds == GROWTH and s.available_seconds == GROWTH
    assert sorted(r.seconds for r in p.rows()) == sorted([STARTER, GROWTH - STARTER])
    assert s.period_start == p.start and s.period_end == p.end  # the period itself did not move


def test_upgrade_keeps_minutes_already_used_as_used():
    p = Period()
    p.apply("starter")
    add_usage(p.org, 100 * MIN)
    assert p.summary().included_remaining_seconds == STARTER - 100 * MIN
    p.apply("growth")
    s = p.summary()
    assert s.included_used_seconds == 100 * MIN  # counted once, not once per grant row
    assert s.included_remaining_seconds == GROWTH - 100 * MIN
    assert s.available_seconds == GROWTH - 100 * MIN


def test_a_customer_whose_first_snapshot_is_already_growth_gets_exactly_growth():
    p = Period()
    p.apply("growth")
    assert [r.seconds for r in p.rows()] == [GROWTH]
    assert p.summary().included_remaining_seconds == GROWTH


# ---------------- idempotency ----------------

def test_repeated_webhooks_and_reconciliation_never_duplicate_the_topup():
    p = Period()
    p.apply("starter")
    p.apply("growth")
    before = sorted(r.idempotency_key for r in p.rows())
    for _ in range(3):
        p.apply("growth")  # redelivered webhook / reconciler snapshot
    db = SessionLocal()
    try:
        report = ReconcileReport()
        _ensure_grants(db, report, apply=True, organization_ids={uuid.UUID(p.org)})
        assert p.sub_id not in report.grants_created and p.sub_id not in report.missing_grants
    finally:
        db.close()
    assert sorted(r.idempotency_key for r in p.rows()) == before
    assert len(before) == 2
    assert p.summary().included_remaining_seconds == GROWTH


def test_upgrade_downgrade_cycles_grant_the_topup_once():
    p = Period()
    for plan in ("starter", "growth", "starter", "growth", "starter", "growth"):
        p.apply(plan)
    assert len(p.rows()) == 2
    assert sum(r.seconds for r in p.rows()) == GROWTH
    assert p.summary().included_remaining_seconds == GROWTH


def test_concurrent_upgrade_snapshots_grant_one_topup():
    p = Period()
    p.apply("starter")
    errors = []

    def run():
        db = SessionLocal()
        try:
            sub = db.query(Subscription).filter_by(external_subscription_id=p.sub_id).one()
            sub.plan_id = "growth"
            db.flush()
            credits.ensure_plan_grant(db, sub)
            db.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(p.rows()) == 2 and sum(r.seconds for r in p.rows()) == GROWTH


def test_reconciler_repairs_a_missed_upgrade_once():
    p = Period()
    p.apply("starter")
    db = SessionLocal()  # the plan changed but the grant never happened (lost webhook path)
    try:
        db.query(Subscription).filter_by(external_subscription_id=p.sub_id).one().plan_id = "growth"
        db.commit()
        dry = ReconcileReport()
        _ensure_grants(db, dry, apply=False, organization_ids={uuid.UUID(p.org)})
        assert p.sub_id in dry.missing_grants
        assert len(p.rows()) == 1  # report-only wrote nothing
        applied = ReconcileReport()
        _ensure_grants(db, applied, apply=True, organization_ids={uuid.UUID(p.org)})
        assert p.sub_id in applied.grants_created
        again = ReconcileReport()
        _ensure_grants(db, again, apply=True, organization_ids={uuid.UUID(p.org)})
        assert p.sub_id not in again.grants_created and p.sub_id not in again.missing_grants
    finally:
        db.close()
    assert len(p.rows()) == 2 and p.summary().included_remaining_seconds == GROWTH


# ---------------- downgrade ----------------

def test_downgrade_in_the_same_period_keeps_the_existing_allowance():
    p = Period()
    p.apply("growth")
    add_usage(p.org, 100 * MIN)
    p.apply("starter")
    s = p.summary()
    assert len(p.rows()) == 1  # nothing granted, nothing clawed back
    assert s.included_granted_seconds == GROWTH
    assert s.included_remaining_seconds == GROWTH - 100 * MIN


def test_downgrade_after_an_upgrade_keeps_the_topped_up_allowance():
    p = Period()
    p.apply("starter")
    p.apply("growth")
    p.apply("starter")
    assert p.summary().included_remaining_seconds == GROWTH
    assert len(p.rows()) == 2


# ---------------- new period, expiry ----------------

def test_a_genuine_new_period_grants_the_new_plans_allowance_without_rollover():
    p = Period(start=now() - timedelta(days=45))
    p.apply("growth")  # old period, granted on time? (applied now: stale, so grant is skipped)
    p.apply("starter", start=now() - timedelta(days=1))  # renewal on the lower plan
    s = p.summary()
    assert s.included_granted_seconds == STARTER
    assert s.included_remaining_seconds == STARTER  # 480 min Growth leftovers did not roll over
    assert s.period_start == p.start


def test_new_period_after_a_mid_period_upgrade_does_not_carry_the_topup_or_old_usage():
    p = Period()
    p.apply("starter")
    add_usage(p.org, 60 * MIN)
    p.apply("growth")
    old_start = p.start
    p.apply("growth", start=old_start + timedelta(days=30), end=old_start + timedelta(days=60))
    # advance the clock past the old period: the new period is current
    s = p.summary(at=old_start + timedelta(days=31))
    assert s.included_granted_seconds == GROWTH and s.included_remaining_seconds == GROWTH
    assert s.period_start == old_start + timedelta(days=30)


def test_the_topup_expires_with_its_period():
    p = Period()
    p.apply("starter")
    p.apply("growth")
    s = p.summary(at=p.end + timedelta(hours=1))
    assert s.included_granted_seconds == 0 and s.included_remaining_seconds == 0
    assert all(r.period_end == p.end for r in p.rows())


def test_a_stale_snapshot_of_a_finished_period_grants_no_topup():
    p = Period(start=now() - timedelta(days=45))
    db = SessionLocal()
    try:
        _c = Subscription(
            organization_id=uuid.UUID(p.org), plan_id="growth", status="active", billing_provider="paddle",
            external_subscription_id=p.sub_id, current_period_start=p.start, current_period_end=p.end,
            last_event_at=p.start, status_changed_at=p.start,
        )
        db.add(_c)
        db.flush()
        assert credits.ensure_plan_grant(db, _c) is False
        db.commit()
    finally:
        db.close()
    assert p.rows() == []


# ---------------- purchased credits ----------------

def test_purchased_credits_are_untouched_by_plan_changes():
    p = Period()
    p.apply("starter")
    _topup(p.org, 50, f"t:{p.org}")
    add_usage(p.org, 100 * MIN)  # inside the included allowance: purchased is not spent
    before = p.summary()
    assert before.purchased_balance_seconds == 50 * MIN
    p.apply("growth")
    p.apply("starter")
    p.apply("growth")
    after = p.summary()
    assert after.purchased_balance_seconds == 50 * MIN
    assert after.included_remaining_seconds == GROWTH - 100 * MIN
    assert after.available_seconds == GROWTH - 100 * MIN + 50 * MIN
    db = SessionLocal()
    try:
        purchased = db.query(CreditLedgerEntry).filter_by(organization_id=uuid.UUID(p.org), bucket="purchased").all()
    finally:
        db.close()
    assert [r.seconds for r in purchased] == [50 * MIN]  # a plan change never writes to the purchased bucket


def test_usage_that_overflowed_into_purchased_is_absorbed_by_the_larger_allowance():
    """Pinned behaviour: balances are derived. Minutes used beyond Starter's 180 were charged to the
    purchased balance; once the period's allowance is 480 they fall inside it, so purchased reads full again.
    No purchased ledger row changes."""
    p = Period()
    p.apply("starter")
    _topup(p.org, 50, f"t:{p.org}")
    add_usage(p.org, 200 * MIN)  # 180 included + 20 purchased
    mid = p.summary()
    assert mid.included_remaining_seconds == 0 and mid.purchased_balance_seconds == 30 * MIN
    p.apply("growth")
    s = p.summary()
    assert s.included_remaining_seconds == GROWTH - 200 * MIN
    assert s.purchased_balance_seconds == 50 * MIN


# ---------------- timezone independence ----------------

def test_grant_idempotency_does_not_depend_on_the_database_session_timezone():
    """Postgres hands timestamptz back in the session timezone (+05:30 here, +00:00 in a webhook payload).
    The same period must still be one period: no second base grant, no phantom top-up."""
    from sqlalchemy import text

    p = Period()
    p.apply("growth")
    assert [r.seconds for r in p.rows()] == [GROWTH]
    db = SessionLocal()
    try:
        db.execute(text("SET TIME ZONE 'Asia/Kolkata'"))
        sub = db.query(Subscription).filter_by(external_subscription_id=p.sub_id).one()
        assert sub.current_period_start.utcoffset() == timedelta(hours=5, minutes=30)  # really re-read in IST
        assert credits.ensure_plan_grant(db, sub) is False
        report = ReconcileReport()
        _ensure_grants(db, report, apply=True, organization_ids={uuid.UUID(p.org)})
        assert report.grants_created == [] and report.missing_grants == []
        db.commit()
    finally:
        db.close()
    assert [r.seconds for r in p.rows()] == [GROWTH]
    assert p.summary().included_remaining_seconds == GROWTH
