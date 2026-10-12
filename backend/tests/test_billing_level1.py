"""Level 1: public plan catalog, one-open-checkout, duplicate-subscription defence,
canceled-is-terminal, and customer-usage accuracy."""
import uuid
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.db.models.billing import Subscription, UsageLedgerEntry
from app.services import plan_pricing
from app.services.billing_state import current_subscription
from app.services.duplicate_subscriptions import duplicate_subscriptions, schedule_duplicate_cancellations
from app.services.usage import usage_summary
from tests.test_billing import SECRET, _FakeResp, _new_org, _post
from tests.test_entitlements import _ev


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_API_KEY", "test_key")
    monkeypatch.setattr(s, "PADDLE_ENV", "sandbox")
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")
    for key in plan_pricing._redis.scan_iter(f"{plan_pricing.CACHE_PREFIX}*"):
        plan_pricing._redis.delete(key)


def _price_entity(amount="2900", status="active", interval="month"):
    return {
        "status": status,
        "unit_price": {"amount": amount, "currency_code": "USD"},
        "billing_cycle": {"interval": interval, "frequency": 1},
    }


# ---------------- public plan catalog ----------------

def test_plans_endpoint_reports_paddle_price_names_and_features_without_ids(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp
    from fastapi.testclient import TestClient

    from app.main import app

    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        return _FakeResp(200, {"data": _price_entity("2900" if url.endswith("pri_starter") else "6900")})

    monkeypatch.setattr(pp.httpx, "get", fake_get)
    resp = TestClient(app).get("/api/v1/plans")  # no session: public
    assert resp.status_code == 200
    plans = {p["id"]: p for p in resp.json()}
    assert [p["name"] for p in resp.json()] == ["Starter", "Growth"]
    assert plans["starter"]["price"] == {"amount_minor": 2900, "currency": "USD", "interval": "month", "interval_count": 1}
    assert plans["growth"]["price"]["amount_minor"] == 6900
    starter_features = {f["id"] for f in plans["starter"]["features"]}
    assert starter_features <= {f["id"] for f in plans["growth"]["features"]}  # Growth includes Starter
    assert "pri_starter" not in resp.text and "pri_growth" not in resp.text  # no provider ids leak
    assert len(seen) == 2


def test_plan_prices_are_cached_not_refetched_per_request(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeResp(200, {"data": _price_entity()})

    monkeypatch.setattr(pp.httpx, "get", fake_get)
    plan_pricing.price_for_plan("starter")
    plan_pricing.price_for_plan("starter")
    assert len(calls) == 1


def test_price_is_null_not_wrong_when_paddle_is_unreadable_or_not_a_subscription_price(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp

    monkeypatch.setattr(pp.httpx, "get", lambda *a, **k: _FakeResp(500, {}))
    assert plan_pricing.price_for_plan("starter") is None

    for key in plan_pricing._redis.scan_iter(f"{plan_pricing.CACHE_PREFIX}*"):
        plan_pricing._redis.delete(key)
    monkeypatch.setattr(pp.httpx, "get", lambda *a, **k: _FakeResp(200, {"data": _price_entity(interval=None)}))
    assert plan_pricing.price_for_plan("starter") is None  # one-off price: not a plan price

    for key in plan_pricing._redis.scan_iter(f"{plan_pricing.CACHE_PREFIX}*"):
        plan_pricing._redis.delete(key)
    monkeypatch.setattr(pp.httpx, "get", lambda *a, **k: _FakeResp(200, {"data": _price_entity(status="archived")}))
    assert plan_pricing.price_for_plan("starter") is None


# ---------------- one open checkout per org ----------------

def _checkout_posts(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp

    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(json["items"][0]["price_id"])
        n = len(posts)
        return _FakeResp(201, {"data": {"id": f"txn_{n}", "checkout": {"url": f"http://localhost:3000/checkout?_ptxn=txn_{n}"}}})

    def fake_get(url, headers=None, timeout=None, **_):
        # Paddle's own view of the transaction: created and payable, not paid (services/checkout_recovery.py reads this)
        txn = url.rsplit("/", 1)[-1]
        return _FakeResp(200, {"data": {"id": txn, "status": "ready"}})

    def fake_patch(url, headers=None, json=None, timeout=None):
        return _FakeResp(400, {"error": {"code": "transaction_cannot_be_canceled"}})  # Paddle refuses: the old one stays payable

    monkeypatch.setattr(pp.httpx, "post", fake_post)
    monkeypatch.setattr(pp.httpx, "get", fake_get)
    monkeypatch.setattr(pp.httpx, "patch", fake_patch)
    return posts


def test_same_plan_checkout_twice_reuses_one_paddle_transaction(monkeypatch):
    posts = _checkout_posts(monkeypatch)
    c, org = _new_org()
    first = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"})
    second = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(posts) == 1


def test_a_different_plan_while_a_checkout_is_open_is_refused_when_paddle_will_not_cancel_the_old_one(monkeypatch):
    posts = _checkout_posts(monkeypatch)
    c, org = _new_org()
    assert c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"}).status_code == 200
    resp = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "growth"})
    assert resp.status_code == 409
    assert "checkout open" in resp.json()["detail"]
    assert posts == ["pri_starter"]


def test_pending_checkout_is_cleared_when_the_subscription_arrives(monkeypatch):
    from app.services.checkout_guard import get_pending

    _checkout_posts(monkeypatch)
    c, org = _new_org()
    c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"})
    assert get_pending(org) is not None
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_starter"))
    assert get_pending(org) is None


def test_pending_checkouts_are_per_organization(monkeypatch):
    posts = _checkout_posts(monkeypatch)
    c_a, org_a = _new_org()
    c_b, org_b = _new_org()
    c_a.post(f"/api/v1/orgs/{org_a}/billing/checkout-session", json={"plan": "starter"})
    assert c_b.post(f"/api/v1/orgs/{org_b}/billing/checkout-session", json={"plan": "growth"}).status_code == 200
    assert posts == ["pri_starter", "pri_growth"]


# ---------------- duplicate subscriptions ----------------

def test_second_active_subscription_is_scheduled_to_cancel_at_period_end_once(db, _guard_never_calls_real_paddle):
    _c, org = _new_org()
    first, second = f"sub_{uuid.uuid4().hex[:12]}", f"sub_{uuid.uuid4().hex[:12]}"
    evt = _ev("subscription.created", second, org, "pri_growth", at="2026-10-01T00:00:05Z")
    _post(_ev("subscription.created", first, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    assert _guard_never_calls_real_paddle.cancelled == []  # one subscription: nothing to do
    _post(evt)
    assert _guard_never_calls_real_paddle.cancelled == [(second, False)]  # newer one, at period end, never immediately
    _post(evt)  # Paddle redelivers the same event
    assert _guard_never_calls_real_paddle.cancelled == [(second, False)]


def test_oldest_subscription_stays_the_one_that_counts(db):
    _c, org = _new_org()
    first, second = f"sub_{uuid.uuid4().hex[:12]}", f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", first, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.created", second, org, "pri_growth", at="2026-10-01T00:00:05Z"))
    db.rollback()
    import uuid as _u

    assert current_subscription(db, _u.UUID(org)).external_subscription_id == first
    assert [s.external_subscription_id for s in duplicate_subscriptions(db, _u.UUID(org))] == [second]


def test_a_paddle_failure_never_fails_the_webhook_and_is_retried_later(db, _guard_never_calls_real_paddle):
    _c, org = _new_org()
    first, second = f"sub_{uuid.uuid4().hex[:12]}", f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", first, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _guard_never_calls_real_paddle.fail = True
    resp = _post(_ev("subscription.created", second, org, "pri_starter", at="2026-10-01T00:00:05Z"))
    assert resp.status_code == 204  # the event is stored and acknowledged
    assert _guard_never_calls_real_paddle.cancelled == []
    _guard_never_calls_real_paddle.fail = False
    db.rollback()
    assert schedule_duplicate_cancellations(db, __import__("uuid").UUID(org)) == [second]  # next run succeeds


def test_a_single_subscription_is_left_alone(db, _guard_never_calls_real_paddle):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter"))
    assert _guard_never_calls_real_paddle.cancelled == []


def test_duplicate_guard_only_looks_at_its_own_organization(db, _guard_never_calls_real_paddle):
    _c_a, org_a = _new_org()
    _c_b, org_b = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_a, "pri_starter"))
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_b, "pri_starter"))
    assert _guard_never_calls_real_paddle.cancelled == []


