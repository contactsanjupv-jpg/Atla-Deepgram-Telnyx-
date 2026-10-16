"""
POST /checkout-session is the only way to start a purchase. The Subscription
row itself is written only by the Paddle webhook, never by this endpoint.
"""
import logging

from datetime import datetime

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, get_current_user, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.core.locks import LockNotAcquired, redis_lock
from app.services import topups
from app.services.billing_state import current_subscription, has_active_subscription, has_blocking_subscription
from app.services.billing_status import subscription_view
from app.services.checkout_guard import set_pending
from app.services.checkout_recovery import release_for_new_plan, resolve_checkout
from app.services.plans import PLANS, price_id_for_plan
from app.db.base import get_db
from app.db.models.billing import TopupPurchase
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
from app.schemas.billing import CreateCheckoutSessionRequest, CreateCheckoutSessionResponse, SubscriptionOut

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/billing", tags=["billing"])
settings = get_settings()
logger = logging.getLogger("atla.billing")



@router.get("/subscription", response_model=SubscriptionOut | None)
def get_subscription(
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    return subscription_view(db, membership.organization_id)


@router.post("/checkout-session", response_model=CreateCheckoutSessionResponse)
def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if has_blocking_subscription(db, membership.organization_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "You already have a subscription. Manage it from your billing settings."
        )
    if payload.plan not in PLANS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown plan: {payload.plan}")
    price_id = price_id_for_plan(payload.plan)
    if not price_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable")

    try:
        # Lock so a double-click can't create two Paddle transactions.
        with redis_lock(f"checkout:{membership.organization_id}", ttl_seconds=30):
            # One open checkout per org, decided from Paddle's own view of it (services/checkout_recovery.py):
            # same plan -> the same link back (no second transaction); another plan -> only after Paddle
            # confirms the old transaction is canceled; payment in progress / unknown -> never a second one.
            state = release_for_new_plan(db, membership.organization_id, payload.plan)
            if state.state == "open" and state.plan == payload.plan and state.checkout_url:
                return CreateCheckoutSessionResponse(checkout_url=state.checkout_url)
            if state.state in ("open", "processing", "unconfirmed"):
                raise HTTPException(status.HTTP_409_CONFLICT, state.message)
            provider = PaddleBillingProvider()
            session = provider.create_checkout_session(
                organization_id=str(membership.organization_id),
                plan_id=payload.plan,
                plan_price_id=price_id,
                customer_email=user.email,
                success_url=f"{settings.FRONTEND_URL}/dashboard/go-live",
                cancel_url=f"{settings.FRONTEND_URL}/dashboard/go-live",
            )
    except LockNotAcquired as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "Checkout is already starting — one moment.") from e
    except RuntimeError as e:  # includes PaddleAPIError
        logger.error("Checkout failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable") from e

    set_pending(membership.organization_id, payload.plan, session.checkout_url, session.provider_session_id)
    return CreateCheckoutSessionResponse(checkout_url=session.checkout_url)

class ManageOut(BaseModel):
    update_payment_method_url: str | None


def _paying_subscription(db: Session, organization_id):
    sub = current_subscription(db, organization_id)
    if sub is None or sub.status not in ("trialing", "active", "past_due", "paused") or not sub.external_subscription_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "There's no active subscription to manage.")
    return sub


@router.post("/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_subscription(
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    db: Session = Depends(get_db),
):
    """Cancel at the end of the paid period. Our state changes only when Paddle's webhook says so."""
    sub = _paying_subscription(db, membership.organization_id)
    if sub.cancel_effective_at is not None:
        return {"status": "already_scheduled"}
    try:
        provider = PaddleBillingProvider()
        try:
            provider.cancel_subscription(sub.external_subscription_id, immediately=False)
        except RuntimeError as e:
            # The request may have reached Paddle even though we saw an error (lost response / timeout), or a
            # retry may be refused because it is already scheduled. Only Paddle's own record can say which.
            if not _cancellation_in_effect(provider, sub.external_subscription_id):
                raise
            logger.warning("Cancel call failed (%s) but Paddle reports the cancellation is in effect", e)
            return {"status": "already_scheduled"}
    except RuntimeError as e:
        logger.error("Cancel failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't cancel right now. Please try again.") from e
    return {"status": "requested"}


