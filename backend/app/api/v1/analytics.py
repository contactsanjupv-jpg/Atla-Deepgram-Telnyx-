"""
Growth analytics: real trends from calls, credits and leads this organization actually has.

Everything here is a count or a sum over stored rows, grouped by calendar day in the viewer's timezone.
A day with nothing in it is a true zero. There are no revenue, ROI, conversion or savings figures:
the system does not record them, so it does not show them.
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import require_feature
from app.db.base import get_db
from app.db.models.crm import Lead, LeadStatus
from app.db.models.tenancy import OrganizationMember
from app.services.credits import daily_usage, resolve_timezone
from app.services.plans import Feature

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/analytics", tags=["analytics"])


class AnalyticsDay(BaseModel):
    date: str
    calls: int
    credit_seconds: int
    leads: int


class AnalyticsOut(BaseModel):
    days: int
    series: list[AnalyticsDay]
    total_calls: int
    total_credit_seconds: int
    total_leads: int
    leads_by_status: dict[str, int]  # leads created in the window, by current status
    leads_awaiting_follow_up: int  # all leads still marked "new" (not limited to the window)
    oldest_unattended_lead_at: datetime | None


@router.get("/overview", response_model=AnalyticsOut)
def overview(
    days: int = Query(default=30, ge=1, le=90),
    tz: str | None = Query(default=None, max_length=64),
    membership: OrganizationMember = Depends(require_feature(Feature.analytics)),
    db: Session = Depends(get_db),
):
    org_id = membership.organization_id
    zone = resolve_timezone(tz)
    usage = daily_usage(db, org_id, days, tz)
    first = date.fromisoformat(usage[0]["date"])
    start_utc = datetime.combine(first, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)

    day = func.date(func.timezone(zone.key, Lead.created_at)).label("day")
    lead_rows = (
        db.query(day, func.count()).filter(Lead.organization_id == org_id, Lead.created_at >= start_utc).group_by(day).all()
    )
    leads_by_day = {r[0]: int(r[1]) for r in lead_rows}

    series = [
        AnalyticsDay(
            date=u["date"],
            calls=u["calls"],
            credit_seconds=u["credit_seconds"],
            leads=leads_by_day.get(date.fromisoformat(u["date"]), 0),
        )
        for u in usage
    ]
    status_rows = (
        db.query(Lead.status, func.count()).filter(Lead.organization_id == org_id, Lead.created_at >= start_utc).group_by(Lead.status).all()
    )
    awaiting = db.query(func.count(Lead.id), func.min(Lead.created_at)).filter(
        Lead.organization_id == org_id, Lead.status == LeadStatus.new
    ).one()
    return AnalyticsOut(
        days=days,
        series=series,
        total_calls=sum(d.calls for d in series),
        total_credit_seconds=sum(d.credit_seconds for d in series),
        total_leads=sum(d.leads for d in series),
        leads_by_status={s.value: int(n) for s, n in status_rows},
        leads_awaiting_follow_up=int(awaiting[0]),
        oldest_unattended_lead_at=awaiting[1],
    )
