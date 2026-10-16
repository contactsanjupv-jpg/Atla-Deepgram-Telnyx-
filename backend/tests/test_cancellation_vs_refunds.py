"""
Cancelling and refunding are SEPARATE. Cancel = schedule at the end of the paid period (no refund, access continues
until Paddle's date, repeatable). Refund = an explicit platform-admin workflow with eligibility checks, an audit
trail and Paddle's own confirmation. Only the provider is faked: nothing here observed real Paddle behaviour.
"""
import pathlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry, RefundRequest, Subscription
from app.db.models.platform import AdminUser, AuditLog
from app.db.models.tenancy import User
from app.main import app
from app.providers.billing.paddle_billing_provider import PaddleAPIError
from app.services.billing_state import has_active_subscription
from app.services.credits import credit_summary
from app.services.refunds import RefundRejected, request_refund
from tests.test_account import _acct
from tests.test_billing import SECRET, _event, _post as paddle_post

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
END = "2026-11-01T00:00:00.000000Z"


class FakePaddle:
    """Records every provider call. Refund-shaped calls are tracked so tests can prove none were made."""

    def __init__(self, remote=None, txn=None, refund=None):
        self.remote, self.txn, self.refund_result = remote, txn, refund
        self.cancels, self.refunds, self.other = [], [], []
        self.adjustments: list[dict] = []  # what Paddle holds
        self.accept_then_timeout = 0  # next N refund posts are accepted by Paddle but our call times out
        self.list_fails = False
        self.post_delay = 0.0

    def cancel_subscription(self, ext, immediately=False):
        self.cancels.append((ext, immediately))

    def get_subscription(self, ext):
        return self.remote

    def get_transaction(self, txn_id):
        return self.txn

    def create_refund(self, txn_id, reason):
        import time

        self.refunds.append((txn_id, reason))
        time.sleep(self.post_delay)
        if isinstance(self.refund_result, Exception):
            raise self.refund_result
        adj = self.refund_result or {"id": f"adj_{uuid.uuid4().hex[:8]}", "status": "pending_approval"}
        self.adjustments.append({**adj, "transaction_id": txn_id, "action": "refund"})
        if self.accept_then_timeout > 0:
            self.accept_then_timeout -= 1
            raise PaddleAPIError("Could not reach Paddle")  # status_code 0: outcome unknown
        return adj

    def list_refund_adjustments(self, txn_id):
        if self.list_fails:
            raise PaddleAPIError("Could not reach Paddle")
        return [a for a in self.adjustments if a["transaction_id"] == txn_id]

    def cancel_transaction(self, *a, **k):
        self.other.append("cancel_transaction")


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_WEBHOOK_SECRET", SECRET)


def _subscribe(org, scheduled=None, status="active"):
    sid = f"sub_{uuid.uuid4().hex[:10]}"
    ev = _event("subscription.created", sid, org, status=status)
    if scheduled:
        ev["data"]["scheduled_change"] = {"action": "cancel", "effective_at": scheduled}
    assert paddle_post(ev).status_code in (200, 204)
    return sid


def _sub_row(org):
    db = SessionLocal()
    try:
        s = db.query(Subscription).filter(Subscription.organization_id == uuid.UUID(org)).one()
        db.expunge(s)
        return s
    finally:
        db.close()


def _view(c, org):
    return c.get(f"/api/v1/orgs/{org}/billing/subscription").json()


def _use(monkeypatch, fake):
    monkeypatch.setattr("app.api.v1.billing.PaddleBillingProvider", lambda: fake)
    monkeypatch.setattr("app.api.v1.admin_refunds.PaddleBillingProvider", lambda: fake)
    return fake


# ---------------- cancel never refunds ----------------

def test_cancelling_schedules_at_period_end_and_makes_no_refund_call(monkeypatch):
    c, org, *_ = _acct()
    sid = _subscribe(org)
    fake = _use(monkeypatch, FakePaddle(remote={"status": "active", "scheduled_change": {"action": "cancel", "effective_at": END}}))
    fake.remote_first = None
    # Paddle has nothing scheduled until we ask: first lookup None, then scheduled.
    answers = [None, fake.remote]
    fake.get_subscription = lambda ext: answers.pop(0) if answers else fake.remote
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.status_code == 202 and resp.json() == {"status": "requested", "effective_at": END}
    assert fake.cancels == [(sid, False)]  # end of period, never immediately
    assert fake.refunds == [] and fake.other == []
    db = SessionLocal()
    assert db.query(RefundRequest).filter(RefundRequest.organization_id == uuid.UUID(org)).count() == 0
    assert db.query(CreditLedgerEntry).filter(CreditLedgerEntry.organization_id == uuid.UUID(org), CreditLedgerEntry.kind == "refund").count() == 0
    db.close()


