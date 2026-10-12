"""The credit ledger: grants, usage attribution, balances, reporting and what happens at zero."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry, Subscription
from app.db.models.telephony import PhoneNumberStatus
from app.db.models.voice_agent import AgentStatus
from app.services import credits
from app.services.plans import PLANS, Plan
from app.services.subscription_sync import sync_subscription
from tests.billing_helpers import add_usage, new_org, now, subscribe, summary

MIN = 60


@pytest.fixture(autouse=True)
def _paddle_prices(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


def _grants(org_id):
    db = SessionLocal()
    try:
        return db.query(CreditLedgerEntry).filter_by(organization_id=uuid.UUID(org_id), kind="plan_grant").all()
    finally:
        db.close()


def _snapshot(sub_id, org_id, start, end, status="active", price="pri_starter", updated=None):
    return {
        "id": sub_id, "status": status, "custom_data": {"organization_id": org_id},
        "items": [{"status": "active", "quantity": 1, "price": {"id": price}}],
        "current_billing_period": {"starts_at": start.isoformat(), "ends_at": end.isoformat()},
        "updated_at": (updated or start).isoformat(),
    }


# ---------------- grants ----------------

def test_monthly_grant_is_created_exactly_once_for_the_period_however_often_it_is_applied():
    _c, org = new_org()
    start = now() - timedelta(days=1)
    snap = _snapshot(f"sub_{uuid.uuid4().hex[:8]}", org, start, start + timedelta(days=30))
    for i in range(3):  # webhook, redelivered webhook, reconciler snapshot
        db = SessionLocal()
        sync_subscription(db, snap, start + timedelta(minutes=i))
        db.commit()
        db.close()
    grants = _grants(org)
    assert len(grants) == 1 and grants[0].seconds == PLANS["starter"].included_minutes * MIN


def test_concurrent_application_of_one_period_still_grants_once():
    _c, org = new_org()
    sub_id = subscribe(org, grant=False)
    errors = []

    def apply():
        db = SessionLocal()
        try:
            sub = db.query(Subscription).filter_by(external_subscription_id=sub_id).one()
            credits.ensure_plan_grant(db, sub)
            db.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=apply) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(_grants(org)) == 1


def test_growth_grant_is_larger_than_starter_and_matches_the_catalog():
    _c1, a = new_org()
    _c2, b = new_org()
    subscribe(a, "starter")
    subscribe(b, "growth")
    assert summary(b).included_granted_seconds > summary(a).included_granted_seconds
    assert summary(a).included_granted_seconds == PLANS["starter"].included_minutes * MIN
    assert summary(b).included_granted_seconds == PLANS["growth"].included_minutes * MIN


def test_no_grant_for_inactive_subscriptions_or_a_period_that_already_ended():
    _c, org = new_org()
    subscribe(org, status="past_due")
    assert _grants(org) == []
    _c, org2 = new_org()
    start = now() - timedelta(days=40)
    sub_id = subscribe(org2, start=start, end=start + timedelta(days=30), grant=False)
    db = SessionLocal()
    sub = db.query(Subscription).filter_by(external_subscription_id=sub_id).one()
    assert credits.ensure_plan_grant(db, sub) is False  # a stale snapshot of a finished period grants nothing
    db.commit()
    db.close()
    assert _grants(org2) == []


def test_each_new_billing_period_gets_its_own_grant_and_unused_minutes_do_not_roll_over():
    _c, org = new_org()
    p1 = now() - timedelta(days=45)
    sub_id = subscribe(org, start=p1, end=p1 + timedelta(days=30), grant=False)
    db = SessionLocal()
    sub = db.query(Subscription).filter_by(external_subscription_id=sub_id).one()
    credits.ensure_plan_grant(db, sub, now=p1 + timedelta(days=1))  # the old period's grant, made on time
    add_usage(org, 100 * MIN, at=p1 + timedelta(days=5))
    sub.current_period_start, sub.current_period_end = now() - timedelta(days=1), now() + timedelta(days=29)
    db.flush()
    credits.ensure_plan_grant(db, sub)
    db.commit()
    db.close()
    s = summary(org)
    full = PLANS["starter"].included_minutes * MIN
    assert len(_grants(org)) == 2
    assert s.included_granted_seconds == full and s.included_remaining_seconds == full  # no carry-over, old use not charged here
    assert s.purchased_balance_seconds == 0


# ---------------- usage and balance ----------------

def test_a_call_consumes_credits_exactly_once_and_remaining_is_grant_minus_use():
    _c, org = new_org()
    subscribe(org)
    full = PLANS["starter"].included_minutes * MIN
    add_usage(org, 61)
    s = summary(org)
    assert s.included_used_seconds == 61 and s.included_remaining_seconds == full - 61 and s.available_seconds == full - 61


def test_the_free_test_call_is_never_charged_but_a_paid_test_is():
    _c, org = new_org()
    subscribe(org)
    full = PLANS["starter"].included_minutes * MIN
    add_usage(org, 120, kind="free_test", cost=40)
    assert summary(org).available_seconds == full
    add_usage(org, 90, kind="test")
    assert summary(org).available_seconds == full - 90


def test_usage_from_before_the_first_grant_is_never_charged():
    _c, org = new_org()
    add_usage(org, 500, at=now() - timedelta(days=60))
    subscribe(org)
    full = PLANS["starter"].included_minutes * MIN
    assert summary(org).available_seconds == full


def test_late_usage_is_attributed_to_the_period_the_call_happened_in():
    """Out-of-order delivery: a call from the previous period arrives after the new period's grant exists."""
    _c, org = new_org()
    p1 = now() - timedelta(days=45)
    sub_id = subscribe(org, start=p1, end=p1 + timedelta(days=30), grant=False)
    db = SessionLocal()
    sub = db.query(Subscription).filter_by(external_subscription_id=sub_id).one()
    credits.ensure_plan_grant(db, sub, now=p1 + timedelta(days=1))
    sub.current_period_start, sub.current_period_end = now() - timedelta(days=1), now() + timedelta(days=29)
    db.flush()
    credits.ensure_plan_grant(db, sub)
    db.commit()
    db.close()
    before = summary(org)
    add_usage(org, 50 * MIN, at=p1 + timedelta(days=10))  # arrives late
    assert summary(org).available_seconds == before.available_seconds  # charged to the OLD period, not this one


