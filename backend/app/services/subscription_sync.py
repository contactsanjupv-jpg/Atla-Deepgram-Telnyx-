"""
Applies one Paddle subscription snapshot to our `subscriptions` row. Shared by
the webhook (event data + event.occurred_at) and the reconciliation job (GET
/subscriptions/{id} data + its updated_at), so both obey the same rules:

* one local row per Paddle subscription id
* a snapshot older than the newest one already applied is ignored (webhooks are
  at-least-once and unordered; an old `active` can never resurrect a `canceled`)
* the PLAN comes from the subscription's actual price ids (services/plans.py);
  custom_data.plan_id is only a fallback for payloads that carry no items
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models.billing import Subscription
from app.db.models.tenancy import Organization
from app.services.credits import ensure_plan_grant
from app.services.plans import UNKNOWN_PLAN, plan_id_from_items

logger = logging.getLogger("atla.subscription_sync")


def parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def resolve_plan_id(data: dict, existing: str | None) -> str:
    from_items = plan_id_from_items(data.get("items"))
    if from_items is not None:
        if from_items == UNKNOWN_PLAN:
            logger.error("Paddle subscription %s has a price that maps to no configured plan", data.get("id"))
        return from_items
    custom = data.get("custom_data") or {}
    return custom.get("plan_id") or existing or UNKNOWN_PLAN


def sync_subscription(db: Session, data: dict, occurred_at: datetime) -> bool:
    """Returns True if the snapshot was applied, False if ignored."""
    external_id = data.get("id")
    new_status = data.get("status")
    custom = data.get("custom_data") or {}
    if not external_id or not new_status:
        logger.error("Paddle subscription snapshot missing id/status")
        return False

    subscription = (
        db.query(Subscription)
        .filter(Subscription.billing_provider == "paddle", Subscription.external_subscription_id == external_id)
        .with_for_update()
        .first()
    )
    if subscription is None:
        org_id = parse_uuid(custom.get("organization_id"))
        if org_id is None or db.get(Organization, org_id) is None:
            logger.error("Paddle subscription %s has no valid organization_id in custom_data", external_id)
            return False
        subscription = Subscription(
            organization_id=org_id,
            billing_provider="paddle",
            external_subscription_id=external_id,
            plan_id=resolve_plan_id(data, None),
            status=new_status,
            status_changed_at=occurred_at,
        )
        db.add(subscription)
    else:
        if subscription.last_event_at is not None and occurred_at < subscription.last_event_at:
            logger.warning("Ignoring stale Paddle snapshot for %s (%s < %s)", external_id, occurred_at, subscription.last_event_at)
            return False
        if (
            subscription.status == "canceled"
            and new_status != "canceled"
            and subscription.last_event_at is not None
            and occurred_at <= subscription.last_event_at
        ):
            logger.warning("Ignoring non-canceled snapshot for already-canceled subscription %s", external_id)
            return False
        if subscription.status != new_status:
            subscription.status_changed_at = occurred_at
        subscription.status = new_status
        subscription.plan_id = resolve_plan_id(data, subscription.plan_id)

    subscription.last_event_at = occurred_at
    scheduled = data.get("scheduled_change") if isinstance(data.get("scheduled_change"), dict) else None
    subscription.cancel_effective_at = (
        parse_dt(scheduled.get("effective_at")) if scheduled and scheduled.get("action") == "cancel" else None
    )
    period = data.get("current_billing_period") or {}
    subscription.current_period_start = parse_dt(period.get("starts_at")) or subscription.current_period_start
    subscription.current_period_end = parse_dt(period.get("ends_at")) or subscription.current_period_end

    # Monthly included credits: one grant per billing period, created here for every path that
    # applies a snapshot (webhook AND reconciler), idempotent by (subscription, period start).
    db.flush()
    ensure_plan_grant(db, subscription)
    return True