# ---------------- canceled is terminal ----------------

def test_same_timestamp_active_event_cannot_resurrect_a_canceled_subscription(db):
    _c, org = _new_org()
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sub, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.canceled", sub, org, "pri_starter", status="canceled", at="2026-10-02T00:00:00Z"))
    _post(_ev("subscription.updated", sub, org, "pri_starter", status="active", at="2026-10-02T00:00:00Z"))
    db.rollback()
    assert db.query(Subscription).filter(Subscription.external_subscription_id == sub).one().status == "canceled"


# ---------------- tenant isolation of billing + plan-gated routes ----------------

def test_other_orgs_billing_and_paid_routes_are_404_even_if_that_org_is_on_growth():
    c_a, _org_a = _new_org()
    _c_b, org_b = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_b, "pri_growth"))
    assert c_a.get(f"/api/v1/orgs/{org_b}/billing/subscription").status_code == 404
    assert c_a.post(f"/api/v1/orgs/{org_b}/phone-numbers", json={"country": "US", "area_code": "305"}).status_code == 404
    body = {"agent_id": str(uuid.uuid4()), "phone_number_id": str(uuid.uuid4())}
    assert c_a.post(f"/api/v1/orgs/{org_b}/activate", json=body).status_code == 404
    assert c_a.get(f"/api/v1/orgs/{org_b}/usage").status_code == 404


