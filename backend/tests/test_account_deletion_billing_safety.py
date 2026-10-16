"""
Account deletion must never purge data unless billing is PROVEN stopped.

A failed cancel is only acceptable if Paddle's own record of the subscription says "canceled".
No HTTP status code is trusted on its own (401/403 = bad credentials, 404 = maybe the wrong
environment, 5xx / network = unknown), and one unverifiable subscription out of several blocks the purge.
"""
import uuid

import pytest

from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.db.models.tenancy import Organization
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.providers.billing.billing_provider import BillingProvider
from app.providers.billing.paddle_billing_provider import PaddleAPIError
from app.services.account_deletion import AccountDeletionBlocked, delete_organization
from tests.fakes import FakePhone
from app.config import get_settings
from tests.test_account import _fully_populated_org, _rows
from tests.test_call_pipeline import KEY
from tests.test_agent_config import FakeRetell
from tests.test_phone_lifecycle import _subscribe

OK = "ok"


@pytest.fixture(autouse=True)
def _retell_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "RETELL_API_KEY", KEY)  # the seeded org's call arrives through the signed Retell webhook


class FakePaddle(BillingProvider):
    """cancel[ext] = OK or an exception to raise; remote[ext] = what Paddle's GET says (dict / None / exception)."""

    def __init__(self):
        self.cancel: dict[str, object] = {}
        self.remote: dict[str, object] = {}
        self.cancel_calls: list[str] = []
        self.lookups: list[str] = []

    def create_checkout_session(self, **kw):  # pragma: no cover
        raise NotImplementedError

    def get_management_urls(self, ext):  # pragma: no cover
        return {}

    def cancel_subscription(self, ext, immediately=False):
        assert immediately is True
        self.cancel_calls.append(ext)
        outcome = self.cancel.get(ext, OK)
        if outcome != OK:
            raise outcome

    def get_subscription(self, ext):
        self.lookups.append(ext)
        outcome = self.remote.get(ext)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run(org, billing, phones=None):
    db = SessionLocal()
    try:
        delete_organization(
            db, uuid.UUID(org), billing=billing, phones=phones or FakePhone(), agents=RetellAgentProvider(client=FakeRetell())
        )
    finally:
        db.close()


def _intact(org):
    db = SessionLocal()
    try:
        return db.get(Organization, uuid.UUID(org)) is not None and _rows(org, "subscriptions") >= 1 and _rows(org, "calls") == 1
    finally:
        db.close()


def _gone(org):
    db = SessionLocal()
    try:
        return db.get(Organization, uuid.UUID(org)) is None and _rows(org, "subscriptions") == 0 and _rows(org, "calls") == 0
    finally:
        db.close()


def _sub_ids(org):
    db = SessionLocal()
    try:
        return sorted(s.external_subscription_id for s in db.query(Subscription).filter_by(organization_id=uuid.UUID(org)).all())
    finally:
        db.close()


def _blocked(org, billing):
    phones = FakePhone()
    with pytest.raises(AccountDeletionBlocked):
        _run(org, billing, phones)
    assert _intact(org), "organization data must survive a blocked deletion"
    assert phones.releases == [], "phone numbers must not be released while billing is unverified"


# ---------------- success ----------------

def test_successful_cancellation_permits_deletion():
    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = FakePaddle()
    phones = FakePhone()
    _run(org, billing, phones)
    assert billing.cancel_calls == [sub] and billing.lookups == []  # a 2xx cancel needs no second opinion
    assert _gone(org) and len(phones.releases) == 1


# ---------------- unverified failures block ----------------

def test_a_401_authentication_error_blocks_deletion_even_when_the_lookup_fails_too():
    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[sub] = PaddleAPIError("Paddle returned 401", 401)
    billing.remote[sub] = PaddleAPIError("Paddle returned 401", 401)  # same bad key: cannot verify anything
    _blocked(org, billing)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
