"""
Retell webhook -> WebhookEvent -> Call -> Transcript -> Lead -> UsageLedger.
Payload shapes follow Retell's documented call object (see call_ingestion.py);
the signing scheme matches app/webhooks/retell_signature.py.
"""
import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import UsageLedgerEntry
from app.db.models.business import Business
from app.db.models.calls import Call, CallDirection, CallTranscript
from app.db.models.crm import Lead
from app.db.models.platform import WebhookEvent, WebhookSource
from app.db.models.telephony import PhoneNumber
from app.db.models.voice_agent import Agent, AgentStatus, Voice
from app.main import app
from app.workers import retell_events as worker
from tests.test_tenant_isolation import _signup

settings = get_settings()
KEY = "test-retell-key"
anon = TestClient(app)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "RETELL_API_KEY", KEY)


def _new_org():
    tag = uuid.uuid4().hex[:10]
    return _signup(f"pipe-{tag}@pipe-test.com", f"Pipe Org {tag}")


def _seed(org_id: str, with_phone: bool = True):
    """An org's synced agent (and optionally a live phone number). Returns (retell_agent_id, e164)."""
    db = SessionLocal()
    try:
        biz = Business(organization_id=uuid.UUID(org_id), name="Pipe Biz", website_url="https://pipe.example")
        voice = Voice(retell_voice_id=f"voice_{uuid.uuid4().hex[:8]}", name="V", provider="elevenlabs")
        db.add_all([biz, voice])
        db.flush()
        retell_agent_id = f"agent_{uuid.uuid4().hex[:10]}"
        agent = Agent(
            organization_id=uuid.UUID(org_id), business_id=biz.id, voice_id=voice.id, name="Rec",
            personality="friendly", language="en-US", tasks={}, status=AgentStatus.active, version=1,
            synced_version=1, retell_agent_id=retell_agent_id, retell_llm_id=f"llm_{uuid.uuid4().hex[:10]}",
        )
        db.add(agent)
        db.flush()
        number = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
        if with_phone:
            db.add(PhoneNumber(organization_id=uuid.UUID(org_id), agent_id=agent.id, retell_phone_number_id=number, number=number))
        db.commit()
        return retell_agent_id, number
    finally:
        db.close()


def _call(call_id, agent_id, to_number=None, **over):
    base = {
        "call_id": call_id, "agent_id": agent_id, "call_type": "phone_call", "direction": "inbound",
        "from_number": "+15559990000", "to_number": to_number, "call_status": "ended",
        "start_timestamp": 1_800_000_000_000, "end_timestamp": 1_800_000_061_499, "duration_ms": 61_499,
        "disconnection_reason": "user_hangup", "transcript": "Agent: Hi. Caller: Hello.",
        "transcript_object": [{"role": "agent", "content": "Hi"}],
    }
    base.update(over)
    return base


def _analysis(captured=True):
    return {
        "call_summary": "Caller asked about a cleaning.",
        "user_sentiment": "Positive",
        "custom_analysis_data": {"captured_lead": captured, "caller_name": "Maria", "callback_number": "+15551110000", "reason": "Cleaning price"},
    }


def _post(event: str, call: dict, key=KEY):
    body = json.dumps({"event": event, "call": call}).encode()
    ts = int(time.time() * 1000)
    digest = hmac.new(key.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()
    return anon.post("/webhooks/retell", content=body, headers={"x-retell-signature": f"v={ts},d={digest}", "Content-Type": "application/json"})


def _count(model, **filters):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**filters).count()
    finally:
        db.close()


def _event_row(call_id, event):
    db = SessionLocal()
    try:
        row = db.query(WebhookEvent).filter(WebhookEvent.external_event_id == f"{call_id}:{event}").one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _call_row(call_id):
    db = SessionLocal()
    try:
        row = db.query(Call).filter(Call.retell_call_id == call_id).one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _make_due(call_id, event):
    db = SessionLocal()
    row = db.query(WebhookEvent).filter(WebhookEvent.external_event_id == f"{call_id}:{event}").one()
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    db.close()


