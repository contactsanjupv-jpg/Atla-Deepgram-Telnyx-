"""
Customer-chosen credit top-ups (minimum $20) paid through a one-time Paddle transaction.

Safety rules, in order of importance:
  1. Credits are granted ONLY when Paddle says the transaction is completed — from the signed webhook,
     or by the reconciler reading the transaction back from Paddle. A browser redirect grants nothing.
  2. A grant is idempotent: ledger key `topup:<transaction id>` is unique, and the purchase row is locked
     and status-checked, so duplicate / retried / concurrent deliveries credit once.
  3. The amount credited comes from OUR row (written server-side before the transaction existed), and the
     transaction's own line items must match it exactly; otherwise nothing is granted and it is logged.
  4. Failed, canceled, abandoned and still-unpaid checkouts grant zero.
  5. Refunds and chargebacks claw credits back through the same ledger (see apply_adjustment).

Refund / chargeback policy (customer-facing and auditable):
  * An APPROVED refund removes the same share of the top-up's credits as the share of the price refunded.
  * An APPROVED chargeback removes all of that top-up's remaining credits; if the chargeback is later
    reversed in the customer's favour for us, they are restored.
  * Clawed-back credits may already have been spent: the purchased balance then goes negative, which blocks
    new calls (and, past the grace limit, pauses the receptionist) until the customer buys more.
  * Subscription refunds do NOT change credits; they are handled by cancelling the subscription.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.billing import TopupPurchase
from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
from app.services.credits import add_ledger_entry, lock_org_credits

logger = logging.getLogger("atla.topups")

CURRENCY = "USD"
GRANTED_STATES = ("completed", "partially_refunded", "refunded", "chargeback")


class InvalidTopup(ValueError):
    """The requested amount isn't allowed. The message is written for the customer."""


def credit_seconds_for(amount_minor: int) -> int:
    return amount_minor * 60 // get_settings().TOPUP_CENTS_PER_MINUTE


def validate_amount(amount_minor) -> int:
    s = get_settings()
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise InvalidTopup("Enter a whole number of cents.")
    if amount_minor < s.TOPUP_MIN_CENTS:
        raise InvalidTopup(f"The minimum top-up is ${s.TOPUP_MIN_CENTS / 100:,.0f}.")
    if amount_minor > s.TOPUP_MAX_CENTS:
        raise InvalidTopup(f"The maximum top-up is ${s.TOPUP_MAX_CENTS / 100:,.0f}. Contact us for larger amounts.")
    return amount_minor


def quote(amount_minor) -> dict:
    amount = validate_amount(amount_minor)
    seconds = credit_seconds_for(amount)
    return {
        "amount_minor": amount,
        "currency": CURRENCY,
        "credit_seconds": seconds,
        "credit_minutes": seconds // 60,
        "cents_per_minute": get_settings().TOPUP_CENTS_PER_MINUTE,
    }


def create_topup(db: Session, organization_id: uuid.UUID, user_id: uuid.UUID, amount_minor: int, provider=None) -> TopupPurchase:
    """Writes the purchase row FIRST, then asks Paddle for the one-time transaction. Raises RuntimeError on provider failure."""
    q = quote(amount_minor)
    row = TopupPurchase(
        organization_id=organization_id,
        user_id=user_id,
        amount_minor=q["amount_minor"],
        currency=CURRENCY,
        credit_seconds=q["credit_seconds"],
        cents_per_minute=q["cents_per_minute"],
        status="created",
    )
    db.add(row)
    db.commit()
    try:
        provider = provider or PaddleBillingProvider()
        session = provider.create_topup_transaction(
            organization_id=str(organization_id),
            topup_id=str(row.id),
            amount_minor=row.amount_minor,
            currency=CURRENCY,
            description=f"Atla call credits: {q['credit_minutes']} minutes",
            display_name=f"Call credits ({q['credit_minutes']} min)",
        )
    except RuntimeError:
        row.status = "failed"  # nothing exists at Paddle, nothing can be paid
        db.commit()
        raise
    row.provider_transaction_id = session.provider_session_id
    row.checkout_url = session.checkout_url
    db.commit()
    return row


def _find(db: Session, data: dict) -> TopupPurchase | None:
    txn_id = data.get("id") or data.get("transaction_id")
    row = None
    if txn_id:
        row = db.query(TopupPurchase).filter(TopupPurchase.provider_transaction_id == txn_id).with_for_update().first()
    if row is None:
        topup_id = (data.get("custom_data") or {}).get("topup_id")
        try:
            tid = uuid.UUID(str(topup_id)) if topup_id else None
        except ValueError:
            tid = None
        if tid is not None:
            row = db.query(TopupPurchase).filter(TopupPurchase.id == tid).with_for_update().first()
    return row


def is_topup_transaction(data: dict) -> bool:
    return (data.get("custom_data") or {}).get("kind") == "topup"


def _items_total_minor(data: dict) -> int | None:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    total = 0
    for it in items:
        price = it.get("price") if isinstance(it, dict) else None
        unit = (price or {}).get("unit_price") or {}
        try:
            total += int(unit["amount"]) * int(it.get("quantity", 1))
        except (KeyError, TypeError, ValueError):
            return None
        if unit.get("currency_code") not in (None, CURRENCY):
            return None
    return total


def _grant(db: Session, row: TopupPurchase, txn_id: str) -> bool:
    lock_org_credits(db, row.organization_id)
    granted = add_ledger_entry(
        db,
        organization_id=row.organization_id,
        kind="topup",
        seconds=row.credit_seconds,
        idempotency_key=f"topup:{txn_id}",
        reference=txn_id,
        note=f"Top-up ${row.amount_minor / 100:,.2f}",
    )
    row.status = "completed"
    row.completed_at = row.completed_at or datetime.now(timezone.utc)
    row.provider_transaction_id = row.provider_transaction_id or txn_id
    return granted


