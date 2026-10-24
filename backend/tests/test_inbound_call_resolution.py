"""
Level 1: inbound-call tenant resolution in the existing ingestion pipeline
(signed webhook -> WebhookEvent -> process_event -> call_ingestion.ingest_event,
retried by retry_due_events). Payloads deliberately carry no duration or
transcript, so these tests exercise attribution/idempotency only.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.base import SessionLocal
from app.db.models.business import Business
from app.db.models.calls import Call, CallDirection
from app.db.models.platform import WebhookEvent
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.tenancy import Organization
from app.db.models.voice_agent import Agent
from app.main import app
from app.webhooks import retell as retell_webhook
from app.workers import retell_events

KEY = "level1-resolution-test-key"


@pytest.fixture(autouse=True)
def _retell_key(monkeypatch):
    monkeypatch.setattr(retell_webhook.settings, "RETELL_API_KEY", KEY)


def _post(payload: dict, signed: bool = True):
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if signed:
        ts = int(time.time() * 1000)
        digest = hmac.new(KEY.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()
        headers["x-retell-signature"] = f"v={ts},d={digest}"
    return TestClient(app).post("/webhooks/retell", content=body, headers=headers)


def _number() -> str:
    return "+1555" + str(uuid.uuid4().int % 10_000_000).zfill(7)


def _org(label: str) -> dict:
    """An org + business + agent (with a Retell id). Phone numbers are added separately."""
    db = SessionLocal()
    try:
        org = Organization(name=f"L1 {label} {uuid.uuid4().hex[:6]}")
        db.add(org)
        db.flush()
        business = Business(organization_id=org.id, name=f"{label} Salon")
        db.add(business)
        db.flush()
        agent = Agent(
            organization_id=org.id,
            business_id=business.id,
            name=f"{label} agent",
            retell_agent_id=f"agent_{uuid.uuid4().hex[:12]}",
        )
        db.add(agent)
        db.commit()
        return {"org_id": org.id, "agent_id": agent.id, "retell_agent_id": agent.retell_agent_id}
    finally:
        db.close()


def _phone(org: dict, number: str, status=PhoneNumberStatus.active, with_agent=True) -> uuid.UUID:
    db = SessionLocal()
    try:
        row = PhoneNumber(
            organization_id=org["org_id"],
            agent_id=org["agent_id"] if with_agent else None,
            retell_phone_number_id=f"pn_{uuid.uuid4().hex[:12]}",
            number=number,
            status=status,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _event(event: str, call_id: str, number: str, agent: dict | None, **overrides) -> dict:
    call = {
        "call_id": call_id,
        "call_type": "phone_call",
        "direction": "inbound",
        "from_number": "+14155550123",
        "to_number": number,
        "call_status": "ongoing",
    }
    if agent is not None:
        call["agent_id"] = agent["retell_agent_id"]
    call.update(overrides)
    return {"event": event, "call": call}


def _call(call_id: str) -> Call | None:
    db = SessionLocal()
    try:
        return db.query(Call).filter(Call.retell_call_id == call_id).first()
    finally:
        db.close()


def _wh(call_id: str, event: str) -> WebhookEvent:
    db = SessionLocal()
    try:
        return db.query(WebhookEvent).filter(WebhookEvent.external_event_id == f"{call_id}:{event}").one()
    finally:
        db.close()


def _cid() -> str:
    return f"call_{uuid.uuid4().hex}"


# ---- guards for behaviour that already works and must keep working ----

def test_new_inbound_call_is_attributed_to_the_agents_org_and_number():
    org, number, cid = _org("alpha"), _number(), _cid()
    phone_id = _phone(org, number)

    assert _post(_event("call_started", cid, number, org)).status_code == 204

    call = _call(cid)
    assert call.organization_id == org["org_id"]
    assert call.agent_id == org["agent_id"]
    assert call.phone_number_id == phone_id
    assert call.direction == CallDirection.inbound
    assert _wh(cid, "call_started").status == "processed"


def test_duplicate_and_out_of_order_events_keep_one_row_and_never_regress():
    org, number, cid = _org("order"), _number(), _cid()
    _phone(org, number)

    _post(_event("call_ended", cid, number, org, call_status="ended"))
    _post(_event("call_started", cid, number, org, call_status="ongoing"))  # late
    _post(_event("call_started", cid, number, org, call_status="ongoing"))  # duplicate

    db = SessionLocal()
    try:
        rows = db.query(Call).filter(Call.retell_call_id == cid).all()
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0].status == "ended"


def test_the_other_partys_number_never_decides_the_tenant():
    a, b = _org("ours"), _org("theirs")
    a_number, b_number, cid = _number(), _number(), _cid()
    _phone(a, a_number)
    _phone(b, b_number)

    # Tenant B's number is the *caller*; the dialled number is tenant A's.
    _post(_event("call_started", cid, a_number, None, from_number=b_number))

    assert _call(cid).organization_id == a["org_id"]


def test_unresolvable_call_is_failed_not_processed_and_recovers_via_sweeper():
    org, number, cid = _org("late"), _number(), _cid()
    late_agent = {"retell_agent_id": f"agent_late_{uuid.uuid4().hex[:8]}"}

    assert _post(_event("call_started", cid, number, late_agent)).status_code == 204

    ev = _wh(cid, "call_started")
    assert _call(cid) is None
    assert ev.status == "failed" and ev.processed_at is None and ev.last_error

    # The agent shows up later; the sweeper picks the event up once backoff elapses.
    db = SessionLocal()
    try:
        db.query(Agent).filter(Agent.id == org["agent_id"]).update({"retell_agent_id": late_agent["retell_agent_id"]})
        db.query(WebhookEvent).filter(WebhookEvent.id == ev.id).update(
            {"next_attempt_at": ev.next_attempt_at - timedelta(hours=2)}
        )
        db.commit()
    finally:
        db.close()
    retell_events.retry_due_events()

    assert _call(cid).organization_id == org["org_id"]
    assert _wh(cid, "call_started").status == "processed"


def test_unsigned_webhook_creates_nothing():
    org, number, cid = _org("unsigned"), _number(), _cid()
    _phone(org, number)

    assert _post(_event("call_started", cid, number, org), signed=False).status_code == 401
    assert _call(cid) is None


# ---- Level 1 fixes ----

@pytest.mark.parametrize("old_first", [True, False])
def test_reassigned_number_resolves_to_the_active_owner_not_the_released_one(old_first):
    """A released row for the same E.164 in another org must never win."""
    old, new = _org("former-owner"), _org("current-owner")
    number, cid = _number(), _cid()
    if old_first:
        _phone(old, number, status=PhoneNumberStatus.released)
        new_phone = _phone(new, number)
    else:
        new_phone = _phone(new, number)
        _phone(old, number, status=PhoneNumberStatus.released)

    _post(_event("call_started", cid, number, new))

    call = _call(cid)
    assert call is not None, "call was dropped (agent/phone mismatch against the released row)"
    assert call.organization_id == new["org_id"]
    assert call.phone_number_id == new_phone
    assert _wh(cid, "call_started").status == "processed"


@pytest.mark.parametrize("old_first", [True, False])
def test_number_only_event_prefers_the_active_owner_over_a_released_row(old_first):
    old, new = _org("former"), _org("current")
    number, cid = _number(), _cid()
    if old_first:
        _phone(old, number, status=PhoneNumberStatus.released)
        new_phone = _phone(new, number)
    else:
        new_phone = _phone(new, number)
        _phone(old, number, status=PhoneNumberStatus.released)

    _post(_event("call_started", cid, number, None))  # payload without agent_id

    call = _call(cid)
    assert call.organization_id == new["org_id"]
    assert call.phone_number_id == new_phone


def test_number_active_in_two_orgs_with_no_agent_is_ambiguous_and_never_guessed():
    a, b = _org("dup-a"), _org("dup-b")
    number, cid = _number(), _cid()
    _phone(a, number)
    _phone(b, number)

    _post(_event("call_started", cid, number, None))

    ev = _wh(cid, "call_started")
    assert _call(cid) is None
    assert ev.status == "failed" and ev.processed_at is None


def test_number_only_event_takes_the_agent_from_the_number():
    org, number, cid = _org("phone-only"), _number(), _cid()
    _phone(org, number, with_agent=True)

    _post(_event("call_started", cid, number, None))

    call = _call(cid)
    assert call.organization_id == org["org_id"]
    assert call.agent_id == org["agent_id"]


def test_event_naming_another_orgs_agent_cannot_update_an_existing_call():
    owner, intruder = _org("owner"), _org("intruder")
    number, cid = _number(), _cid()
    _phone(owner, number)
    _post(_event("call_started", cid, number, owner))

    _post(_event("call_ended", cid, number, intruder, call_status="ended"))

    call = _call(cid)
    assert call.organization_id == owner["org_id"]
    assert call.status == "ongoing"
    ev = _wh(cid, "call_ended")
    assert ev.status == "failed" and ev.processed_at is None
