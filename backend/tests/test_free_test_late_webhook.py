"""
A call admitted through the FREE test flow stays free even if its webhook arrives after the customer subscribes.
Regression: usage was classified when the webhook was INGESTED (subscribed now? -> chargeable "test"), so a delayed or
retried webhook for a free call, delivered after checkout, was charged against the customer's included minutes.
"""
import time
import uuid

from app.db.base import SessionLocal
from app.db.models.billing import FreeTestAllowance, UsageLedgerEntry
from app.services.credits import credit_summary
from tests.billing_helpers import subscribe
from tests.test_agent_config import fake  # noqa: F401  (fixture)
from tests.test_free_test_call import _allowances, _one_free_call, _ready_org  # noqa: F401  (fixture + helpers)
from tests.test_call_pipeline import _analysis, _call, _post as retell_post


def _web(call_id, agent, **over):
    # Timestamps are "now" so the call falls inside the current billing period (and so would be charged if chargeable).
    end = int(time.time() * 1000)
    base = {"start_timestamp": end - 90_000, "end_timestamp": end}
    base.update(over)
    return _call(call_id, agent, None, call_type="web_call", direction=None, from_number=None, **base)


def _usage(call_id):
    db = SessionLocal()
    try:
        return [(r.kind, r.billable_seconds) for r in db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).all()]
    finally:
        db.close()


def _used(org):
    db = SessionLocal()
    try:
        return credit_summary(db, uuid.UUID(org)).included_used_seconds
    finally:
        db.close()


def test_a_free_call_whose_webhook_arrives_after_subscribing_is_still_free_and_not_charged(fake):
    c, org, url = _ready_org()
    resp = c.post(url)
    assert resp.json()["is_free"] is True
    call_id = resp.json()["call_id"]
    subscribe(org)  # the customer subscribes BEFORE Retell's call_ended webhook is processed
    retell_post("call_ended", _web(call_id, fake.ids["agent"], duration_ms=90_000, call_cost={"combined_cost": 9.5}))
    assert _usage(call_id) == [("free_test", 90)]
    assert _used(org) == 0  # none of the customer's included minutes were spent
    db = SessionLocal()
    row = db.query(UsageLedgerEntry).filter_by(retell_call_id=call_id).one()
    assert float(row.provider_cost) == 9.5 and row.period_start is None  # real cost kept, nothing attributed to the plan
    db.close()


def test_retries_and_the_analyzed_event_after_subscribing_keep_it_free(fake):
    c, org, url = _ready_org()
    call_id = c.post(url).json()["call_id"]
    retell_post("call_ended", _web(call_id, fake.ids["agent"], duration_ms=60_000))  # processed before subscribing
    subscribe(org)
    retell_post("call_ended", _web(call_id, fake.ids["agent"], duration_ms=60_000))  # Retell retries it
    retell_post("call_analyzed", _web(call_id, fake.ids["agent"], duration_ms=60_000, call_analysis=_analysis(False)))
    assert _usage(call_id) == [("free_test", 60)] and _used(org) == 0


def test_the_first_event_may_be_call_analyzed_after_subscribing(fake):
    c, org, url = _ready_org()
    call_id = c.post(url).json()["call_id"]
    subscribe(org)
    retell_post("call_analyzed", _web(call_id, fake.ids["agent"], duration_ms=45_000, call_analysis=_analysis(False)))
    assert _usage(call_id) == [("free_test", 45)] and _used(org) == 0


def test_a_free_call_that_never_connected_is_still_returned_when_its_webhook_is_late(fake):
    c, org, url = _ready_org()
    call_id = c.post(url).json()["call_id"]
    subscribe(org)
    retell_post("call_ended", _web(call_id, fake.ids["agent"], duration_ms=0, end_timestamp=1_800_000_000_000))
    assert _usage(call_id) == [("free_test", 0)] and _allowances(org) == 0


def test_a_call_admitted_as_a_paid_test_is_still_charged(fake):
    c, org, url = _ready_org()
    subscribe(org)
    resp = c.post(url)
    assert resp.json()["is_free"] is False
    call_id = resp.json()["call_id"]
    retell_post("call_ended", _web(call_id, fake.ids["agent"], duration_ms=90_000))
    assert _usage(call_id) == [("test", 90)] and _used(org) == 90


def test_the_free_allowance_row_is_what_marks_the_call_not_the_subscription(fake):
    c, org, url = _ready_org()
    call_id = c.post(url).json()["call_id"]
    db = SessionLocal()
    assert db.query(FreeTestAllowance).filter_by(retell_call_id=call_id).count() == 1
    db.close()