"""
Automatic phone-number purchase once a Paddle subscription is confirmed ACTIVE.

Paddle stays the payment processor; the number is a separate PROVIDER (Retell) cost paid from the founder's
Retell account. Nothing the customer paid is sent to Retell, and no included minutes or credits change here.

Safe against replays: eligibility is decided from our own database (subscription status, the phone-provisioning
row), never from the webhook body, so a duplicate webhook either finds a number already bought, a purchase in
flight (skipped), or a rejected purchase still inside its cool-down (no provider call). The purchase itself goes
through provision_number(): one row per org, a Redis lock, a deterministic nickname and nickname look-up before
any re-buy. This module never marks anything live: that stays activation.activate_receptionist, which verifies
the number is assigned to the agent at the provider.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.billing import Subscription
from app.db.models.telephony import PhoneProvisioning
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.services.billing_state import current_subscription
from app.services.phone_provisioning import (
    ProvisioningFailed,
    ProvisioningInProgress,
    ProvisioningPending,
    ProvisioningPaymentRequired,
    active_number,
    provision_number,
)
from app.services.plans import Feature, plan_allows

logger = logging.getLogger("atla.auto_provisioning")

SWEEP_LIMIT = 25
# Statuses a rejected/never-sent purchase can be retried from. "failed" is retried ONLY when nothing reached the
# provider (status=0); a definitive provider rejection such as HTTP 400 is not repeated automatically.
_RETRYABLE_FAILURE = "status=0"


def _eligible_subscription(db: Session, org_id: uuid.UUID) -> Subscription | None:
    sub = current_subscription(db, org_id)
    if sub is None or sub.status != "active" or not plan_allows(db, org_id, Feature.phone_number):
        return None
    return sub


def _may_attempt(prov: PhoneProvisioning | None, now: datetime) -> bool:
    if prov is None:
        return True
    cfg = get_settings()
    if prov.status == "requested":
        return True
    retryable = prov.status == "blocked_payment" or (prov.status == "failed" and prov.last_error == _RETRYABLE_FAILURE)
    if not retryable:
        return False  # purchasing / ambiguous (reconcile owns it), purchased, released, or a definitive rejection
    if prov.attempts >= cfg.AUTO_PROVISION_MAX_ATTEMPTS:
        return False
    return prov.updated_at is None or prov.updated_at <= now - timedelta(minutes=cfg.AUTO_PROVISION_RETRY_MINUTES)


def auto_provision_for_org(db: Session, org_id: uuid.UUID, provider=None, now: datetime | None = None) -> str:
    """Returns: disabled | not_eligible | already_provisioned | skipped | purchased | blocked_payment |
    failed | pending | in_progress. Never raises for provider problems."""
    cfg = get_settings()
    if not cfg.AUTO_PROVISION_NUMBERS:
        return "disabled"
    now = now or datetime.now(timezone.utc)
    if _eligible_subscription(db, org_id) is None:
        return "not_eligible"
    if active_number(db, org_id) is not None:
        return "already_provisioned"
    prov = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == org_id).first()
    if not _may_attempt(prov, now):
        return "skipped"
    try:
        provision_number(db, org_id, cfg.AUTO_PROVISION_COUNTRY, None, provider or RetellPhoneProvider())
        return "purchased"
    except ProvisioningInProgress:
        return "in_progress"
    except ProvisioningPending:
        return "pending"
    except ProvisioningPaymentRequired:
        logger.error("Number purchase blocked: the provider account needs a payment method or balance [org=%s]", org_id)
        return "blocked_payment"
    except ProvisioningFailed:
        return "failed"
    except RuntimeError:  # provider not configured (no API key)
        db.rollback()
        logger.error("Retell is not configured; cannot provision a number [org=%s]", org_id)
        return "failed"


def settle_auto_provisioning(db: Session, provider=None, now: datetime | None = None, organization_ids=None) -> int:
    """Worker sweep: retry paid orgs whose number is missing (blocked, never-sent, or no row yet).
    Returns how many purchases succeeded."""
    cfg = get_settings()
    if not cfg.AUTO_PROVISION_NUMBERS:
        return 0
    now = now or datetime.now(timezone.utc)
    q = db.query(Subscription.organization_id).filter(Subscription.status == "active").distinct()
    if organization_ids is not None:
        q = q.filter(Subscription.organization_id.in_(organization_ids))
    done = tried = 0
    for (org_id,) in q.all():
        if tried >= SWEEP_LIMIT:
            break
        if active_number(db, org_id) is not None:
            continue
        prov = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == org_id).first()
        if not _may_attempt(prov, now) or _eligible_subscription(db, org_id) is None:
            continue
        tried += 1
        if auto_provision_for_org(db, org_id, provider, now) == "purchased":
            done += 1
    return done


def provisioning_state(db: Session, org_id: uuid.UUID, now: datetime | None = None) -> dict:
    """Customer-safe status. Never says "live" (that is the activation flow) and never mentions refunds."""
    cfg = get_settings()
    now = now or datetime.now(timezone.utc)
    number = active_number(db, org_id)
    if number is not None:
        return {"state": "ready", "message": "Your phone number is set up. Activate your receptionist to take calls.",
                "number": number.number, "retry_at": None}
    if _eligible_subscription(db, org_id) is None:
        return {"state": "none", "message": "A phone number is set up after your paid subscription is confirmed.",
                "number": None, "retry_at": None}
    prov = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == org_id).first()
    if prov is None or prov.status in ("requested", "purchasing", "ambiguous"):
        return {"state": "provisioning", "message": "Your subscription is active. We are setting up your phone number.",
                "number": None, "retry_at": None}
    retry_at = None
    if prov.status == "blocked_payment" or (prov.status == "failed" and prov.last_error == _RETRYABLE_FAILURE):
        if prov.attempts < cfg.AUTO_PROVISION_MAX_ATTEMPTS and prov.updated_at is not None:
            retry_at = prov.updated_at + timedelta(minutes=cfg.AUTO_PROVISION_RETRY_MINUTES)
    if prov.status == "blocked_payment":
        msg = ("Your subscription is active, but your phone number could not be set up yet because of a problem on our "
               "side with our telephony provider. Your receptionist is not live. Nothing is needed from you; we will "
               "retry and contact you.")
    else:
        msg = ("Your subscription is active, but setting up your phone number did not complete. Your receptionist is "
               "not live yet. Please contact support.")
    return {"state": "blocked", "message": msg, "number": None, "retry_at": retry_at}