def complete_topup(db: Session, data: dict) -> bool:
    """Called with a Paddle transaction (webhook or reconciler read-back) whose status is `completed`.
    Returns True only if THIS call granted credits."""
    if data.get("status") != "completed":
        return False
    row = _find(db, data)
    if row is None:
        logger.error("Completed top-up transaction %s matches no purchase row — NOT granting; investigate", data.get("id"))
        return False
    if row.status in GRANTED_STATES:
        return False  # already credited (duplicate / retried delivery)
    org = (data.get("custom_data") or {}).get("organization_id")
    if org and str(row.organization_id) != str(org):
        logger.error("Top-up %s org mismatch (row=%s paddle=%s) — NOT granting", row.id, row.organization_id, org)
        return False
    paid = _items_total_minor(data)
    if paid != row.amount_minor:
        logger.error("Top-up %s amount mismatch (row=%s paddle=%s) — NOT granting", row.id, row.amount_minor, paid)
        return False
    return _grant(db, row, str(data["id"]))


def fail_topup(db: Session, data: dict) -> bool:
    """canceled / payment_failed / past_due: nothing was paid, nothing is granted. Can still complete later if it is paid."""
    row = _find(db, data)
    if row is None or row.status != "created":
        return False
    row.status = "failed"
    return True


def apply_adjustment(db: Session, adj: dict) -> bool:
    """Paddle adjustment (refund / chargeback) for a top-up transaction. Idempotent per adjustment id."""
    if adj.get("status") != "approved":
        return False  # pending / rejected / reversed: no money has moved
    action = adj.get("action")
    if action not in ("refund", "chargeback", "chargeback_reverse"):
        return False
    row = _find(db, {"id": adj.get("transaction_id")})
    if row is None:
        return False  # not a top-up (e.g. a subscription transaction): credits unaffected by policy
    adj_id = str(adj.get("id"))
    txn_id = row.provider_transaction_id or str(adj.get("transaction_id"))
    if row.status == "created":
        # An approved adjustment proves the transaction was paid; credit it first so the ledger tells the whole story.
        _grant(db, row, txn_id)
    lock_org_credits(db, row.organization_id)

    if action == "refund":
        try:
            refunded = int(((adj.get("totals") or {}).get("subtotal")))
        except (TypeError, ValueError):
            logger.error("Refund %s has no usable subtotal — flagged for manual review", adj_id)
            return False
        row.refunded_minor = min(row.amount_minor, row.refunded_minor + refunded)
        target = row.credit_seconds * row.refunded_minor // row.amount_minor
        delta = target - row.clawed_back_seconds
        applied = delta > 0 and add_ledger_entry(
            db, organization_id=row.organization_id, kind="refund", seconds=-delta,
            idempotency_key=f"refund:{adj_id}", reference=adj_id, note=f"Refund ${refunded / 100:,.2f}",
        )
        if applied:
            row.clawed_back_seconds += delta
            row.status = "refunded" if row.refunded_minor >= row.amount_minor else "partially_refunded"
        return bool(applied)

    if action == "chargeback":
        remaining = row.credit_seconds - row.clawed_back_seconds
        applied = remaining > 0 and add_ledger_entry(
            db, organization_id=row.organization_id, kind="chargeback", seconds=-remaining,
            idempotency_key=f"chargeback:{adj_id}", reference=adj_id, note="Chargeback",
        )
        if applied:
            row.clawed_back_seconds += remaining
            row.status = "chargeback"
        return bool(applied)

    # chargeback_reverse: the dispute was decided in our favour — give the credits back.
    restored = add_ledger_entry(
        db, organization_id=row.organization_id, kind="adjustment", seconds=row.clawed_back_seconds,
        idempotency_key=f"chargeback_reverse:{adj_id}", reference=adj_id, note="Chargeback reversed",
    ) if row.status == "chargeback" and row.clawed_back_seconds > 0 else False
    if restored:
        row.clawed_back_seconds = 0
        row.status = "completed"
    return bool(restored)


def reconcile_topups(db: Session, provider=None, now: datetime | None = None) -> dict:
    """
    Settles top-ups whose webhook never arrived (or arrived out of order) by reading the transaction back
    from Paddle. completed -> credits (idempotent); canceled / past_due / payment failed -> failed, zero credits;
    still draft/ready/billed -> left alone (the customer may yet pay). Rows that never got a transaction
    (a crash between our insert and Paddle's reply) are failed after 30 minutes: nothing can have been paid.
    """
    now = now or datetime.now(timezone.utc)
    result = {"granted": 0, "failed": 0, "still_open": 0, "errors": 0}
    stale = now - timedelta(minutes=10)
    rows = db.query(TopupPurchase).filter(TopupPurchase.status == "created", TopupPurchase.created_at < stale).all()
    for row in rows:
        if not row.provider_transaction_id:
            if row.created_at < now - timedelta(minutes=30):
                row.status = "failed"
                result["failed"] += 1
            continue
        try:
            provider = provider or PaddleBillingProvider()
            txn = provider.get_transaction(row.provider_transaction_id)
        except RuntimeError:
            result["errors"] += 1
            continue
        if txn is None:
            continue
        status = txn.get("status")
        if status == "completed":
            if complete_topup(db, txn):
                result["granted"] += 1
        elif status in ("canceled", "past_due"):
            row.status = "failed"
            result["failed"] += 1
        else:
            result["still_open"] += 1
    db.commit()
    return result
