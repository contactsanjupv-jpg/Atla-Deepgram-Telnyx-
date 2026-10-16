"""
The customer-facing cancel route (POST /billing/cancel).

It must never report success it cannot back up, and must never change local state itself (state follows
Paddle's webhook). A cancel call that FAILED on our side but is IN EFFECT at Paddle (lost response, timeout, a
retry refused because it is already scheduled) is reported as such - confirmed from Paddle's own record only.
"""
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.providers.billing.paddle_billing_provider import PaddleAPIError
from tests.test_account import _acct
from tests.test_billing import SECRET, _event, _post as paddle_post

OK = "ok"


class FakePaddle:
    def __init__(self, cancel=OK, remote=None):
        self.cancel, self.remote, self.cancels, self.lookups = cancel, remote, 0, 0

    def cancel_subscription(self, ext, immediately=False):
        assert immediately is False  # the customer keeps what they paid for
        self.cancels += 1
        if self.cancel != OK:
            raise self.cancel

    def get_subscription(self, ext):
        self.lookups += 1
        remote = self.remote.pop(0) if isinstance(self.remote, list) else self.remote  # a list = one answer per lookup
        if isinstance(remote, Exception):
            raise remote
        return remote


@pytest.fixture
def org_with_sub(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_WEBHOOK_SECRET", SECRET)
    c, org, *_ = _acct()
    paddle_post(_event("subscription.created", f"sub_{uuid.uuid4().hex[:10]}", org, at="2026-10-01T00:00:00.000000Z"))
    return c, org


def _use(monkeypatch, fake):
    monkeypatch.setattr("app.api.v1.billing.PaddleBillingProvider", lambda: fake)
    return fake


def _local(org):
    db = SessionLocal()
    try:
        s = db.query(Subscription).filter_by(organization_id=uuid.UUID(org)).one()
        return s.status, s.cancel_effective_at
    finally:
        db.close()


def test_a_successful_cancel_is_requested_and_changes_no_local_state(org_with_sub, monkeypatch):
    c, org = org_with_sub
    before = _local(org)
    fake = _use(monkeypatch, FakePaddle())
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.status_code == 202 and resp.json() == {"status": "requested", "effective_at": None} and fake.cancels == 1 and fake.lookups == 2
    assert _local(org) == before  # only Paddle's webhook changes it


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429, 500, 503, 0])
@pytest.mark.parametrize("remote", ["active_not_scheduled", "no_record", "lookup_fails"])
def test_a_failed_cancel_is_reported_as_failed_unless_paddle_confirms_it(org_with_sub, monkeypatch, status, remote):
    c, org = org_with_sub
    before = _local(org)
    remote_value = {
        "active_not_scheduled": {"status": "active", "scheduled_change": None},
        "no_record": None,
        "lookup_fails": PaddleAPIError("Paddle returned 401", 401),
    }[remote]
    _use(monkeypatch, FakePaddle(cancel=PaddleAPIError("failed", status), remote=remote_value))
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.status_code == 502 and "paddle" not in resp.text.lower()
    assert _local(org) == before


@pytest.mark.parametrize("status", [0, 409, 500])  # lost response, refused retry, 5xx after it was applied
def test_a_cancel_that_is_in_effect_at_paddle_is_not_reported_as_a_failure(org_with_sub, monkeypatch, status):
    c, org = org_with_sub
    before = _local(org)
    remote = {"status": "active", "scheduled_change": {"action": "cancel", "effective_at": "2026-11-01T00:00:00Z"}}
    # Paddle's record is checked first, so an existing schedule is confirmed WITHOUT another cancel call.
    fake = _use(monkeypatch, FakePaddle(cancel=PaddleAPIError("response lost", status), remote=remote))
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.status_code == 202 and resp.json() == {"status": "already_scheduled", "effective_at": "2026-11-01T00:00:00Z"}
    assert fake.cancels == 0 and fake.lookups == 1 and _local(org) == before


@pytest.mark.parametrize("status", [0, 409, 500])
def test_a_cancel_that_failed_but_is_then_confirmed_scheduled_is_not_a_failure(org_with_sub, monkeypatch, status):
    c, org = org_with_sub
    scheduled = {"status": "active", "scheduled_change": {"action": "cancel", "effective_at": "2026-11-01T00:00:00Z"}}
    fake = _use(monkeypatch, FakePaddle(cancel=PaddleAPIError("response lost", status), remote=[None, scheduled]))
    resp = c.post(f"/api/v1/orgs/{org}/billing/cancel")
    assert resp.status_code == 202 and resp.json() == {"status": "already_scheduled", "effective_at": "2026-11-01T00:00:00Z"}
    assert fake.cancels == 1 and fake.lookups == 2


def test_a_subscription_paddle_reports_canceled_is_not_reported_as_a_failure(org_with_sub, monkeypatch):
    c, org = org_with_sub
    fake = _use(monkeypatch, FakePaddle(cancel=PaddleAPIError("cannot cancel", 409), remote={"status": "canceled"}))
    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").json() == {"status": "already_scheduled", "effective_at": None}
    assert fake.cancels == 0


def test_a_scheduled_change_that_is_not_a_cancel_does_not_count(org_with_sub, monkeypatch):
    c, org = org_with_sub
    remote = {"status": "active", "scheduled_change": {"action": "pause", "effective_at": "2026-11-01T00:00:00Z"}}
    _use(monkeypatch, FakePaddle(cancel=PaddleAPIError("x", 500), remote=remote))
    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").status_code == 502