@pytest.mark.parametrize("lookup", ["still_active", "not_found", "lookup_error"])
def test_any_4xx_blocks_unless_paddle_says_canceled(status, lookup):
    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[sub] = PaddleAPIError(f"Paddle returned {status}", status)
    billing.remote[sub] = {"still_active": {"id": sub, "status": "active"}, "not_found": None, "lookup_error": PaddleAPIError("x", 403)}[lookup]
    _blocked(org, billing)  # 404 / "no such subscription" is NOT proof: it may be the wrong Paddle environment


@pytest.mark.parametrize("status", [500, 502, 503, 0])  # 0 = could not reach Paddle at all
def test_network_and_5xx_errors_block_deletion(status):
    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[sub] = PaddleAPIError("Could not reach Paddle" if status == 0 else f"Paddle returned {status}", status)
    billing.remote[sub] = PaddleAPIError("Could not reach Paddle", 0)
    _blocked(org, billing)


@pytest.mark.parametrize("remote_status", ["active", "past_due", "paused", "trialing", None, ""])
def test_a_remote_record_that_is_not_canceled_never_counts(remote_status):
    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[sub] = PaddleAPIError("Paddle returned 409", 409)
    billing.remote[sub] = {"id": sub, "status": remote_status}
    _blocked(org, billing)


def test_a_provider_that_cannot_look_subscriptions_up_fails_closed():
    class NoLookup(FakePaddle):
        get_subscription = BillingProvider.get_subscription  # the interface default: NotImplementedError

    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = NoLookup()
    billing.cancel[sub] = PaddleAPIError("Paddle returned 409", 409)
    _blocked(org, billing)


# ---------------- genuinely already cancelled ----------------

@pytest.mark.parametrize("status", [404, 409, 422, 500, 0])
def test_a_subscription_paddle_confirms_is_canceled_is_handled_safely(status):
    _c, org, *_ = _fully_populated_org()
    (sub,) = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[sub] = PaddleAPIError("cannot cancel an already canceled subscription", status)
    billing.remote[sub] = {"id": sub, "status": "canceled"}  # Paddle's own record
    phones = FakePhone()
    _run(org, billing, phones)
    assert billing.lookups == [sub]
    assert _gone(org) and len(phones.releases) == 1


# ---------------- several subscriptions ----------------

def test_one_unverifiable_subscription_among_several_prevents_the_purge_and_a_retry_completes():
    _c, org, *_ = _fully_populated_org()
    extra = f"sub_{uuid.uuid4().hex[:10]}"
    _subscribe(org, "active", sub_id=extra)
    first, second = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[second] = PaddleAPIError("Paddle returned 500", 500)
    billing.remote[second] = PaddleAPIError("Could not reach Paddle", 0)

    _blocked(org, billing)
    assert sorted(billing.cancel_calls) == [first, second]  # the healthy one was still cancelled, the bad one tried
    assert _sub_ids(org) == [first, second]  # local rows untouched so the retry knows what to do

    billing.cancel[second] = OK  # Paddle recovers; the first now reports "already canceled"
    billing.cancel[first] = PaddleAPIError("already canceled", 409)
    billing.remote[first] = {"id": first, "status": "canceled"}
    _run(org, billing)
    assert _gone(org)


def test_a_verified_cancelled_subscription_does_not_excuse_an_unverified_one():
    _c, org, *_ = _fully_populated_org()
    _subscribe(org, "active", sub_id=f"sub_{uuid.uuid4().hex[:10]}")
    first, second = _sub_ids(org)
    billing = FakePaddle()
    billing.cancel[first] = PaddleAPIError("already canceled", 409)
    billing.remote[first] = {"id": first, "status": "canceled"}
    billing.cancel[second] = PaddleAPIError("Paddle returned 401", 401)
    billing.remote[second] = PaddleAPIError("Paddle returned 401", 401)
    _blocked(org, billing)
    assert sorted(billing.lookups) == [first, second]


def test_all_of_several_subscriptions_cancelled_permits_deletion():
    _c, org, *_ = _fully_populated_org()
    _subscribe(org, "past_due", sub_id=f"sub_{uuid.uuid4().hex[:10]}")
    billing = FakePaddle()
    _run(org, billing)
    assert len(billing.cancel_calls) == 2 and _gone(org)