def test_the_customer_cancel_path_has_no_route_to_the_refund_code():
    """Structural guard: the only callers of the refund provider call are the refund service itself."""
    callers = {str(p.relative_to(APP)) for p in APP.rglob("*.py") if "create_refund" in p.read_text()}
    assert callers == {"providers/billing/paddle_billing_provider.py", "services/refunds.py", "api/v1/admin_refunds.py"}  # provider, service, admin-only route
    billing = (APP / "api/v1/billing.py").read_text()
    import re

    assert not re.search(r"(import|from)\s+[\w.\s,]*refunds", billing)  # docstrings may mention it; imports may not
    assert "create_refund" not in billing and "/adjustments" not in billing
    assert not re.search(r"(import|from)\s+[\w.\s,]*refunds", (APP / "services/subscription_sync.py").read_text())


def test_there_is_no_customer_refund_route():
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert not any("refund" in p and "/admin/" not in p for p in paths)


# ---------------- idempotent ----------------

def test_a_repeat_cancel_returns_the_confirmed_state_without_cancelling_again(monkeypatch):
    c, org, *_ = _acct()
    _subscribe(org, scheduled=END)  # Paddle's webhook already synced the scheduled cancel
    fake = _use(monkeypatch, FakePaddle())
    for _ in range(3):
        resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
        assert resp.status_code == 202 and resp.json()["status"] == "already_scheduled"
        assert resp.json()["effective_at"].startswith("2026-11-01")
    assert fake.cancels == [] and fake.refunds == []


def test_a_repeat_cancel_before_the_webhook_arrives_is_confirmed_from_paddle(monkeypatch):
    c, org, *_ = _acct()
    _subscribe(org)  # our row has no scheduled cancel yet
    fake = _use(monkeypatch, FakePaddle(remote={"status": "active", "scheduled_change": {"action": "cancel", "effective_at": END}}))
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.json() == {"status": "already_scheduled", "effective_at": END}
    assert fake.cancels == []


# ---------------- paid access continues until the right date ----------------

def test_access_continues_after_cancel_is_scheduled_and_the_date_is_shown():
    c, org, *_ = _acct()
    _subscribe(org, scheduled=END)
    view = _view(c, org)
    assert view["status"] == "active" and view["entitled"] is True and view["features"]
    assert view["cancel_effective_at"].startswith("2026-11-01")
    db = SessionLocal()
    uid = uuid.UUID(org)
    assert has_active_subscription(db, uid) and credit_summary(db, uid).entitled
    db.close()


def test_access_ends_only_when_paddle_reports_the_subscription_canceled():
    c, org, *_ = _acct()
    sid = _subscribe(org, scheduled=END)
    ev = _event("subscription.canceled", sid, org, status="canceled", at="2026-11-01T00:00:01.000000Z")
    assert paddle_post(ev).status_code in (200, 204)
    db = SessionLocal()
    assert not has_active_subscription(db, uuid.UUID(org))
    db.close()
    assert _view(c, org)["entitled"] is False


def test_a_canceled_subscription_that_was_resumed_clears_the_scheduled_date():
    c, org, *_ = _acct()
    sid = _subscribe(org, scheduled=END)
    ev = _event("subscription.updated", sid, org, at="2026-10-05T00:00:00.000000Z")  # no scheduled_change: cancel removed at Paddle
    assert paddle_post(ev).status_code in (200, 204)
    assert _view(c, org)["cancel_effective_at"] is None


# ---------------- refunds: separate, authorized, audited ----------------

def _txn(sub_id, status="completed"):
    return {"id": f"txn_{uuid.uuid4().hex[:10]}", "status": status, "subscription_id": sub_id, "currency_code": "USD",
            "details": {"totals": {"grand_total": "4900"}}}


def _admin(c_email_hint=None):
    c, org, email, _name = _acct()
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).one()
    db.add(AdminUser(user_id=user.id))
    db.commit()
    uid = user.id
    db.close()
    return c, org, uid


def _service(org, txn_fake, admin_id, basis="billing_error", reason="Customer was charged twice in error"):
    from types import SimpleNamespace

    db = SessionLocal()
    try:
        row = request_refund(db, admin_id, (txn_fake.txn or {}).get("id", "txn_missing"), basis, reason, txn_fake)
        return SimpleNamespace(status=row.status)
    finally:
        db.close()


