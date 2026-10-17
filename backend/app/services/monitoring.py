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
import logging
import threading
import time
from contextlib import contextmanager
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
# Three signals, so a SLOW worker is not mistaken for a DEAD one:
#   last_pass  - written when a full pass completes (proves the loop is making progress);
#   liveness   - written every 10 s by a background thread (proves the process is up, even inside a long operation);
#   phase      - "name|start" while a known long operation runs (voice sync, a pass), bounded by PHASE_MAX_SECONDS.
# started_at invalidates passes recorded by a previous process, so readiness is never reported before THIS process
# has completed its first pass.
LIVENESS_KEY = "atla:worker:liveness"
STARTED_KEY = "atla:worker:started_at"
PHASE_KEY = "atla:worker:phase"
LIVENESS_INTERVAL_SECONDS = 10
LIVENESS_STALE_SECONDS = 45
PHASE_MAX_SECONDS = 900  # a long operation is tolerated this long; beyond it the worker counts as stalled
STARTUP_GRACE_SECONDS = 900  # time allowed for the first pass (incl. the initial voice-catalog sync)


def record_worker_pass(failed: bool) -> None:
    try:
        _redis.set(HEARTBEAT_KEY, str(time.time()), ex=3600)
        if failed:
            _redis.incr(FAILURES_KEY)
        else:
            _redis.delete(FAILURES_KEY)
    except Exception:  # noqa: BLE001
        logger.error("ALERT could not record the worker heartbeat (Redis unreachable)")


def beat() -> None:
    try:
        _redis.set(LIVENESS_KEY, str(time.time()), ex=120)
    except Exception:  # noqa: BLE001
        pass


def mark_started() -> None:
    """Called once when the worker process starts: passes recorded before this moment no longer count."""
    try:
        _redis.set(STARTED_KEY, str(time.time()), ex=7 * 86400)
    except Exception:  # noqa: BLE001
        pass
    beat()


def start_liveness_thread(interval: float = LIVENESS_INTERVAL_SECONDS) -> threading.Event:
    """Daemon thread that proves the process is alive while the main loop is inside a long operation."""
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            beat()

    threading.Thread(target=loop, name="atla-worker-liveness", daemon=True).start()
    return stop


@contextmanager
def phase(name: str):
    """Marks a long-running operation so it is not mistaken for a stall (bounded by PHASE_MAX_SECONDS)."""
    try:
        _redis.set(PHASE_KEY, f"{name}|{time.time()}", ex=PHASE_MAX_SECONDS + 60)
    except Exception:  # noqa: BLE001
        pass
    try:
        yield
    finally:
        try:
            _redis.delete(PHASE_KEY)
        except Exception:  # noqa: BLE001
            pass


def worker_status(now: float | None = None) -> dict:
    """state: ok | busy | starting | stalled | stopped. `alive` (= ready) is True only for ok / busy."""
    now = now if now is not None else time.time()
    try:
        last, live, started, ph = (_redis.get(k) for k in (HEARTBEAT_KEY, LIVENESS_KEY, STARTED_KEY, PHASE_KEY))
        failures = int(_redis.get(FAILURES_KEY) or 0)
    except Exception:  # noqa: BLE001
        return {"alive": False, "state": "stopped", "age_seconds": None, "consecutive_failures": 0}

    last_t = float(last) if last else None
    started_t = float(started) if started else None
    if last_t is not None and started_t is not None and last_t < started_t:
        last_t = None  # a pass from before this process started proves nothing about it
    liveness_fresh = bool(live) and now - float(live) <= LIVENESS_STALE_SECONDS
    last_age = None if last_t is None else max(now - last_t, 0.0)
    phase_age = None
    if ph and "|" in ph:
        try:
            phase_age = max(now - float(ph.split("|", 1)[1]), 0.0)
        except ValueError:
            phase_age = None

    if last_age is not None and last_age <= WORKER_STALE_SECONDS:
        state = "ok"
    elif liveness_fresh and last_t is not None and phase_age is not None and phase_age <= PHASE_MAX_SECONDS:
        state = "busy"  # was operational, now inside a known long operation, process demonstrably alive
    elif liveness_fresh and last_t is None and started_t is not None and now - started_t <= STARTUP_GRACE_SECONDS:
        state = "starting"  # up, but has not completed its first pass: NOT ready yet, and not an alarm yet
    elif liveness_fresh:
        state = "stalled"  # process is up but no progress and no excuse
    else:
        state = "stopped"
    return {"alive": state in ("ok", "busy"), "state": state, "age_seconds": last_age, "consecutive_failures": failures}


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
    if ws["state"] in ("stopped", "stalled"):
        what = "has stopped" if ws["state"] == "stopped" else "is running but making no progress"
        alerts.append(Alert("worker_stopped", "critical", f"The background worker {what}.", 1))
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

class _DeliveryRejected(Exception):
    def __init__(self, status):
        self.status = status

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
            resp = httpx.post(cfg.ALERT_WEBHOOK_URL, json={"text": f"[Atla {a.severity.upper()}] {a.message} (count: {a.count})"}, timeout=5.0)
            code = getattr(resp, "status_code", None)
            if not (isinstance(code, int) and 200 <= code < 300):
                # 429, 5xx, 4xx and redirects are all failures: the alert did NOT reach anyone.
                raise _DeliveryRejected(code)
            sent += 1
        except Exception as e:  # noqa: BLE001 - never log the URL (it is a secret); class and status only
            detail = f"status={e.status}" if isinstance(e, _DeliveryRejected) else f"error={type(e).__name__}"
            logger.error("ALERT could not be delivered to the alert webhook [%s] %s", a.key, detail)
            try:
                _redis.delete(f"atla:alert:{a.key}")  # a failed delivery must not suppress the retry
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
