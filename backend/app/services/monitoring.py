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
# Signals, so a SLOW worker is not mistaken for a DEAD one and a STUCK one is not mistaken for a slow one:
#   last_pass  - written when a full pass completes (proves the loop is making progress);
#   liveness   - written every 10 s by the WATCHDOG thread, and ONLY while the main loop is within its time budget
#                (workers/watchdog.py). A stuck main loop therefore stops the beat itself, and the process exits;
#   phase      - "name|start|budget" while the main loop runs a bounded operation (a pass). Its budget, not a global
#                constant, decides how long it may keep the worker "busy".
# started_at invalidates passes recorded by a previous process, so readiness is never reported before THIS process
# has completed its first pass. Slow, non-urgent jobs (voice sync, subscription reconciliation) do NOT run in the
# main loop any more; they have their own state below (background jobs) and never affect worker readiness.
LIVENESS_KEY = "atla:worker:liveness"
STARTED_KEY = "atla:worker:started_at"
PHASE_KEY = "atla:worker:phase"
WATCHDOG_EXIT_KEY = "atla:worker:watchdog_exit"
LIVENESS_INTERVAL_SECONDS = 10
LIVENESS_STALE_SECONDS = 45
PHASE_MAX_SECONDS = 300  # default budget of a phase; the worker passes WORKER_PASS_BUDGET_SECONDS
STARTUP_GRACE_SECONDS = 420  # first pass: one pass budget plus a loop interval and a margin


def record_worker_pass(failed: bool) -> None:
    try:
        _redis.set(HEARTBEAT_KEY, str(time.time()), ex=3600)
        if failed:
            _redis.incr(FAILURES_KEY)
            _redis.expire(FAILURES_KEY, 3600)
        else:
            _redis.delete(FAILURES_KEY)
    except Exception:  # noqa: BLE001 - the heartbeat is best effort; the worker must keep working
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


def record_watchdog_exit(phase_name: str) -> None:
    """The watchdog is about to kill a stalled worker; the NEXT worker process announces it (the dead one cannot)."""
    try:
        _redis.set(WATCHDOG_EXIT_KEY, f"{phase_name}|{time.time()}", ex=3600)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def phase(name: str, budget_seconds: int = PHASE_MAX_SECONDS):
    """Marks a bounded operation so it is not mistaken for a stall, for at most `budget_seconds`."""
    try:
        _redis.set(PHASE_KEY, f"{name}|{time.time()}|{budget_seconds}", ex=budget_seconds + 60)
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
    phase_age, phase_budget = None, PHASE_MAX_SECONDS
    if ph and "|" in ph:
        fields = ph.split("|")
        try:
            phase_age = max(now - float(fields[1]), 0.0)
            if len(fields) > 2:
                phase_budget = float(fields[2])
        except (ValueError, IndexError):
            phase_age = None

    if last_age is not None and last_age <= WORKER_STALE_SECONDS:
        state = "ok"
    elif liveness_fresh and last_t is not None and phase_age is not None and phase_age <= phase_budget:
        state = "busy"  # was operational, now inside a bounded operation, process demonstrably alive
    elif liveness_fresh and last_t is None and started_t is not None and now - started_t <= STARTUP_GRACE_SECONDS:
        state = "starting"  # up, but has not completed its first pass: NOT ready yet, and not an alarm yet
    elif liveness_fresh:
        state = "stalled"  # process is up but no progress and no excuse
    else:
        state = "stopped"
    return {"alive": state in ("ok", "busy"), "state": state, "age_seconds": last_age, "consecutive_failures": failures}


# ---------- background jobs (voice sync, subscription reconciliation) ----------
# Each job records when it last started, finished and succeeded, so a job that hangs or keeps failing is visible
# without ever being able to make the worker look dead (or alive).

JOB_KEY = "atla:job:{name}:{field}"
JOB_HARD_BUDGET_SECONDS = {"voice_sync": 900, "billing_reconcile": 1800}


def _job_interval(name: str) -> int:
    cfg = get_settings()
    return {"voice_sync": cfg.VOICE_SYNC_INTERVAL_SECONDS, "billing_reconcile": cfg.BILLING_RECONCILE_INTERVAL_SECONDS}[name]