# ---------------- a real call creates the whole chain ----------------

def test_inbound_call_with_no_local_call_creates_call_transcript_and_usage():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    assert _post("call_ended", _call(cid, agent, number, call_cost={"combined_cost": 70})).status_code == 204

    call = _call_row(cid)
    assert str(call.organization_id) == org and call.direction == CallDirection.inbound
    assert call.status == "ended" and call.duration_seconds == 61 and call.caller_number == "+15559990000"
    assert call.disconnect_reason == "user_hangup" and call.phone_number_id is not None
    assert _count(CallTranscript, call_id=call.id) == 1
    db = SessionLocal()
    led = db.query(UsageLedgerEntry).filter_by(retell_call_id=cid).one()
    assert (led.kind, led.duration_ms, led.billable_seconds, led.provider_cost) == ("production", 61_499, 61, Decimal("70"))
    db.close()
    assert _event_row(cid, "call_ended").status == "processed"


def test_billable_seconds_round_to_the_nearest_second():
    _c, org = _new_org()
    agent, number = _seed(org)
    a, b = f"call_{uuid.uuid4().hex[:10]}", f"call_{uuid.uuid4().hex[:10]}"
    _post("call_ended", _call(a, agent, number, duration_ms=61_499))
    _post("call_ended", _call(b, agent, number, duration_ms=61_500))
    db = SessionLocal()
    assert db.query(UsageLedgerEntry).filter_by(retell_call_id=a).one().billable_seconds == 61
    assert db.query(UsageLedgerEntry).filter_by(retell_call_id=b).one().billable_seconds == 62
    db.close()


# ---------------- duplicates ----------------

def test_duplicate_delivery_creates_one_event_one_call_one_usage():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    for _ in range(3):
        assert _post("call_ended", _call(cid, agent, number)).status_code == 204
    assert _count(WebhookEvent, external_event_id=f"{cid}:call_ended") == 1
    assert _count(Call, retell_call_id=cid) == 1
    assert _count(UsageLedgerEntry, retell_call_id=cid) == 1


