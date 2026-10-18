"""
What a lapsed subscription does to live service. The policy is explicit and
configurable (config.PAST_DUE_GRACE_DAYS, NUMBER_RELEASE_DELAY_DAYS):

    active/trialing ............ service on
    past_due, inside grace ..... service on
    past_due beyond grace, paused, canceled, no subscription
                                 -> SUSPENDED: number unassigned (calls stop being
                                    answered, stop costing minutes), agent paused
    suspended for N days ....... number released (stops the $2/mo)
    active but OUT OF CREDITS beyond the overage grace (config.OVERAGE_GRACE_MINUTES)
                                 -> PAUSED the same way, but the number is KEPT: the
                                    customer paid for it. Buying credits and pressing
                                    "Go live" resumes service.

Every step is idempotent and retried by the sweeper; a provider failure for one
org never blocks another and leaves state to be retried.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.phone.phone_provider import PhoneProvider
from app.services.billing_state import current_subscription, has_active_subscription
from app.services.credits import credits_exhausted_beyond_grace
from app.services.phone_provisioning import release_number

logger = logging.getLogger("atla.entitlement")
settings = get_settings()


def suspended_since(db: Session, org_id: uuid.UUID, now: datetime) -> datetime | None:
    """None while service is entitled; otherwise when suspension began."""
    if has_active_subscription(db, org_id):
        # Paid up, but the allowance AND purchased balance are spent beyond the grace: stop spending our money.
        return now if credits_exhausted_beyond_grace(db, org_id, now) else None
    sub = current_subscription(db, org_id)
    if sub is None:
        return now  # no subscription at all but live resources: suspend immediately
    since = sub.status_changed_at or sub.last_event_at or now
    if sub.status == "past_due":
        grace_ends = since + timedelta(days=settings.PAST_DUE_GRACE_DAYS)
        return None if now < grace_ends else grace_ends
    return since


def enforce_entitlements(
    db: Session, provider: PhoneProvider, now: datetime | None = None, organization_ids: set[uuid.UUID] | None = None
) -> dict:
    now = now or datetime.now(timezone.utc)
    result = {"suspended": 0, "released": 0, "errors": 0}
    org_ids = {
        row[0]
        for row in db.query(PhoneNumber.organization_id).filter(PhoneNumber.status == PhoneNumberStatus.active).all()
    } | {row[0] for row in db.query(Agent.organization_id).filter(Agent.status == AgentStatus.active).all()}

    if organization_ids is not None:
        org_ids &= organization_ids
    for org_id in org_ids:
        since = suspended_since(db, org_id, now)
        if since is None:
            continue
        try:
            for phone in db.query(PhoneNumber).filter(PhoneNumber.organization_id == org_id, PhoneNumber.status == PhoneNumberStatus.active).all():
                if phone.agent_id is not None:
                    provider.unassign_agent(phone.retell_phone_number_id)
                    phone.agent_id = None
                    db.commit()
                    result["suspended"] += 1
            for agent in db.query(Agent).filter(Agent.organization_id == org_id, Agent.status == AgentStatus.active).all():
                agent.status = AgentStatus.paused
            db.commit()
            if now >= since + timedelta(days=settings.NUMBER_RELEASE_DELAY_DAYS):
                for phone in db.query(PhoneNumber).filter(PhoneNumber.organization_id == org_id, PhoneNumber.status == PhoneNumberStatus.active).all():
                    release_number(db, phone, provider, reason="entitlement")
                    result["released"] += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            result["errors"] += 1
            logger.exception("Entitlement enforcement failed [org=%s] — will retry", org_id)
    return result
