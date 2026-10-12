"""
POST /webhooks/paddle — the only writer of `subscriptions` rows. The
checkout endpoint never writes billing state, so a client can't fake "I paid".
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models.platform import WebhookEvent, WebhookSource
from app.services import topups
from app.services.duplicate_subscriptions import after_subscription_applied
from app.services.subscription_sync import parse_dt as _parse_dt
from app.services.subscription_sync import sync_subscription as _sync_subscription

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("atla.webhooks.paddle")

_SUBSCRIPTION_EVENTS = {
    "subscription.created",
    "subscription.activated",
    "subscription.updated",
    "subscription.canceled",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
}
_TOPUP_FAILURE_EVENTS = {"transaction.canceled", "transaction.payment_failed", "transaction.past_due"}


def verify_paddle_signature(
    raw_body: bytes, header: str | None, secret: str, max_skew_seconds: int, now: float | None = None
) -> bool:
    """Header format: ts=<unix>;h1=<hex> (several h1 during secret rotation).
    Signed payload is b'<ts>:' + raw body, HMAC-SHA256 with the endpoint secret."""
    if not header or not secret:
        return False
    ts = None
    signatures: list[str] = []
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            ts = value
        elif key == "h1":
            signatures.append(value)
    if not ts or not signatures:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts_int) > max_skew_seconds:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b":" + raw_body, hashlib.sha256).hexdigest().encode()
    return any(hmac.compare_digest(sig.encode(), expected) for sig in signatures)


@router.post("/webhooks/paddle", status_code=status.HTTP_204_NO_CONTENT)
async def handle_paddle_webhook(
    request: Request,
    db: Session = Depends(get_db),
    paddle_signature: str | None = Header(default=None, alias="Paddle-Signature"),
):
    raw_body = await request.body()

    if not verify_paddle_signature(
        raw_body, paddle_signature, settings.PADDLE_WEBHOOK_SECRET, settings.PADDLE_WEBHOOK_MAX_SKEW_SECONDS
    ):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        event = json.loads(raw_body)
        event_id = event["event_id"]
        event_type = event["event_type"]
        data = event["data"]
        occurred_at = _parse_dt(event["occurred_at"])
        if not isinstance(data, dict) or occurred_at is None:
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    record = WebhookEvent(
        source=WebhookSource.paddle, external_event_id=event_id, event_type=event_type, payload=event
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # duplicate delivery — already handled
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Record + process in ONE transaction: if processing fails, the event row
    # rolls back too, so Paddle's retry is processed instead of dropped as a duplicate.
    try:
        if event_type in _SUBSCRIPTION_EVENTS:
            _sync_subscription(db, data, occurred_at)
        elif event_type == "transaction.completed" and topups.is_topup_transaction(data):
            topups.complete_topup(db, data)  # the ONLY place (besides the reconciler) credits are granted
        elif event_type in _TOPUP_FAILURE_EVENTS and topups.is_topup_transaction(data):
            topups.fail_topup(db, data)
        elif event_type in ("adjustment.created", "adjustment.updated"):
            topups.apply_adjustment(db, data)
        else:
            logger.info("Unhandled Paddle event type: %s", event_type)
        record.processed_at = datetime.now(timezone.utc)
        record.status = "processed"
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Paddle webhook processing failed: %s", event_id)
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if event_type in _SUBSCRIPTION_EVENTS:
        after_subscription_applied(db, data)  # post-commit, never raises
    return Response(status_code=status.HTTP_204_NO_CONTENT)
