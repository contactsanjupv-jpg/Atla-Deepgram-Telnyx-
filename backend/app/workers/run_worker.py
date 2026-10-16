"""
The background worker. Run it next to the API:

    python -m app.workers.run_worker

One loop, Postgres-backed (no extra infrastructure). Every pass it:
  * retries Retell events that failed or were never processed,
  * settles ambiguous phone-number purchases,
  * enforces entitlements (suspend / release numbers for lapsed subscriptions; pause when out of credits),
  * settles top-up purchases whose Paddle webhook never arrived,
and it syncs the voice catalog at start-up and every few hours.
Each job is idempotent, so running two workers is safe (just redundant).
"""
import logging
import time

from app.db.base import SessionLocal
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.services.auto_provisioning import settle_auto_provisioning
from app.services import monitoring
from app.services.entitlement import enforce_entitlements
from app.services.phone_provisioning import reconcile_provisioning
from app.services.topups import reconcile_topups
from app.workers.retell_events import retry_due_events
from app.workers.voice_catalog_sync import sync_voice_catalog

logger = logging.getLogger("atla.worker")
LOOP_SECONDS = 15
VOICE_SYNC_SECONDS = 6 * 3600


def run_once(phone_provider=None) -> dict:
    failed: list[str] = []
    try:
        summary: dict = {"events": retry_due_events()}
    except Exception:  # noqa: BLE001 - a failed sweep (e.g. the database restarting) must not stop the other jobs or the loop
        logger.exception("Retell event sweep failed; will retry next pass")
        summary = {"events": "error"}
        failed.append("events")
    db = SessionLocal()
    try:
        provider = phone_provider or RetellPhoneProvider()
        summary["provisioning_settled"] = reconcile_provisioning(db, provider)
        summary["entitlements"] = enforce_entitlements(db, provider)
        try:
            summary["auto_provisioning"] = settle_auto_provisioning(db, provider)
        except Exception:  # noqa: BLE001 - must not stop the other jobs
            db.rollback()
            logger.exception("Automatic number provisioning sweep failed")
            failed.append("auto_provisioning")
        try:
            summary["topups"] = reconcile_topups(db)  # settles purchases whose webhook never arrived
        except RuntimeError:
            logger.error("Paddle is not configured; skipping top-up reconciliation")
    except RuntimeError:
        logger.error("Retell is not configured; skipping provider jobs")
        failed.append("provider_jobs")
    except Exception:  # noqa: BLE001
        logger.exception("Worker pass failed")
        failed.append("pass")
    try:
        summary["alerts_sent"] = monitoring.sweep(db)
    except Exception:  # noqa: BLE001 - monitoring must never break the worker
        db.rollback()
        logger.exception("Monitoring sweep failed")
    finally:
        db.close()
    monitoring.record_worker_pass(failed=bool(failed))
    summary["failed_jobs"] = failed
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    last_voice_sync = 0.0
    while True:
        if time.time() - last_voice_sync > VOICE_SYNC_SECONDS:
            try:
                logger.info("Voice catalog synced: %s voices", sync_voice_catalog())
                last_voice_sync = time.time()
            except Exception:  # noqa: BLE001
                logger.exception("Voice sync failed; will retry")
                last_voice_sync = time.time() - VOICE_SYNC_SECONDS + 300
        run_once()
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