def test_concurrent_workers_on_one_event_create_one_call_and_one_usage_row():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    db = SessionLocal()
    ev = WebhookEvent(source=WebhookSource.retell, external_event_id=f"{cid}:call_ended", event_type="call_ended",
                      payload={"event": "call_ended", "call": _call(cid, agent, number)})
    db.add(ev)
    db.commit()
    event_id = str(ev.id)
    db.close()
    threads = [threading.Thread(target=worker.process_event, args=(event_id,)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert _count(Call, retell_call_id=cid) == 1 and _count(UsageLedgerEntry, retell_call_id=cid) == 1
    assert _event_row(cid, "call_ended").status == "processed"


# ---------------- unresolved / failures are never "processed" ----------------

def test_unknown_agent_is_stored_failed_and_retryable_not_discarded():
    cid = f"call_{uuid.uuid4().hex[:10]}"
    ghost_agent = f"agent_{uuid.uuid4().hex[:10]}"
    assert _post("call_ended", _call(cid, ghost_agent, "+15550000000")).status_code == 204
    row = _event_row(cid, "call_ended")
    assert row.status == "failed" and row.attempts == 1 and "no agent/phone" in row.last_error
    assert _count(Call, retell_call_id=cid) == 0

    # the agent shows up later (e.g. our DB commit lagged): the sweeper recovers the call
    _c, org = _new_org()
    db = SessionLocal()
    seeded_agent, _n = _seed(org, with_phone=False)
    a = db.query(Agent).filter(Agent.retell_agent_id == seeded_agent).one()
    a.retell_agent_id = ghost_agent
    db.commit()
    db.close()
    _make_due(cid, "call_ended")
    assert worker.retry_due_events() >= 1
    assert _event_row(cid, "call_ended").status == "processed"
    assert _count(Call, retell_call_id=cid) == 1 and _count(UsageLedgerEntry, retell_call_id=cid) == 1


def test_failed_event_waits_for_backoff_then_goes_dead_after_max_attempts(monkeypatch):
    monkeypatch.setattr(settings, "RETELL_EVENT_MAX_ATTEMPTS", 2)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("call_ended", _call(cid, f"agent_{uuid.uuid4().hex[:8]}", "+15550000001"))
    before = worker.retry_due_events()  # backoff has not elapsed
    assert _event_row(cid, "call_ended").attempts == 1 and before >= 0
    _make_due(cid, "call_ended")
    worker.retry_due_events()
    row = _event_row(cid, "call_ended")
    assert row.status == "dead" and row.attempts == 2 and row.last_error
    worker.retry_due_events()
    assert _event_row(cid, "call_ended").attempts == 2  # dead events are not retried forever
    assert worker.requeue_dead(str(row.id)) == 1
    assert _event_row(cid, "call_ended").status == "failed"


def test_worker_crash_midway_leaves_event_retryable_and_no_partial_data(monkeypatch):
    from app.services import call_ingestion

    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    real = call_ingestion.ingest_event

    def crash_after_writing_the_call(db, event):
        call = call_ingestion._get_or_create_call(db, event.payload["call"])  # real write...
        db.flush()
        assert call.id is not None
        raise RuntimeError("process died")  # ...then die before usage/transcript

    with monkeypatch.context() as m:
        m.setattr(worker, "ingest_event", crash_after_writing_the_call)
        _post("call_ended", _call(cid, agent, number))
    row = _event_row(cid, "call_ended")
    assert row.status == "failed" and "process died" in row.last_error
    assert _count(Call, retell_call_id=cid) == 0  # partial write rolled back

    _make_due(cid, "call_ended")
    worker.retry_due_events()
    assert _event_row(cid, "call_ended").status == "processed"
    assert _count(Call, retell_call_id=cid) == 1 and _count(UsageLedgerEntry, retell_call_id=cid) == 1
    assert real is worker.ingest_event


def test_received_but_never_attempted_events_are_picked_up_by_the_sweeper():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    db = SessionLocal()
    db.add(WebhookEvent(source=WebhookSource.retell, external_event_id=f"{cid}:call_ended", event_type="call_ended",
                        payload={"event": "call_ended", "call": _call(cid, agent, number)},
                        created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    db.commit()
    db.close()
    worker.retry_due_events()
    assert _event_row(cid, "call_ended").status == "processed" and _count(Call, retell_call_id=cid) == 1


def test_agent_and_phone_from_different_orgs_is_not_attributed():
    _c1, org1 = _new_org()
    _c2, org2 = _new_org()
    agent1, _n1 = _seed(org1, with_phone=False)
    _a2, number2 = _seed(org2)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("call_ended", _call(cid, agent1, number2))
    assert _event_row(cid, "call_ended").status == "failed" and _count(Call, retell_call_id=cid) == 0


def test_malformed_payload_is_rejected_not_stored():
    body = json.dumps({"event": "call_ended", "call": {}}).encode()
    ts = int(time.time() * 1000)
    sig = f"v={ts},d={hmac.new(KEY.encode(), body + str(ts).encode(), hashlib.sha256).hexdigest()}"
    assert anon.post("/webhooks/retell", content=body, headers={"x-retell-signature": sig}).status_code == 400


def test_unhandled_event_types_are_recorded_as_processed_without_creating_calls():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("transcript_updated", _call(cid, agent, number))
    assert _event_row(cid, "transcript_updated").status == "processed" and _count(Call, retell_call_id=cid) == 0


# ---------------- ordering between event types ----------------

def test_analyzed_before_ended_then_ended_keeps_the_final_state_correct():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("call_analyzed", _call(cid, agent, number, call_analysis=_analysis(), call_cost={"combined_cost": 72}))
    _post("call_ended", _call(cid, agent, number, call_cost={"combined_cost": 70}))
    _post("call_started", _call(cid, agent, number, call_status="ongoing", end_timestamp=None, duration_ms=None))

    call = _call_row(cid)
    assert call.status == "ended" and call.summary == "Caller asked about a cleaning." and call.sentiment == "Positive"
    assert call.duration_seconds == 61
    assert _count(Lead, call_id=call.id) == 1 and _count(UsageLedgerEntry, retell_call_id=cid) == 1
    db = SessionLocal()
    assert db.query(UsageLedgerEntry).filter_by(retell_call_id=cid).one().billable_seconds == 61
    db.close()


def test_ended_then_analyzed_updates_in_place_and_does_not_double_count():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("call_ended", _call(cid, agent, number, call_cost={"combined_cost": 70}))
    _post("call_analyzed", _call(cid, agent, number, call_analysis=_analysis(), call_cost={"combined_cost": 72}))
    _post("call_analyzed", _call(cid, agent, number, call_analysis=_analysis(), call_cost={"combined_cost": 72}))
    db = SessionLocal()
    led = db.query(UsageLedgerEntry).filter_by(retell_call_id=cid).one()
    assert led.provider_cost == Decimal("72")  # set to the final value, not 70 + 72
    db.close()
    assert _count(Lead, call_id=_call_row(cid).id) == 1


def test_lead_created_only_when_analysis_says_captured():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("call_analyzed", _call(cid, agent, number, call_analysis=_analysis(captured=False)))
    assert _count(Lead, call_id=_call_row(cid).id) == 0


def test_lead_fields_come_from_the_analysis():
    _c, org = _new_org()
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    _post("call_analyzed", _call(cid, agent, number, call_analysis=_analysis()))
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.call_id == _call_row(cid).id).one()
    assert (lead.name, lead.phone, lead.reason, lead.source) == ("Maria", "+15551110000", "Cleaning price", "call")
    db.close()


# ---------------- test (browser) calls ----------------

def test_browser_test_call_row_is_completed_by_its_webhook_and_not_billed():
    c, org = _new_org()
    agent, _number = _seed(org, with_phone=False)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    db = SessionLocal()
    agent_row = db.query(Agent).filter(Agent.retell_agent_id == agent).one()
    db.add(Call(organization_id=uuid.UUID(org), retell_call_id=cid, agent_id=agent_row.id, direction=CallDirection.test, status="registered"))
    db.commit()
    db.close()
    _post("call_ended", _call(cid, agent, None, call_type="web_call", direction=None, from_number=None))
    call = _call_row(cid)
    assert call.direction == CallDirection.test and call.status == "ended" and _count(Call, retell_call_id=cid) == 1
    db = SessionLocal()
    assert db.query(UsageLedgerEntry).filter_by(retell_call_id=cid).one().kind == "free_test"
    db.close()
    usage = c.get(f"/api/v1/orgs/{org}/usage").json()
    assert usage["calls_count"] == 0 and usage["billable_seconds"] == 0  # tests are not customer usage


# ---------------- customer-visible usage ----------------

def test_usage_endpoint_sums_production_calls_and_never_exposes_provider_cost():
    c, org = _new_org()
    agent, number = _seed(org)
    for secs in (61_499, 30_000):
        _post("call_ended", _call(f"call_{uuid.uuid4().hex[:10]}", agent, number, duration_ms=secs, call_cost={"combined_cost": 55}))
    usage = c.get(f"/api/v1/orgs/{org}/usage").json()
    assert usage["calls_count"] == 2 and usage["billable_seconds"] == 61 + 30
    assert not any("cost" in k for k in usage)
    calls = c.get(f"/api/v1/orgs/{org}/calls").json()
    assert calls and all("cost" not in k for k in calls[0])


def test_other_tenants_never_see_my_calls_or_usage():
    c1, org1 = _new_org()
    c2, org2 = _new_org()
    agent, number = _seed(org1)
    _post("call_ended", _call(f"call_{uuid.uuid4().hex[:10]}", agent, number))
    assert c2.get(f"/api/v1/orgs/{org2}/calls").json() == []
    assert c2.get(f"/api/v1/orgs/{org2}/usage").json()["calls_count"] == 0
    assert c2.get(f"/api/v1/orgs/{org1}/usage").status_code == 404
