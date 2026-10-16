"""
Level 7 Leads: search, status filter, paging, the call each lead came from, status updates and tenant isolation.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead, LeadStatus
from tests.billing_helpers import new_org, subscribe

T0 = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)


def _call(org, at=T0, seconds=95):
    db = SessionLocal()
    try:
        c = Call(organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:12]}", direction=CallDirection.inbound,
                 status="ended", caller_number="+13055550000", started_at=at, duration_seconds=seconds)
        db.add(c)
        db.commit()
        return str(c.id)
    finally:
        db.close()


def _lead(org, *, name="Maria", phone="+13055551234", email=None, reason=None, summary=None, status=LeadStatus.new, call_id=None, age_hours=0):
    db = SessionLocal()
    try:
        lead = Lead(organization_id=uuid.UUID(org), call_id=uuid.UUID(call_id) if call_id else None, name=name, phone=phone,
                    email=email, reason=reason, summary=summary, status=status)
        db.add(lead)
        db.flush()
        lead.created_at = T0 - timedelta(hours=age_hours)
        db.commit()
        return str(lead.id)
    finally:
        db.close()


def _browse(c, org, **params):
    return c.get(f"/api/v1/orgs/{org}/leads/browse", params=params)


def test_empty_org_returns_an_empty_page_with_zero_counts():
    c, org = new_org("ld")
    assert _browse(c, org).json() == {"items": [], "total": 0, "counts": {"new": 0, "contacted": 0, "booked": 0, "closed": 0}, "limit": 25, "offset": 0}


def test_every_stored_field_is_returned_and_missing_ones_stay_empty():
    c, org = new_org("ld")
    _lead(org, name="Maria Lopez", email="m@example.com", reason="Cleaning", summary="Prefers mornings.")
    _lead(org, name=None, phone=None, age_hours=1)
    first, second = _browse(c, org).json()["items"]
    assert (first["name"], first["phone"], first["email"], first["reason"], first["summary"], first["status"]) == (
        "Maria Lopez", "+13055551234", "m@example.com", "Cleaning", "Prefers mornings.", "new")
    assert first["created_at"] and first["source"] == "call"
    assert all(second[k] is None for k in ("name", "phone", "email", "reason", "summary", "call_id"))


def test_the_call_a_lead_came_from_is_linked():
    c, org = new_org("ld")
    cid = _call(org)
    _lead(org, call_id=cid)
    _lead(org, name="Walk-in", age_hours=2)
    with_call, without = _browse(c, org).json()["items"]
    assert with_call["call_id"] == cid and with_call["call_direction"] == "inbound" and with_call["call_duration_seconds"] == 95
    assert with_call["call_started_at"].startswith("2026-10-12T15:30")
    assert without["call_id"] is None and without["call_started_at"] is None


def test_status_filter_counts_and_paging():
    c, org = new_org("ld")
    for i in range(4):
        _lead(org, name=f"N{i}", age_hours=i)
    _lead(org, name="C", status=LeadStatus.contacted, age_hours=9)
    _lead(org, name="B", status=LeadStatus.booked, age_hours=10)
    body = _browse(c, org).json()
    assert body["total"] == 6 and body["counts"] == {"new": 4, "contacted": 1, "booked": 1, "closed": 0}
    new = _browse(c, org, status="new", limit=3).json()
    assert new["total"] == 4 and [i["name"] for i in new["items"]] == ["N0", "N1", "N2"]
    assert new["counts"]["contacted"] == 1  # counts ignore the status filter, so the tabs stay stable
    assert [i["name"] for i in _browse(c, org, status="new", limit=3, offset=3).json()["items"]] == ["N3"]
    assert _browse(c, org, status="closed").json()["total"] == 0
    oldest = [i["name"] for i in _browse(c, org, sort="oldest", limit=2).json()["items"]]
    assert oldest == ["B", "C"]


def test_search_name_email_reason_notes_and_phone_in_any_format_with_literal_wildcards():
    c, org = new_org("ld")
    _lead(org, name="Maria Lopez", phone="+13055551234", email="maria@example.com")
    _lead(org, name="Tom", phone="+14155559999", reason="Wants whitening", summary="100% sure, mornings")
    _lead(org, name="Ann", phone=None)
    assert _browse(c, org, q="lopez").json()["total"] == 1
    assert _browse(c, org, q="EXAMPLE.com").json()["total"] == 1
    assert _browse(c, org, q="whitening").json()["total"] == 1
    assert _browse(c, org, q="mornings").json()["total"] == 1
    assert _browse(c, org, q="(305) 555").json()["total"] == 1
    assert _browse(c, org, q="415555").json()["total"] == 1
    assert _browse(c, org, q="100%").json()["total"] == 1
    assert _browse(c, org, q="%%").json()["total"] == 0
    assert _browse(c, org, q="a_").json()["total"] == 0
    assert _browse(c, org, q="x").status_code == 422
    assert _browse(c, org, q="  ").json()["total"] == 3
    searched = _browse(c, org, q="lopez", status="booked").json()
    assert searched["total"] == 0 and searched["counts"]["new"] == 1  # tabs reflect the search


def test_invalid_values_are_rejected():
    c, org = new_org("ld")
    for bad in ({"status": "weird"}, {"sort": "random"}, {"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert _browse(c, org, **bad).status_code == 422, bad


def test_available_on_every_plan_and_leaves_the_old_list_unchanged():
    c, org = new_org("ld")
    _lead(org)
    for plan in (None, "starter", "growth"):
        if plan:
            subscribe(org, plan)
        assert _browse(c, org).status_code == 200
    old = c.get(f"/api/v1/orgs/{org}/leads").json()
    assert isinstance(old, list) and len(old) == 1 and "call_id" not in old[0]


def test_status_update_persists_validates_and_shows_in_the_browse():
    c, org = new_org("ld")
    lid = _lead(org)
    r = c.patch(f"/api/v1/orgs/{org}/leads/{lid}", json={"status": "booked"})
    assert r.status_code == 200 and r.json()["status"] == "booked"
    body = _browse(c, org).json()
    assert body["items"][0]["status"] == "booked" and body["counts"]["booked"] == 1 and body["counts"]["new"] == 0
    assert c.patch(f"/api/v1/orgs/{org}/leads/{lid}", json={"status": "nonsense"}).status_code == 400
    assert c.patch(f"/api/v1/orgs/{org}/leads/{uuid.uuid4()}", json={"status": "new"}).status_code == 404


def test_one_org_never_sees_or_changes_anothers_leads():
    a, org_a = new_org("ld")
    b, org_b = new_org("ld")
    cid = _call(org_a)
    lid = _lead(org_a, name="Secret Person", call_id=cid)
    assert _browse(b, org_b).json()["total"] == 0
    assert _browse(b, org_b, q="secret").json()["total"] == 0
    assert _browse(b, org_a).status_code == 404
    assert b.patch(f"/api/v1/orgs/{org_b}/leads/{lid}", json={"status": "closed"}).status_code == 404
    assert b.patch(f"/api/v1/orgs/{org_a}/leads/{lid}", json={"status": "closed"}).status_code == 404
    assert _browse(a, org_a).json()["items"][0]["status"] == "new"