def _cancellation_in_effect(provider, external_subscription_id: str) -> bool:
    """True only if Paddle's own subscription record shows it canceled or with a scheduled cancel.
    A missing record, any other state, or a failed lookup is "not confirmed"."""
    lookup = getattr(provider, "get_subscription", None)
    if lookup is None:
        return False
    try:
        remote = lookup(external_subscription_id)
    except RuntimeError:
        return False
    if not isinstance(remote, dict):
        return False
    scheduled = remote.get("scheduled_change")
    return remote.get("status") == "canceled" or (isinstance(scheduled, dict) and scheduled.get("action") == "cancel")


@router.get("/manage", response_model=ManageOut)
def manage_payment(
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    db: Session = Depends(get_db),
):
    sub = _paying_subscription(db, membership.organization_id)
    try:
        urls = PaddleBillingProvider().get_management_urls(sub.external_subscription_id)
    except RuntimeError as e:
        logger.error("Manage urls failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't load your billing details right now.") from e
    return ManageOut(update_payment_method_url=urls.get("update_payment_method"))


class CheckoutStateOut(BaseModel):
    state: str  # none | open | processing | expired | unconfirmed
    plan: str | None
    checkout_url: str | None
    message: str
    expires_in_seconds: int | None


@router.get("/checkout", response_model=CheckoutStateOut)
def get_checkout_state(
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    db: Session = Depends(get_db),
):
    """
    What is happening with this organization's checkout, read from Paddle (never from the browser's
    "I paid" redirect). Safe to poll: if the payment went through it pulls the subscription now, through
    the same stale-safe path as the webhook.
    """
    st = resolve_checkout(db, membership.organization_id)
    return CheckoutStateOut(
        state=st.state, plan=st.plan, checkout_url=st.checkout_url, message=st.message, expires_in_seconds=st.expires_in_seconds
    )


class TopupQuoteOut(BaseModel):
    amount_minor: int
    currency: str
    credit_minutes: int
    cents_per_minute: int


class TopupRequest(BaseModel):
    amount_minor: int  # USD cents


class TopupCheckoutOut(BaseModel):
    checkout_url: str
    amount_minor: int
    credit_minutes: int


@router.get("/topup-quote", response_model=TopupQuoteOut)
def topup_quote(amount_minor: int = Query(), membership: OrganizationMember = Depends(current_membership)):
    """What a chosen amount buys, before any payment. Validation (minimum / maximum) happens here, server-side."""
    try:
        q = topups.quote(amount_minor)
    except topups.InvalidTopup as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    return TopupQuoteOut(**{k: q[k] for k in TopupQuoteOut.model_fields})


@router.post("/topups", response_model=TopupCheckoutOut)
def create_topup(
    payload: TopupRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Starts a one-time Paddle payment for the chosen amount. Credits are NOT granted here — only when Paddle's
    verified webhook (or the reconciler reading the transaction back) says it was paid.
    """
    org_id = membership.organization_id
    if not has_active_subscription(db, org_id):
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Choose a plan first, then you can buy more credits.")
    try:
        quote = topups.quote(payload.amount_minor)
    except topups.InvalidTopup as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    try:
        check_rate_limit(f"topup:{org_id}", 10, 3600)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many purchase attempts. Please try again later.") from e
    try:
        with redis_lock(f"topup:{org_id}", ttl_seconds=30):
            row = topups.create_topup(db, org_id, user.id, quote["amount_minor"])
    except LockNotAcquired as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "A purchase is already starting. One moment.") from e
    except RuntimeError as e:
        logger.error("Top-up checkout failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable. You haven't been charged.") from e
    return TopupCheckoutOut(checkout_url=row.checkout_url, amount_minor=row.amount_minor, credit_minutes=row.credit_seconds // 60)


class TopupPurchaseOut(BaseModel):
    id: str
    amount_minor: int
    currency: str
    credit_minutes: int
    status: str  # created | completed | failed | partially_refunded | refunded | chargeback
    created_at: datetime


@router.get("/topups", response_model=list[TopupPurchaseOut])
def list_topups(
    limit: int = Query(default=20, ge=1, le=50),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(TopupPurchase)
        .filter(TopupPurchase.organization_id == membership.organization_id)
        .order_by(TopupPurchase.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        TopupPurchaseOut(
            id=str(r.id), amount_minor=r.amount_minor, currency=r.currency, credit_minutes=r.credit_seconds // 60,
            status=r.status, created_at=r.created_at,
        )
        for r in rows
    ]
