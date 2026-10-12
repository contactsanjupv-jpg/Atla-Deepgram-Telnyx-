"""
The blocked checkout ("You already have a checkout open for another plan"): resume, recover, switch safely.
Paddle is faked at its API boundary; the decisions come from the transaction states Paddle reports.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.providers.billing.billing_provider import CheckoutSession
from app.services import checkout_guard
from tests.billing_helpers import new_org, subscribe

settings = get_settings()


class FakePaddle:
    txns: dict[str, dict] = {}
    subs: dict[str, dict] = {}
    created: list[str] = []
    cancels: list[str] = []
    cancel_works = True
    unreachable = False

    def __init__(self):
        pass

    def create_checkout_session(self, *, organization_id, plan_id, plan_price_id, **_):
        t = type(self)
        txn = f"txn_{uuid.uuid4().hex[:10]}"
        t.created.append(plan_id)
        t.txns[txn] = {"id": txn, "status": "ready"}
        return CheckoutSession(checkout_url=f"https://pay.example/{plan_id}?_ptxn={txn}", provider_session_id=txn)

    def get_transaction(self, txn):
        if type(self).unreachable:
            raise RuntimeError("paddle unreachable")
        return type(self).txns.get(txn)

    def cancel_transaction(self, txn):
        t = type(self)
        t.cancels.append(txn)
        if t.cancel_works:
            t.txns[txn]["status"] = "canceled"
        return t.cancel_works

    def get_subscription(self, sub_id):
        return type(self).subs.get(sub_id)


@pytest.fixture(autouse=True)
def paddle(monkeypatch):
    for attr, val in {"PADDLE_API_KEY": "k", "PADDLE_ENV": "sandbox", "PADDLE_STARTER_PRICE_ID": "pri_starter",
                      "PADDLE_GROWTH_PRICE_ID": "pri_growth"}.items():
        monkeypatch.setattr(settings, attr, val)
    FakePaddle.txns, FakePaddle.subs, FakePaddle.created, FakePaddle.cancels = {}, {}, [], []
    FakePaddle.cancel_works, FakePaddle.unreachable = True, False
    monkeypatch.setattr("app.api.v1.billing.PaddleBillingProvider", FakePaddle)
    monkeypatch.setattr("app.providers.billing.paddle_billing_provider.PaddleBillingProvider", FakePaddle)
    return FakePaddle


def _checkout(c, org, plan):
    return c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": plan})


def _state(c, org):
    return c.get(f"/api/v1/orgs/{org}/billing/checkout").json()


def _sub_rows(org):
    db = SessionLocal()
    try:
        return db.query(Subscription).filter_by(organization_id=uuid.UUID(org)).count()
    finally:
        db.close()


def test_an_open_checkout_is_resumed_with_the_same_link_and_no_second_paddle_transaction(paddle):
    c, org = new_org("co")
    first = _checkout(c, org, "starter").json()["checkout_url"]
    again = _checkout(c, org, "starter")
    assert again.status_code == 200 and again.json()["checkout_url"] == first
    assert paddle.created == ["starter"]
    st = _state(c, org)
    assert st["state"] == "open" and st["plan"] == "starter" and st["checkout_url"] == first


def test_choosing_another_plan_cancels_the_old_transaction_first_and_then_opens_the_new_one(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    old = next(iter(paddle.txns))
    resp = _checkout(c, org, "growth")
    assert resp.status_code == 200 and "growth" in resp.json()["checkout_url"]
    assert paddle.cancels == [old] and paddle.txns[old]["status"] == "canceled"
    assert paddle.created == ["starter", "growth"]
    assert _state(c, org)["plan"] == "growth"


def test_if_paddle_will_not_cancel_the_old_checkout_no_second_one_is_created(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    paddle.cancel_works = False
    resp = _checkout(c, org, "growth")
    assert resp.status_code == 409 and "Starter checkout open" in resp.json()["detail"]
    assert paddle.created == ["starter"]  # two payable transactions would mean two subscriptions
    assert _state(c, org)["state"] == "open"  # and the customer can still resume the first


def test_an_expired_or_canceled_checkout_is_recovered_automatically(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    txn = next(iter(paddle.txns))
    paddle.txns[txn]["status"] = "canceled"
    st = _state(c, org)
    assert st["state"] == "expired" and "new one" in st["message"]
    assert checkout_guard.get_pending(uuid.UUID(org)) is None  # cleared because Paddle said it is dead
    assert _checkout(c, org, "growth").status_code == 200


def test_a_payment_still_processing_is_reported_honestly_and_blocks_a_second_checkout(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    txn = next(iter(paddle.txns))
    paddle.txns[txn].update(status="completed", subscription_id="sub_new")  # paid, but Paddle has no subscription to read yet
    st = _state(c, org)
    assert st["state"] == "processing" and "Please don't pay again" in st["message"]
    assert checkout_guard.get_pending(uuid.UUID(org)) is not None  # NOT cleared: the outcome isn't settled locally
    blocked = _checkout(c, org, "growth")
    assert blocked.status_code == 409 and paddle.created == ["starter"] and not paddle.cancels


def test_a_paid_checkout_whose_webhook_never_arrived_is_completed_from_paddles_own_subscription(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    txn = next(iter(paddle.txns))
    paddle.txns[txn].update(status="completed", subscription_id="sub_paid")
    paddle.subs["sub_paid"] = {
        "id": "sub_paid", "status": "active", "custom_data": {"organization_id": org},
        "items": [{"status": "active", "quantity": 1, "price": {"id": "pri_starter"}}],
        "current_billing_period": {"starts_at": "2026-10-11T00:00:00Z", "ends_at": "2099-11-11T00:00:00Z"},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    st = _state(c, org)
    assert st["state"] == "none"
    sub = c.get(f"/api/v1/orgs/{org}/billing/subscription").json()
    assert sub["entitled"] is True and sub["plan_id"] == "starter"
    assert checkout_guard.get_pending(uuid.UUID(org)) is None
    assert _state(c, org)["state"] == "none" and _sub_rows(org) == 1  # polling again changes nothing


def test_entitlement_never_comes_from_opening_or_returning_from_checkout(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    _state(c, org)  # the "return from checkout" poll while Paddle still says ready
    assert c.get(f"/api/v1/orgs/{org}/billing/subscription").json() is None
    assert c.post(f"/api/v1/orgs/{org}/agents/{uuid.uuid4()}/activate", json={"agent_id": str(uuid.uuid4()), "phone_number_id": str(uuid.uuid4())}).status_code in (402, 404, 422)


def test_if_paddle_cannot_be_reached_nothing_is_cleared_and_no_conflicting_checkout_is_allowed(paddle):
    c, org = new_org("co")
    _checkout(c, org, "starter")
    paddle.unreachable = True
    st = _state(c, org)
    assert st["state"] == "unconfirmed" and "Don't pay again" in st["message"]
    assert _checkout(c, org, "growth").status_code == 409
    assert checkout_guard.get_pending(uuid.UUID(org)) is not None
    assert paddle.created == ["starter"]


def test_a_customer_with_an_active_plan_cannot_open_another_checkout(paddle):
    c, org = new_org("co")
    subscribe(org)
    assert _checkout(c, org, "growth").status_code == 409
    assert paddle.created == []


def test_checkout_state_is_private_to_the_organization(paddle):
    a, org_a = new_org("co")
    b, _org_b = new_org("co")
    _checkout(a, org_a, "starter")
    assert b.get(f"/api/v1/orgs/{org_a}/billing/checkout").status_code == 404
    assert b.post(f"/api/v1/orgs/{org_a}/billing/checkout-session", json={"plan": "growth"}).status_code == 404
