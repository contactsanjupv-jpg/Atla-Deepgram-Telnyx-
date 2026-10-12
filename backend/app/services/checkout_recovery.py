"""
What is really going on with this organization's open checkout — read from Paddle, never guessed.

A pending-checkout marker (services/checkout_guard.py) means "we created a Paddle transaction and the
customer may still pay it". This service turns that into one of five honest states and the one safe
next step for each:

  none         no checkout in flight
  open         the transaction can still be paid -> RESUME the same link (never a second transaction)
  processing   Paddle took the money (completed/paid) but our subscription row isn't there yet -> wait;
               we pull the subscription from Paddle right now through the same stale-safe path the webhook uses
  expired      Paddle says canceled / past_due: nothing was paid -> marker cleared, a new checkout is safe
  unconfirmed  Paddle couldn't be reached -> change nothing, do not allow a conflicting checkout

The marker is cleared only for `expired` or once an active subscription exists. A different plan may be
chosen only when the old transaction is provably dead (expired) or Paddle confirms it canceled it
(Paddle allows canceling draft, ready and billed transactions through its API).
Everything is idempotent and safe to call from many tabs at once.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models.billing import Subscription
from app.services.billing_state import ACTIVE_STATUSES, has_active_subscription
from app.services.checkout_guard import clear_pending, get_pending, pending_ttl_seconds
from app.services.subscription_sync import parse_dt, sync_subscription

logger = logging.getLogger("atla.checkout_recovery")

OPEN_STATUSES = ("draft", "ready", "billed")
PAID_STATUSES = ("paid", "completed")
DEAD_STATUSES = ("canceled", "past_due")


@dataclass
class CheckoutState:
    state: str  # none | open | processing | expired | unconfirmed
    plan: str | None = None
    checkout_url: str | None = None
    message: str = ""
    expires_in_seconds: int | None = None


def _provider(provider):
    if provider is not None:
        return provider
    from app.providers.billing.paddle_billing_provider import PaddleBillingProvider

    return PaddleBillingProvider()


def _pull_subscription(db: Session, provider, txn: dict) -> bool:
    """The transaction is paid: fetch its subscription from Paddle and apply it (stale-safe). True once an active row exists."""
    sub_id = txn.get("subscription_id")
    if not sub_id:
        return False
    snapshot = provider.get_subscription(sub_id)
    if snapshot is not None:
        at = parse_dt(snapshot.get("updated_at")) or datetime.now(timezone.utc)
        sync_subscription(db, snapshot, at)
        db.commit()
    row = db.query(Subscription).filter(Subscription.external_subscription_id == sub_id).first()
    return row is not None and row.status in ACTIVE_STATUSES


def resolve_checkout(db: Session, organization_id: uuid.UUID, provider=None) -> CheckoutState:
    if has_active_subscription(db, organization_id):
        clear_pending(organization_id)
        return CheckoutState("none", message="Your plan is active.")
    pending = get_pending(organization_id)
    if pending is None:
        return CheckoutState("none")

    plan, url, txn_id = pending.get("plan"), pending.get("checkout_url"), pending.get("transaction_id")
    ttl = pending_ttl_seconds(organization_id)
    if not txn_id:  # created before transaction ids were recorded: we can only trust the 15-minute window
        return CheckoutState("open", plan, url, "You have a checkout in progress. Finish it to start your plan.", ttl)

    try:
        provider = _provider(provider)
        txn = provider.get_transaction(txn_id)
    except RuntimeError:
        return CheckoutState(
            "unconfirmed", plan, None,
            "We can't confirm your payment status right now. Please wait a minute and refresh. Don't pay again.", ttl,
        )
    if txn is None:
        clear_pending(organization_id)
        return CheckoutState("expired", plan, None, "That checkout is no longer available. You can start a new one.")

    status = txn.get("status")
    if status in DEAD_STATUSES:
        clear_pending(organization_id)
        return CheckoutState("expired", plan, None, "Your last checkout expired without a payment. You can start a new one.")
    if status in PAID_STATUSES:
        try:
            ready = _pull_subscription(db, provider, txn)
        except RuntimeError:
            ready = False
        if ready:
            clear_pending(organization_id)
            return CheckoutState("none", plan, None, "Your plan is active.")
        return CheckoutState(
            "processing", plan, None,
            "Your payment went through. We're finishing your setup — this usually takes a few seconds. Please don't pay again.",
        )
    # draft / ready / billed (or any status we don't recognise): still payable. Never assume it is dead.
    return CheckoutState("open", plan, url, "You have a checkout in progress. Finish it to start your plan.", ttl)


def release_for_new_plan(db: Session, organization_id: uuid.UUID, wanted_plan: str, provider=None) -> CheckoutState:
    """
    The customer asked for `wanted_plan` while a checkout for another plan is pending. Returns a state whose
    `.state` is "none"/"expired" when a new checkout is now safe; anything else means it is NOT safe yet.
    Paddle must confirm the old transaction is canceled before we let a second one exist.
    """
    current = resolve_checkout(db, organization_id, provider)
    if current.state != "open" or current.plan == wanted_plan:
        return current
    pending = get_pending(organization_id) or {}
    txn_id = pending.get("transaction_id")
    if txn_id:
        try:
            if _provider(provider).cancel_transaction(txn_id):
                clear_pending(organization_id)
                return CheckoutState("expired", current.plan, None, "Your previous checkout was canceled.")
        except RuntimeError:
            pass
    current.message = (
        f"You already have a {str(current.plan).title()} checkout open. Finish it to start your plan, "
        "or try again in a moment to switch to a different plan."
    )
    return current
