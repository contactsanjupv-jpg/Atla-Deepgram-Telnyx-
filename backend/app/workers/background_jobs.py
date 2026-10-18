"""
Slow, non-urgent jobs that must NEVER delay the worker's time-sensitive loop (event retries, provisioning,
entitlement enforcement, top-up settlement, alerts):

  * voice_sync .......... mirrors Retell's voice catalog into our table (a provider call over the whole catalog);
  * billing_reconcile ... compares our subscriptions with Paddle's own record and repairs drift.

Design (a thread per job inside the worker process, rather than a second worker process):
  * one thread per job, so a job never overlaps itself in this process;
  * a Redis lock per job, so several worker processes never run the same job at once;
  * the "next due" key is checked again AFTER taking the lock, so a second worker that queued behind the first
    does not repeat a run that just finished;
  * a job that raises is retried after a short delay and counted; a job that hangs cannot block anything else and
    is reported by monitoring (workers cannot kill threads; the lock's TTL bounds how long it can block the next run);
  * each run uses its OWN database session and the repairs go through the same stale-safe, idempotent code the
    webhooks use.
Trade-off: a separate process would isolate a wedged driver call completely, but needs a second supervisor entry;
the hard time budget, the lock TTL and the monitoring alerts cover the thread's risks at far less operating cost.
"""
import json
import logging
import threading
import time

import redis

from app.config import get_settings
from app.core.locks import LockNotAcquired, redis_lock
from app.services import monitoring

logger = logging.getLogger("atla.jobs")
_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
TICK_SECONDS = 15
LOCKED_RETRY_SECONDS = 60


def run_job(name: str, fn, *, interval: int, retry: int, budget: int) -> str:
    """One scheduling decision. Returns: not_due | locked | ok | failed."""
    next_key = f"atla:job:{name}:next"
    try:
        if _redis.exists(next_key):
            return "not_due"
        with redis_lock(f"job:{name}", ttl_seconds=budget):
            if _redis.exists(next_key):  # another worker finished while we waited for the lock
                return "not_due"
            monitoring.job_record(name, "last_start", str(time.time()))
            try:
                fn()
            except Exception:  # noqa: BLE001 - a failing job is counted and retried, never fatal
                logger.exception("Background job '%s' failed; will retry", name)
                try:
                    _redis.incr(f"atla:job:{name}:failures")
                    _redis.expire(f"atla:job:{name}:failures", 7 * 86400)
                    _redis.set(next_key, "1", ex=retry)
                except Exception:  # noqa: BLE001
                    pass
                monitoring.job_record(name, "last_finish", str(time.time()))
                return "failed"
            monitoring.job_record(name, "last_finish", str(time.time()))
            monitoring.job_record(name, "last_success", str(time.time()))
            try:
                _redis.delete(f"atla:job:{name}:failures")
                _redis.set(next_key, "1", ex=interval)
            except Exception:  # noqa: BLE001
                pass
            return "ok"
    except LockNotAcquired:
        return "locked"
    except Exception:  # noqa: BLE001 - Redis down: try again next tick
        logger.exception("Background job '%s' could not be scheduled", name)
        return "failed"


def voice_sync_job() -> None:
    from app.workers.voice_catalog_sync import sync_voice_catalog

    logger.info("Voice catalog synced: %s voices", sync_voice_catalog())


def billing_reconcile_job() -> None:
    """Apply mode, stale-safe: it only updates rows we already have (never creates a subscription), reads Paddle's own
    record over the authenticated API, and skips a repair when our state is newer than Paddle's snapshot. Missing-at-
    Paddle subscriptions are never changed; they are reported for manual review."""
    from app.db.base import SessionLocal
    from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
    from app.services.billing_reconcile import reconcile_subscriptions

    db = SessionLocal()
    try:
        report = reconcile_subscriptions(db, PaddleBillingProvider(), apply=True)
    finally:
        db.close()
    summary = {
        "checked": report.checked,
        "drift_repaired": sum(1 for d in report.drifted if d.repaired),
        "drift_unresolved": sum(1 for d in report.drifted if not d.repaired),
        "missing_at_paddle": len(report.missing_at_paddle),
        "read_errors": len(report.errors),
        "duplicate_active_orgs": len(report.duplicate_active_orgs),
    }  # counts only: no ids, no field values
    monitoring.job_record("billing_reconcile", "report", json.dumps(summary), ex=3 * get_settings().BILLING_RECONCILE_INTERVAL_SECONDS)
    logger.info("Billing reconciliation: %s", summary)


def jobs() -> list[tuple[str, object, int, int, int]]:
    cfg = get_settings()
    return [
        ("voice_sync", voice_sync_job, cfg.VOICE_SYNC_INTERVAL_SECONDS, 300, monitoring.JOB_HARD_BUDGET_SECONDS["voice_sync"]),
        ("billing_reconcile", billing_reconcile_job, cfg.BILLING_RECONCILE_INTERVAL_SECONDS, cfg.BILLING_RECONCILE_RETRY_SECONDS,
         monitoring.JOB_HARD_BUDGET_SECONDS["billing_reconcile"]),
    ]


def start_background_jobs(tick: float = TICK_SECONDS) -> threading.Event:
    stop = threading.Event()
    for name, fn, interval, retry, budget in jobs():
        def loop(name=name, fn=fn, interval=interval, retry=retry, budget=budget):
            while True:
                try:
                    run_job(name, fn, interval=interval, retry=retry, budget=budget)
                except Exception:  # noqa: BLE001
                    logger.exception("Background job loop '%s' error", name)
                if stop.wait(tick):
                    return

        threading.Thread(target=loop, name=f"atla-job-{name}", daemon=True).start()
    return stop
