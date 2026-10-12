import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, get_current_user, has_active_subscription, require_feature, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.services import free_test
from app.services.billing_state import ACTIVE_STATUSES, current_subscription
from app.services.credits import can_cover_paid_test, credit_summary, ensure_current_grant, lock_org_credits
from app.services.plans import Feature
from app.db.base import get_db
from app.db.models.calls import Call, CallDirection
from app.db.models.billing import FreeTestAllowance
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.call.retell_call_provider import RetellCallProvider
from app.services.activation import (
    ActivationBlocked,
    ActivationNotFound,
    ActivationUnverified,
    activate_receptionist as activate_service,
)
from app.providers.retell_client import RetellAPIError
from app.schemas.activation import ActivateRequest, ActivateResponse, TestCallResponse

router = APIRouter(prefix="/api/v1/orgs/{organization_id}", tags=["activation"])
settings = get_settings()
logger = logging.getLogger("atla.activation")


@router.post("/agents/{agent_id}/test-call", response_model=TestCallResponse)
def start_test_call(
    agent_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Starts a real browser (WebRTC) call against the customer's saved agent. Every test call costs real
    Retell money, so it is decided SERVER-SIDE, under a per-organization lock, before anything is created:

      * not subscribed  -> exactly one free test call, claimed by inserting a row that is unique per
                           organization and per user (services/free_test.py): atomic, never resettable
      * subscribed      -> same credit rules as any call: the available credits (counting test calls
                           already in flight) must cover a full-length test call
      * subscription lapsed -> no testing until billing is fixed

    A placeholder Call row is committed first so concurrent requests see this call as in flight.
    """
    agent = (
        db.query(Agent)
        .filter(Agent.id == agent_id, Agent.organization_id == membership.organization_id)
        .first()
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    if agent.retell_agent_id is None or agent.synced_version != agent.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your latest changes haven't been applied to your receptionist yet. Save it again, then test.",
        )

    org_id = membership.organization_id
    allowance = None
    is_free = False
    lock_org_credits(db, org_id)  # held until the commit below
    sub = current_subscription(db, org_id)
    if sub is not None and sub.status in ACTIVE_STATUSES:
        ensure_current_grant(db, org_id)
        if not can_cover_paid_test(db, org_id):
            db.rollback()
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "You're out of credits. Buy more credits to keep testing your receptionist.",
            )
    elif sub is not None:
        db.rollback()
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Your plan isn't active right now. Update your billing to keep testing your receptionist.",
        )
    else:
        allowance = free_test.reserve(db, org_id, user.id)
        if allowance is None:
            db.rollback()
            logger.warning("Free test call refused [org=%s user=%s]: allowance used", org_id, user.id)
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                "You've used your free test call. Choose a plan to test your receptionist again.",
            )
        is_free = True

    try:
        check_rate_limit(f"test-call:{org_id}", settings.TEST_CALLS_PER_DAY_PER_ORG, 86400)
    except RateLimitExceeded as e:
        db.rollback()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily test call limit reached") from e

    placeholder = Call(
        organization_id=org_id,
        retell_call_id=f"pending:{uuid.uuid4()}",
        agent_id=agent.id,
        direction=CallDirection.test,
        status="registered",
    )
    db.add(placeholder)
    db.commit()  # releases the lock; from here concurrent requests see this call as in flight
    placeholder_id, allowance_id = placeholder.id, allowance.id if allowance is not None else None

    def _undo(release_allowance: bool) -> None:
        db.rollback()
        row = db.get(Call, placeholder_id)
        if row is not None:
            db.delete(row)
        if release_allowance and allowance_id is not None:
            held = db.get(FreeTestAllowance, allowance_id)
            if held is not None:
                db.delete(held)
        db.commit()

    try:
        provider = RetellCallProvider()
        session = provider.create_test_call(
            agent.retell_agent_id,
            metadata={"organization_id": str(org_id), "agent_id": str(agent.id), "kind": "free_test" if is_free else "test"},
        )
    except RetellAPIError as e:
        # logger.exception records the full traceback INCLUDING the chained original
        # error (e.g. the httpx connect/timeout error behind status=0). Server-side only.
        logger.exception(
            "Retell test call failed [agent=%s]: status=%s ambiguous=%s body=%s error=%s",
            agent.id, e.status_code, e.ambiguous, e.body, e,
        )
        # A definite refusal means no call exists: give the free call back. If Retell MAY have
        # created it (timeout), keep it spent — we would rather lose one retry than pay twice.
        _undo(release_allowance=not e.ambiguous)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not start a test call with the voice provider") from e
    except RuntimeError as e:
        logger.exception("Test call provider unavailable [agent=%s]: %s", agent.id, e)
        _undo(release_allowance=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Provider temporarily unavailable") from e

    row = db.get(Call, placeholder_id)
    row.retell_call_id = session.provider_call_id  # the Retell webhook for this call resolves against it
    if allowance_id is not None:
        held = db.get(FreeTestAllowance, allowance_id)
        if held is not None:
            held.retell_call_id = session.provider_call_id
    if agent.status == AgentStatus.draft:
        agent.status = AgentStatus.testing
    db.commit()

    return TestCallResponse(
        call_id=session.provider_call_id,
        access_token=session.access_token,
        max_seconds=settings.TEST_CALL_MAX_SECONDS,
        is_free=is_free,
    )


@router.post("/activate", response_model=ActivateResponse)
def activate_receptionist(
    payload: ActivateRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.admin)),
    _subscribed: OrganizationMember = Depends(require_feature(Feature.go_live)),
    db: Session = Depends(get_db),
):
    """
    Goes live ONLY when the provider confirms the number routes to this agent
    (services/activation.py). Ids in the body are re-verified against the org.
    Safe to call repeatedly.
    """
    try:
        agent_id, phone_id = uuid.UUID(payload.agent_id), uuid.UUID(payload.phone_number_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid id") from e
    # Going live with no credits left would be paused again by the sweeper within seconds.
    ensure_current_grant(db, membership.organization_id)  # self-heals a subscription that pre-dates credits
    db.commit()
    balance = credit_summary(db, membership.organization_id)
    if balance.period_start is not None and balance.available_seconds <= 0:  # metered (has a grant) and spent
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "You're out of credits. Buy more credits, then go live again.",
        )
    try:
        agent = activate_service(db, membership.organization_id, agent_id, phone_id)
    except ActivationNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receptionist or phone number not found") from e
    except ActivationBlocked as e:
        raise HTTPException(status.HTTP_409_CONFLICT, e.reason) from e
    except ActivationUnverified as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "We couldn't confirm your receptionist is connected to your number yet. Please try again."
        ) from e
    except (RetellAPIError, RuntimeError) as e:
        logger.error("Activation provider error [org=%s]: %s", membership.organization_id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't reach our phone provider. Please try again.") from e
    return ActivateResponse(phone_number_id=str(phone_id), agent_id=str(agent.id), status=agent.status.value)
