"""
"Cancellation requested, awaiting confirmation" must be durable server state, not browser state: it survives a
page reload, never changes entitlement, is cleared only by provider-confirmed state, and a repeat request stays
idempotent (Paddle is never asked to cancel twice).
"""
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from tests.test_account import _acct
from tests.test_billing import SECRET, _event, _post as paddle_post
from tests.test_billing_cancel_safety import FakePaddle, _use

END = "2026-11-07T00:00:00Z"


@pytest.fixture
def acct(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_WEBHOOK_SECRET", SECRET)
    c, org, *_ = _acct()
    sid = f"sub_{uuid.uuid4().hex[:10]}"
    paddle_post(_event("subscription.created", sid, org, at="2026-10-01T00:00:00.000000Z"))
    return c, org, sid


def _sub_view(c, org):
    r = c.get(f"/api/v1/orgs/{org}/billing/subscription")
    assert r.status_code == 200
    return r.json()


def _row(org):
    db = SessionLocal()
    try:
        s = db.query(Subscription).filter_by(organization_id=uuid.UUID(org)).one()
        return s.status, s.cancel_effective_at, s.cancel_requested_at
    finally:
        db.close()


def test_accepted_request_is_pending_in_the_subscription_view_and_survives_reloads(acct, monkeypatch):
    c, org, _ = acct
    assert _sub_view(c, org)["cancel_requested_at"] is None
    # Paddle accepted the cancel, but its record does not show it yet (and the webhook hasn't arrived)
    fake = _use(monkeypatch, FakePaddle(remote={"status": "active", "scheduled_change": None}))
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.status_code == 202 and resp.json()["status"] == "requested" and fake.cancels == 1
    for _ in range(3):  # "reload": every GET reads the same server state
        v = _sub_view(c, org)
        assert v["cancel_requested_at"] is not None and v["cancel_effective_at"] is None
        assert v["status"] == "active" and v["entitled"] is True  # still entitled; nothing is confirmed
    status, effective, requested = _row(org)
    assert status == "active" and effective is None and requested is not None


def test_a_repeat_request_while_pending_does_not_cancel_twice_at_paddle_when_paddle_shows_it(acct, monkeypatch):
    c, org, _ = acct
    _use(monkeypatch, FakePaddle(remote={"status": "active", "scheduled_change": None}))
    c.post(f"/api/v1/orgs/{org}/billing/cancel")
    first_requested = _row(org)[2]
    scheduled = {"status": "active", "scheduled_change": {"action": "cancel", "effective_at": END}}
    fake = _use(monkeypatch, FakePaddle(remote=scheduled))
    r = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert r.status_code == 202 and r.json() == {"status": "already_scheduled", "effective_at": END}
    assert fake.cancels == 0  # idempotent: Paddle is not asked to cancel again
    assert _row(org)[2] == first_requested  # the original request time is kept


def test_webhook_confirmation_sets_the_date_and_clears_the_pending_marker(acct, monkeypatch):
    c, org, sid = acct
    _use(monkeypatch, FakePaddle(remote={"status": "active", "scheduled_change": None}))
    c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert _sub_view(c, org)["cancel_requested_at"] is not None
    ev = _event("subscription.updated", sid, org, at="2026-10-20T00:00:00.000000Z")
    ev["data"]["scheduled_change"] = {"action": "cancel", "effective_at": END}
    paddle_post(ev)
    v = _sub_view(c, org)
    assert v["cancel_effective_at"].startswith("2026-11-07") and v["cancel_requested_at"] is None
    assert _row(org)[2] is None


def test_an_unrelated_webhook_does_not_erase_the_pending_request(acct, monkeypatch):
    c, org, sid = acct
    _use(monkeypatch, FakePaddle(remote={"status": "active", "scheduled_change": None}))
    c.post(f"/api/v1/orgs/{org}/billing/cancel")
    paddle_post(_event("subscription.updated", sid, org, at="2026-10-20T00:00:00.000000Z"))  # no scheduled change in it
    v = _sub_view(c, org)
    assert v["cancel_requested_at"] is not None and v["cancel_effective_at"] is None  # still honest: not confirmed


def test_when_paddles_own_record_already_shows_the_cancel_it_is_applied_and_confirmed(acct, monkeypatch):
    c, org, sid = acct
    snapshot = {
        "id": sid, "status": "active", "updated_at": "2026-10-21T00:00:00Z",
        "scheduled_change": {"action": "cancel", "effective_at": END},
        "custom_data": {"organization_id": org, "plan_id": "starter"},
        "current_billing_period": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-11-01T00:00:00Z"},
    }
    fake = _use(monkeypatch, FakePaddle(remote=[snapshot, snapshot]))  # lookup before the cancel and after
    r = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert r.status_code == 202 and r.json() == {"status": "already_scheduled", "effective_at": END}
    assert fake.cancels == 0
    v = _sub_view(c, org)
    assert v["cancel_effective_at"].startswith("2026-11-07") and v["cancel_requested_at"] is None
    assert v["status"] == "active" and v["entitled"] is True  # keeps service until the date; no immediate cancel


def test_a_stale_paddle_snapshot_is_not_applied_but_the_request_is_still_recorded(acct, monkeypatch):
    c, org, sid = acct
    stale = {"id": sid, "status": "active", "updated_at": "2026-09-01T00:00:00Z", "scheduled_change": {"action": "cancel", "effective_at": END}}
    _use(monkeypatch, FakePaddle(remote=[stale, stale]))
    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").status_code == 202
    status, effective, requested = _row(org)
    assert effective is None and requested is not None  # older than local state: never overwrites it


def test_a_failed_cancel_records_nothing(acct, monkeypatch):
    from app.providers.billing.paddle_billing_provider import PaddleAPIError
    c, org, _ = acct
    _use(monkeypatch, FakePaddle(cancel=PaddleAPIError("x", 500), remote={"status": "active", "scheduled_change": None}))
    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").status_code == 502
    assert _sub_view(c, org)["cancel_requested_at"] is None