def test_an_ordinary_customer_cannot_trigger_a_refund(monkeypatch):
    c, org, *_ = _acct()  # an organization owner, not a platform admin
    sid = _subscribe(org)
    fake = _use(monkeypatch, FakePaddle(txn=_txn(sid)))
    r = c.post("/api/v1/admin/refunds", json={"transaction_id": fake.txn["id"], "basis": "goodwill", "reason": "please refund me now"})
    assert r.status_code == 403 and fake.refunds == []
    assert TestClient(app).post("/api/v1/admin/refunds", json={}).status_code in (401, 403, 422)


def test_an_admin_refund_is_audited_and_not_approved_until_paddle_says_so(monkeypatch):
    c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = _use(monkeypatch, FakePaddle(txn=_txn(sid)))
    r = c.post("/api/v1/admin/refunds", json={"transaction_id": fake.txn["id"], "basis": "legal_requirement", "reason": "Statutory withdrawal right invoked"})
    assert r.status_code == 202 and r.json()["status"] == "submitted"  # Paddle: pending_approval
    assert len(fake.refunds) == 1
    db = SessionLocal()
    row = db.query(RefundRequest).filter(RefundRequest.transaction_id == fake.txn["id"]).one()
    actions = [a.action for a in db.query(AuditLog).filter(AuditLog.target_id == str(row.id)).order_by(AuditLog.created_at)]
    assert actions == ["refund.requested", "refund.submitted"] and row.requested_by == admin_id
    adj_id = row.provider_adjustment_id
    db.close()

    adj = {"id": adj_id, "action": "refund", "status": "approved", "transaction_id": fake.txn["id"], "totals": {"subtotal": "4900"}}
    body = {"event_id": f"evt_{uuid.uuid4().hex}", "event_type": "adjustment.updated", "occurred_at": "2026-10-02T00:00:00.000000Z", "data": adj}
    assert paddle_post(body).status_code in (200, 204)
    db = SessionLocal()
    row = db.query(RefundRequest).filter(RefundRequest.transaction_id == fake.txn["id"]).one()
    assert row.status == "approved"
    assert db.query(AuditLog).filter(AuditLog.target_id == str(row.id), AuditLog.action == "refund.approved").count() == 1
    db.close()


def test_a_refund_does_not_cancel_or_change_the_subscription_or_credits(monkeypatch):
    c, org, admin_id = _admin()
    sid = _subscribe(org)
    uid = uuid.UUID(org)
    db = SessionLocal()
    before = credit_summary(db, uid).available_seconds
    db.close()
    fake = FakePaddle(txn=_txn(sid), refund={"id": "adj_1", "status": "approved"})
    row = _service(org, fake, admin_id)
    assert row.status == "approved" and fake.cancels == []
    assert _sub_row(org).status == "active"
    db = SessionLocal()
    assert credit_summary(db, uid).available_seconds == before
    db.close()


@pytest.mark.parametrize("txn_status", ["billed", "canceled", "past_due", "draft"])
def test_only_a_transaction_paddle_reports_completed_is_refundable(txn_status):
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid, txn_status))
    with pytest.raises(RefundRejected):
        _service(org, fake, admin_id)
    assert fake.refunds == []


def test_unknown_transactions_foreign_subscriptions_bad_basis_and_missing_reason_are_rejected():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    for fake, kw in (
        (FakePaddle(txn=None), {}),
        (FakePaddle(txn=_txn("sub_not_ours")), {}),
        (FakePaddle(txn=_txn(sid)), {"basis": "because"}),
        (FakePaddle(txn=_txn(sid)), {"reason": "no"}),
    ):
        with pytest.raises(RefundRejected):
            _service(org, fake, admin_id, **kw)
        assert fake.refunds == []


def test_the_same_transaction_is_never_refunded_twice():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    _service(org, fake, admin_id)
    for _ in range(2):
        with pytest.raises(RefundRejected):
            _service(org, fake, admin_id)
    assert len(fake.refunds) == 1


def test_a_refund_paddle_refused_is_recorded_failed_and_can_be_retried_once_fixed():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid), refund=PaddleAPIError("nope", 400))
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    db = SessionLocal()
    row = db.query(RefundRequest).filter(RefundRequest.transaction_id == fake.txn["id"]).one()
    assert row.status == "failed" and row.last_error == "paddle_status=400"
    db.close()
    fake.refund_result = None
    assert _service(org, fake, admin_id).status == "submitted" and len(fake.refunds) == 2


