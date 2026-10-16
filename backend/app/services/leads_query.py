"""
Searching, filtering and paging of an organization's leads, joined to the call each one came from.

Everything returned is a stored row; nothing is derived or guessed. Search matches the lead's name, email,
reason and notes (case-insensitive) and their phone number in any format. LIKE wildcards typed by the
customer are literal.
"""
import re
import uuid
from typing import Literal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Query, Session

from app.db.models.calls import Call
from app.db.models.crm import Lead, LeadStatus
from app.services.calls_query import MIN_QUERY_CHARS, _like, clean_query

Sort = Literal["newest", "oldest"]


def base_query(db: Session, organization_id: uuid.UUID, q: str | None) -> Query:
    """Leads of this organization with their call (if any), search applied, status NOT applied."""
    query = (
        db.query(Lead, Call)
        .outerjoin(Call, and_(Call.id == Lead.call_id, Call.organization_id == Lead.organization_id))
        .filter(Lead.organization_id == organization_id)
    )
    term = clean_query(q)
    if term:
        pattern = _like(term)
        conditions = [
            Lead.name.ilike(pattern, escape="\\"),
            Lead.email.ilike(pattern, escape="\\"),
            Lead.reason.ilike(pattern, escape="\\"),
            Lead.summary.ilike(pattern, escape="\\"),
            Lead.phone.ilike(pattern, escape="\\"),
        ]
        digits = re.sub(r"\D", "", term)
        if len(digits) >= MIN_QUERY_CHARS:
            conditions.append(func.regexp_replace(Lead.phone, r"\D", "", "g").like(f"%{digits}%"))
        query = query.filter(or_(*conditions))
    return query


def with_status(query: Query, status: str | None) -> Query:
    return query.filter(Lead.status == LeadStatus(status)) if status else query


def order(query: Query, sort: Sort) -> Query:
    if sort == "oldest":
        return query.order_by(Lead.created_at.asc(), Lead.id.asc())
    return query.order_by(Lead.created_at.desc(), Lead.id.desc())


def status_counts(query: Query) -> dict[str, int]:
    """Leads per status for the searched set (so the tabs show what each would contain). All four keys always present."""
    rows = query.with_entities(Lead.status, func.count(Lead.id)).group_by(Lead.status).all()
    counts = {s.value: 0 for s in LeadStatus}
    for status, n in rows:
        counts[status.value] = int(n)
    return counts
