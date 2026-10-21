"""
Safe phone-number provisioning. The guarantees, and how each is enforced:

  * One number per org .......... UNIQUE(organization_id) on phone_provisionings + an
                                  "already has an active number" short-circuit.
  * Double-click / concurrency .. Redis lock per org; losers get ProvisioningInProgress.
  * Timeout after provider success: status becomes `ambiguous`; the next attempt (or the
                                  sweeper) asks the provider for the number by our
                                  deterministic nickname and ADOPTS it instead of buying.
  * DB failure after provider success: the row was committed as `purchasing` BEFORE the
                                  provider call, so the same nickname lookup recovers it.
Whether the provider honours `nickname` is UNVERIFIED (see retell_phone_provider.py).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.locks import LockNotAcquired, redis_lock
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus, PhoneProvisioning
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.phone.phone_provider import PhoneProvider, ProvisionedNumber
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellAPIError

logger = logging.getLogger("atla.provisioning")


class ProvisioningInProgress(Exception):
    pass


class ProvisioningPending(Exception):
    """The provider may have bought the number; it will be reconciled automatically."""


class ProvisioningFailed(Exception):
    pass


class ProvisioningPaymentRequired(ProvisioningFailed):
    """The provider refused the purchase for lack of payment (HTTP 402). Nothing was bought; recoverable once
    the PROVIDER account is funded. (That Retell uses 402 for this is UNVERIFIED - see retell_phone_provider.py.)"""


PAYMENT_REQUIRED = 402
# Set by the reconciler when it PROVED (provider list, by our deterministic nickname) that an ambiguous purchase never
# happened. That makes the row safe to retry later; auto_provisioning treats exactly this marker as retryable.
NOT_FOUND_AFTER_RECONCILE = "no provider number found after 10 minutes"


def _nickname(org_id: uuid.UUID, generation: int) -> str:
    return f"atla-{org_id}-{generation}"


def active_number(db: Session, org_id: uuid.UUID) -> PhoneNumber | None:
    return (
        db.query(PhoneNumber)
        .filter(PhoneNumber.organization_id == org_id, PhoneNumber.status == PhoneNumberStatus.active)
        .first()
    )


def _adopt(db: Session, prov: PhoneProvisioning, pn: ProvisionedNumber) -> PhoneNumber:
    """Record a provider-side number locally and mark provisioning done — one commit."""
    row = db.query(PhoneNumber).filter(PhoneNumber.retell_phone_number_id == pn.provider_phone_number_id).first()
    if row is None:
        row = PhoneNumber(
            organization_id=prov.organization_id,
            retell_phone_number_id=pn.provider_phone_number_id,
            number=pn.number,
            area_code=pn.area_code or prov.area_code,
            country=pn.country or prov.country,
            monthly_cost_cents=pn.monthly_cost_cents,
            status=PhoneNumberStatus.active,
        )
        db.add(row)
        db.flush()
    prov.phone_number_id = row.id
    prov.status, prov.last_error = "purchased", None
    db.commit()
    return row


def provision_number(
    db: Session, org_id: uuid.UUID, country: str, area_code: str | None, provider: PhoneProvider | None = None
) -> PhoneNumber:
    try:
        with redis_lock(f"provision:{org_id}", ttl_seconds=90):
            existing = active_number(db, org_id)
            if existing is not None:
                return existing  # idempotent: the org already has its number

            prov = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == org_id).first()
            if prov is None:
                prov = PhoneProvisioning(
                    organization_id=org_id, nickname=_nickname(org_id, 1), country=country, area_code=area_code
                )
                db.add(prov)
                db.commit()
            elif prov.status == "released":  # a previous number was released: new generation, new nickname
                prov.generation += 1
                prov.nickname = _nickname(org_id, prov.generation)
                prov.status, prov.phone_number_id, prov.attempts = "requested", None, 0
                prov.country, prov.area_code = country, area_code
                db.commit()

            provider = provider or RetellPhoneProvider()

            # Look the nickname up BEFORE any (re)purchase: for every row that is not a first-ever attempt, and for
            # any in-flight/finished one. A retry must adopt a number the provider already has, never buy a second.
            if prov.attempts > 0 or prov.status in ("purchasing", "ambiguous", "purchased"):
                try:
                    found = provider.find_number_by_nickname(prov.nickname)
                except (RetellAPIError, RuntimeError) as e:
                    # Cannot prove nothing exists, so do NOT buy. Leave the row untouched; the next sweep retries.
                    logger.error("Nickname look-up failed before a retry; not purchasing [org=%s]: %s", org_id, e)
                    raise ProvisioningPending() from e
                if found is not None:
                    return _adopt(db, prov, found)  # reconcile instead of buying again

            prov.status, prov.attempts = "purchasing", prov.attempts + 1
            prov.country, prov.area_code = country, area_code
            db.commit()  # durable intent BEFORE the provider call
            try:
                pn = provider.purchase_number(country, area_code, prov.nickname)
            except RetellAPIError as e:
                prov.last_error = f"status={e.status_code}"
                if e.ambiguous or e.status_code >= 500:
                    prov.status = "ambiguous"
                    db.commit()
                    logger.error("Ambiguous number purchase [org=%s]: %s", org_id, e)
                    raise ProvisioningPending() from e
                blocked = e.status_code == PAYMENT_REQUIRED
                prov.status = "blocked_payment" if blocked else "failed"
                db.commit()
                logger.error("Number purchase failed [org=%s]: status=%s body=%s error=%s", org_id, e.status_code, e.body, e)
                raise (ProvisioningPaymentRequired() if blocked else ProvisioningFailed()) from e
            return _adopt(db, prov, pn)
    except LockNotAcquired as e:
        raise ProvisioningInProgress() from e


def reconcile_provisioning(
    db: Session, provider: PhoneProvider, now: datetime | None = None, organization_ids: set[uuid.UUID] | None = None
) -> int:
    """Sweeper: settle purchases left `purchasing`/`ambiguous` (crash, timeout)."""
    now = now or datetime.now(timezone.utc)
    settled = 0
    q = db.query(PhoneProvisioning).filter(
        PhoneProvisioning.status.in_(("purchasing", "ambiguous")), PhoneProvisioning.updated_at <= now - timedelta(minutes=2)
    )
    if organization_ids is not None:
        q = q.filter(PhoneProvisioning.organization_id.in_(organization_ids))
    stuck = q.all()
    for prov in stuck:
        try:
            with redis_lock(f"provision:{prov.organization_id}", ttl_seconds=60):
                found = provider.find_number_by_nickname(prov.nickname)
                if found is not None:
                    _adopt(db, prov, found)
                    settled += 1
                elif prov.updated_at <= now - timedelta(minutes=10):
                    prov.status, prov.last_error = "failed", NOT_FOUND_AFTER_RECONCILE
                    db.commit()
                    settled += 1
        except LockNotAcquired:
            continue
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("Provisioning reconcile failed [org=%s]", prov.organization_id)
    return settled


RELEASED_BY_ENTITLEMENT = "released_by=entitlement"


def release_number(db: Session, phone: PhoneNumber, provider: PhoneProvider, reason: str = "customer") -> None:
    """Release at the provider (idempotent) and reflect it locally. Safe to retry.
    `reason` is kept on the provisioning row: only a release caused by a lapsed subscription ("entitlement") may be
    re-provisioned automatically when the subscription becomes active again. A release the customer asked for, or one
    made when the account was deleted, never buys a replacement by itself."""
    provider.release_number(phone.retell_phone_number_id)
    agent_id = phone.agent_id
    phone.status, phone.agent_id = PhoneNumberStatus.released, None
    prov = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == phone.organization_id).first()
    if prov is not None and prov.phone_number_id == phone.id:
        prov.status, prov.last_error = "released", f"released_by={reason}"
    if agent_id is not None:
        agent = db.get(Agent, agent_id)
        if agent is not None and agent.status == AgentStatus.active:
            agent.status = AgentStatus.paused
    db.commit()
