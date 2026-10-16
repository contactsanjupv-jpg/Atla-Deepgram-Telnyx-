"""
Filtering, searching, sorting and paging of an organization's calls - everything from rows we really stored.

Time is the call's start (falling back to when we first saw it). Dates are calendar days in the viewer's
timezone, both ends inclusive. Search matches the caller's number (digits only, so "(305) 555" finds
+13055551234) and the call summary; LIKE wildcards in what the customer typed are literal.
"""
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Query, Session

from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead
from app.services.credits import resolve_timezone

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 100
MAX_PAGE_SIZE = 100

Sort = Literal["newest", "oldest", "longest"]


@dataclass(frozen=True)
class CallFilters:
    q: str | None = None
    direction: str | None = None
    status: str | None = None
    sentiment: str | None = None
    has_lead: bool | None = None
    date_from: date | None = None
    date_to: date | None = None
    tz: str | None = None


def call_time():
    return func.coalesce(Call.started_at, Call.created_at)


def clean_query(q: str | None) -> str | None:
    if q is None:
        return None
    term = re.sub(r"\s+", " ", q).strip()[:MAX_QUERY_CHARS]
    return term or None


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _local_midnight_utc(d: date, tz_name: str | None) -> datetime:
    return datetime.combine(d, time.min, tzinfo=resolve_timezone(tz_name)).astimezone(timezone.utc)


def base_query(db: Session, organization_id: uuid.UUID, f: CallFilters) -> Query:
    """Calls joined to the lead they produced (at most one per call), with every filter applied. No ordering."""
    query = (
        db.query(Call, Lead.id, Lead.name, Lead.status)
        .outerjoin(Lead, and_(Lead.call_id == Call.id, Lead.organization_id == Call.organization_id))
        .filter(Call.organization_id == organization_id)
    )
    if f.direction:
        query = query.filter(Call.direction == CallDirection(f.direction))
    if f.status:
        query = query.filter(Call.status == f.status)
    if f.sentiment:
        query = query.filter(func.lower(Call.sentiment) == f.sentiment.lower())
    if f.has_lead is True:
        query = query.filter(Lead.id.is_not(None))
    elif f.has_lead is False:
        query = query.filter(Lead.id.is_(None))
    if f.date_from:
        query = query.filter(call_time() >= _local_midnight_utc(f.date_from, f.tz))
    if f.date_to:
        query = query.filter(call_time() < _local_midnight_utc(f.date_to + timedelta(days=1), f.tz))

    term = clean_query(f.q)
    if term:
        pattern = _like(term)
        conditions = [Call.summary.ilike(pattern, escape="\\"), Call.caller_number.ilike(pattern, escape="\\")]
        digits = re.sub(r"\D", "", term)
        if len(digits) >= MIN_QUERY_CHARS:
            conditions.append(func.regexp_replace(Call.caller_number, r"\D", "", "g").like(f"%{digits}%"))
        query = query.filter(or_(*conditions))
    return query


def order(query: Query, sort: Sort) -> Query:
    if sort == "oldest":
        return query.order_by(call_time().asc(), Call.id.asc())
    if sort == "longest":
        return query.order_by(Call.duration_seconds.desc().nulls_last(), call_time().desc(), Call.id.desc())
    return query.order_by(call_time().desc(), Call.id.desc())


def totals(query: Query) -> tuple[int, int, int]:
    """(calls, seconds on those calls, calls that produced a lead) for the filtered set."""
    n, seconds, leads = query.with_entities(
        func.count(Call.id), func.coalesce(func.sum(Call.duration_seconds), 0), func.count(Lead.id)
    ).one()
    return int(n), int(seconds), int(leads)
