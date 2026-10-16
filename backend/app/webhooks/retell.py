"""
POST /webhooks/retell — the single ingress point for all Retell call
events (call_started, call_ended, call_analyzed, transcript_updated, ...).

Security properties enforced here, in order:
  1. Raw-body signature verification (app/webhooks/retell_signature.py)
     BEFORE anything else touches the payload.
  2. Idempotency via the webhook_events table's UNIQUE(source, external_event_id)
     — a duplicate delivery is recorded as a no-op, never double-processed.
  3. The event is persisted BEFORE processing; processing failures are recorded
     on the event and retried by the sweeper, never lost.
"""
import json
import logging

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models.platform import WebhookEvent, WebhookSource
from app.services import monitoring
from app.webhooks.retell_signature import InvalidRetellSignature, verify_retell_signature

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("atla.webhooks.retell")


@router.post("/webhooks/retell", status_code=status.HTTP_204_NO_CONTENT)
async def handle_retell_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_retell_signature: str | None = Header(default=None),
):
    raw_body = await request.body()

    try:
        verify_retell_signature(
            raw_body=raw_body,
            signature_header=x_retell_signature,
            api_key=settings.RETELL_API_KEY,
            max_skew_seconds=settings.RETELL_WEBHOOK_MAX_SKEW_SECONDS,
        )
    except InvalidRetellSignature as exc:
        # The RESPONSE stays a bare 401 (don't tell an attacker why), but the reason is
        # logged server-side so a misconfiguration is diagnosable. Never log the key itself:
        # only its last 4 characters, enough to tell which key this process loaded.
        logger.warning(
            "Retell webhook rejected: %s | signature_header_present=%s body_bytes=%d api_key_last4=%s",
            exc,
            bool(x_retell_signature),
            len(raw_body),
            (settings.RETELL_API_KEY or "")[-4:] or "<EMPTY - RETELL_API_KEY is not set>",
        )
        monitoring.count_event("webhook_rejected:retell")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = json.loads(raw_body)
        event_type = payload["event"]
        call_id = payload["call"]["call_id"]
        if not isinstance(event_type, str) or not isinstance(call_id, str) or not call_id:
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # Retell sends no universal event id; call_id + event name is a stable
    # idempotency key (a duplicate delivery of the same event is a no-op).
    event = WebhookEvent(
        source=WebhookSource.retell,
        external_event_id=f"{call_id}:{event_type}",
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # The event is durable now. Process it inline (fast DB work, well inside
    # Retell's 10s window). Failure is recorded on the event and retried by the
    # sweeper — we ALWAYS ack, because re-delivery would only hit the unique key.
    from app.workers.retell_events import enqueue_event_processing

    enqueue_event_processing(str(event.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
