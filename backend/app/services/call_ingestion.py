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
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
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
    except (TypeError, ValueError, OverflowError, OSError):  # OSError: out-of-range epoch on some platforms
        return None


_MAX_COST_CENTS = Decimal("1000000000")  # far above any real call; keeps the Numeric(14, 4) column from overflowing


def _decimal(value) -> Decimal | None:
    """A usable cost, or None. NaN/Infinity/negative/absurd values are provider garbage: storing them would poison
    every SUM over the ledger or make the insert fail, so they are treated as "no cost reported"."""
    try:
        d = Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None
    if d is None or not d.is_finite() or d < 0 or d > _MAX_COST_CENTS:
        return None
    return d


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
    agent = None
    if pcall.get("agent_id"):
        agent = db.query(Agent).filter(Agent.retell_agent_id == pcall["agent_id"]).first()
    if call is not None:
        # An event may only update a call owned by the tenant of the agent it names.
        if agent is not None and agent.organization_id != call.organization_id:
            raise UnresolvedCall(f"event for call {call_id} names an agent of a different organization")
        return call

    # Only OUR side of the call identifies the tenant: the number that was called on an inbound call, the number
    # dialled FROM on an outbound one. The other party's number may belong to another Atla customer, and must
    # never decide who is billed. Web calls have no numbers.
    if pcall.get("call_type") == "web_call":
        our_number_field = None
    elif pcall.get("direction") == "outbound":
        our_number_field = "from_number"
    else:
        our_number_field = "to_number"
    phone = None
    if our_number_field and pcall.get(our_number_field):
        phone = _resolve_phone(db, agent, pcall[our_number_field], call_id)
    if agent is None and phone is None:
        raise UnresolvedCall(f"no agent/phone matches call {call_id}")
    if agent is not None and phone is not None and agent.organization_id != phone.organization_id:
        raise UnresolvedCall(f"agent and phone belong to different organizations for call {call_id}")
    if agent is None and phone.agent_id is not None:
        # Number-only event: the agent the stored number is attached to (same org by construction).
        agent = db.query(Agent).filter(Agent.id == phone.agent_id, Agent.organization_id == phone.organization_id).first()
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


def _resolve_phone(db: Session, agent: Agent | None, number: str, call_id: str) -> PhoneNumber | None:
    """The stored number row for OUR side of the call. phone_numbers.number is not unique: a released number can
    be re-provisioned to another org, so the same E.164 may exist in several orgs. Never pick one arbitrarily:
    the agent's own org wins; otherwise a live (non-released) row; otherwise it must be unambiguous."""
    rows = (
        db.query(PhoneNumber)
        .filter(PhoneNumber.number == number)
        .order_by(PhoneNumber.created_at.desc(), PhoneNumber.id)
        .all()
    )
    if not rows:
        return None
    if agent is not None:
        own = [r for r in rows if r.organization_id == agent.organization_id]
        if own:
            return _prefer_live(own)
        # Only other organizations own this number: never link it; the caller reports the mismatch.
        return _prefer_live(rows)
    live = [r for r in rows if r.status != PhoneNumberStatus.released] or rows
    if len({r.organization_id for r in live}) > 1:
        raise UnresolvedCall(f"number on call {call_id} belongs to several organizations; cannot attribute without an agent")
    return _prefer_live(live)


def _prefer_live(rows: list[PhoneNumber]) -> PhoneNumber:
    """rows are newest-first; take the newest non-released one, else the newest."""
    return next((r for r in rows if r.status != PhoneNumberStatus.released), rows[0])


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
    """Call length in ms, or None when it is not known. A malformed or negative value is NOT "0 seconds" (that would
    also release a free-test allowance as "never connected"): it is ignored and the timestamps are tried instead."""
    try:
        value = int(pcall["duration_ms"]) if pcall.get("duration_ms") is not None else None
    except (TypeError, ValueError):
        value = None
    if value is not None and value >= 0:
        return value
    start, end = pcall.get("start_timestamp"), pcall.get("end_timestamp")
    if start is not None and end is not None:
        try:
            span = int(end) - int(start)
        except (TypeError, ValueError):
            return None
        return span if span >= 0 else None
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


def _is_true(value) -> bool:
    """Retell's custom analysis returns a real boolean for a boolean field, but a text field (or a prompt that asks
    for "true"/"false" text) arrives as a string, and any non-empty string is truthy in Python: "false" must not count."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1")
    return isinstance(value, (int, float)) and value == 1


def _clean_text(value, limit: int) -> str | None:
    """Stripped text cut to the column size, or None when empty. A value that is too long must never fail the whole
    event (it would roll back the call and its usage), and whitespace is not data."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text[:limit] or None


def _callback_phone(value) -> str | None:
    """A callback number only if it looks like one: 7-15 digits (E.164 allows 15) in at most 32 characters. Spoken
    words, fragments or an over-long string are not a phone number, and cutting one to fit would store a WRONG number."""
    text = _clean_text(value, 64)
    if text is None or len(text) > 32:
        return None
    digits = sum(ch.isdigit() for ch in text)
    return text if 7 <= digits <= 15 else None


def _upsert_lead(db: Session, call: Call, pcall: dict) -> None:
    analysis = pcall.get("call_analysis") if isinstance(pcall.get("call_analysis"), dict) else {}
    custom = analysis.get("custom_analysis_data") if isinstance(analysis.get("custom_analysis_data"), dict) else {}
    if not _is_true(custom.get("captured_lead")):
        return
    if db.query(Lead).filter(Lead.call_id == call.id).first() is not None:
        return
    # The number the caller asked to be called back on wins; otherwise the number they called from.
    phone = _callback_phone(custom.get("callback_number")) or _clean_text(call.caller_number, 32)
    try:
        with db.begin_nested():
            db.add(
                Lead(
                    organization_id=call.organization_id,
                    call_id=call.id,
                    name=_clean_text(custom.get("caller_name"), 255),
                    phone=phone,
                    reason=_clean_text(custom.get("reason"), 2000),
                    summary=_clean_text(analysis.get("call_summary"), 4000),
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
