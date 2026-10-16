"""
Monitoring: readiness for an external uptime monitor, alert conditions for things that need a human, and a
deduplicated push. Responses and logs carry counts/categories only - never secrets, ids, payment or call data.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import RefundRequest
from app.db.models.platform import WebhookEvent, WebhookSource
from app.db.models.telephony import PhoneProvisioning
from app.main import app
from app.services import monitoring
from app.workers import run_worker
from tests.fakes import FakePhone
from tests.test_phone_lifecycle import _org

client = TestClient(app)
TOKEN = "monitoring-token-for-tests-1234567890"


@pytest.fixture(autouse=True)
def _clean_monitoring_state(monkeypatch):
    r = redis_lib.from_url(get_settings().REDIS_URL, decode_responses=True)
    for key in r.scan_iter("atla:alert:*"):
        r.delete(key)
    for key in r.scan_iter("atla:counter:*"):
        r.delete(key)
    r.delete(monitoring.HEARTBEAT_KEY, monitoring.FAILURES_KEY, "atla:monitoring:sweep")
    monkeypatch.setattr(get_settings(), "MONITORING_TOKEN", TOKEN)
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "")
    yield


def _keys(db=None):
    own = db is None
    db = db or SessionLocal()
    try:
        return {a.key: a.count for a in monitoring.alert_conditions(db)}
    finally:
        if own:
            db.close()


# ---------------- readiness (for the external uptime monitor) ----------------

def test_ready_is_200_only_when_database_redis_and_a_fresh_worker_heartbeat_all_work():
    monitoring.record_worker_pass(failed=False)
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": True, "redis": True, "worker": True}


def test_ready_fails_when_the_worker_has_never_reported_or_has_gone_quiet():
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["worker"] is False
    monitoring._redis.set(monitoring.HEARTBEAT_KEY, str(time.time() - monitoring.WORKER_STALE_SECONDS - 30))
    assert client.get("/health/ready").status_code == 503


def test_ready_fails_when_redis_or_the_database_is_down_without_leaking_why(monkeypatch):
    monitoring.record_worker_pass(failed=False)

    def boom(*a, **k):
        raise ConnectionError("redis://user:secret-password@internal-host:6379 refused")

    monkeypatch.setattr(monitoring._redis, "ping", boom)
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["redis"] is False and "secret" not in r.text and "internal-host" not in r.text
    monkeypatch.undo()
    monkeypatch.setattr(get_settings(), "MONITORING_TOKEN", TOKEN)
    monkeypatch.setattr(monitoring, "check_database", lambda db: False)
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["database"] is False


# ---------------- worker heartbeat and crash loops ----------------

def test_a_worker_pass_records_a_heartbeat_and_repeated_failures_raise_an_alert(monkeypatch):
    monkeypatch.setattr(run_worker, "retry_due_events", lambda: 0)
    monkeypatch.setattr(run_worker, "reconcile_provisioning", lambda db, p: 0)
    monkeypatch.setattr(run_worker, "enforce_entitlements", lambda db, p: {})
    monkeypatch.setattr(run_worker, "settle_auto_provisioning", lambda db, p: 0)
    monkeypatch.setattr(run_worker, "reconcile_topups", lambda db: 0)
    assert run_worker.run_once(phone_provider=FakePhone())["failed_jobs"] == []
    assert monitoring.worker_status()["alive"] is True and "worker_failing" not in _keys()

    def boom(*a, **k):
        raise RuntimeError("database restarting")

    monkeypatch.setattr(run_worker, "retry_due_events", boom)
    for _ in range(monitoring.WORKER_FAILURE_THRESHOLD):
        assert run_worker.run_once(phone_provider=FakePhone())["failed_jobs"] == ["events"]
    assert _keys()["worker_failing"] >= monitoring.WORKER_FAILURE_THRESHOLD
    monkeypatch.setattr(run_worker, "retry_due_events", lambda: 0)
    run_worker.run_once(phone_provider=FakePhone())
    assert "worker_failing" not in _keys()  # recovery clears it


def test_a_silent_worker_is_reported_as_stopped():
    assert "worker_stopped" in _keys()
    monitoring.record_worker_pass(failed=False)
    assert "worker_stopped" not in _keys()


# ---------------- business conditions ----------------

def _delta(make, key):
    before = _keys().get(key, 0)
    make()
    return _keys().get(key, 0) - before


def test_a_dead_webhook_event_is_an_alert():
    def make():
        db = SessionLocal()
        db.add(WebhookEvent(source=WebhookSource.retell, external_event_id=f"x{uuid.uuid4().hex}:call_ended", event_type="call_ended",
                            payload={"call": {"transcript": "PRIVATE CALL CONTENT"}}, status="dead", attempts=8, last_error="boom"))
        db.commit()
        db.close()

    assert _delta(make, "webhook_dead") == 1


def test_stuck_provisioning_and_payment_blocked_provisioning_are_alerts():
    def row(status, minutes_ago):
        _c, org = _org(paid=False)
        db = SessionLocal()
        db.add(PhoneProvisioning(organization_id=uuid.UUID(org), nickname=f"atla-{org}-1", status=status))
        db.commit()
        db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == uuid.UUID(org)).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)})
        db.commit()
        db.close()

    assert _delta(lambda: row("purchasing", 30), "provisioning_stuck") == 1
    assert _delta(lambda: row("purchasing", 1), "provisioning_stuck") == 0  # a purchase in flight is normal
    assert _delta(lambda: row("blocked_payment", 1), "provisioning_blocked_payment") == 1
    assert _delta(lambda: row("failed", 1), "provisioning_failed") == 1


def test_refunds_stuck_before_paddle_confirmed_or_pending_too_long_are_alerts():
    _c, org = _org(paid=False)  # one organization: signups are rate limited

    def row(status, minutes_ago):
        db = SessionLocal()
        tid = f"txn_{uuid.uuid4().hex[:12]}"
        db.add(RefundRequest(organization_id=uuid.UUID(org), transaction_id=tid, basis="goodwill", reason="x" * 12, status=status))
        db.commit()
        db.query(RefundRequest).filter(RefundRequest.transaction_id == tid).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)})
        db.commit()
        db.close()

    assert _delta(lambda: row("unconfirmed", 30), "refund_stuck") == 1
    assert _delta(lambda: row("requested", 30), "refund_stuck") == 1
    assert _delta(lambda: row("requested", 1), "refund_stuck") == 0
    assert _delta(lambda: row("submitted", 60 * 49), "refund_pending_approval") == 1
    assert _delta(lambda: row("submitted", 60), "refund_pending_approval") == 0
    assert _delta(lambda: row("approved", 60 * 100), "refund_stuck") == 0


def test_rejected_webhook_signatures_raise_an_alert_once_they_pile_up(monkeypatch):
    monkeypatch.setattr(get_settings(), "RETELL_API_KEY", "some-key")
    for _ in range(monitoring.WEBHOOK_REJECTION_THRESHOLD - 1):
        assert client.post("/webhooks/retell", content=b"{}", headers={"X-Retell-Signature": "v=1,d=ab"}).status_code == 401
    assert "webhook_rejected_retell" not in _keys()
    client.post("/webhooks/retell", content=b"{}", headers={"X-Retell-Signature": "v=1,d=ab"})
    assert _keys()["webhook_rejected_retell"] >= monitoring.WEBHOOK_REJECTION_THRESHOLD
    for _ in range(monitoring.WEBHOOK_REJECTION_THRESHOLD):
        assert client.post("/webhooks/paddle", content=b"{}", headers={"Paddle-Signature": "ts=1;h1=bad"}).status_code == 401
    assert "webhook_rejected_paddle" in _keys()


# ---------------- operator endpoint ----------------

def test_the_alerts_endpoint_is_disabled_without_a_token_and_rejects_a_wrong_one(monkeypatch):
    assert client.get("/health/alerts").status_code == 401
    assert client.get("/health/alerts", headers={"Authorization": "Bearer nope"}).status_code == 401
    monkeypatch.setattr(get_settings(), "MONITORING_TOKEN", "")
    assert client.get("/health/alerts", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_the_alerts_endpoint_returns_counts_only(monkeypatch):
    monitoring.record_worker_pass(failed=False)
    r = client.get("/health/alerts", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and set(r.json()) == {"ok", "alerts"}
    assert TOKEN not in r.text and "PRIVATE CALL CONTENT" not in r.text
    for a in r.json()["alerts"]:
        assert set(a) == {"key", "severity", "message", "count"}


# ---------------- push notification ----------------

def test_alerts_are_pushed_once_per_cooldown_and_carry_no_secrets(monkeypatch, caplog):
    url = "https://hooks.example.com/services/SECRET-PATH-TOKEN"
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", url)
    posts = []
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: posts.append((u, json)))
    alert = monitoring.Alert("worker_stopped", "critical", "The background worker has not completed a pass recently.", 1)
    with caplog.at_level(logging.ERROR):
        assert monitoring.notify([alert]) == 1
        assert monitoring.notify([alert]) == 0  # same alert inside the cooldown
    assert len(posts) == 1 and posts[0][0] == url and "worker" in posts[0][1]["text"]
    assert "SECRET-PATH-TOKEN" not in caplog.text
    assert any("ALERT" in r.message for r in caplog.records)


def test_a_failed_delivery_is_retried_on_the_next_sweep_and_never_logs_the_url(monkeypatch, caplog):
    url = "https://hooks.example.com/services/SECRET-PATH-TOKEN"
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", url)

    def down(*a, **k):
        raise ConnectionError(f"cannot reach {url}")

    monkeypatch.setattr(monitoring.httpx, "post", down)
    alert = monitoring.Alert("webhook_dead", "critical", "x", 2)
    with caplog.at_level(logging.ERROR):
        assert monitoring.notify([alert]) == 0
    assert "SECRET-PATH-TOKEN" not in caplog.text
    sent = []
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: sent.append(1))
    assert monitoring.notify([alert]) == 1 and sent == [1]


def test_without_an_alert_webhook_nothing_is_sent_but_the_error_is_still_logged(monkeypatch, caplog):
    monkeypatch.setattr(monitoring.httpx, "post", lambda *a, **k: pytest.fail("no webhook configured"))
    with caplog.at_level(logging.ERROR):
        assert monitoring.notify([monitoring.Alert("webhook_dead", "critical", "x", 1)]) == 0
    assert "ALERT [critical] webhook_dead" in caplog.text