def test_use_beyond_the_allowance_spends_the_purchased_balance_then_goes_negative(monkeypatch):
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 1))
    _c, org = new_org()
    subscribe(org)
    db = SessionLocal()
    credits.add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=5 * MIN, idempotency_key=f"t:{org}")
    db.commit()
    db.close()
    add_usage(org, 3 * MIN)  # 1 min included + 2 min purchased
    s = summary(org)
    assert s.included_remaining_seconds == 0 and s.purchased_balance_seconds == 3 * MIN and s.available_seconds == 3 * MIN
    add_usage(org, 6 * MIN)
    assert summary(org).purchased_balance_seconds == -3 * MIN  # a call cannot be refused mid-flight: it goes negative
    assert summary(org).status == "empty"


def test_status_thresholds_ok_low_empty(monkeypatch):
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 100))
    _c, org = new_org()
    subscribe(org)
    assert summary(org).status == "ok"
    add_usage(org, 85 * MIN)  # 15 of 100 minutes left, under the 20% line
    assert summary(org).status == "low"
    add_usage(org, 15 * MIN)
    assert summary(org).status == "empty"


def test_ledger_entries_are_idempotent_by_key():
    _c, org = new_org()
    db = SessionLocal()
    first = credits.add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=600, idempotency_key=f"dup:{org}")
    second = credits.add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=600, idempotency_key=f"dup:{org}")
    db.commit()
    assert first and not second
    assert db.query(CreditLedgerEntry).filter_by(organization_id=uuid.UUID(org)).count() == 1
    db.close()


# ---------------- reporting ----------------

def test_daily_usage_has_one_real_row_per_day_in_the_viewers_timezone():
    _c, org = new_org()
    subscribe(org)
    # 23:30 UTC on the 10th is already the 11th in India (UTC+5:30)
    add_usage(org, 120, at=datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc))
    db = SessionLocal()
    try:
        ref = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        ist = credits.daily_usage(db, uuid.UUID(org), 3, "Asia/Kolkata", now=ref)
        utc = credits.daily_usage(db, uuid.UUID(org), 3, "UTC", now=ref)
    finally:
        db.close()
    assert [d["date"] for d in ist] == ["2026-10-09", "2026-10-10", "2026-10-11"]
    assert {d["date"]: d["calls"] for d in ist}["2026-10-11"] == 1
    assert {d["date"]: d["calls"] for d in utc}["2026-10-10"] == 1
    assert ist[0]["calls"] == 0 and ist[0]["credit_seconds"] == 0  # empty days are real zeros


def test_unknown_timezone_falls_back_to_utc_instead_of_failing():
    assert credits.resolve_timezone("Not/AZone").key == "UTC"
    assert credits.resolve_timezone(None).key == "UTC"


# ---------------- what happens at zero ----------------

def test_credits_exhausted_beyond_the_grace_pauses_the_receptionist_but_keeps_the_number(monkeypatch):
    from app.services.entitlement import enforce_entitlements
    from tests.fakes import FakePhone
    from tests.test_call_pipeline import _seed

    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 1))
    monkeypatch.setattr(get_settings(), "OVERAGE_GRACE_MINUTES", 30)
    _c, org = new_org()
    _agent, number = _seed(org)
    subscribe(org)
    provider = FakePhone()
    provider.numbers[number] = {"nickname": "n", "agent": "agent_x"}
    db = SessionLocal()
    try:
        add_usage(org, 20 * MIN)  # 19 min over: inside the 30-minute grace -> still live
        enforce_entitlements(db, provider, organization_ids={uuid.UUID(org)})
        assert db.query(__import__("app.db.models.voice_agent", fromlist=["Agent"]).Agent).filter_by(organization_id=uuid.UUID(org)).one().status == AgentStatus.active
        add_usage(org, 20 * MIN)  # now 39 min over
        result = enforce_entitlements(db, provider, organization_ids={uuid.UUID(org)})
        db.expire_all()
        from app.db.models.telephony import PhoneNumber
        from app.db.models.voice_agent import Agent

        assert db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one().status == AgentStatus.paused
        phone = db.query(PhoneNumber).filter_by(organization_id=uuid.UUID(org)).one()
        assert phone.agent_id is None and phone.status == PhoneNumberStatus.active  # paused, NOT released
        assert provider.unassigns and result["released"] == 0
    finally:
        db.close()
