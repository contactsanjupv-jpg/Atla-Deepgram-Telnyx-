"""
Paddle <-> database reconciliation. Webhooks are at-least-once but a delivery can
still be lost (endpoint down, ngrok restarted, 500s past Paddle's retry window).
This compares each local, non-canceled Paddle subscription with Paddle's own
view and reports drift.

Safety rules:
* Default is REPORT ONLY. `apply=True` repairs through the same stale-safe path
  the webhook uses (services/subscription_sync), with Paddle's `updated_at` as
  the snapshot time, so a repair can never overwrite newer local state.
* Never creates a subscription, never deletes one, never touches a subscription
  Paddle says doesn't exist (reported for a human).
* Idempotent: running it twice changes nothing the second time.
* Also finds orgs holding more than one trialing/active subscription (two paid
  checkouts). Report-only just lists them; --apply schedules the NEWER ones to
  cancel at period end (services/duplicate_subscriptions). Refunds stay manual.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.billing import Subscription
from app.services.billing_state import ACTIVE_STATUSES
from app.services.credits import ensure_plan_grant, period_grant_deficit_seconds
from app.services.duplicate_subscriptions import schedule_duplicate_cancellations
from app.services.plans import plan_id_from_items
from app.services.subscription_sync import parse_dt, sync_subscription

logger = logging.getLogger("atla.billing_reconcile")


@dataclass
class Drift:
    external_subscription_id: str
    organization_id: str
    differences: dict[str, tuple]  # field -> (local, paddle)
    repaired: bool = False
    note: str = ""


@dataclass
class ReconcileReport:
    checked: int = 0
    in_sync: int = 0
    drifted: list[Drift] = field(default_factory=list)
    missing_at_paddle: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duplicate_active_orgs: list[str] = field(default_factory=list)
    duplicate_cancels_requested: list[str] = field(default_factory=list)
    missing_grants: list[str] = field(default_factory=list)  # active subscriptions with no credits for the current period
    grants_created: list[str] = field(default_factory=list)


def _utc(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _differences(local: Subscription, remote: dict) -> dict[str, tuple]:
    diffs: dict[str, tuple] = {}
    if remote.get("status") and local.status != remote["status"]:
        diffs["status"] = (local.status, remote["status"])
    remote_plan = plan_id_from_items(remote.get("items"))
    if remote_plan is not None and local.plan_id != remote_plan:
        diffs["plan"] = (local.plan_id, remote_plan)
    period_end = parse_dt((remote.get("current_billing_period") or {}).get("ends_at"))
    if period_end is not None and _utc(local.current_period_end) != _utc(period_end):
        diffs["period_end"] = (local.current_period_end, period_end)
    scheduled = remote.get("scheduled_change") if isinstance(remote.get("scheduled_change"), dict) else None
    remote_cancel = parse_dt(scheduled.get("effective_at")) if scheduled and scheduled.get("action") == "cancel" else None
    if _utc(local.cancel_effective_at) != _utc(remote_cancel):
        diffs["cancel_effective_at"] = (local.cancel_effective_at, remote_cancel)
    return diffs


def _ensure_grants(db: Session, report: ReconcileReport, apply: bool) -> None:
    """Every active subscription must hold its included credits for the CURRENT period (one grant, idempotent)."""
    from datetime import datetime

    now = datetime.now(timezone.utc)
    for sub in db.query(Subscription).filter(Subscription.status.in_(ACTIVE_STATUSES)).all():
        if not sub.current_period_start or not sub.current_period_end or sub.current_period_end <= now:
            continue
        if period_grant_deficit_seconds(db, sub, now) <= 0:
            continue  # the period already holds the current plan's full allowance (a downgrade never claws back)
        label = sub.external_subscription_id or str(sub.id)
        if not apply:
            report.missing_grants.append(label)
        elif ensure_plan_grant(db, sub, now):
            report.grants_created.append(label)
    if apply:
        db.commit()


def reconcile_subscriptions(db: Session, provider, apply: bool = False) -> ReconcileReport:
    report = ReconcileReport()

    rows = (
        db.query(Subscription)
        .filter(
            Subscription.billing_provider == "paddle",
            Subscription.external_subscription_id.isnot(None),
            Subscription.status != "canceled",  # terminal in Paddle: nothing to repair
        )
        .order_by(Subscription.created_at)
        .all()
    )
    for local in rows:
        external_id = local.external_subscription_id
        report.checked += 1
        try:
            remote = provider.get_subscription(external_id)
        except Exception as e:  # noqa: BLE001 — one failure must not stop the sweep
            logger.error("Reconcile: could not read %s from Paddle: %s", external_id, e)
            report.errors.append(external_id)
            continue
        if remote is None:
            report.missing_at_paddle.append(external_id)
            continue
        diffs = _differences(local, remote)
        if not diffs:
            report.in_sync += 1
            continue
        drift = Drift(external_id, str(local.organization_id), diffs)
        if apply:
            snapshot_at = parse_dt(remote.get("updated_at"))
            if snapshot_at is None:
                drift.note = "not repaired: Paddle snapshot has no updated_at"
            else:
                try:
                    drift.repaired = sync_subscription(db, remote, snapshot_at)
                    db.commit()
                    if not drift.repaired:
                        drift.note = "not repaired: local state is newer than Paddle's snapshot"
                except Exception:  # noqa: BLE001
                    db.rollback()
                    logger.exception("Reconcile: repair failed for %s", external_id)
                    drift.note = "repair failed"
        report.drifted.append(drift)
    _ensure_grants(db, report, apply)
    dupes = (
        db.query(Subscription.organization_id)
        .filter(Subscription.billing_provider == "paddle", Subscription.status.in_(ACTIVE_STATUSES))
        .group_by(Subscription.organization_id)
        .having(func.count(Subscription.id) > 1)
        .all()
    )
    report.duplicate_active_orgs = [str(row[0]) for row in dupes]
    if apply:
        for org in report.duplicate_active_orgs:
            report.duplicate_cancels_requested += schedule_duplicate_cancellations(
                db, uuid.UUID(org), provider_factory=lambda: provider
            )
    return report
