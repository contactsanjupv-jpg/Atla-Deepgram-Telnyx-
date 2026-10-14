"""
Retell event -> Call -> Transcript -> Lead -> UsageLedgerEntry.

Field names come from Retell's documented webhook/call object:
  event, call.{call_id, agent_id, call_type, call_status, direction,
  start_timestamp, end_timestamp, duration_ms, transcript, transcript_object,
  disconnection_reason, call_analysis.{call_summary, user_sentiment,
  custom_analysis_data}, call_cost.combined_cost, from_number, to_number}.
UNVERIFIED until one real payload is captured: from_number/to_number,
user_sentiment, and the unit of combined_cost.

Rules that make this safe to run repeatedly and out of order:
  * Every write is an upsert keyed on retell_call_id (unique in the DB).
  * Fields only move forward: a late/duplicate event never blanks data or
    regresses a finished call back to "ongoing".
  * Anything that cannot be attributed to a tenant RAISES — the caller marks
    the event failed/retryable; nothing is ever silently discarded.
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.billing import FreeTestAllowance, UsageLedgerEntry
from app.db.models.calls import Call, CallDirection, CallTranscript
from app.db.models.crm import Lead
from app.db.models.platform import WebhookEvent
from app.db.models.telephony import PhoneNumber
from app.db.models.voice_agent import Agent
from app.services import free_test
from app.services.billing_state import ACTIVE_STATUSES, current_subscription

logger = logging.getLogger("atla.calls")

HANDLED_EVENTS = {"call_started", "call_ended", "call_analyzed"}
_RANK = {"registered": 0, "ongoing": 1, "ended": 2, "error": 2}


class UnresolvedCall(Exception):
    """Valid event, but we can't (yet) tie it to a tenant. Retryable."""


class InvalidEvent(Exception):
    """Malformed payload that will never become valid. Not retryable."""