def test_no_time_window_or_blanket_denial_blocks_an_eligible_refund():
    """Eligibility is evidence-based. A refund for a long-completed transaction is still allowed, so refunds that
    law or the payment provider's terms require are never refused by Atla's own code."""
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn={**_txn(sid), "created_at": "2020-01-01T00:00:00Z"})
    assert _service(org, fake, admin_id, basis="legal_requirement").status == "submitted"


# ---------------- financial-safety review: retries, timeouts, races, webhooks ----------------

def _row(txn_id):
    db = SessionLocal()
    try:
        r = db.query(RefundRequest).filter(RefundRequest.transaction_id == txn_id).one()
        return r.status, r.provider_adjustment_id, r.attempts
    finally:
        db.close()


def _adj_webhook(adj, event_id=None, signature="auto"):
    body = {"event_id": event_id or f"evt_{uuid.uuid4().hex}", "event_type": "adjustment.updated",
            "occurred_at": "2026-10-02T00:00:00.000000Z", "data": adj}
    return paddle_post(body, signature=signature)


def test_the_database_itself_allows_only_one_refund_request_per_transaction():
    from sqlalchemy.exc import IntegrityError

    _c, org, _admin_id = _admin()
    db = SessionLocal()
    for i in range(2):
        db.add(RefundRequest(organization_id=uuid.UUID(org), transaction_id="txn_dupe_db", basis="goodwill", reason="x" * 12))
        if i == 0:
            db.commit()
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_concurrent_requests_for_one_transaction_reach_paddle_once():
    import threading

    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    fake.post_delay = 0.3
    results = []

    def go():
        try:
            results.append(_service(org, fake, admin_id).status)
        except RefundRejected:
            results.append("rejected")

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(fake.refunds) == 1 and results.count("rejected") == 5


def test_concurrent_retries_of_a_failed_request_reach_paddle_once():
    import threading

    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid), refund=PaddleAPIError("nope", 400))
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    fake.refund_result, fake.post_delay, fake.refunds = None, 0.3, []
    out = []

    def go():
        try:
            out.append(_service(org, fake, admin_id).status)
        except RefundRejected:
            out.append("rejected")

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(fake.refunds) == 1 and out.count("rejected") == 4


def test_paddle_accepts_but_our_call_times_out_then_a_retry_adopts_it_without_a_second_refund():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    fake.accept_then_timeout = 1
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    assert _row(fake.txn["id"])[0] == "unconfirmed" and len(fake.refunds) == 1
    assert _service(org, fake, admin_id).status == "submitted"
    assert len(fake.refunds) == 1  # asked Paddle first, found the adjustment, did NOT post again
    status, adj_id, _ = _row(fake.txn["id"])
    assert adj_id == fake.adjustments[0]["id"]


def test_a_timeout_where_paddle_did_nothing_is_safely_re_posted():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid), refund=PaddleAPIError("down", 503))
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    assert _row(fake.txn["id"])[0] == "unconfirmed"  # a 5xx is not proof it was refused
    fake.refund_result = None
    fake.adjustments.clear()
    assert _service(org, fake, admin_id).status == "submitted" and len(fake.refunds) == 2


def test_if_paddle_cannot_be_asked_an_unconfirmed_request_is_not_re_posted():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    fake.accept_then_timeout = 1
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    fake.list_fails = True
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    assert len(fake.refunds) == 1


def test_the_webhook_resolves_an_unconfirmed_request_whose_response_was_lost():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    fake.accept_then_timeout = 1
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    adj = {**fake.adjustments[0], "status": "approved", "totals": {"subtotal": "4900"}}
    assert _adj_webhook(adj).status_code in (200, 204)
    status, adj_id, _ = _row(fake.txn["id"])
    assert status == "approved" and adj_id == adj["id"]


def test_a_stale_requested_row_left_by_a_crash_is_recoverable_but_a_fresh_one_is_not():
    from datetime import datetime, timedelta, timezone

    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    db = SessionLocal()
    db.add(RefundRequest(organization_id=uuid.UUID(org), transaction_id=fake.txn["id"], basis="goodwill", reason="x" * 12, status="requested"))
    db.commit()
    db.close()
    with pytest.raises(RefundRejected):  # just written: another worker may be mid-call
        _service(org, fake, admin_id)
    db = SessionLocal()
    db.query(RefundRequest).filter(RefundRequest.transaction_id == fake.txn["id"]).update(
        {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=10)})
    db.commit()
    db.close()
    assert _service(org, fake, admin_id).status == "submitted" and len(fake.refunds) == 1


