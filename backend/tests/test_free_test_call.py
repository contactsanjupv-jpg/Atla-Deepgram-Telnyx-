"""
One free test call per prospect, enforced on the server; paid test calls obey the credit rules.
Drives the real HTTP route with the real Postgres; only Retell's network boundary is faked.
"""
import threading
import uuid
from datetime import timedelta

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import FreeTestAllowance, Subscription, UsageLedgerEntry
from app.db.models.calls import Call
from app.providers.call.retell_call_provider import RetellCallProvider
from app.providers.retell_client import RetellAPIError
from app.services import free_test
from app.services.plans import PLANS, Plan
from tests.billing_helpers import add_usage, new_org, now, subscribe
from tests.test_agent_config import _agent_id, _body, _put, _seed_business, fake  # noqa: F401  (fake is a fixture)
from tests.test_call_pipeline import _call, _post as retell_post, _seed

settings = get_settings()
FREE_MESSAGE = "You've used your free test call. Choose a plan to test your receptionist again."


@pytest.fixture(autouse=True)
def _one_free_call(monkeypatch):
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 1)
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 1000)
    monkeypatch.setattr(settings, "RETELL_API_KEY", "test-retell-key")


def _ready_org():
    c, org = new_org("ft")
    biz, voice = _seed_business(org)
    assert _put(c, org, biz, _body(voice)).status_code == 200
    return c, org, f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call"


def _allowances(org):
    db = SessionLocal()
    try:
        return db.query(FreeTestAllowance).filter_by(organization_id=uuid.UUID(org)).count()
    finally:
        db.close()


# ---------------- the free call ----------------

def test_a_new_prospect_gets_exactly_one_free_call_and_is_told_it_is_free(fake):
    c, org, url = _ready_org()
    first = c.post(url)
    assert first.status_code == 200 and first.json()["is_free"] is True
    second = c.post(url)
    assert second.status_code == 402 and second.json()["detail"] == FREE_MESSAGE
    assert len(fake.to("/v2/create-web-call")) == 1  # the refused attempt cost nothing


def test_repeated_requests_and_refreshes_never_reset_the_allowance(fake):
    c, org, url = _ready_org()
    assert c.post(url).status_code == 200
    for _ in range(5):
        assert c.post(url).status_code == 402
    assert _allowances(org) == 1
    assert c.post(url).status_code == 402
    assert len(fake.to("/v2/create-web-call")) == 1


def test_concurrent_requests_cannot_spend_the_free_call_twice(fake):
    c, org, url = _ready_org()
    codes, gate = [], threading.Barrier(8)

    def go():
        gate.wait()
        codes.append(c.post(url).status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes).count(200) == 1 and codes.count(402) == 7, codes
    assert len(fake.to("/v2/create-web-call")) == 1 and _allowances(org) == 1


def test_the_same_user_cannot_collect_a_free_call_in_a_second_organization():
    _c1, org1 = new_org("ft")
    _c2, org2 = new_org("ft")
    db = SessionLocal()
    try:
        user = uuid.uuid4()
        assert free_test.reserve(db, uuid.UUID(org1), user) is not None
        db.commit()
        assert free_test.reserve(db, uuid.UUID(org2), user) is None  # one per person, not just per organization
        db.rollback()
    finally:
        db.close()


def test_the_allowance_is_per_organization_so_one_tenant_cannot_spend_anothers(fake):
    c1, org1, url1 = _ready_org()
    c2, org2, url2 = _ready_org()
    assert c1.post(url1).status_code == 200
    assert c2.post(url2).status_code == 200  # a different prospect still has theirs
    assert c1.post(url2).status_code == 404  # and cannot touch the other org's receptionist


def test_concurrent_reservations_in_the_database_yield_one_winner():
    _c, org = new_org("ft")
    user = uuid.uuid4()
    wins, gate = [], threading.Barrier(10)

    def go():
        db = SessionLocal()
        try:
            gate.wait()
            row = free_test.reserve(db, uuid.UUID(org), user)
            db.commit()
            wins.append(row is not None)
        finally:
            db.close()

    threads = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_a_call_the_provider_definitely_refused_gives_the_free_call_back(fake):
    c, org, url = _ready_org()
    fake.fail["/v2/create-web-call"] = 1
    assert c.post(url).status_code == 502
    assert _allowances(org) == 0
    assert c.post(url).status_code == 200  # nothing was spent, so they still have their free call