def test_my_growth_subscription_does_not_unlock_my_neighbours_org():
    c_a, org_a = _new_org()
    c_b, org_b = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_a, "pri_growth"))
    assert c_b.post(f"/api/v1/orgs/{org_b}/phone-numbers", json={"country": "US", "area_code": "305"}).status_code == 402


# ---------------- customer usage accuracy ----------------

def _ledger(db, org_id, call_id_suffix, kind, seconds, at):
    from app.db.models.calls import Call, CallDirection

    call = Call(
        organization_id=uuid.UUID(org_id),
        retell_call_id=f"call_{call_id_suffix}_{uuid.uuid4().hex[:8]}",
        direction=CallDirection.test if kind == "test" else CallDirection.inbound,
        status="ended",
    )
    db.add(call)
    db.flush()
    db.add(
        UsageLedgerEntry(
            organization_id=uuid.UUID(org_id),
            call_id=call.id,
            retell_call_id=call.retell_call_id,
            kind=kind,
            duration_ms=seconds * 1000,
            billable_seconds=seconds,
            occurred_at=at,
        )
    )
    db.commit()


def test_customer_usage_counts_only_production_calls_inside_the_current_period(db):
    import uuid as _u

    _c, org = _new_org()
    _post(_ev("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, "pri_starter"))  # period 2026-10-01 .. 2026-11-01
    utc = timezone.utc
    _ledger(db, org, "in", "production", 60, datetime(2026, 10, 10, tzinfo=utc))
    _ledger(db, org, "edge_start", "production", 30, datetime(2026, 10, 1, tzinfo=utc))  # period start is inclusive
    _ledger(db, org, "before", "production", 500, datetime(2026, 9, 30, 23, 59, tzinfo=utc))  # previous period
    _ledger(db, org, "edge_end", "production", 500, datetime(2026, 11, 1, tzinfo=utc))  # period end is exclusive
    _ledger(db, org, "test", "test", 500, datetime(2026, 10, 10, tzinfo=utc))  # test calls are never usage
    db.rollback()
    summary = usage_summary(db, _u.UUID(org))
    assert summary["calls_count"] == 2
    assert summary["billable_seconds"] == 90
    assert "provider_cost" not in summary