def job_record(name: str, field: str, value: str, ex: int = 7 * 86400) -> None:
    try:
        _redis.set(JOB_KEY.format(name=name, field=field), value, ex=ex)
    except Exception:  # noqa: BLE001
        pass


def job_get(name: str, field: str) -> str | None:
    try:
        return _redis.get(JOB_KEY.format(name=name, field=field))
    except Exception:  # noqa: BLE001
        return None


def job_failure_count(name: str) -> int:
    return int(job_get(name, "failures") or 0)


# ---------- configuration consistency (API vs worker) ----------

CONFIG_KEY = "atla:config:{role}"


def publish_config_fingerprint(role: str) -> None:
    try:
        _redis.set(CONFIG_KEY.format(role=role), get_settings().config_fingerprint(), ex=7 * 86400)
    except Exception:  # noqa: BLE001 - best effort; Redis may not be up yet
        pass


def config_mismatch() -> bool:
    try:
        api, worker = _redis.get(CONFIG_KEY.format(role="api")), _redis.get(CONFIG_KEY.format(role="worker"))
    except Exception:  # noqa: BLE001
        return False
    return bool(api and worker and api != worker)


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

    if config_mismatch():
        alerts.append(Alert("config_mismatch", "critical", "The API and the worker are running with different environment settings (environment, Paddle mode, URLs, database or price ids).", 1))
    if _redis_get(WATCHDOG_EXIT_KEY):
        alerts.append(Alert("worker_watchdog_restart", "warning", "The worker stalled and was restarted by its watchdog.", 1))
    alerts.extend(_job_alerts(now))

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


def _redis_get(key: str):
    try:
        return _redis.get(key)
    except Exception:  # noqa: BLE001
        return None


def _job_alerts(now: datetime) -> list[Alert]:
    """Voice sync is cosmetic-ish (alert only when long overdue); billing reconciliation protects revenue."""
    out: list[Alert] = []
    ts = now.timestamp()
    started = _redis_get(STARTED_KEY)
    for name, label in (("voice_sync", "Voice catalog sync"), ("billing_reconcile", "Paddle subscription reconciliation")):
        interval = _job_interval(name)
        began, finished = job_get(name, "last_start"), job_get(name, "last_finish")
        if began and (not finished or float(finished) < float(began)) and ts - float(began) > JOB_HARD_BUDGET_SECONDS[name]:
            out.append(Alert(f"job_stalled_{name}", "critical" if name == "billing_reconcile" else "warning", f"{label} has been running far longer than allowed.", 1))
        last_ok = job_get(name, "last_success")
        reference = float(last_ok) if last_ok else (float(started) if started else None)
        if reference is not None and ts - reference > 3 * interval + JOB_HARD_BUDGET_SECONDS[name]:
            out.append(Alert(f"job_overdue_{name}", "critical" if name == "billing_reconcile" else "warning", f"{label} has not succeeded for over three intervals.", 1))
        fails = job_failure_count(name)
        if fails >= 2:
            out.append(Alert(f"job_failing_{name}", "critical" if name == "billing_reconcile" else "warning", f"{label} is failing repeatedly.", fails))
    import json

    raw = _redis_get(JOB_KEY.format(name="billing_reconcile", field="report"))
    if raw:
        try:
            r = json.loads(raw)
        except ValueError:
            r = {}
        unresolved = int(r.get("drift_unresolved", 0)) + int(r.get("missing_at_paddle", 0)) + int(r.get("duplicate_active_orgs", 0))
        if unresolved:
            out.append(Alert("billing_drift_unresolved", "critical", "Subscriptions differ from Paddle and could not be repaired automatically (or are missing at Paddle): manual review needed.", unresolved))
        if int(r.get("drift_repaired", 0)):
            out.append(Alert("billing_drift_repaired", "warning", "Reconciliation repaired subscriptions that differed from Paddle: a webhook may have been lost.", int(r["drift_repaired"])))
        if int(r.get("read_errors", 0)) and int(r.get("read_errors", 0)) >= int(r.get("checked", 0)) > 0:
            out.append(Alert("billing_reconcile_blind", "critical", "Reconciliation could not read ANY subscription from Paddle (wrong environment or credentials?).", int(r["read_errors"])))
    return out


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
