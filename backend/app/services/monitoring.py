"""
Minimal production monitoring. Three layers, none of which needs a vendor SDK:

  1. GET /health/ready  - for an EXTERNAL uptime monitor (UptimeRobot, Better Stack, ...). 200 only if the database,
     Redis and the background worker (fresh heartbeat) all work. This is what catches "the whole thing is down" and
     "the worker died", because it does not depend on our own process being alive to report itself.
  2. alert_conditions() - things that need a human: dead/stuck webhooks, rejected webhook signatures, failing worker,
     stuck or payment-blocked phone provisioning, stuck refunds. Read via GET /health/alerts (token) and pushed by
     the worker to ALERT_WEBHOOK_URL once per cooldown.
  3. ERROR logs prefixed "ALERT" with counts only.

Nothing here ever includes secrets, URLs, ids of customers, payment data or call content: counts and categories only.
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.billing import RefundRequest
from app.db.models.platform import WebhookEvent
from app.db.models.telephony import PhoneProvisioning

logger = logging.getLogger("atla.monitoring")
_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)

HEARTBEAT_KEY = "atla:worker:last_pass"
FAILURES_KEY = "atla:worker:consecutive_failures"
WORKER_STALE_SECONDS = 120  # the loop runs every 15s
WORKER_FAILURE_THRESHOLD = 3
STUCK_MINUTES = 15
REFUND_PENDING_HOURS = 48
WEBHOOK_REJECTION_THRESHOLD = 10  # bad signatures per hour: a wrong secret rejects every genuine event
WEBHOOK_FAILURE_THRESHOLD = 3
COUNTER_WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str  # "critical" | "warning"
    message: str
    count: int = 0


# ---------- counters (rolling-hour, Redis) ----------

def count_event(name: str) -> None:
    """Best-effort: monitoring must never break the request it observes."""
    try:
        key = f"atla:counter:{name}"
        if _redis.incr(key) == 1:
            _redis.expire(key, COUNTER_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001
        pass


def counter(name: str) -> int:
    try:
        return int(_redis.get(f"atla:counter:{name}") or 0)
    except Exception:  # noqa: BLE001
        return 0


# ---------- worker heartbeat ----------

def record_worker_pass(failed: bool) -> None:
    try:
        _redis.set(HEARTBEAT_KEY, str(time.time()), ex=3600)
        if failed:
            _redis.incr(FAILURES_KEY)
        else:
            _redis.delete(FAILURES_KEY)
    except Exception:  # noqa: BLE001
        logger.error("ALERT could not record the worker heartbeat (Redis unreachable)")


def worker_status(now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    try:
        last = _redis.get(HEARTBEAT_KEY)
        failures = int(_redis.get(FAILURES_KEY) or 0)
    except Exception:  # noqa: BLE001
        return {"alive": False, "age_seconds": None, "consecutive_failures": 0}
    age = None if last is None else max(now - float(last), 0.0)
    return {"alive": age is not None and age <= WORKER_STALE_SECONDS, "age_seconds": age, "consecutive_failures": failures}


# ---------- readiness ----------

def check_database(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def check_redis() -> bool:
    try:
        return bool(_redis.ping())
    except Exception:  # noqa: BLE001
        return False


# ---------- alert conditions ----------

def alert_conditions(db: Session, now: datetime | None = None) -> list[Alert]:
    now = now or datetime.now(timezone.utc)
    stuck_before = now - timedelta(minutes=STUCK_MINUTES)
    alerts: list[Alert] = []

    def add(key, severity, message, count):
        if count > 0:
            alerts.append(Alert(key, severity, message, count))

    ws = worker_status()
    if not ws["alive"]:
        alerts.append(Alert("worker_stopped", "critical", "The background worker has not completed a pass recently.", 1))
    if ws["consecutive_failures"] >= WORKER_FAILURE_THRESHOLD:
        alerts.append(Alert("worker_failing", "critical", "The background worker is failing repeatedly.", ws["consecutive_failures"]))

    for source in ("paddle", "retell"):
        n = counter(f"webhook_rejected:{source}")
        add(f"webhook_rejected_{source}", "critical", f"{source.title()} webhooks are being rejected (signature/secret misconfigured?).", n if n >= WEBHOOK_REJECTION_THRESHOLD else 0)
    n = counter("webhook_failed:paddle")
    add("webhook_failed_paddle", "critical", "Paddle webhooks are failing to process.", n if n >= WEBHOOK_FAILURE_THRESHOLD else 0)

    events = db.query(WebhookEvent)
    add("webhook_dead", "critical", "Webhook events gave up after repeated failures and need attention.", events.filter(WebhookEvent.status == "dead").count())
    add("webhook_stuck", "warning", "Webhook events are stuck unprocessed.",
        events.filter(WebhookEvent.status.in_(("received", "failed")), WebhookEvent.created_at <= stuck_before,
                      (WebhookEvent.next_attempt_at.is_(None)) | (WebhookEvent.next_attempt_at <= stuck_before)).count())

    prov = db.query(PhoneProvisioning)
    add("provisioning_stuck", "critical", "Phone-number purchases are stuck in progress.",
        prov.filter(PhoneProvisioning.status.in_(("requested", "purchasing", "ambiguous")), PhoneProvisioning.updated_at <= stuck_before).count())
    add("provisioning_blocked_payment", "critical", "Paid customers have no number: the telephony provider account needs a payment method or balance.",
        prov.filter(PhoneProvisioning.status == "blocked_payment").count())
    add("provisioning_failed", "warning", "Phone-number purchases failed.", prov.filter(PhoneProvisioning.status == "failed").count())

    refunds = db.query(RefundRequest)
    add("refund_stuck", "critical", "Refund requests are stuck before Paddle confirmed them.",
        refunds.filter(RefundRequest.status.in_(("requested", "unconfirmed")), RefundRequest.updated_at <= stuck_before).count())
    add("refund_pending_approval", "warning", "Refunds have waited for Paddle approval for over 48 hours.",
        refunds.filter(RefundRequest.status == "submitted", RefundRequest.updated_at <= now - timedelta(hours=REFUND_PENDING_HOURS)).count())
    return alerts


# ---------- push notification (deduplicated) ----------

def notify(alerts: list[Alert]) -> int:
    """POST newly-firing alerts to ALERT_WEBHOOK_URL, each at most once per cooldown. Returns how many were sent."""
    cfg = get_settings()
    sent = 0
    for a in alerts:
        logger.error("ALERT [%s] %s count=%s", a.severity, a.key, a.count)
        if not cfg.ALERT_WEBHOOK_URL:
            continue
        try:
            if not _redis.set(f"atla:alert:{a.key}", "1", nx=True, ex=cfg.ALERT_COOLDOWN_SECONDS):
                continue  # already announced within the cooldown
        except Exception:  # noqa: BLE001
            continue
        try:
            httpx.post(cfg.ALERT_WEBHOOK_URL, json={"text": f"[Atla {a.severity.upper()}] {a.message} (count: {a.count})"}, timeout=5.0)
            sent += 1
        except Exception:  # noqa: BLE001 - never log the URL (it is a secret)
            logger.error("ALERT could not be delivered to the alert webhook [%s]", a.key)
            try:
                _redis.delete(f"atla:alert:{a.key}")  # retry on the next sweep
            except Exception:  # noqa: BLE001
                pass
    return sent


def sweep(db: Session) -> int:
    """Worker job, at most once a minute (Redis gate; safe with several workers)."""
    try:
        if not _redis.set("atla:monitoring:sweep", "1", nx=True, ex=60):
            return 0
    except Exception:  # noqa: BLE001
        return 0
    return notify(alert_conditions(db))
