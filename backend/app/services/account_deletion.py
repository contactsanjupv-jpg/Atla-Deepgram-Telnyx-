"""
Delete an organization and everything it owns. External resources go FIRST and
are idempotent, so a failure leaves the account intact and the customer can
simply retry; data is only purged once nothing is left running at a provider.
"""
import logging
import uuid

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.auth.sessions import destroy_all_sessions_for_user
from app.db.models.billing import Subscription
from app.db.models.calls import Call
from app.db.models.tenancy import Organization, OrganizationMember, User
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.providers.billing.billing_provider import BillingProvider
from app.providers.billing.paddle_billing_provider import PaddleAPIError, PaddleBillingProvider
from app.providers.phone.phone_provider import PhoneProvider
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellAPIError
from app.services.phone_provisioning import release_number

logger = logging.getLogger("atla.account")
_LIVE_SUBSCRIPTION_STATES = ("trialing", "active", "past_due", "paused")


class AccountDeletionBlocked(Exception):
    """An external cleanup step failed; nothing was deleted. Safe to retry."""


def _ignore_missing(fn, *args) -> None:
    try:
        fn(*args)
    except RetellAPIError as e:
        if e.status_code != 404:
            raise


def _provider_says_canceled(billing: BillingProvider, external_subscription_id: str) -> bool:
    """True ONLY if the billing provider's own record of this subscription says it is canceled.
    A missing record (None), any other status, an error, or a provider that cannot look it up all mean "not verified"."""
    lookup = getattr(billing, "get_subscription", None)
    if lookup is None:
        return False
    try:
        remote = lookup(external_subscription_id)
    except RuntimeError:  # PaddleAPIError (auth, 4xx, 5xx, network) and NotImplementedError are both RuntimeErrors
        return False
    return isinstance(remote, dict) and remote.get("status") == "canceled"


def delete_organization(
    db: Session,
    org_id: uuid.UUID,
    billing: BillingProvider | None = None,
    phones: PhoneProvider | None = None,
    agents: RetellAgentProvider | None = None,
) -> None:
    try:
        subs = db.query(Subscription).filter(
            Subscription.organization_id == org_id,
            Subscription.status.in_(_LIVE_SUBSCRIPTION_STATES),
            Subscription.external_subscription_id.isnot(None),
        ).all()
        if subs:
            billing = billing or PaddleBillingProvider()
            unverified: list[str] = []
            for sub in subs:  # try every subscription so as many as possible stop billing, then decide
                try:
                    billing.cancel_subscription(sub.external_subscription_id, immediately=True)
                except PaddleAPIError as e:
                    # No error code proves anything (401/403 = bad key, 404 = maybe the wrong environment,
                    # 4xx/5xx/network = unknown). Only Paddle itself saying "canceled" lets us continue.
                    if _provider_says_canceled(billing, sub.external_subscription_id):
                        logger.warning("Cancel refused for %s (%s) but Paddle reports it canceled", sub.external_subscription_id, e.status_code)
                        continue
                    logger.error("Cannot confirm %s is canceled (cancel returned %s)", sub.external_subscription_id, e.status_code)
                    unverified.append(sub.external_subscription_id)
            if unverified:
                raise PaddleAPIError(f"Could not confirm cancellation of {len(unverified)} subscription(s)")

        phone_rows = db.query(PhoneNumber).filter(
            PhoneNumber.organization_id == org_id, PhoneNumber.status != PhoneNumberStatus.released
        ).all()
        if phone_rows:
            phones = phones or RetellPhoneProvider()
            for phone in phone_rows:
                release_number(db, phone, phones)

        agent_rows = db.query(Agent).filter(Agent.organization_id == org_id).all()
        if agent_rows:
            agents = agents or RetellAgentProvider()
            for agent in agent_rows:
                if agent.retell_agent_id:
                    _ignore_missing(agents.delete_agent, agent.retell_agent_id)
                if agent.retell_llm_id:
                    _ignore_missing(agents.delete_llm, agent.retell_llm_id)
    except (PaddleAPIError, RetellAPIError, RuntimeError) as e:
        db.rollback()
        logger.error("Account deletion blocked [org=%s]: %s", org_id, e)
        raise AccountDeletionBlocked() from e

    # Everything external is gone: purge our data.
    call_ids = [row[0] for row in db.query(Call.retell_call_id).filter(Call.organization_id == org_id).all()]
    if call_ids:
        db.execute(
            text("DELETE FROM webhook_events WHERE source = 'retell' AND split_part(external_event_id, ':', 1) = ANY(:ids)"),
            {"ids": call_ids},
        )
    db.execute(
        text("DELETE FROM webhook_events WHERE source = 'paddle' AND payload->'data'->'custom_data'->>'organization_id' = :org"),
        {"org": str(org_id)},
    )
    member_ids = [row[0] for row in db.query(OrganizationMember.user_id).filter(OrganizationMember.organization_id == org_id).all()]
    # Core DELETEs, not ORM deletes: the database's ON DELETE CASCADE removes the
    # business, agents, calls, leads, usage, subscriptions, numbers and memberships.
    db.execute(delete(Organization).where(Organization.id == org_id))
    for user_id in member_ids:
        if db.query(OrganizationMember).filter(OrganizationMember.user_id == user_id).first() is None:
            db.execute(delete(User).where(User.id == user_id))
    db.commit()
    for user_id in member_ids:
        destroy_all_sessions_for_user(str(user_id))
