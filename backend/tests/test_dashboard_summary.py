"""
Level 4 dashboard summary: real counts from the org's own calls and leads, on every plan, no invented numbers.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1 import dashboard
from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead, LeadStatus
from tests.billing_helpers import new_org, subscribe

NOW = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)  # 21:00 in Asia/Kolkata, same calendar day


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(dashboard, "_now", lambda: NOW)


def _call(org, at, seconds=60, direction=CallDirection.inbound):
    db = SessionLocal()
    try:
        db.add(
            Call(
                organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:12]}", direction=direction,
                status="ended", started_at=at, ended_at=at + timedelta(seconds=seconds), duration_seconds=seconds,
            )
        )
        db.commit()
    finally:
        db.close()


def _lead(org, at, status=LeadStatus.new, name="Maria"):
    db = SessionLocal()
    try:
        lead = Lead(organization_id=uuid.UUID(org), name=name, phone="+15551110000", reason="Cleaning", status=status)
        db.add(lead)
        db.flush()
        lead.created_at = at
        db.commit()
    finally:
        db.close()


def _get(c, org, **params):
    return c.get(f"/api/v1/orgs/{org}/dashboard/summary", params=params)


def test_empty_org_returns_true_zeros_with_a_full_set_of_buckets_for_every_range():
    c, org = new_org("dash")
    for rng, n, gran in (("today", 24, "hour"), ("7d", 7, "day"), ("30d", 30, "day")):
        body = _get(c, org, range=rng, tz="UTC").json()
        assert len(body["series"]) == n and body["granularity"] == gran
        assert body["totals"] == {"calls": 0, "call_seconds": 0, "leads": 0}
        assert body["previous"] == {"calls": 0, "call_seconds": 0, "leads": 0}
        assert body["leads_awaiting_follow_up"] == 0 and body["awaiting_leads"] == [] and body["oldest_unattended_lead_at"] is None
        assert all(b["calls"] == 0 and b["leads"] == 0 for b in body["series"])


def test_counts_calls_minutes_and_leads_and_excludes_test_calls():
    c, org = new_org("dash")
    _call(org, NOW - timedelta(hours=1), 120)
    _call(org, NOW - timedelta(hours=2), 30)
    _call(org, NOW - timedelta(hours=1), 600, direction=CallDirection.test)  # a test call is not customer traffic
    _lead(org, NOW - timedelta(hours=1))
    body = _get(c, org, range="today", tz="UTC").json()
    assert body["totals"] == {"calls": 2, "call_seconds": 150, "leads": 1}
    by_hour = {b["start"]: b for b in body["series"]}
    assert by_hour["2026-10-12T14:00:00"]["calls"] == 1 and by_hour["2026-10-12T14:00:00"]["call_seconds"] == 120
    assert by_hour["2026-10-12T14:00:00"]["leads"] == 1
    assert by_hour["2026-10-12T13:00:00"]["calls"] == 1


def test_ranges_are_nested_and_previous_window_is_real():
    c, org = new_org("dash")
    _call(org, NOW - timedelta(hours=1))                # today
    _call(org, NOW - timedelta(days=3))                 # inside 7d
    _call(org, NOW - timedelta(days=10))                # inside 30d, previous window of 7d
    _call(org, NOW - timedelta(days=40))                # previous window of 30d
    _call(org, NOW - timedelta(days=1, hours=1))        # yesterday: previous window of "today"
    today, week, month = (_get(c, org, range=r, tz="UTC").json() for r in ("today", "7d", "30d"))
    assert (today["totals"]["calls"], week["totals"]["calls"], month["totals"]["calls"]) == (1, 3, 4)
    assert today["previous"]["calls"] == 1
    assert week["previous"]["calls"] == 1   # the day-10 call
    assert month["previous"]["calls"] == 1  # the day-40 call


def test_the_day_boundary_follows_the_viewers_timezone():
    c, org = new_org("dash")
    _call(org, datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc))  # 01:30 on the 12th in Kolkata, still the 11th in UTC
    assert _get(c, org, range="today", tz="UTC").json()["totals"]["calls"] == 0
    kolkata = _get(c, org, range="today", tz="Asia/Kolkata").json()
    assert kolkata["totals"]["calls"] == 1
    assert {b["start"]: b["calls"] for b in kolkata["series"]}["2026-10-12T01:00:00"] == 1


def test_unknown_timezone_falls_back_to_utc_and_bad_range_is_rejected():
    c, org = new_org("dash")
    assert _get(c, org, range="7d", tz="Not/AZone").status_code == 200
    assert _get(c, org, range="90d").status_code == 422


def test_awaiting_leads_are_only_new_ones_oldest_first_and_capped():
    c, org = new_org("dash")
    for i in range(7):
        _lead(org, NOW - timedelta(days=i), name=f"Lead {i}")
    _lead(org, NOW - timedelta(days=20), status=LeadStatus.contacted, name="Done")
    body = _get(c, org, range="today", tz="UTC").json()
    assert body["leads_awaiting_follow_up"] == 7
    assert [l["name"] for l in body["awaiting_leads"]] == ["Lead 6", "Lead 5", "Lead 4", "Lead 3", "Lead 2"]
    assert body["oldest_unattended_lead_at"].startswith("2026-10-06")


def test_it_is_available_on_every_plan_with_or_without_a_subscription():
    c, org = new_org("dash")
    assert _get(c, org).status_code == 200  # no subscription
    subscribe(org, "starter")
    assert _get(c, org).status_code == 200
    subscribe(org, "growth")
    assert _get(c, org).status_code == 200


def test_one_org_never_sees_anothers_numbers():
    a, org_a = new_org("dash")
    b, org_b = new_org("dash")
    _call(org_a, NOW - timedelta(hours=1))
    _lead(org_a, NOW - timedelta(hours=1))
    mine = _get(b, org_b, range="30d", tz="UTC").json()
    assert mine["totals"]["calls"] == 0 and mine["leads_awaiting_follow_up"] == 0
    assert _get(b, org_a, range="30d").status_code == 404  # another org's id is not even confirmed to exist