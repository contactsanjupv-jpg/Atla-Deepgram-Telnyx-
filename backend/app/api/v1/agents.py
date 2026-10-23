import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_role
from app.core.locks import LockNotAcquired
from app.db.base import get_db
from app.db.models.business import Business
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.db.models.voice_agent import Agent, Voice
from app.providers.retell_client import RetellAPIError
from app.schemas.catalog import (
    PERSONALITIES,
    SUPPORTED_TASKS,
    AgentConfigOut,
    AgentConfigRequest,
    AgentOut,
    CallHandlingOut,
)
from app.services.activation import verify_receptionist_live
from app.services.auto_provisioning import provisioning_state
from app.services.phone_provisioning import active_number
from app.services.agent_service import save_agent_settings, sync_agent_to_provider

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/agents", tags=["agents"])
logger = logging.getLogger("atla.agents")


def _agent_out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=str(agent.id),
        name=agent.name,
        status=agent.status.value,
        retell_agent_id=agent.retell_agent_id,
        synced=agent.retell_agent_id is not None and agent.synced_version == agent.version,
    )


@router.get("", response_model=list[AgentOut])
def list_agents(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    agents = db.query(Agent).filter(Agent.organization_id == membership.organization_id).all()
    return [_agent_out(a) for a in agents]


@router.put("/{business_id}", response_model=AgentOut)
def upsert_agent(
    business_id: uuid.UUID,
    payload: AgentConfigRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    try:
        voice = db.query(Voice).filter(Voice.id == uuid.UUID(payload.voice_id)).first()
    except ValueError:
        voice = None
    if voice is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown voice_id")

    agent = save_agent_settings(
        db, organization_id=membership.organization_id, business=business, voice=voice, payload=payload
    )

    agent = sync_or_http_error(db, agent, business, voice)

    return _agent_out(agent)


def sync_or_http_error(db: Session, agent: Agent, business: Business, voice: Voice) -> Agent:
    """Push saved settings to the provider. The caller has ALREADY saved to our DB, so every
    failure here means "saved, but not applied" — never a silent success."""
    try:
        return sync_agent_to_provider(db, agent, business, voice)
    except LockNotAcquired as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "A save is already in progress — try again in a moment") from e
    except RetellAPIError as e:
        logger.error(
            "Retell sync failed [agent=%s]: status=%s ambiguous=%s body=%s", agent.id, e.status_code, e.ambiguous, e.body
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Your changes are saved, but we couldn't apply them to your receptionist yet. Please try again.",
        ) from e
    except RuntimeError as e:
        logger.error("Voice provider not configured: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Voice provider temporarily unavailable") from e


def _tenant_business(db: Session, membership: OrganizationMember, business_id: uuid.UUID) -> Business:
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return business


def _tenant_agent(db: Session, membership: OrganizationMember, business: Business) -> Agent | None:
    return (
        db.query(Agent)
        .filter(Agent.business_id == business.id, Agent.organization_id == membership.organization_id)
        .first()
    )


@router.get("/{business_id}/config", response_model=AgentConfigOut)
def get_agent_config(
    business_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """The complete saved receptionist configuration (what the customer last saved, not what the provider has)."""
    business = _tenant_business(db, membership, business_id)
    agent = _tenant_agent(db, membership, business)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receptionist not set up yet")
    tasks = agent.tasks or {}
    return AgentConfigOut(
        id=str(agent.id),
        name=agent.name,
        greeting=agent.greeting,
        personality=agent.personality,
        language=agent.language,
        voice_id=str(agent.voice_id) if agent.voice_id else None,
        tasks={k: bool(tasks.get(k, False)) for k in SUPPORTED_TASKS},
        transfer_number=agent.transfer_number,
        status=agent.status.value,
        version=agent.version,
        synced_version=agent.synced_version,
        synced=agent.retell_agent_id is not None and agent.synced_version == agent.version,
        supported_tasks=list(SUPPORTED_TASKS),
        personalities=list(PERSONALITIES),
    )


@router.get("/{business_id}/call-handling", response_model=CallHandlingOut)
def get_call_handling(
    business_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """Business number + receptionist/sync status. "live" only if the provider confirms it right now."""
    business = _tenant_business(db, membership, business_id)
    agent = _tenant_agent(db, membership, business)
    prov = provisioning_state(db, membership.organization_id)
    number = active_number(db, membership.organization_id)

    if agent is None:
        sync_state, live, msg = "not_configured", "not_configured", "Your receptionist isn't set up yet."
    else:
        if agent.retell_agent_id is None:
            sync_state = "never_synced"
        elif agent.synced_version == agent.version:
            sync_state = "synced"
        else:
            sync_state = "pending"
        live, msg = verify_receptionist_live(db, membership.organization_id, agent, number)
    return CallHandlingOut(
        business_number=number.number if number else None,
        number_state=prov["state"],
        number_message=prov["message"],
        receptionist_state=live,
        receptionist_message=msg,
        sync_state=sync_state,
        version=agent.version if agent else None,
        synced_version=agent.synced_version if agent else None,
    )