def _dt(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def ingest_event(db: Session, event: WebhookEvent) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    event_type = payload.get("event")
    pcall = payload.get("call")
    if not isinstance(pcall, dict) or not pcall.get("call_id"):
        raise InvalidEvent("payload has no call.call_id")
    if event_type not in HANDLED_EVENTS:
        return

    call = _get_or_create_call(db, pcall)
    _apply_call_fields(call, event_type, pcall)
    db.flush()
    if event_type in ("call_ended", "call_analyzed"):
        _upsert_transcript(db, call, pcall)
        _upsert_usage(db, call, pcall)
    if event_type == "call_analyzed":
        _upsert_lead(db, call, pcall)
    db.flush()


# ---------- resolution ----------

def _get_or_create_call(db: Session, pcall: dict) -> Call:
    call_id = pcall["call_id"]
    call = db.query(Call).filter(Call.retell_call_id == call_id).first()
    if call is not None:
        return call

    agent = None
    if pcall.get("agent_id"):
        agent = db.query(Agent).filter(Agent.retell_agent_id == pcall["agent_id"]).first()
    phone = None
    for key in ("to_number", "from_number"):
        if pcall.get(key):
            phone = db.query(PhoneNumber).filter(PhoneNumber.number == pcall[key]).first()
            if phone is not None:
                break
    if agent is None and phone is None:
        raise UnresolvedCall(f"no agent/phone matches call {call_id}")
    if agent is not None and phone is not None and agent.organization_id != phone.organization_id:
        raise UnresolvedCall(f"agent and phone belong to different organizations for call {call_id}")
    org_id = agent.organization_id if agent is not None else phone.organization_id

    if pcall.get("call_type") == "web_call":
        direction = CallDirection.test
    elif pcall.get("direction") == "outbound":
        direction = CallDirection.outbound
    else:
        direction = CallDirection.inbound

    try:
        with db.begin_nested():
            call = Call(
                organization_id=org_id,
                retell_call_id=call_id,
                agent_id=agent.id if agent is not None else None,
                phone_number_id=phone.id if phone is not None else None,
                direction=direction,
                status="registered",
            )
            db.add(call)
            db.flush()
    except IntegrityError:  # a concurrent worker created it first
        call = db.query(Call).filter(Call.retell_call_id == call_id).one()
    return call


# ---------- merging ----------

def _apply_call_fields(call: Call, event_type: str, pcall: dict) -> None:
    incoming = pcall.get("call_status")
    if event_type in ("call_ended", "call_analyzed") and incoming not in _RANK:
        incoming = "ended"
    elif event_type == "call_started" and incoming not in _RANK:
        incoming = "ongoing"
    if incoming in _RANK and _RANK[incoming] >= _RANK.get(call.status or "registered", 0):
        call.status = incoming

    if call.caller_number is None and call.direction == CallDirection.inbound and pcall.get("from_number"):
        call.caller_number = str(pcall["from_number"])[:32]
    if call.started_at is None and _dt(pcall.get("start_timestamp")):
        call.started_at = _dt(pcall.get("start_timestamp"))
    if event_type in ("call_ended", "call_analyzed"):
        if _dt(pcall.get("end_timestamp")):
            call.ended_at = _dt(pcall.get("end_timestamp"))
        duration_ms = _duration_ms(pcall)
        if duration_ms is not None:
            call.duration_seconds = (duration_ms + 500) // 1000
        if pcall.get("disconnection_reason"):
            call.disconnect_reason = str(pcall["disconnection_reason"])[:128]
    analysis = pcall.get("call_analysis")
    if isinstance(analysis, dict):
        if analysis.get("call_summary"):
            call.summary = analysis["call_summary"]
        if analysis.get("user_sentiment"):
            call.sentiment = str(analysis["user_sentiment"])[:32]


def _duration_ms(pcall: dict) -> int | None:
    if pcall.get("duration_ms") is not None:
        try:
            return max(int(pcall["duration_ms"]), 0)
        except (TypeError, ValueError):
            return None
    start, end = pcall.get("start_timestamp"), pcall.get("end_timestamp")
    if start is not None and end is not None:
        try:
            return max(int(end) - int(start), 0)
        except (TypeError, ValueError):
            return None
    return None


def _upsert_transcript(db: Session, call: Call, pcall: dict) -> None:
    text, segments = pcall.get("transcript"), pcall.get("transcript_object")
    if not text and not segments:
        return
    row = db.query(CallTranscript).filter(CallTranscript.call_id == call.id).first()
    if row is None:
        db.add(CallTranscript(call_id=call.id, transcript={"text": text, "segments": segments}))
    else:
        row.transcript = {"text": text or row.transcript.get("text"), "segments": segments or row.transcript.get("segments")}


def _upsert_lead(db: Session, call: Call, pcall: dict) -> None:
    analysis = pcall.get("call_analysis") if isinstance(pcall.get("call_analysis"), dict) else {}
    custom = analysis.get("custom_analysis_data") if isinstance(analysis.get("custom_analysis_data"), dict) else {}
    if not custom.get("captured_lead"):
        return
    if db.query(Lead).filter(Lead.call_id == call.id).first() is not None:
        return
    try:
        with db.begin_nested():
            db.add(
                Lead(
                    organization_id=call.organization_id,
                    call_id=call.id,
                    name=(custom.get("caller_name") or None),
                    phone=str(custom.get("callback_number") or call.caller_number or "")[:32] or None,
                    reason=custom.get("reason") or None,
                    summary=analysis.get("call_summary") or None,
                    source="test_call" if call.direction == CallDirection.test else "call",
                )
            )
            db.flush()
    except IntegrityError:
        pass  # a concurrent worker already created it


def _upsert_usage(db: Session, call: Call, pcall: dict) -> None:
    duration_ms = _duration_ms(pcall)
    if duration_ms is None:
        logger.warning("call %s ended without a duration; usage will be recorded when it arrives", call.retell_call_id)
        return
    cost_raw = pcall.get("call_cost") if isinstance(pcall.get("call_cost"), dict) else None
    cost = _decimal(cost_raw.get("combined_cost")) if cost_raw else None

    entry = db.query(UsageLedgerEntry).filter(UsageLedgerEntry.retell_call_id == call.retell_call_id).first()
    if entry is None:
        sub = current_subscription(db, call.organization_id)
        subscribed = sub is not None and sub.status in ACTIVE_STATUSES
        if call.direction != CallDirection.test:
            kind = "production"
        elif db.query(FreeTestAllowance.id).filter(FreeTestAllowance.retell_call_id == call.retell_call_id).first() is not None:
            # Classified when the call was ADMITTED: it consumed the free allowance, so it stays free (recorded with
            # its real cost, never charged) even if its webhook arrives late, after the customer has subscribed.
            kind = "free_test"
        else:
            # Not admitted through the free flow: a test call while subscribed spends credits like any call;
            # without a subscription it falls back to the free classification as before.
            kind = "test" if subscribed else "free_test"
        try:
            with db.begin_nested():
                db.add(
                    UsageLedgerEntry(
                        organization_id=call.organization_id,
                        call_id=call.id,
                        retell_call_id=call.retell_call_id,
                        kind=kind,
                        duration_ms=duration_ms,
                        billable_seconds=(duration_ms + 500) // 1000,
                        provider_cost=cost,
                        provider_cost_raw=cost_raw,
                        occurred_at=call.ended_at or datetime.now(timezone.utc),
                        period_start=sub.current_period_start if sub and kind != "free_test" else None,
                        period_end=sub.current_period_end if sub and kind != "free_test" else None,
                    )
                )
                db.flush()
            if kind == "free_test" and duration_ms == 0:
                free_test.release_if_never_connected(db, call.retell_call_id)
            return
        except IntegrityError:
            entry = db.query(UsageLedgerEntry).filter(UsageLedgerEntry.retell_call_id == call.retell_call_id).one()
    # Same call seen again (duplicate, or call_analyzed after call_ended): SET, never add.
    entry.duration_ms = duration_ms
    entry.billable_seconds = (duration_ms + 500) // 1000
    if cost is not None:
        entry.provider_cost, entry.provider_cost_raw = cost, cost_raw
