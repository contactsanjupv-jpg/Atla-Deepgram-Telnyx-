"""
Activation = make the receptionist answer the org's number, and only report
"live" once the PROVIDER confirms it. Safe to call repeatedly.
"""
import logging
import uuid

from sqlalchemy.orm import Session

from app.core.locks import LockNotAcquired, redis_lock
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.phone.phone_provider import PhoneProvider
from app.providers.phone.retell_phone_provider import RetellPhoneProvider

logger = logging.getLogger("atla.activation")


class ActivationNotFound(Exception):
    pass


class ActivationBlocked(Exception):
    """A precondition isn't met; `reason` is a customer-safe message."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ActivationUnverified(Exception):
    """The provider did not confirm the assignment."""


def activate_receptionist(
    db: Session, org_id: uuid.UUID, agent_id: uuid.UUID, phone_id: uuid.UUID, provider: PhoneProvider | None = None
) -> Agent:
    try:
        with redis_lock(f"activate:{org_id}", ttl_seconds=60):
            agent = db.query(Agent).filter(Agent.id == agent_id, Agent.organization_id == org_id).first()
            phone = db.query(PhoneNumber).filter(PhoneNumber.id == phone_id, PhoneNumber.organization_id == org_id).first()
            if agent is None or phone is None:
                raise ActivationNotFound()
            if agent.retell_agent_id is None or agent.synced_version != agent.version:
                raise ActivationBlocked("Your latest receptionist changes haven't been applied yet. Save it again, then go live.")
            if phone.status != PhoneNumberStatus.active:
                raise ActivationBlocked("This phone number is no longer active. Get a new number to go live.")

            provider = provider or RetellPhoneProvider()
            current = provider.get_number(phone.retell_phone_number_id)
            if current is None:
                phone.status, phone.agent_id = PhoneNumberStatus.released, None
                db.commit()
                raise ActivationBlocked("We couldn't find this phone number with our provider. Get a new number to go live.")

            if current.inbound_agent_id != agent.retell_agent_id:
                provider.assign_agent(phone.retell_phone_number_id, agent.retell_agent_id)
                current = provider.get_number(phone.retell_phone_number_id)
                if current is None or current.inbound_agent_id != agent.retell_agent_id:
                    raise ActivationUnverified()

            phone.agent_id = agent.id
            agent.status = AgentStatus.active
            db.commit()
            db.refresh(agent)
            return agent
    except LockNotAcquired as e:
        raise ActivationBlocked("Activation is already in progress — one moment.") from e



def verify_receptionist_live(db: Session, org_id: uuid.UUID, agent: Agent, number: PhoneNumber | None, provider: PhoneProvider | None = None) -> tuple[str, str]:
    """
    Returns (state, customer message). Read-only: never mutates our DB.

    DEFINITION. "live" means exactly one thing: the PROVIDER reports, right now, that this
    org's number routes inbound calls to THIS receptionist's provider agent id. It does NOT
    mean the latest saved configuration is applied — that is the separate sync state
    (`agent.synced_version == agent.version`), and a live receptionist with unsynced edits
    is still "live" but the message says callers hear the previous version.

    "live" is NEVER inferred from our database alone. Required for "live":
      * our records say active, with this number assigned to this agent (necessary, not sufficient);
      * the agent has a provider agent id (without one it cannot be routed to — a provider
        number whose inbound agent is also null must not match "None == None");
      * a fresh provider lookup returns the number with inbound_agent_id == that agent id.
    """
    if agent.status != AgentStatus.active or number is None or number.agent_id != agent.id:
        return "not_live", "Your receptionist is not taking calls yet."
    if not agent.retell_agent_id:
        # No provider agent exists, so nothing can be routed to it. Do not even ask the provider.
        return "not_live", "Your receptionist hasn't been created with our provider yet, so it is not taking calls."
    try:
        current = (provider or RetellPhoneProvider()).get_number(number.retell_phone_number_id)
    except Exception:  # noqa: BLE001 - any provider failure means we cannot confirm
        logger.warning("Live check could not reach provider [agent=%s]", agent.id)
        return "unverified", "We couldn't confirm your receptionist's status with our provider just now. Please refresh in a moment."
    if current is None or not current.inbound_agent_id or current.inbound_agent_id != agent.retell_agent_id:
        return "not_live", "Your phone number is not currently connected to your receptionist. Go live again from the dashboard."
    if agent.synced_version != agent.version:
        return "live", "Your receptionist is answering calls, but your latest changes haven't been applied yet — callers hear the previous version."
    return "live", "Your receptionist is live and answering calls."
