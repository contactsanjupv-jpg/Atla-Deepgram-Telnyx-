"""
Level 5 Calls: filtering, search, sorting, paging and lead links over the organization's own calls.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead, LeadStatus
from tests.billing_helpers import new_org, subscribe

T0 = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)


def _call(org, at, *, seconds=60, direction=CallDirection.inbound, status="ended", caller="+13055551234",
          summary=None, sentiment=None, disconnect="user_hangup"):
    db = SessionLocal()
    try:
        c = Call(
            organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:12]}", direction=direction,
            status=status, caller_number=caller, started_at=at, ended_at=at + timedelta(seconds=seconds or 0),
            duration_seconds=seconds, summary=summary, sentiment=sentiment, disconnect_reason=disconnect,
        )
        db.add(c)
        db.commit()
        return str(c.id)
    finally:
        db.close()


def _lead_for(org, call_id, name="Maria", status=LeadStatus.new):
    db = SessionLocal()
    try:
        db.add(Lead(organization_id=uuid.UUID(org), call_id=uuid.UUID(call_id), name=name, phone="+13055551234", status=status))
        db.commit()
    finally:
        db.close()


def _browse(c, org, **params):
    return c.get(f"/api/v1/orgs/{org}/calls/browse", params=params)


def test_empty_org_returns_an_empty_page_not_an_error():
    c, org = new_org("cl")
    body = _browse(c, org).json()
    assert body == {"items": [], "total": 0, "total_seconds": 0, "leads_captured": 0, "limit": 25, "offset": 0}


def test_default_is_newest_first_and_totals_cover_the_whole_filtered_set_not_just_the_page():
    c, org = new_org("cl")
    for i in range(5):
        _call(org, T0 - timedelta(hours=i), seconds=100 + i)
    body = _browse(c, org, limit=2).json()
    assert [r["duration_seconds"] for r in body["items"]] == [100, 101]
    assert body["total"] == 5 and body["total_seconds"] == 510 and len(body["items"]) == 2
    page2 = _browse(c, org, limit=2, offset=2).json()
    assert [r["duration_seconds"] for r in page2["items"]] == [102, 103]
    assert _browse(c, org, limit=2, offset=4).json()["items"][0]["duration_seconds"] == 104


def test_sorting_oldest_and_longest():
    c, org = new_org("cl")
    _call(org, T0 - timedelta(hours=3), seconds=30)
    _call(org, T0 - timedelta(hours=2), seconds=500)
    _call(org, T0 - timedelta(hours=1), seconds=None, status="ongoing")
    oldest = [r["duration_seconds"] for r in _browse(c, org, sort="oldest").json()["items"]]
    assert oldest == [30, 500, None]
    longest = [r["duration_seconds"] for r in _browse(c, org, sort="longest").json()["items"]]
    assert longest == [500, 30, None]  # a call with no duration yet sorts last


def test_filters_direction_status_sentiment():
    c, org = new_org("cl")
    _call(org, T0, direction=CallDirection.inbound, sentiment="Positive")
    _call(org, T0, direction=CallDirection.outbound, sentiment="Negative")
    _call(org, T0, direction=CallDirection.test)
    _call(org, T0, status="error", disconnect="error_llm_websocket_open")
    assert _browse(c, org).json()["total"] == 4
    assert _browse(c, org, direction="test").json()["total"] == 1
    assert _browse(c, org, direction="outbound").json()["total"] == 1
    assert _browse(c, org, status="error").json()["total"] == 1
    assert _browse(c, org, sentiment="positive").json()["total"] == 1  # stored as Retell's "Positive"
    assert _browse(c, org, sentiment="negative", direction="outbound").json()["total"] == 1
    assert _browse(c, org, sentiment="negative", direction="inbound").json()["total"] == 0


def test_invalid_filter_values_are_rejected():
    c, org = new_org("cl")
    for bad in ({"direction": "sideways"}, {"status": "weird"}, {"sentiment": "angry"}, {"sort": "random"}, {"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert _browse(c, org, **bad).status_code == 422, bad


def test_date_range_is_inclusive_and_follows_the_viewers_timezone():
    c, org = new_org("cl")
    _call(org, datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc))   # 01:30 on the 12th in Kolkata, the 11th in UTC
    _call(org, datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc))
    _call(org, datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc))
    day = {"date_from": "2026-10-12", "date_to": "2026-10-12"}
    assert _browse(c, org, tz="UTC", **day).json()["total"] == 1
    assert _browse(c, org, tz="Asia/Kolkata", **day).json()["total"] == 2
    assert _browse(c, org, tz="UTC", date_from="2026-10-12").json()["total"] == 2   # open-ended
    assert _browse(c, org, tz="UTC", date_to="2026-10-11").json()["total"] == 1
    assert _browse(c, org, date_from="2026-10-13", date_to="2026-10-12").status_code == 422


def test_search_matches_summary_and_phone_numbers_in_any_format_and_wildcards_are_literal():
    c, org = new_org("cl")
    _call(org, T0, caller="+13055551234", summary="Asked about teeth whitening pricing.")
    _call(org, T0, caller="+14155559999", summary="Wants to reschedule; 100% sure.")
    _call(org, T0, caller=None, summary=None)
    assert _browse(c, org, q="whitening").json()["total"] == 1
    assert _browse(c, org, q="WHITENING").json()["total"] == 1
    assert _browse(c, org, q="(305) 555").json()["total"] == 1       # formatted number finds the stored E.164
    assert _browse(c, org, q="415555").json()["total"] == 1
    assert _browse(c, org, q="+1305").json()["total"] == 1
    assert _browse(c, org, q="100%").json()["total"] == 1             # % is not a wildcard
    assert _browse(c, org, q="%%").json()["total"] == 0
    assert _browse(c, org, q="a_").json()["total"] == 0               # _ is not a wildcard either
    assert _browse(c, org, q="nothing like this").json()["total"] == 0
    assert _browse(c, org, q="x").status_code == 422                  # too short to be useful
    assert _browse(c, org, q="   ").json()["total"] == 3              # blank search = no search


def test_leads_are_linked_to_their_call_and_filterable():
    c, org = new_org("cl")
    with_lead = _call(org, T0 - timedelta(hours=1))
    _call(org, T0 - timedelta(hours=2))
    _lead_for(org, with_lead, name="Maria", status=LeadStatus.contacted)
    body = _browse(c, org).json()
    first, second = body["items"]
    assert first["lead_name"] == "Maria" and first["lead_status"] == "contacted" and first["lead_id"]
    assert second["lead_id"] is None and second["lead_name"] is None
    assert body["leads_captured"] == 1
    assert _browse(c, org, has_lead="true").json()["total"] == 1
    assert _browse(c, org, has_lead="false").json()["total"] == 1
    assert _browse(c, org, has_lead="true").json()["items"][0]["id"] == with_lead
    detail = c.get(f"/api/v1/orgs/{org}/calls/{with_lead}").json()
    assert detail["lead_name"] == "Maria" and detail["duration_seconds"] == 60


def test_available_on_every_plan_and_never_returns_transcripts_or_cost():
    c, org = new_org("cl")
    _call(org, T0, summary="hello")
    for plan in (None, "starter", "growth"):
        if plan:
            subscribe(org, plan)
        resp = _browse(c, org)
        assert resp.status_code == 200
        text = resp.text
        assert "transcript" not in text and "cost" not in text and "recording" not in text


def test_one_org_never_sees_anothers_calls_or_leads():
    a, org_a = new_org("cl")
    b, org_b = new_org("cl")
    cid = _call(org_a, T0, summary="secret roof repair")
    _lead_for(org_a, cid)
    assert _browse(b, org_b).json()["total"] == 0
    assert _browse(b, org_b, q="secret").json()["total"] == 0
    assert _browse(b, org_a).status_code == 404
    assert b.get(f"/api/v1/orgs/{org_a}/calls/{cid}").status_code == 404
    assert b.get(f"/api/v1/orgs/{org_b}/calls/{cid}").status_code == 404  # a real id from another org, asked through my own org


def test_the_old_list_endpoint_is_unchanged():
    c, org = new_org("cl")
    _call(org, T0)
    body = c.get(f"/api/v1/orgs/{org}/calls").json()
    assert isinstance(body, list) and len(body) == 1 and "lead_id" not in body[0]
