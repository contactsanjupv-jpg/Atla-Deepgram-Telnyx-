"""Paddle reconciliation: detects drift, repairs safely, never overwrites newer state."""
import uuid

import pytest

from app.config import get_settings
from app.db.models.billing import Subscription
from app.services.billing_reconcile import reconcile_subscriptions
from tests.test_billing import SECRET, _new_org, _post
from tests.test_entitlements import _ev


@pytest.fixture(autouse=True)
def _paddle_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


class FakePaddle:
    def __init__(self, snapshots=None, fail_ids=()):
        self.snapshots = snapshots or {}
        self.fail_ids = set(fail_ids)
        self.cancelled = []

    def cancel_subscription(self, sid, immediately=False):
        self.cancelled.append((sid, immediately))

    def get_subscription(self, sid):
        if sid in self.fail_ids:
            raise RuntimeError("paddle down")
        return self.snapshots.get(sid)


def _snapshot(sub_id, org_id, status="active", price="pri_starter", updated_at="2026-10-05T00:00:00Z", cancel_at=None):
    snap = {
        "id": sub_id,
        "status": status,
        "updated_at": updated_at,
        "custom_data": {"organization_id": org_id},
        "items": [{"price": {"id": price}}],
        "current_billing_period": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-11-01T00:00:00Z"},
    }
    if cancel_at:
        snap["scheduled_change"] = {"action": "cancel", "effective_at": cancel_at}
    return snap


def _fresh(db, sub_id):
    db.rollback()
    return db.query(Subscription).filter(Subscription.external_subscription_id == sub_id).one()


def _mine(report, sub_id):
    return [d for d in report.drifted if d.external_subscription_id == sub_id]


def test_in_sync_subscription_is_reported_clean(db):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    report = reconcile_subscriptions(db, FakePaddle({sid: _snapshot(sid, org)}))
    assert not _mine(report, sid)
    assert report.in_sync >= 1


def test_missed_webhook_is_detected_but_not_changed_in_report_mode(db):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    report = reconcile_subscriptions(db, FakePaddle({sid: _snapshot(sid, org, status="past_due")}), apply=False)
    drift = _mine(report, sid)[0]
    assert drift.differences["status"] == ("active", "past_due")
    assert _fresh(db, sid).status == "active"  # report-only changed nothing


def test_apply_repairs_status_plan_and_scheduled_cancel_then_is_idempotent(db):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    snap = _snapshot(sid, org, status="active", price="pri_growth", cancel_at="2026-11-01T00:00:00Z")
    report = reconcile_subscriptions(db, FakePaddle({sid: snap}), apply=True)
    assert _mine(report, sid)[0].repaired
    sub = _fresh(db, sid)
    assert sub.plan_id == "growth" and sub.cancel_effective_at is not None
    again = reconcile_subscriptions(db, FakePaddle({sid: snap}), apply=True)
    assert not _mine(again, sid)


def test_repair_never_overwrites_newer_local_state(db):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter", status="canceled", at="2026-10-09T00:00:00Z"))
    old_active = _snapshot(sid, org, status="active", updated_at="2026-10-02T00:00:00Z")
    report = reconcile_subscriptions(db, FakePaddle({sid: old_active}), apply=True)
    # a locally-canceled row is terminal and skipped entirely
    assert not _mine(report, sid)
    assert _fresh(db, sid).status == "canceled"


def test_snapshot_older_than_local_event_is_not_applied(db):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter", at="2026-10-09T00:00:00Z"))
    stale = _snapshot(sid, org, status="past_due", updated_at="2026-10-02T00:00:00Z")
    report = reconcile_subscriptions(db, FakePaddle({sid: stale}), apply=True)
    drift = _mine(report, sid)[0]
    assert not drift.repaired and "newer" in drift.note
    assert _fresh(db, sid).status == "active"


def test_subscription_missing_at_paddle_is_reported_never_changed(db):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    report = reconcile_subscriptions(db, FakePaddle({}), apply=True)
    assert sid in report.missing_at_paddle
    assert _fresh(db, sid).status == "active"


