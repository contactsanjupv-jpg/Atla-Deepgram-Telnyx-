import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.auth.deps import current_membership, require_role
from app.db.base import get_db
from app.db.models.crm import Lead, LeadStatus
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.schemas.calls_leads import LeadOut
from app.services import calls_query, leads_query

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/leads", tags=["leads"])

MAX_PAGE_SIZE = 100


class LeadStatusUpdate(BaseModel):
    status: str


@router.get("", response_model=list[LeadOut])
def list_leads(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=25, le=MAX_PAGE_SIZE, gt=0),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    query = db.query(Lead).filter(Lead.organization_id == membership.organization_id)
    if status_filter:
        try:
            query = query.filter(Lead.status == LeadStatus(status_filter))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status: {status_filter}")
    leads = query.order_by(Lead.created_at.desc()).limit(limit).offset(offset).all()
    return [LeadOut.model_validate(lead) for lead in leads]

class LeadRow(LeadOut):
    """A lead plus the call it came from, if it came from one (leads can also exist without a call)."""

    call_id: str | None = None
    call_started_at: datetime | None = None
    call_direction: str | None = None
    call_duration_seconds: int | None = None


class LeadPage(BaseModel):
    items: list[LeadRow]
    total: int  # leads matching the search and status
    counts: dict[str, int]  # per status, for the search (ignores the status filter)
    limit: int
    offset: int


def _lead_row(lead: Lead, call) -> LeadRow:
    return LeadRow(
        **LeadOut.model_validate(lead).model_dump(),
        call_id=str(call.id) if call else None,
        call_started_at=(call.started_at or call.created_at) if call else None,
        call_direction=call.direction.value if call and hasattr(call.direction, "value") else (call.direction if call else None),
        call_duration_seconds=call.duration_seconds if call else None,
    )

@router.get("/browse", response_model=LeadPage)
def browse_leads(
    q: str | None = Query(default=None, max_length=200),
    lead_status: Literal["new", "contacted", "booked", "closed"] | None = Query(default=None, alias="status"),
    sort: Literal["newest", "oldest"] = "newest",
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """Every lead this organization has captured: searchable, filterable by status, pageable. All plans."""
    term = calls_query.clean_query(q)
    if term is not None and len(term) < calls_query.MIN_QUERY_CHARS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Type at least {calls_query.MIN_QUERY_CHARS} characters to search.")
    searched = leads_query.base_query(db, membership.organization_id, term)
    counts = leads_query.status_counts(searched)
    filtered = leads_query.with_status(searched, lead_status)
    total = counts[lead_status] if lead_status else sum(counts.values())
    rows = leads_query.order(filtered, sort).limit(limit).offset(offset).all()
    return LeadPage(items=[_lead_row(*r) for r in rows], total=total, counts=counts, limit=limit, offset=offset)

@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead_status(
    lead_id: uuid.UUID,
    payload: LeadStatusUpdate,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.organization_id == membership.organization_id).first()
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")

    try:
        lead.status = LeadStatus(payload.status)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status: {payload.status}")

    db.commit()
    db.refresh(lead)
    return LeadOut.model_validate(lead)