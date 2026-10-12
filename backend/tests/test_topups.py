"""
Customer-chosen top-ups (minimum $20). Credits come only from a Paddle-verified completed payment, once;
failed / canceled / ambiguous payments grant nothing; refunds and chargebacks follow the documented policy.
"""
import threading
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry, TopupPurchase
from app.services import topups
from app.services.credits import credit_summary
from app.services.plans import PLANS
from tests.billing_helpers import add_usage, new_org, subscribe
from tests.test_billing import SECRET, _post as paddle_post

settings = get_settings()
MIN = 60


@pytest.fixture(autouse=True)
def _paddle(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "test_key")
    monkeypatch.setattr(settings, "PADDLE_ENV", "sandbox")
    monkeypatch.setattr(settings, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(settings, "PADDLE_GROWTH_PRICE_ID", "pri_growth")
    monkeypatch.setattr(settings, "TOPUP_MIN_CENTS", 2000)
    monkeypatch.setattr(settings, "TOPUP_MAX_CENTS", 50000)


class FakePaddle:
    """Stands in for PaddleBillingProvider: records what we asked for, serves canned transactions."""

    created: list[dict] = []
    transactions: dict[str, dict] = {}
    fail_create = False

    def __init__(self):
        pass

    def create_topup_transaction(self, **kw):
        if type(self).fail_create:
            raise RuntimeError("paddle down")
        type(self).created.append(kw)
        txn = f"txn_{uuid.uuid4().hex[:12]}"
        from app.providers.billing.billing_provider import CheckoutSession

        return CheckoutSession(checkout_url=f"https://pay.example/?_ptxn={txn}", provider_session_id=txn)

    def get_transaction(self, txn_id):
        return type(self).transactions.get(txn_id)


@pytest.fixture
def paddle(monkeypatch):
    FakePaddle.created, FakePaddle.transactions, FakePaddle.fail_create = [], {}, False
    monkeypatch.setattr(topups, "PaddleBillingProvider", FakePaddle)
    monkeypatch.setattr("app.api.v1.billing.PaddleBillingProvider", FakePaddle)
    return FakePaddle


def _purchase(org, amount=2500):
    """A top-up row with a Paddle transaction id, as create_topup leaves it before payment."""
    db = SessionLocal()
    try:
        row = topups.create_topup(db, uuid.UUID(org), uuid.uuid4(), amount, provider=FakePaddle())
        return str(row.id), row.provider_transaction_id, row.credit_seconds
    finally:
        db.close()


def _txn(org, topup_id, txn_id, amount=2500, status="completed"):
    return {
        "id": txn_id, "status": status,
        "custom_data": {"organization_id": org, "kind": "topup", "topup_id": topup_id},
        "items": [{"quantity": 1, "price": {"unit_price": {"amount": str(amount), "currency_code": "USD"}}}],
    }


def _complete(org, topup_id, txn_id, amount=2500):
    db = SessionLocal()
    try:
        granted = topups.complete_topup(db, _txn(org, topup_id, txn_id, amount))
        db.commit()
        return granted
    finally:
        db.close()


def _balance(org):
    db = SessionLocal()
    try:
        return credit_summary(db, uuid.UUID(org)).purchased_balance_seconds
    finally:
        db.close()


def _entries(org, kind=None):
    db = SessionLocal()
    try:
        q = db.query(CreditLedgerEntry).filter_by(organization_id=uuid.UUID(org))
        return (q.filter_by(kind=kind) if kind else q).all()
    finally:
        db.close()


def _adjustment(txn_id, action="refund", status="approved", subtotal=None, adj_id=None):
    return {"id": adj_id or f"adj_{uuid.uuid4().hex[:10]}", "transaction_id": txn_id, "action": action, "status": status,
            "totals": {"subtotal": str(subtotal) if subtotal is not None else "0"}}


def _adjust(adj):
    db = SessionLocal()
    try:
        applied = topups.apply_adjustment(db, adj)
        db.commit()
        return applied
    finally:
        db.close()


# ---------------- amount rules ----------------

@pytest.mark.parametrize("bad", [0, 1, 1999, -2000, 50001, 2000.5, "2500", None, True])
def test_amounts_below_20_dollars_above_the_cap_or_not_whole_cents_are_rejected(bad):
    with pytest.raises(topups.InvalidTopup):
        topups.validate_amount(bad)


def test_any_amount_from_20_dollars_up_is_accepted_and_the_quote_states_the_minutes_before_payment():
    for amount in (2000, 2001, 3333, 12345, 50000):
        q = topups.quote(amount)
        assert q["credit_seconds"] == amount * 60 // settings.TOPUP_CENTS_PER_MINUTE
        assert q["credit_minutes"] == q["credit_seconds"] // 60 and q["credit_minutes"] > 0
    assert "$20" in str(pytest.raises(topups.InvalidTopup, topups.validate_amount, 1999).value)


def test_more_money_always_buys_at_least_as_many_credits():
    seconds = [topups.credit_seconds_for(a) for a in range(2000, 6000, 137)]
    assert seconds == sorted(seconds)


# ---------------- creating a purchase ----------------

def test_creating_a_topup_writes_our_row_first_and_asks_paddle_for_exactly_that_amount(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org, 2500)
    sent = paddle.created[0]
    assert sent["amount_minor"] == 2500 and sent["currency"] == "USD" and sent["topup_id"] == topup_id
    db = SessionLocal()
    row = db.get(TopupPurchase, uuid.UUID(topup_id))
    assert row.status == "created" and row.provider_transaction_id == txn and row.credit_seconds == seconds
    db.close()
    assert _balance(org) == 0  # creating a checkout grants nothing


def test_if_paddle_is_down_nothing_is_charged_and_nothing_is_granted(paddle):
    _c, org = new_org("tp")
    paddle.fail_create = True
    db = SessionLocal()
    with pytest.raises(RuntimeError):
        topups.create_topup(db, uuid.UUID(org), uuid.uuid4(), 2500, provider=FakePaddle())
    db.close()
    db = SessionLocal()
    assert db.query(TopupPurchase).filter_by(organization_id=uuid.UUID(org)).one().status == "failed"
    db.close()
    assert _balance(org) == 0


# ---------------- granting ----------------

def test_a_completed_payment_grants_the_purchased_credits_once(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    assert _complete(org, topup_id, txn) is True
    assert _balance(org) == seconds
    assert _complete(org, topup_id, txn) is False  # retried / duplicated delivery
    assert _balance(org) == seconds and len(_entries(org, "topup")) == 1


def test_concurrent_deliveries_of_the_same_payment_grant_once(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    results, gate = [], threading.Barrier(8)

    def go():
        gate.wait()
        results.append(_complete(org, topup_id, txn))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and _balance(org) == seconds and len(_entries(org, "topup")) == 1


@pytest.mark.parametrize("status", ["draft", "ready", "billed", "paid", "canceled", "past_due"])
def test_only_a_completed_transaction_grants_anything(paddle, status):
    _c, org = new_org("tp")
    topup_id, txn, _s = _purchase(org)
    db = SessionLocal()
    assert topups.complete_topup(db, _txn(org, topup_id, txn, status=status)) is False
    db.commit()
    db.close()
    assert _balance(org) == 0


def test_the_amount_credited_comes_from_our_row_and_a_mismatching_payment_grants_nothing(paddle):
    _c, org = new_org("tp")
    topup_id, txn, _s = _purchase(org, 2500)
    assert _complete(org, topup_id, txn, amount=100) is False  # paid far less than the row says
    assert _complete(org, topup_id, txn, amount=999999) is False
    assert _balance(org) == 0


def test_a_payment_claiming_another_organization_or_no_purchase_row_grants_nothing(paddle):
    _c, org = new_org("tp")
    _c2, other = new_org("tp")
    topup_id, txn, _s = _purchase(org)
    forged = _txn(other, topup_id, txn)  # custom_data points at someone else
    db = SessionLocal()
    assert topups.complete_topup(db, forged) is False
    unknown = _txn(org, str(uuid.uuid4()), "txn_nobody")
    assert topups.complete_topup(db, unknown) is False
    db.commit()
    db.close()
    assert _balance(org) == 0 and _balance(other) == 0


def test_failed_or_canceled_checkouts_grant_zero_but_can_still_complete_if_paid_later(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    db = SessionLocal()
    assert topups.fail_topup(db, _txn(org, topup_id, txn, status="canceled")) is True
    db.commit()
    db.close()
    assert _balance(org) == 0
    assert _complete(org, topup_id, txn) is True  # the customer paid after all: money received means credits granted
    assert _balance(org) > 0


def test_signed_paddle_webhooks_drive_the_whole_flow_and_event_redelivery_is_harmless(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    event = {
        "event_id": f"evt_{uuid.uuid4().hex}", "event_type": "transaction.completed", "occurred_at": "2026-10-11T10:00:00.000000Z",
        "notification_id": f"ntf_{uuid.uuid4().hex}", "data": _txn(org, topup_id, txn),
    }
    assert paddle_post(event).status_code == 204
    assert paddle_post(event).status_code in (200, 204)  # same event id again
    other_event = dict(event, event_id=f"evt_{uuid.uuid4().hex}", notification_id=f"ntf_{uuid.uuid4().hex}")
    assert paddle_post(other_event).status_code == 204  # a DIFFERENT event describing the same payment
    assert _balance(org) == seconds and len(_entries(org, "topup")) == 1


def test_an_unsigned_webhook_cannot_grant_credits(paddle):
    _c, org = new_org("tp")
    topup_id, txn, _s = _purchase(org)
    event = {"event_id": "evt_forged", "event_type": "transaction.completed", "occurred_at": "2026-10-11T10:00:00Z",
             "notification_id": "ntf_forged", "data": _txn(org, topup_id, txn)}
    assert paddle_post(event, signature="ts=1;h1=deadbeef").status_code in (400, 401)
    assert paddle_post(event, signature=None).status_code in (400, 401)
    assert _balance(org) == 0


# ---------------- refunds and chargebacks ----------------

def test_a_partial_refund_removes_the_same_share_of_credits_and_is_applied_once(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org, 4000)
    _complete(org, topup_id, txn, amount=4000)
    adj = _adjustment(txn, subtotal=1000)  # a quarter of the price
    assert _adjust(adj) is True and _adjust(adj) is False
    assert _balance(org) == seconds - seconds * 1000 // 4000
    assert len(_entries(org, "refund")) == 1


def test_a_full_refund_removes_all_credits_across_several_adjustments(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org, 4000)
    _complete(org, topup_id, txn, amount=4000)
    _adjust(_adjustment(txn, subtotal=1000))
    _adjust(_adjustment(txn, subtotal=3000))
    assert _balance(org) == 0


def test_pending_or_rejected_adjustments_and_subscription_refunds_change_nothing(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    _complete(org, topup_id, txn)
    assert _adjust(_adjustment(txn, status="pending_approval", subtotal=2500)) is False
    assert _adjust(_adjustment(txn, status="rejected", subtotal=2500)) is False
    assert _adjust(_adjustment("txn_a_subscription_payment", subtotal=7900)) is False
    assert _balance(org) == seconds


def test_a_chargeback_removes_what_is_left_and_a_won_dispute_restores_it(paddle):
    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    _complete(org, topup_id, txn)
    cb = _adjustment(txn, action="chargeback")
    assert _adjust(cb) is True and _balance(org) == 0
    assert _adjust(cb) is False
    assert _adjust(_adjustment(txn, action="chargeback_reverse")) is True
    assert _balance(org) == seconds


def test_refunding_credits_that_were_already_spent_leaves_a_debt_that_blocks_new_calls(paddle):
    _c, org = new_org("tp")
    subscribe(org)
    topup_id, txn, seconds = _purchase(org)
    _complete(org, topup_id, txn)
    full = PLANS["starter"].included_minutes * MIN
    add_usage(org, full + seconds)  # spent the allowance AND all purchased credits
    _adjust(_adjustment(txn, action="chargeback"))
    db = SessionLocal()
    s = credit_summary(db, uuid.UUID(org))
    db.close()
    assert s.purchased_balance_seconds == -seconds and s.available_seconds < 0 and s.status == "empty"


# ---------------- reconciliation ----------------

def test_the_reconciler_grants_a_payment_whose_webhook_never_arrived_and_only_once(paddle, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _c, org = new_org("tp")
    topup_id, txn, seconds = _purchase(org)
    paddle.transactions[txn] = _txn(org, topup_id, txn)
    later = datetime.now(timezone.utc) + timedelta(minutes=20)
    db = SessionLocal()
    first = topups.reconcile_topups(db, FakePaddle(), now=later)
    second = topups.reconcile_topups(db, FakePaddle(), now=later)
    db.close()
    assert first["granted"] == 1 and second["granted"] == 0 and _balance(org) == seconds


def test_the_reconciler_fails_canceled_checkouts_and_leaves_unpaid_ones_alone(paddle):
    from datetime import datetime, timedelta, timezone

    _c, org = new_org("tp")
    a_id, a_txn, _ = _purchase(org)
    b_id, b_txn, _ = _purchase(org)
    paddle.transactions[a_txn] = _txn(org, a_id, a_txn, status="canceled")
    paddle.transactions[b_txn] = _txn(org, b_id, b_txn, status="ready")
    db = SessionLocal()
    result = topups.reconcile_topups(db, FakePaddle(), now=datetime.now(timezone.utc) + timedelta(minutes=20))
    statuses = {r.id: r.status for r in db.query(TopupPurchase).filter_by(organization_id=uuid.UUID(org)).all()}
    db.close()
    assert result["failed"] == 1 and result["still_open"] == 1
    assert statuses[uuid.UUID(a_id)] == "failed" and statuses[uuid.UUID(b_id)] == "created" and _balance(org) == 0


# ---------------- the HTTP API ----------------

def test_topup_endpoint_enforces_the_minimum_and_returns_a_checkout_for_valid_amounts(paddle):
    c, org = new_org("tp")
    subscribe(org)
    low = c.post(f"/api/v1/orgs/{org}/billing/topups", json={"amount_minor": 1500})
    assert low.status_code == 422 and "$20" in low.json()["detail"] and not paddle.created
    ok = c.post(f"/api/v1/orgs/{org}/billing/topups", json={"amount_minor": 2500})
    assert ok.status_code == 200 and ok.json()["checkout_url"].startswith("https://") and ok.json()["credit_minutes"] > 0
    assert paddle.created[0]["amount_minor"] == 2500
    assert _balance(org) == 0  # still nothing until Paddle confirms payment


def test_topups_need_an_active_plan(paddle):
    c, org = new_org("tp")
    resp = c.post(f"/api/v1/orgs/{org}/billing/topups", json={"amount_minor": 2500})
    assert resp.status_code == 402 and not paddle.created


def test_quote_endpoint_tells_the_customer_what_they_will_get_and_rejects_bad_amounts(paddle):
    c, org = new_org("tp")
    good = c.get(f"/api/v1/orgs/{org}/billing/topup-quote", params={"amount_minor": 5000})
    assert good.status_code == 200 and good.json()["credit_minutes"] == topups.quote(5000)["credit_minutes"]
    assert "provider" not in str(good.json()).lower() and "cost" not in str(good.json()).lower()
    assert c.get(f"/api/v1/orgs/{org}/billing/topup-quote", params={"amount_minor": 100}).status_code == 422


def test_a_paddle_outage_returns_a_calm_message_and_no_provider_error(paddle):
    c, org = new_org("tp")
    subscribe(org)
    paddle.fail_create = True
    resp = c.post(f"/api/v1/orgs/{org}/billing/topups", json={"amount_minor": 2500})
    assert resp.status_code == 502 and "paddle down" not in resp.text and "haven't been charged" in resp.json()["detail"]


def test_one_organization_cannot_buy_for_or_see_the_purchases_of_another(paddle):
    a, org_a = new_org("tp")
    b, org_b = new_org("tp")
    subscribe(org_a)
    assert a.post(f"/api/v1/orgs/{org_a}/billing/topups", json={"amount_minor": 2500}).status_code == 200
    assert b.post(f"/api/v1/orgs/{org_a}/billing/topups", json={"amount_minor": 2500}).status_code == 404
    assert b.get(f"/api/v1/orgs/{org_a}/billing/topups").status_code == 404
    assert b.get(f"/api/v1/orgs/{org_b}/billing/topups").json() == []
    assert len(a.get(f"/api/v1/orgs/{org_a}/billing/topups").json()) == 1
    assert b.get(f"/api/v1/orgs/{org_a}/usage/credits").status_code == 404


def test_purchases_appear_in_the_usage_history_once_credited(paddle):
    c, org = new_org("tp")
    subscribe(org)
    topup_id, txn, seconds = _purchase(org)
    _complete(org, topup_id, txn)
    events = c.get(f"/api/v1/orgs/{org}/usage/events").json()
    kinds = [e["kind"] for e in events]
    assert "topup" in kinds and "plan_grant" in kinds
    assert next(e for e in events if e["kind"] == "topup")["seconds"] == seconds
