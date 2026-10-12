import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_feature, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.services.plans import Feature
from app.db.base import get_db
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellAPIError
from app.schemas.catalog import PhoneNumberOut, PurchaseNumberRequest
from app.services.phone_provisioning import (
    ProvisioningFailed,
    ProvisioningInProgress,
    ProvisioningPending,
    provision_number,
    release_number,
)

logger = logging.getLogger("atla.phone_numbers")

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/phone-numbers", tags=["phone-numbers"])
settings = get_settings()


@router.get("", response_model=list[PhoneNumberOut])
def list_phone_numbers(
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    return (
        db.query(PhoneNumber)
        .filter(PhoneNumber.organization_id == membership.organization_id)
        .order_by(PhoneNumber.created_at.desc())
        .all()
    )


@router.post("", response_model=PhoneNumberOut, status_code=status.HTTP_201_CREATED)
def purchase_phone_number(
    payload: PurchaseNumberRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.admin)),
    _subscribed: OrganizationMember = Depends(require_feature(Feature.phone_number)),
    db: Session = Depends(get_db),
):
    """
    Gives the org its (single) number. Costs real money, so it sits behind the
    subscription gate, and provisioning is durable and idempotent: repeated or
    concurrent calls, and retries after an ambiguous provider response, resolve
    to ONE number (see services/phone_provisioning.py).
    """
    try:
        check_rate_limit(f"number-purchase:{membership.organization_id}", limit=10, window_seconds=86400)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts today. Please try again tomorrow.") from e

    try:
        return provision_number(db, membership.organization_id, payload.country, payload.area_code)
    except ProvisioningInProgress as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "We're setting up your number — one moment.") from e
    except ProvisioningPending as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "We're confirming your number with our phone provider. This usually takes under a minute — try again shortly.",
        ) from e
    except ProvisioningFailed as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't set up a number right now. Please try again.") from e
    except (RetellAPIError, RuntimeError) as e:
        logger.error("Provisioning provider error [org=%s]: %s", membership.organization_id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't set up a number right now. Please try again.") from e


@router.delete("/{phone_number_id}", status_code=status.HTTP_204_NO_CONTENT)
def release_phone_number(
    phone_number_id: uuid.UUID,
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    db: Session = Depends(get_db),
):
    number = (
        db.query(PhoneNumber)
        .filter(PhoneNumber.id == phone_number_id, PhoneNumber.organization_id == membership.organization_id)
        .first()
    )
    if number is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Phone number not found")
    if number.status == PhoneNumberStatus.released:
        return
    try:
        release_number(db, number, RetellPhoneProvider())
    except (RetellAPIError, RuntimeError) as e:
        logger.error("Number release failed [number=%s]: %s", number.id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't release this number right now. Please try again.") from e
