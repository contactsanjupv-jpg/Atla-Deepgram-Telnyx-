"""
The customer-facing view of an org's subscription: what the plan is called, what
it includes right now, and whether paid features are unlocked. Computed here so
the frontend only renders it — it never decides entitlement itself.
"""
import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.billing_state import current_subscription
from app.services.plans import FEATURE_LABELS, PLANS, Feature, active_plan


def subscription_view(db: Session, organization_id: uuid.UUID) -> dict | None:
    sub = current_subscription(db, organization_id)
    if sub is None:
        return None
    entitled_plan = active_plan(db, organization_id)  # trialing/active AND a recognised plan
    known_plan = PLANS.get(sub.plan_id)

    service_ends_at = None
    if sub.status == "past_due":
        # Same rule services/entitlement.suspended_since enforces: service continues for the grace period.
        since = sub.status_changed_at or sub.last_event_at
        if since is not None:
            service_ends_at = since + timedelta(days=get_settings().PAST_DUE_GRACE_DAYS)

    return {
        "plan_id": sub.plan_id,
        "plan_name": known_plan.name if known_plan else None,
        "status": sub.status,
        "entitled": entitled_plan is not None,
        "features": (
            [{"id": f.value, "label": FEATURE_LABELS[f]} for f in Feature if f in entitled_plan.features]
            if entitled_plan is not None
            else []
        ),
        "current_period_start": sub.current_period_start,
        "current_period_end": sub.current_period_end,
        "cancel_effective_at": sub.cancel_effective_at,
        # Accepted by the provider but not yet confirmed in our state; only meaningful while nothing is scheduled.
        "cancel_requested_at": (
            sub.cancel_requested_at if sub.cancel_effective_at is None and sub.status in ("trialing", "active") else None
        ),
        "service_ends_at": service_ends_at,
    }