def test_a_rejected_refund_is_final_and_is_not_silently_re_requested():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    _service(org, fake, admin_id)
    adj = {**fake.adjustments[0], "status": "rejected"}
    _adj_webhook(adj)
    assert _row(fake.txn["id"])[0] == "rejected"
    with pytest.raises(RefundRejected):
        _service(org, fake, admin_id)
    assert len(fake.refunds) == 1


def test_adjustment_webhooks_need_a_valid_signature_are_deduplicated_and_never_regress():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid))
    _service(org, fake, admin_id)
    adj = {**fake.adjustments[0], "status": "approved", "totals": {"subtotal": "4900"}}
    assert _adj_webhook(adj, signature="ts=1;h1=forged").status_code == 401
    assert _adj_webhook(adj, signature=None).status_code == 401
    assert _row(fake.txn["id"])[0] == "submitted"  # forged / unsigned events changed nothing
    ev = f"evt_{uuid.uuid4().hex}"
    assert _adj_webhook(adj, event_id=ev).status_code in (200, 204)
    assert _adj_webhook(adj, event_id=ev).status_code == 204  # redelivery: deduplicated
    assert _row(fake.txn["id"])[0] == "approved"
    _adj_webhook({**adj, "status": "pending_approval"})  # an older event arriving late
    assert _row(fake.txn["id"])[0] == "approved"
    db = SessionLocal()
    rid = db.query(RefundRequest.id).filter(RefundRequest.transaction_id == fake.txn["id"]).scalar()
    assert db.query(AuditLog).filter(AuditLog.target_id == str(rid), AuditLog.action == "refund.approved").count() == 1
    db.close()


def test_an_adjustment_id_only_ever_moves_the_request_it_belongs_to():
    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    a, b = FakePaddle(txn=_txn(sid)), FakePaddle(txn=_txn(sid))
    _service(org, a, admin_id)
    _service(org, b, admin_id)
    _adj_webhook({**a.adjustments[0], "status": "approved"})
    assert _row(a.txn["id"])[0] == "approved" and _row(b.txn["id"])[0] == "submitted"
    # an unrelated dashboard refund (unknown adjustment id, unknown transaction) matches nothing
    _adj_webhook({"id": "adj_unrelated", "action": "refund", "status": "approved", "transaction_id": "txn_other"})
    assert _row(b.txn["id"])[0] == "submitted"


def test_platform_admin_is_enforced_by_the_server(monkeypatch):
    c, org, admin_id = _admin()
    sid = _subscribe(org)
    body = None
    fake = FakePaddle(txn=_txn(sid))
    body = {"transaction_id": fake.txn["id"], "basis": "goodwill", "reason": "a reason long enough"}
    assert TestClient(app).post("/api/v1/admin/refunds", json=body).status_code == 401  # no session at all
    db = SessionLocal()
    db.query(AdminUser).filter(AdminUser.user_id == admin_id).delete()  # admin rights revoked
    db.commit()
    db.close()
    _use(monkeypatch, fake)
    assert c.post("/api/v1/admin/refunds", json=body).status_code == 403 and fake.refunds == []


def test_a_retry_waits_on_the_row_lock_and_then_sees_the_winner(monkeypatch):
    """Deterministic proof of the row lock: while another transaction holds the refund row, a second request blocks
    (it cannot read 'failed' and post), and once the holder commits it sees the new state and is rejected."""
    import threading
    import time

    _c, org, admin_id = _admin()
    sid = _subscribe(org)
    fake = FakePaddle(txn=_txn(sid), refund=PaddleAPIError("nope", 400))
    with pytest.raises(PaddleAPIError):
        _service(org, fake, admin_id)
    fake.refund_result, fake.refunds = None, []

    holder = SessionLocal()
    row = holder.query(RefundRequest).filter(RefundRequest.transaction_id == fake.txn["id"]).with_for_update().one()
    outcome = []

    def second():
        try:
            outcome.append(_service(org, fake, admin_id).status)
        except RefundRejected:
            outcome.append("rejected")

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.7)
    assert outcome == [] and fake.refunds == []  # blocked on the lock
    row.status = "submitted"
    holder.commit()
    holder.close()
    t.join(5)
    assert outcome == ["rejected"] and fake.refunds == []
