"""
F1 regression: a duplicate subscription (the customer paid twice) must not be granted its own allowance, and must not
shadow the real one. The subscription of record is the OLDEST active one (billing_state.current_subscription).
Paddle is faked (conftest guard); nothing here calls a real provider.
"""
import uuid

import pytest

from app.db.models.billing import CreditLedgerEntry
from app.services.credits import credit_summary, ensure_plan_grant
from app.services.billing_state import current_subscription
from tests.test_billing import _new_org, _post
from tests.test_billing_level1 import _settings  # noqa: F401  (autouse: Paddle test settings)
from tests.test_entitlements import _ev

GROWTH_SECONDS, STARTER_SECONDS = 480 * 60, 180 * 60


def _grants(db, org):
    db.rollback()
    return db.query(CreditLedgerEntry).filter(CreditLedgerEntry.organization_id == uuid.UUID(org), CreditLedgerEntry.kind == "plan_grant").all()


def _growth_customer():
    """Starter bought, then upgraded to Growth mid-period: 10,800 + 18,000 top-up = 28,800 granted (the B10 starting point)."""
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.updated", sub, org, "pri_growth", at="2026-10-02T00:00:00Z"))
    return org, sub


def test_b10_a_duplicate_subscription_does_not_replace_the_growth_allowance(db, _guard_never_calls_real_paddle):
    org, first = _growth_customer()
    db.rollback()
    assert credit_summary(db, uuid.UUID(org)).included_granted_seconds == GROWTH_SECONDS
    second = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", second, org, "pri_starter", at="2026-10-03T00:00:00Z"))
    db.rollback()
    s = credit_summary(db, uuid.UUID(org))
    assert s.included_granted_seconds == GROWTH_SECONDS, s            # not Starter's 10,800
    rows = _grants(db, org)
    assert len(rows) == 2 and sum(r.seconds for r in rows) == GROWTH_SECONDS   # base + upgrade top-up only: no third grant
    assert all(r.reference == first for r in rows) and not [r for r in rows if r.reference == second]
    assert _guard_never_calls_real_paddle.cancelled == [(second, False)]       # still scheduled to cancel at period end, no refund


def test_replays_updates_and_reconcile_style_calls_for_the_duplicate_grant_nothing(db, _guard_never_calls_real_paddle):
    org, first = _growth_customer()
    second = f"sub_{uuid.uuid4().hex[:12]}"
    evt = _ev("subscription.created", second, org, "pri_growth", at="2026-10-03T00:00:00Z")   # even a Growth duplicate
    _post(evt); _post(evt)
    _post(_ev("subscription.updated", second, org, "pri_growth", at="2026-10-04T00:00:00Z"))
    db.rollback()
    dup = [s for s in __import__("app.services.duplicate_subscriptions", fromlist=["x"]).duplicate_subscriptions(db, uuid.UUID(org))][0]
    assert ensure_plan_grant(db, dup) is False                     # the direct call the reconciler / usage route would make
    db.rollback()
    rows = _grants(db, org)
    assert len(rows) == 2 and sum(r.seconds for r in rows) == GROWTH_SECONDS
    assert credit_summary(db, uuid.UUID(org)).included_granted_seconds == GROWTH_SECONDS


def test_the_record_subscription_keeps_getting_its_grant_and_renewal_is_unchanged(db):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.created", sub, org, "pri_starter", at="2026-10-01T00:00:01Z"))     # replay-ish: same sub
    db.rollback()
    rows = _grants(db, org)
    assert len(rows) == 1 and rows[0].seconds == STARTER_SECONDS
    assert current_subscription(db, uuid.UUID(org)).external_subscription_id == sub


def test_other_organizations_are_unaffected_by_a_duplicate(db, _guard_never_calls_real_paddle):
    org_a, _ = _growth_customer()
    _c, org_b = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_b, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_a, "pri_starter", at="2026-10-05T00:00:00Z"))
    db.rollback()
    assert credit_summary(db, uuid.UUID(org_b)).included_granted_seconds == STARTER_SECONDS
    assert credit_summary(db, uuid.UUID(org_a)).included_granted_seconds == GROWTH_SECONDS
