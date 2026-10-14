"""
Read-only calls endpoints. Every query below filters by
membership.organization_id — the same tenant-isolation pattern as every
other route in this app (see app/auth/deps.py:current_membership). A call
belonging to another org returns 404, never 403, so existence is never
confirmed to a non-member either.
"""
import uuid

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_feature
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.db.base import get_db
from app.db.models.crm import Lead
from app.db.models.calls import Call, CallTranscript
from app.db.models.tenancy import OrganizationMember
from app.schemas.calls_leads import CallOut
from app.services import calls_query, transcripts
from app.services.plans import Feature

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/calls", tags=["calls"])

MAX_PAGE_SIZE = 100


@router.get("", response_model=list[CallOut])
def list_calls(
    limit: int = Query(default=25, le=MAX_PAGE_SIZE, gt=0),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    calls = (
        db.query(Call)
        .filter(Call.organization_id == membership.organization_id)
        .order_by(Call.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [CallOut.model_validate(c) for c in calls]

class CallRow(CallOut):
    """A call plus the lead it produced, if any (a call produces at most one lead)."""

    lead_id: str | None = None
    lead_name: str | None = None
    lead_status: str | None = None


class CallPage(BaseModel):
    items: list[CallRow]
    total: int  # calls matching the filters
    total_seconds: int  # time on those calls
    leads_captured: int  # of those calls, how many produced a lead
    limit: int
    offset: int


def _row(call: Call, lead_id, lead_name, lead_status) -> CallRow:
    return CallRow(
        **CallOut.model_validate(call).model_dump(),
        lead_id=str(lead_id) if lead_id else None,
        lead_name=lead_name,
        lead_status=lead_status.value if hasattr(lead_status, "value") else lead_status,
    )


@router.get("/browse", response_model=CallPage)
def browse_calls(
    q: str | None = Query(default=None, max_length=200),
    direction: Literal["inbound", "outbound", "test"] | None = None,
    call_status: Literal["registered", "ongoing", "ended", "error"] | None = Query(default=None, alias="status"),
    sentiment: Literal["positive", "negative", "neutral", "unknown"] | None = None,
    has_lead: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    tz: str | None = Query(default=None, max_length=64),
    sort: Literal["newest", "oldest", "longest"] = "newest",
    limit: int = Query(default=25, ge=1, le=calls_query.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """
    Every call this organization has had, filterable and pageable. All plans: it returns call metadata and the
    summary only; the full conversation stays a Growth feature (GET /{id}/transcript).
    """
    term = calls_query.clean_query(q)
    if term is not None and len(term) < calls_query.MIN_QUERY_CHARS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Type at least {calls_query.MIN_QUERY_CHARS} characters to search.")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The start date is after the end date.")
    filters = calls_query.CallFilters(
        q=term, direction=direction, status=call_status, sentiment=sentiment, has_lead=has_lead,
        date_from=date_from, date_to=date_to, tz=tz,
    )
    query = calls_query.base_query(db, membership.organization_id, filters)
    total, seconds, leads = calls_query.totals(query)
    rows = calls_query.order(query, sort).limit(limit).offset(offset).all()
    return CallPage(
        items=[_row(*r) for r in rows], total=total, total_seconds=seconds, leads_captured=leads, limit=limit, offset=offset
    )

class SearchMatch(BaseModel):
    speaker: str
    snippet: str
    start_seconds: float | None


class SearchHit(BaseModel):
    call_id: str
    direction: str
    caller_number: str | None
    started_at: datetime | None
    summary: str | None
    matches: list[SearchMatch]
    match_count: int


@router.get("/search", response_model=list[SearchHit])
def search_calls(
    q: str = Query(min_length=transcripts.MIN_QUERY_CHARS, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    membership: OrganizationMember = Depends(require_feature(Feature.transcript_search)),
    db: Session = Depends(get_db),
):
    """Growth: search what callers actually said, across this organization's stored transcripts only."""
    try:
        check_rate_limit(f"transcript-search:{membership.organization_id}", 60, 60)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many searches. Try again in a moment.") from e
    return transcripts.search_transcripts(db, membership.organization_id, q, limit)


class CallDetail(CallRow):
    """One call with the details of the lead it produced. Never carries the transcript (that is Growth-only, below)."""

    lead_phone: str | None = None
    lead_email: str | None = None
    lead_reason: str | None = None
    lead_summary: str | None = None


@router.get("/{call_id}", response_model=CallDetail)
def get_call(
    call_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    row = (
        db.query(Call, Lead)
        .outerjoin(Lead, (Lead.call_id == Call.id) & (Lead.organization_id == Call.organization_id))
        .filter(Call.id == call_id, Call.organization_id == membership.organization_id)
        .first()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    call, lead = row
    base = _row(call, lead.id if lead else None, lead.name if lead else None, lead.status if lead else None)
    return CallDetail(
        **base.model_dump(),
        lead_phone=lead.phone if lead else None,
        lead_email=lead.email if lead else None,
        lead_reason=lead.reason if lead else None,
        lead_summary=lead.summary if lead else None,
    )


@router.get("/{call_id}/transcript")
def get_call_transcript(
    call_id: uuid.UUID,
    membership: OrganizationMember = Depends(require_feature(Feature.transcripts)),
    db: Session = Depends(get_db),
):
    """
    Growth: the full conversation with speaker labels and timestamps. Starter gets 402 here — the
    summary and call metadata stay on GET /calls/{id}. The plan check runs BEFORE any transcript is read.
    """
    call = db.query(Call).filter(Call.id == call_id, Call.organization_id == membership.organization_id).first()
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")

    transcript = db.query(CallTranscript).filter(CallTranscript.call_id == call.id).first()
    if transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No transcript available for this call")
    return {"call_id": str(call.id), **transcripts.shape_transcript(transcript.transcript)}


# Recording playback intentionally not exposed yet: spec requires private
# storage + short-lived signed URLs (never a raw storage key or public
# link), and no storage/signing provider is wired up yet. Add a
# GET /{call_id}/recording-url endpoint that generates a signed URL
# on demand once that provider exists — do not add a field that returns
# `recording_storage_key` directly to the frontend in the meantime.