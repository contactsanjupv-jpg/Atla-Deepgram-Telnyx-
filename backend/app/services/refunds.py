"""
Refunds: a SEPARATE, explicitly authorized workflow. Cancelling a subscription never reaches this module.

Who: platform admins only (api/v1/admin_refunds.py). Customers cannot trigger a refund by any route.
What it checks before asking Paddle (eligibility, not a blanket denial - there is deliberately NO time-window or
"no refunds" rule here, so refunds that law or Paddle's terms require are always possible):
  * a written reason and a basis (see BASES);
  * Paddle itself reports the transaction as completed (we never refund on our own say-so);
  * the transaction belongs to a subscription / organization we know;
  * it was not already refunded or requested (one request per transaction, enforced by a unique row that is
    written BEFORE the provider call).
Provider confirmation: a request is "approved" only when Paddle's adjustment says approved (the adjustment webhook
or the immediate response); until then it is "submitted". Every step is written to audit_logs.
Full refunds only; partial refunds are done by hand in the Paddle dashboard and then arrive as adjustment webhooks.
Credits: this module changes no credit ledger. (Top-up refunds claw credits back in topups.apply_adjustment.)
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.billing import RefundRequest, Subscription
from app.db.models.platform import AuditLog
from app.providers.billing.paddle_billing_provider import PaddleAPIError

logger = logging.getLogger("atla.refunds")

BASES = ("legal_requirement", "provider_or_dispute", "service_failure", "billing_error", "goodwill")
MIN_REASON_CHARS = 10


class RefundRejected(Exception):
    """Not eligible / invalid. The message is safe to show to the admin."""


def _audit(db: Session, row: RefundRequest, action: str, actor: uuid.UUID | None, **meta) -> None:
    db.add(AuditLog(organization_id=row.organization_id, actor_user_id=actor, action=action, target_type="refund_request",
                    target_id=str(row.id), audit_metadata={"transaction_id": row.transaction_id, "basis": row.basis, **meta}))


def _organization_for(db: Session, txn: dict) -> uuid.UUID | None:
    sub_id = txn.get("subscription_id")
    if sub_id:
        sub = db.query(Subscription).filter(Subscription.billing_provider == "paddle", Subscription.external_subscription_id == sub_id).first()
        if sub is not None:
            return sub.organization_id
    return None


def request_refund(db: Session, admin_user_id: uuid.UUID, transaction_id: str, basis: str, reason: str, provider) -> RefundRequest:
    reason = (reason or "").strip()
    if basis not in BASES:
        raise RefundRejected("Unknown refund basis.")
    if len(reason) < MIN_REASON_CHARS:
        raise RefundRejected("A written reason is required.")
    txn = provider.get_transaction(transaction_id)
    if not txn:
        raise RefundRejected("Paddle has no such transaction.")
    if txn.get("status") != "completed":
        raise RefundRejected("Paddle does not report this transaction as completed, so there is nothing to refund.")
    org_id = _organization_for(db, txn)
    if org_id is None:
        raise RefundRejected("This transaction does not belong to a subscription we know.")

    totals = ((txn.get("details") or {}).get("totals") or {})
    try:
        amount = int(totals.get("grand_total"))
    except (TypeError, ValueError):
        amount = None

    # Lock any existing row so two concurrent requests for the same transaction cannot both reach the provider.
    row = db.query(RefundRequest).filter(RefundRequest.transaction_id == transaction_id).with_for_update().first()
    if row is not None and not _retryable(row):
        raise RefundRejected(f"A refund for this transaction already exists (status: {row.status}).")
    if row is None:
        try:
            with db.begin_nested():
                row = RefundRequest(organization_id=org_id, transaction_id=transaction_id, requested_by=admin_user_id,
                                    basis=basis, reason=reason, amount_minor=amount, currency=txn.get("currency_code"))
                db.add(row)
                db.flush()  # the unique(transaction_id) constraint decides a race: the loser lands below
        except IntegrityError as e:
            db.rollback()
            raise RefundRejected("A refund for this transaction is already being requested.") from e
    else:  # re-attempt of a failed / unconfirmed / stale request on the SAME row
        row.requested_by, row.basis, row.reason, row.status, row.last_error = admin_user_id, basis, reason, "requested", None
    _audit(db, row, "refund.requested", admin_user_id, amount_minor=amount)
    db.commit()  # durable intent BEFORE the provider call

    # A request whose outcome we never saw (timeout / 5xx / crash) may have been accepted by Paddle. Before posting
    # again, ask Paddle: if a refund adjustment exists for this transaction, adopt it instead of creating another.
    if row.attempts > 0:
        existing = _existing_refund(provider, transaction_id)
        if existing is not None:
            return _apply_provider_result(db, row, existing, admin_user_id, adopted=True)

    row.attempts += 1
    db.commit()
    try:
        adj = provider.create_refund(transaction_id, f"[{basis}] {reason}")
    except PaddleAPIError as e:
        code = getattr(e, "status_code", 0) or 0
        definitive = 400 <= code < 500  # Paddle answered and refused: nothing was created
        row.status = "failed" if definitive else "unconfirmed"
        row.last_error = f"paddle_status={code}"
        _audit(db, row, f"refund.{row.status}", admin_user_id, error=row.last_error)
        db.commit()
        logger.error("Refund request %s [txn=%s]: %s", row.status, transaction_id, e)
        raise
    return _apply_provider_result(db, row, adj, admin_user_id)


STALE_REQUEST = timedelta(minutes=5)


def _retryable(row: RefundRequest) -> bool:
    if row.status in ("failed", "unconfirmed"):
        return True
    # "requested" with no provider answer for a while = the process died between intent and call
    return row.status == "requested" and row.updated_at is not None and row.updated_at <= datetime.now(timezone.utc) - STALE_REQUEST


def _existing_refund(provider, transaction_id: str) -> dict | None:
    """Raises PaddleAPIError if Paddle cannot be asked: then we must NOT post a new refund."""
    for adj in provider.list_refund_adjustments(transaction_id):
        if adj.get("status") in ("pending_approval", "approved") and adj.get("id"):
            return adj
    return None


def _apply_provider_result(db: Session, row: RefundRequest, adj: dict, admin_user_id, adopted: bool = False) -> RefundRequest:
    row.provider_adjustment_id, row.provider_status = adj.get("id"), adj.get("status")
    row.status = "approved" if adj.get("status") == "approved" else "submitted"
    _audit(db, row, "refund.adopted" if adopted else "refund.submitted", admin_user_id, provider_status=row.provider_status)
    db.commit()
    return row


def record_adjustment(db: Session, adj: dict) -> bool:
    """Paddle adjustment webhook: the provider's confirmation. Moves a request to approved / rejected."""
    if adj.get("action") != "refund" or not adj.get("id"):
        return False
    row = db.query(RefundRequest).filter(RefundRequest.provider_adjustment_id == str(adj["id"])).with_for_update().first()
    if row is None and adj.get("transaction_id"):
        # Paddle accepted a request whose response we never saved: the webhook is how we learn the adjustment id.
        row = (
            db.query(RefundRequest)
            .filter(RefundRequest.transaction_id == str(adj["transaction_id"]),
                    RefundRequest.provider_adjustment_id.is_(None),
                    RefundRequest.status.in_(("requested", "unconfirmed", "failed")))
            .with_for_update()
            .first()
        )
        if row is not None:
            row.provider_adjustment_id = str(adj["id"])
    if row is None:
        return False  # a refund made in the Paddle dashboard: topups.apply_adjustment handles any credit effect
    new = {"approved": "approved", "rejected": "rejected", "pending_approval": "submitted"}.get(adj.get("status"))
    if new is None or row.status == new or row.status in ("approved", "rejected"):
        return False  # terminal states never move backwards; an unchanged status is not news
    row.status, row.provider_status = new, adj.get("status")
    _audit(db, row, f"refund.{new}", None, provider_status=adj.get("status"))
    return True
