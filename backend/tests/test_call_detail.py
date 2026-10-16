"""
Level 6 Call detail: what one call shows on each plan, and that the transcript is refused by the SERVER for Starter.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models.calls import Call, CallDirection, CallTranscript
from app.db.models.crm import Lead, LeadStatus
from tests.billing_helpers import new_org, subscribe

T0 = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)
SECRET = "my gate code is 4417"
SEGMENTS = [
    {"role": "agent", "content": "Thanks for calling.", "words": [{"word": "Thanks", "start": 0.4, "end": 0.8}]},
    {"role": "user", "content": SECRET, "words": [{"word": "my", "start": 5.0, "end": 5.2}]},
]


def _call_with_everything(org, *, with_lead=True, with_transcript=True):
    db = SessionLocal()
    try:
        call = Call(
            organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:12]}", direction=CallDirection.inbound,
            status="ended", caller_number="+13055551234", started_at=T0, ended_at=T0 + timedelta(seconds=95), duration_seconds=95,
            summary="Wants a cleaning next week.", sentiment="Positive", disconnect_reason="user_hangup",
        )
        db.add(call)
        db.flush()
        if with_transcript:
            db.add(CallTranscript(call_id=call.id, transcript={"text": "x", "segments": SEGMENTS}))
        if with_lead:
            db.add(Lead(organization_id=uuid.UUID(org), call_id=call.id, name="Maria Lopez", phone="+13055559999",
                        email="maria@example.com", reason="Cleaning", summary="Prefers mornings.", status=LeadStatus.contacted))
        db.commit()
        return str(call.id)
    finally:
        db.close()


def test_detail_has_every_stored_field_and_the_lead_details_on_starter():
    c, org = new_org("cd")
    subscribe(org, "starter")
    cid = _call_with_everything(org)
    d = c.get(f"/api/v1/orgs/{org}/calls/{cid}")
    assert d.status_code == 200
    body = d.json()
    assert body["caller_number"] == "+13055551234" and body["direction"] == "inbound" and body["status"] == "ended"
    assert body["duration_seconds"] == 95 and body["disconnect_reason"] == "user_hangup"
    assert body["summary"] == "Wants a cleaning next week." and body["sentiment"] == "Positive"
    assert body["started_at"] and body["ended_at"]
    assert body["lead_name"] == "Maria Lopez" and body["lead_status"] == "contacted"
    assert body["lead_phone"] == "+13055559999" and body["lead_email"] == "maria@example.com"
    assert body["lead_reason"] == "Cleaning" and body["lead_summary"] == "Prefers mornings."


def test_a_lead_name_is_never_presented_as_the_caller_and_no_caller_name_is_invented():
    c, org = new_org("cd")
    subscribe(org, "starter")
    body = c.get(f"/api/v1/orgs/{org}/calls/{_call_with_everything(org)}").json()
    assert not any(k in body for k in ("caller_name", "name", "contact_name"))
    assert body["lead_name"] == "Maria Lopez"  # only ever under a lead_ key


def test_a_call_without_a_lead_has_empty_lead_fields():
    c, org = new_org("cd")
    subscribe(org, "starter")
    body = c.get(f"/api/v1/orgs/{org}/calls/{_call_with_everything(org, with_lead=False)}").json()
    assert all(body[k] is None for k in ("lead_id", "lead_name", "lead_status", "lead_phone", "lead_email", "lead_reason", "lead_summary"))


def test_starter_is_refused_the_transcript_by_the_api_and_the_detail_never_leaks_it():
    c, org = new_org("cd")
    subscribe(org, "starter")
    cid = _call_with_everything(org)
    t = c.get(f"/api/v1/orgs/{org}/calls/{cid}/transcript")
    assert t.status_code == 402 and "Growth" in t.json()["detail"] and SECRET not in t.text
    detail = c.get(f"/api/v1/orgs/{org}/calls/{cid}")
    assert SECRET not in detail.text and "segments" not in detail.text and "transcript" not in detail.text
    assert c.get(f"/api/v1/orgs/{org}/calls/search", params={"q": "gate"}).status_code == 402  # search is gated too
    assert SECRET not in c.get(f"/api/v1/orgs/{org}/calls/browse").text


def test_no_subscription_gets_the_call_record_but_not_the_transcript():
    c, org = new_org("cd")
    cid = _call_with_everything(org)
    assert c.get(f"/api/v1/orgs/{org}/calls/{cid}/transcript").status_code == 402


def test_growth_gets_speaker_labels_and_timestamps():
    c, org = new_org("cd")
    subscribe(org, "growth")
    cid = _call_with_everything(org)
    t = c.get(f"/api/v1/orgs/{org}/calls/{cid}/transcript")
    assert t.status_code == 200
    segs = t.json()["segments"]
    assert [s["speaker"] for s in segs] == ["Receptionist", "Caller"]
    assert segs[0]["start_seconds"] == 0.4 and segs[1]["start_seconds"] == 5.0 and segs[1]["text"] == SECRET


def test_growth_cannot_read_another_organizations_call_or_transcript():
    a, org_a = new_org("cd")
    b, org_b = new_org("cd")
    subscribe(org_b, "growth")
    cid = _call_with_everything(org_a)
    assert b.get(f"/api/v1/orgs/{org_b}/calls/{cid}/transcript").status_code == 404
    assert b.get(f"/api/v1/orgs/{org_b}/calls/{cid}").status_code == 404
    assert b.get(f"/api/v1/orgs/{org_a}/calls/{cid}/transcript").status_code == 404