def test_a_provider_timeout_keeps_the_free_call_spent_because_the_call_may_exist(fake, monkeypatch):
    c, org, url = _ready_org()

    def ambiguous(self, *a, **k):
        raise RetellAPIError(0, "No response from Retell", ambiguous=True)

    monkeypatch.setattr(RetellCallProvider, "create_test_call", ambiguous)
    assert c.post(url).status_code == 502
    assert _allowances(org) == 1  # we would rather lose a retry than pay for two calls


def test_a_free_call_that_never_connected_is_returned(fake):
    c, org, url = _ready_org()
    call_id = c.post(url).json()["call_id"]
    agent = fake.ids["agent"]
    retell_post("call_ended", _call(call_id, agent, None, call_type="web_call", direction=None, from_number=None, duration_ms=0, end_timestamp=1_800_000_000_000))
    assert _allowances(org) == 0
    assert c.post(url).status_code == 200


def test_free_calls_are_recorded_distinctly_with_their_real_cost_and_never_charged(fake):
    c, org, url = _ready_org()
    call_id = c.post(url).json()["call_id"]
    retell_post("call_ended", _call(call_id, fake.ids["agent"], None, call_type="web_call", direction=None, from_number=None, call_cost={"combined_cost": 12.13}))
    db = SessionLocal()
    row = db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one()
    assert row.kind == "free_test" and float(row.provider_cost) == 12.13  # cost kept for margin tracking
    db.close()
    usage = c.get(f"/api/v1/orgs/{org}/usage/credits")
    assert usage.status_code == 200 and usage.json()["free_test_calls_remaining"] == 0


# ---------------- paid customers ----------------

def test_a_subscriber_tests_against_their_credits_and_the_call_is_not_free(fake):
    c, org, url = _ready_org()
    subscribe(org)
    resp = c.post(url)
    assert resp.status_code == 200 and resp.json()["is_free"] is False
    assert _allowances(org) == 0  # the free allowance is untouched, not consumed by a paying customer
    call_id = resp.json()["call_id"]
    retell_post("call_ended", _call(call_id, fake.ids["agent"], None, call_type="web_call", direction=None, from_number=None, duration_ms=90_000))
    db = SessionLocal()
    assert db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one().kind == "test"  # chargeable
    db.close()


def test_a_subscriber_with_no_credits_left_cannot_start_a_test_call(fake, monkeypatch):
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 1))
    c, org, url = _ready_org()
    subscribe(org)
    add_usage(org, 10 * 60)  # spent the 1 included minute and more
    resp = c.post(url)
    assert resp.status_code == 402 and "out of credits" in resp.json()["detail"].lower()
    assert len(fake.to("/v2/create-web-call")) == 0


def test_two_simultaneous_test_calls_cannot_spend_the_same_last_minutes(fake, monkeypatch):
    minutes = settings.TEST_CALL_MAX_SECONDS // 60  # exactly enough for ONE full-length call
    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, minutes))
    c, org, url = _ready_org()
    subscribe(org)
    codes, gate = [], threading.Barrier(4)

    def go():
        gate.wait()
        codes.append(c.post(url).status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == 1 and codes.count(402) == 3, codes


def test_a_lapsed_subscription_cannot_test_and_does_not_get_the_free_call_back(fake):
    c, org, url = _ready_org()
    subscribe(org, status="past_due")
    resp = c.post(url)
    assert resp.status_code == 402 and "billing" in resp.json()["detail"].lower()
    assert _allowances(org) == 0 and len(fake.to("/v2/create-web-call")) == 0


def test_a_member_of_another_organization_cannot_start_my_test_call(fake):
    _c, org, url = _ready_org()
    other, _org2 = new_org("ft")
    assert other.post(url).status_code == 404