def test_one_provider_failure_does_not_stop_the_sweep(db):
    _c1, org1 = _new_org()
    _c2, org2 = _new_org()
    bad, good = f"sub_{uuid.uuid4().hex[:12]}", f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", bad, org1, "pri_starter"))
    _post(_ev("subscription.created", good, org2, "pri_starter"))
    report = reconcile_subscriptions(db, FakePaddle({good: _snapshot(good, org2, status="paused")}, fail_ids=[bad]), apply=True)
    assert bad in report.errors
    assert _mine(report, good)[0].repaired


def test_duplicate_active_subscriptions_are_flagged_and_only_cancelled_on_apply(db, _guard_never_calls_real_paddle):
    _c, org = _new_org()
    first, second = f"sub_{uuid.uuid4().hex[:12]}", f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", first, org, "pri_starter", at="2026-10-01T00:00:00Z"))
    _post(_ev("subscription.created", second, org, "pri_starter", at="2026-10-01T00:00:05Z"))
    _guard_never_calls_real_paddle.cancelled.clear()  # the webhook guard already acted; isolate reconcile's behaviour

    paddle = FakePaddle({})
    report = reconcile_subscriptions(db, paddle, apply=False)
    assert org in report.duplicate_active_orgs
    assert paddle.cancelled == [] and report.duplicate_cancels_requested == []  # report-only never acts

    from app.services import duplicate_subscriptions as ds

    ds._redis.delete(f"dup-cancel:{second}")  # the webhook guard already requested it; let reconcile do it once
    applied = reconcile_subscriptions(db, paddle, apply=True)
    assert applied.duplicate_cancels_requested == [second]
    assert paddle.cancelled == [(second, False)]  # the NEWER one, at period end
    reconcile_subscriptions(db, paddle, apply=True)
    assert paddle.cancelled == [(second, False)]  # idempotent: not requested twice

# ---------- the scheduled job wrapper (workers/background_jobs.billing_reconcile_job) ----------

def _run_job(monkeypatch, fake):
    import json

    from app.services import monitoring
    from app.workers import background_jobs
    from app.providers.billing import paddle_billing_provider as mod

    monkeypatch.setattr(mod, "PaddleBillingProvider", lambda: fake)
    background_jobs.billing_reconcile_job()
    return json.loads(monitoring.job_get("billing_reconcile", "report"))


def test_the_scheduled_job_repairs_drift_and_publishes_counts_only(db, monkeypatch):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    summary = _run_job(monkeypatch, FakePaddle({sid: _snapshot(sid, org, status="past_due")}))
    assert _fresh(db, sid).status == "past_due" and summary["drift_repaired"] >= 1
    assert set(summary) == {"checked", "drift_repaired", "drift_unresolved", "missing_at_paddle", "read_errors", "duplicate_active_orgs"}
    assert all(isinstance(v, int) for v in summary.values())  # no ids, no field values leave the job


def test_the_scheduled_job_leaves_a_subscription_missing_at_paddle_untouched_and_counts_it(db, monkeypatch):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    summary = _run_job(monkeypatch, FakePaddle({}))  # Paddle knows nothing of it
    assert _fresh(db, sid).status == "active" and summary["missing_at_paddle"] >= 1


def test_a_snapshot_of_a_different_subscription_is_never_applied(db, monkeypatch):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    wrong = _snapshot("sub_someone_else", org, status="canceled")
    summary = _run_job(monkeypatch, FakePaddle({sid: wrong}))
    assert _fresh(db, sid).status == "active" and summary["read_errors"] >= 1


def test_a_stale_paddle_snapshot_cannot_move_newer_local_state_and_a_canceled_row_is_not_resurrected(db, monkeypatch):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    _post(_ev("subscription.canceled", sid, org, "pri_starter", status="canceled", at="2026-10-02T00:00:00.000000Z"))
    assert _fresh(db, sid).status == "canceled"
    _run_job(monkeypatch, FakePaddle({sid: _snapshot(sid, org, status="active", updated_at="2020-01-01T00:00:00Z")}))
    assert _fresh(db, sid).status == "canceled"  # terminal rows are excluded; stale snapshots are ignored


def test_running_the_scheduled_job_twice_changes_nothing_the_second_time(db, monkeypatch):
    _c, org = _new_org()
    sid = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_ev("subscription.created", sid, org, "pri_starter"))
    fake = FakePaddle({sid: _snapshot(sid, org, status="past_due")})
    first = _run_job(monkeypatch, fake)
    second = _run_job(monkeypatch, fake)
    assert first["drift_repaired"] >= 1 and second["drift_repaired"] == 0
