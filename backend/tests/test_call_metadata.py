"""
Level 2: call metadata, usage and transcript persistence through the real signed webhook and ingestion pipeline.
Cost unit (verified against Retell's API reference): call_cost.combined_cost is CENTS and may be fractional.
"""
import uuid

import pytest

from app.db.base import SessionLocal
from app.db.models.billing import UsageLedgerEntry
from app.db.models.calls import Call, CallTranscript
from app.services import call_ingestion
from tests.test_inbound_call_resolution import (  # noqa: F401  (_retell_key is an autouse fixture)
    _call, _cid, _event, _number, _org, _phone, _post, _retell_key, _wh,
)

START, END = 1_700_000_000_000, 1_700_000_044_400


def _setup(label):
    org, number, cid = _org(label), _number(), _cid()
    _phone(org, number)
    return org, number, cid


def _ended(org, number, cid, **over):
    call = dict(
        call_status="ended", start_timestamp=START, end_timestamp=END, duration_ms=44_400,
        disconnection_reason="user_hangup", transcript="Agent: hi\nUser: hello",
        transcript_object=[{"role": "agent", "content": "hi", "words": [{"word": "hi", "start": 0.1, "end": 0.4}]}],
        call_cost={"combined_cost": 12.13},
    )
    call.update(over)
    return _event("call_ended", cid, number, org, **call)


def _as(event_name, payload, **over):
    """Same call, re-labelled as another event type (Retell sends the full call object on call_analyzed)."""
    call = {**payload["call"], **over}
    return {"event": event_name, "call": call}


def _ledger(cid):
    db = SessionLocal()
    try:
        return db.query(UsageLedgerEntry).filter(UsageLedgerEntry.retell_call_id == cid).all()
    finally:
        db.close()


def _transcript(cid):
    db = SessionLocal()
    try:
        c = db.query(Call).filter(Call.retell_call_id == cid).one()
        return db.query(CallTranscript).filter(CallTranscript.call_id == c.id).first()
    finally:
        db.close()


def test_every_metadata_field_is_persisted_with_correct_units():
    org, number, cid = _setup("meta")
    assert _post(_ended(org, number, cid)).status_code == 204

    c = _call(cid)
    assert c.caller_number == "+14155550123"
    assert c.started_at.timestamp() == START / 1000
    assert c.ended_at.timestamp() == END / 1000
    assert c.duration_seconds == 44  # 44.4 s
    assert c.disconnect_reason == "user_hangup"
    (entry,) = _ledger(cid)
    assert entry.duration_ms == 44_400 and entry.billable_seconds == 44
    assert float(entry.provider_cost) == 12.13  # cents, kept fractional in the ledger


def test_transcript_is_stored_with_text_and_segments():
    org, number, cid = _setup("tr")
    _post(_ended(org, number, cid))

    t = _transcript(cid).transcript
    assert t["text"] == "Agent: hi\nUser: hello"
    assert t["segments"][0]["role"] == "agent" and t["segments"][0]["content"] == "hi"


def test_missing_optional_values_still_create_a_call_without_inventing_data():
    org, number, cid = _setup("sparse")
    _post(_event("call_ended", cid, number, org, call_status="ended"))

    c = _call(cid)
    assert c.status == "ended"
    assert c.duration_seconds is None and c.ended_at is None and c.disconnect_reason is None
    assert _ledger(cid) == [] and _transcript(cid) is None
    assert _wh(cid, "call_ended").status == "processed"


def test_late_sparse_event_never_blanks_data_from_a_fuller_one():
    org, number, cid = _setup("order")
    _post(_as("call_analyzed", _ended(org, number, cid), call_analysis={"call_summary": "s", "user_sentiment": "Positive"}))
    _post(_event("call_ended", cid, number, org, call_status="ended"))  # arrives late, carries nothing
    _post(_event("call_started", cid, number, org, start_timestamp=START))

    c = _call(cid)
    assert (c.status, c.duration_seconds, c.disconnect_reason, c.summary, c.sentiment) == ("ended", 44, "user_hangup", "s", "Positive")
    assert c.ended_at.timestamp() == END / 1000
    assert _transcript(cid).transcript["text"] == "Agent: hi\nUser: hello"
    (entry,) = _ledger(cid)
    assert float(entry.provider_cost) == 12.13


def test_cost_arriving_on_a_later_event_updates_the_single_ledger_row():
    org, number, cid = _setup("lateCost")
    _post(_ended(org, number, cid, call_cost=None))
    (entry,) = _ledger(cid)
    assert entry.provider_cost is None

    _post(_as("call_analyzed", _ended(org, number, cid), call_cost={"combined_cost": 12.13}))

    (entry,) = _ledger(cid)
    assert float(entry.provider_cost) == 12.13


def test_duplicate_events_do_not_duplicate_ledger_or_transcript():
    org, number, cid = _setup("dupe")
    payload = _ended(org, number, cid)
    for _ in range(3):
        _post(payload)

    assert len(_ledger(cid)) == 1
    assert _transcript(cid) is not None


def test_malformed_duration_falls_back_to_timestamps_instead_of_losing_usage():
    org, number, cid = _setup("badDur")
    _post(_ended(org, number, cid, duration_ms="abc"))

    assert _call(cid).duration_seconds == 44
    assert len(_ledger(cid)) == 1


def test_negative_duration_is_not_treated_as_a_zero_second_call():
    org, number, cid = _setup("negDur")
    _post(_ended(org, number, cid, duration_ms=-5))

    assert _call(cid).duration_seconds == 44  # from timestamps, not 0
    (entry,) = _ledger(cid)
    assert entry.duration_ms == 44_400


def test_end_before_start_is_unknown_duration_not_zero():
    org, number, cid = _setup("backwards")
    _post(_ended(org, number, cid, duration_ms=None, start_timestamp=END, end_timestamp=START))

    assert _call(cid).duration_seconds is None
    assert _ledger(cid) == []


def test_out_of_range_timestamp_does_not_kill_the_event():
    org, number, cid = _setup("hugeTs")
    _post(_ended(org, number, cid, start_timestamp=10**20, end_timestamp=END))

    assert _wh(cid, "call_ended").status == "processed"
    c = _call(cid)
    assert c.started_at is None and c.duration_seconds == 44


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-3", "1e30", "abc"])
def test_garbage_cost_is_recorded_as_unknown_never_stored(bad):
    org, number, cid = _setup(f"badCost{bad}")
    _post(_ended(org, number, cid, call_cost={"combined_cost": bad}))

    assert _wh(cid, "call_ended").status == "processed"
    (entry,) = _ledger(cid)
    assert entry.provider_cost is None


def test_cost_helper_accepts_fractional_cents_and_zero():
    from decimal import Decimal

    assert call_ingestion._decimal(12.13) == Decimal("12.13")
    assert call_ingestion._decimal(0) == Decimal("0")
    assert call_ingestion._decimal(None) is None
