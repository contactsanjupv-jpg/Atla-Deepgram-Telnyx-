"""
Dashboard summary: what the receptionist actually did in the last day / 7 days / 30 days.

Every figure is a count or a sum over rows this organization really has (calls and leads). A bucket with
nothing in it is a true zero. Nothing is estimated: no revenue, no ROI, no "missed call" or conversion
figures, because the system does not record them. Real test calls are excluded - this is customer traffic.
Available on every plan (it only reads calls and leads); Growth analytics is the longer trend view.

range=today  -> 24 hourly buckets of today, compared with yesterday
range=7d     -> 7 daily buckets (today included), compared with the 7 days before
range=30d    -> 30 daily buckets (today included), compared with the 30 days before
Buckets are calendar hours/days in the viewer's timezone (`tz`, an IANA name; unknown -> UTC).
"""
from datetime import datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import current_membership
from app.db.base import get_db
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead, LeadStatus
from app.db.models.tenancy import OrganizationMember
from app.services.credits import resolve_timezone

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/dashboard", tags=["dashboard"])

RANGE_DAYS = {"today": 1, "7d": 7, "30d": 30}
AWAITING_LIMIT = 5


class Bucket(BaseModel):
    start: str  # local ISO time of the bucket start: "2026-10-12T14:00:00" (hourly) or "2026-10-12" (daily)
    calls: int
    call_seconds: int
    leads: int


class Totals(BaseModel):
    calls: int
    call_seconds: int
    leads: int


class AwaitingLead(BaseModel):
    id: str
    name: str | None
    phone: str | None
    reason: str | None
    created_at: datetime
    call_id: str | None


class SummaryOut(BaseModel):
    range: str
    granularity: Literal["hour", "day"]
    series: list[Bucket]
    totals: Totals
    previous: Totals  # the equally long window right before this one (yesterday for "today")
    leads_awaiting_follow_up: int  # all leads still "new", not limited to the window
    oldest_unattended_lead_at: datetime | None
    awaiting_leads: list[AwaitingLead]  # oldest waiting first


def _call_time():
    return func.coalesce(Call.started_at, Call.created_at)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _totals(db: Session, org_id, start_utc: datetime, end_utc: datetime) -> Totals:
    calls, seconds = (
        db.query(func.count(Call.id), func.coalesce(func.sum(Call.duration_seconds), 0))
        .filter(Call.organization_id == org_id, Call.direction != CallDirection.test, _call_time() >= start_utc, _call_time() < end_utc)
        .one()
    )
    leads = db.query(func.count(Lead.id)).filter(Lead.organization_id == org_id, Lead.created_at >= start_utc, Lead.created_at < end_utc).scalar()
    return Totals(calls=int(calls), call_seconds=int(seconds), leads=int(leads))


@router.get("/summary", response_model=SummaryOut)
def summary(
    window: Literal["today", "7d", "30d"] = Query(default="7d", alias="range"),
    tz: str | None = Query(default=None, max_length=64),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    org_id = membership.organization_id
    zone = resolve_timezone(tz)
    now = _now()
    days = RANGE_DAYS[window]
    today = now.astimezone(zone).date()
    first = today - timedelta(days=days - 1)

    def local_midnight(d):
        return datetime.combine(d, time.min, tzinfo=zone).astimezone(timezone.utc)

    start_utc = local_midnight(first)
    end_utc = local_midnight(today + timedelta(days=1))
    prev_start_utc = local_midnight(first - timedelta(days=days))

    hourly = window == "today"
    unit = "hour" if hourly else "day"
    call_bucket = func.date_trunc(unit, func.timezone(zone.key, _call_time())).label("b")
    lead_bucket = func.date_trunc(unit, func.timezone(zone.key, Lead.created_at)).label("b")

    call_rows = (
        db.query(call_bucket, func.count(Call.id), func.coalesce(func.sum(Call.duration_seconds), 0))
        .filter(Call.organization_id == org_id, Call.direction != CallDirection.test, _call_time() >= start_utc, _call_time() < end_utc)
        .group_by(call_bucket)
        .all()
    )
    lead_rows = (
        db.query(lead_bucket, func.count(Lead.id))
        .filter(Lead.organization_id == org_id, Lead.created_at >= start_utc, Lead.created_at < end_utc)
        .group_by(lead_bucket)
        .all()
    )
    calls_by = {r[0]: (int(r[1]), int(r[2])) for r in call_rows}
    leads_by = {r[0]: int(r[1]) for r in lead_rows}

    step = timedelta(hours=1) if hourly else timedelta(days=1)
    count = 24 if hourly else days
    cursor = datetime.combine(first, time.min)  # naive local time, matching date_trunc on a local timestamp
    series = []
    for _ in range(count):
        c, s = calls_by.get(cursor, (0, 0))
        series.append(
            Bucket(
                start=cursor.isoformat() if hourly else cursor.date().isoformat(),
                calls=c,
                call_seconds=s,
                leads=leads_by.get(cursor, 0),
            )
        )
        cursor += step

    totals = Totals(
        calls=sum(b.calls for b in series), call_seconds=sum(b.call_seconds for b in series), leads=sum(b.leads for b in series)
    )
    awaiting = db.query(func.count(Lead.id), func.min(Lead.created_at)).filter(Lead.organization_id == org_id, Lead.status == LeadStatus.new).one()
    oldest = (
        db.query(Lead)
        .filter(Lead.organization_id == org_id, Lead.status == LeadStatus.new)
        .order_by(Lead.created_at.asc())
        .limit(AWAITING_LIMIT)
        .all()
    )
    return SummaryOut(
        range=window,
        granularity=unit,
        series=series,
        totals=totals,
        previous=_totals(db, org_id, prev_start_utc, start_utc),
        leads_awaiting_follow_up=int(awaiting[0]),
        oldest_unattended_lead_at=awaiting[1],
        awaiting_leads=[
            AwaitingLead(id=str(l.id), name=l.name, phone=l.phone, reason=l.reason, created_at=l.created_at, call_id=str(l.call_id) if l.call_id else None)
            for l in oldest
        ],
    )