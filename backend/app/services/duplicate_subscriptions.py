"""
Second line of defence against paying twice (the first is services/checkout_guard).

If an org somehow holds more than one trialing/active Paddle subscription, the
OLDEST one is the subscription of record (billing_state.current_subscription uses
the same rule). Every other one is scheduled to cancel at the END of its paid
period: the customer is not charged again, nobody loses access mid-period, and
nothing is refunded or deleted automatically — the first charge of the duplicate
may need a manual refund, which is logged at ERROR level for a human.

Safe to run repeatedly and from concurrent workers: a Redis marker per
subscription means each cancel is requested once; a failed request clears the
marker so it is retried by the next webhook or reconciliation run.
"""
import logging
import uuid

import redis
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.billing import Subscription
from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
from app.services.billing_state import ACTIVE_STATUSES
from app.services.checkout_guard import clear_pending

logger = logging.getLogger("atla.duplicate_subscriptions")
_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
_MARKER_TTL_SECONDS = 86400


def duplicate_subscriptions(db: Session, organization_id: uuid.UUID) -> list[Subscription]:
    rows = (
        db.query(Subscription)
        .filter(
            Subscription.organization_id == organization_id,
            Subscription.billing_provider == "paddle",
            Subscription.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Subscription.created_at.asc(), Subscription.id.asc())
        .all()
    )
    return rows[1:]


def schedule_duplicate_cancellations(db: Session, organization_id: uuid.UUID, provider_factory=None) -> list[str]:
    """Returns the Paddle subscription ids a cancellation was requested for just now."""
    duplicates = [
        s for s in duplicate_subscriptions(db, organization_id) if s.external_subscription_id and s.cancel_effective_at is None
    ]
    if not duplicates:
        return []
    provider = (provider_factory or PaddleBillingProvider)()
    requested: list[str] = []
    for sub in duplicates:
        marker = f"dup-cancel:{sub.external_subscription_id}"
        if not _redis.set(marker, "1", nx=True, ex=_MARKER_TTL_SECONDS):
            continue  # already requested (or being requested by another worker)
        try:
            provider.cancel_subscription(sub.external_subscription_id, immediately=False)
        except Exception as e:  # noqa: BLE001 — retried on the next webhook / reconcile run
            _redis.delete(marker)
            logger.error("Could not schedule cancel of duplicate subscription [org=%s sub=%s]: %s", organization_id, sub.external_subscription_id, e)
            continue
        requested.append(sub.external_subscription_id)
        logger.error(
            "DUPLICATE SUBSCRIPTION: scheduled cancel at period end; customer may have been charged twice — "
            "review a refund [org=%s duplicate=%s]",
            organization_id,
            sub.external_subscription_id,
        )
    return requested


def after_subscription_applied(db: Session, data: dict) -> None:
    """Run AFTER the webhook transaction committed. Never raises: a billing-side-effect
    problem must not turn a correctly stored event into a webhook failure."""
    sub = None
    try:
        external_id = data.get("id")
        sub = (
            db.query(Subscription)
            .filter(Subscription.billing_provider == "paddle", Subscription.external_subscription_id == external_id)
            .first()
        )
        if sub is None or sub.status not in ACTIVE_STATUSES:
            return
        clear_pending(sub.organization_id)
        schedule_duplicate_cancellations(db, sub.organization_id)
    except Exception:  # noqa: BLE001
        logger.exception("Post-webhook duplicate-subscription check failed")
    try:
        from app.services.auto_provisioning import auto_provision_for_org  # local import: avoids a cycle

        if sub is not None and sub.status == "active":
            auto_provision_for_org(db, sub.organization_id)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Post-webhook number provisioning failed")