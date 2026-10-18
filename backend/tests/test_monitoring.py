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


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


TOKEN = "monitoring-token-for-tests-1234567890"


@pytest.fixture(autouse=True)
def _clean_monitoring_state(monkeypatch):
    r = redis_lib.from_url(get_settings().REDIS_URL, decode_responses=True)
    for key in r.scan_iter("atla:alert:*"):
        r.delete(key)
    for key in r.scan_iter("atla:counter:*"):
        r.delete(key)
    for pattern in ("atla:worker:*", "atla:job:*", "atla:config:*", "lock:job:*"):
        for key in r.scan_iter(pattern):
            r.delete(key)
    r.delete("atla:monitoring:sweep")
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
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": True, "redis": True, "worker": True, "worker_state": "ok"}


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
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: posts.append((u, json)) or _Resp(200))
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
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: sent.append(1) or _Resp(200))
    assert monitoring.notify([alert]) == 1 and sent == [1]


def test_without_an_alert_webhook_nothing_is_sent_but_the_error_is_still_logged(monkeypatch, caplog):
    monkeypatch.setattr(monitoring.httpx, "post", lambda *a, **k: pytest.fail("no webhook configured"))
    with caplog.at_level(logging.ERROR):
        assert monitoring.notify([monitoring.Alert("webhook_dead", "critical", "x", 1)]) == 0
    assert "ALERT [critical] webhook_dead" in caplog.text


# ---------------- real connection failures (no mocks on the client behaviour) ----------------

def test_check_database_is_false_for_a_database_that_really_cannot_be_reached():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("postgresql+psycopg://nobody:nopass@127.0.0.1:1/none", connect_args={"connect_timeout": 2})
    session = sessionmaker(bind=engine)()
    try:
        assert monitoring.check_database(session) is False
    finally:
        session.close()


def test_a_really_unreachable_redis_makes_ready_503_and_the_worker_look_dead(monkeypatch):
    dead = redis_lib.Redis(host="127.0.0.1", port=1, socket_connect_timeout=1, decode_responses=True)
    monkeypatch.setattr(monitoring, "_redis", dead)
    assert monitoring.check_redis() is False
    assert monitoring.worker_status()["alive"] is False
    monitoring.record_worker_pass(failed=False)  # must not raise even though Redis is down
    monitoring.count_event("webhook_rejected:retell")  # nor this
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["redis"] is False and r.json()["worker"] is False
    assert monitoring.notify([monitoring.Alert("webhook_dead", "critical", "x", 1)]) == 0  # no crash, no send


# ---------------- delivery, retry and the six-hour deduplication ----------------

def test_the_dedupe_window_is_six_hours_and_a_still_firing_alert_is_announced_again_after_it(monkeypatch):
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    sent = []
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: sent.append(json) or _Resp(200))
    alert = monitoring.Alert("provisioning_blocked_payment", "critical", "needs funding", 3)
    assert monitoring.notify([alert]) == 1
    ttl = monitoring._redis.ttl("atla:alert:provisioning_blocked_payment")
    assert 6 * 3600 - 60 <= ttl <= 6 * 3600
    assert monitoring.notify([alert]) == 0  # inside the window
    monitoring._redis.delete("atla:alert:provisioning_blocked_payment")  # what Redis does when the 6 hours elapse
    assert monitoring.notify([alert]) == 1 and len(sent) == 2  # condition persists in the DB, so it is announced again


def test_an_unreachable_alert_webhook_is_retried_by_every_sweep_until_it_works(monkeypatch):
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    state = {"up": False, "attempts": 0}

    def post(u, json=None, timeout=None):
        state["attempts"] += 1
        if not state["up"]:
            raise ConnectionError("unreachable")
        return _Resp(200)

    monkeypatch.setattr(monitoring.httpx, "post", post)
    db = SessionLocal()
    try:
        monitoring._redis.delete("atla:monitoring:sweep")
        assert monitoring.sweep(db) == 0  # worker_stopped fires, delivery fails
        monitoring._redis.delete("atla:monitoring:sweep")
        assert monitoring.sweep(db) == 0 and state["attempts"] >= 2  # retried on the next sweep, not forgotten
        state["up"] = True
        monitoring._redis.delete("atla:monitoring:sweep")
        assert monitoring.sweep(db) >= 1  # delivered as soon as the webhook is back
    finally:
        db.close()


def test_the_sweep_runs_at_most_once_a_minute():
    db = SessionLocal()
    try:
        monitoring._redis.delete("atla:monitoring:sweep")
        monitoring.sweep(db)
        assert monitoring.sweep(db) == 0
    finally:
        db.close()


def test_monitoring_failures_never_break_webhook_processing(monkeypatch):
    """Counters are best-effort: a Redis outage must not turn a rejected/failed webhook response into a crash."""
    dead = redis_lib.Redis(host="127.0.0.1", port=1, socket_connect_timeout=1, decode_responses=True)
    monkeypatch.setattr(monitoring, "_redis", dead)
    monkeypatch.setattr(get_settings(), "RETELL_API_KEY", "some-key")
    assert client.post("/webhooks/retell", content=b"{}", headers={"X-Retell-Signature": "v=1,d=ab"}).status_code == 401
    assert client.post("/webhooks/paddle", content=b"{}", headers={"Paddle-Signature": "ts=1;h1=bad"}).status_code == 401


# ---------------- delivery: HTTP status codes ----------------

@pytest.mark.parametrize("code", [301, 400, 401, 403, 404, 429, 500, 502, 503])
def test_a_non_2xx_response_is_a_failed_delivery_and_does_not_suppress_the_retry(monkeypatch, caplog, code):
    url = "https://hooks.example.com/services/SECRET-PATH-TOKEN"
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", url)
    calls = []
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: calls.append(1) or _Resp(code))
    alert = monitoring.Alert("webhook_dead", "critical", "x", 1)
    with caplog.at_level(logging.ERROR):
        assert monitoring.notify([alert]) == 0
    assert f"status={code}" in caplog.text and "SECRET-PATH-TOKEN" not in caplog.text
    assert monitoring._redis.get("atla:alert:webhook_dead") is None  # not marked as announced
    assert monitoring.notify([alert]) == 0 and len(calls) == 2  # attempted again immediately, not after 6 hours
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: calls.append(1) or _Resp(200))
    assert monitoring.notify([alert]) == 1  # and delivered once the webhook recovers


@pytest.mark.parametrize("code", [200, 201, 202, 204])
def test_any_2xx_response_is_a_successful_delivery_and_is_deduplicated(monkeypatch, code):
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    calls = []
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: calls.append(1) or _Resp(code))
    alert = monitoring.Alert("webhook_dead", "critical", "x", 1)
    assert monitoring.notify([alert]) == 1 and monitoring.notify([alert]) == 0 and len(calls) == 1


def test_the_existing_five_second_timeout_is_kept_and_delivery_errors_never_escape(monkeypatch):
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    seen = {}

    def post(u, json=None, timeout=None):
        seen["timeout"] = timeout
        raise monitoring.httpx.ReadTimeout("slow")

    monkeypatch.setattr(monitoring.httpx, "post", post)
    assert monitoring.notify([monitoring.Alert("webhook_dead", "critical", "x", 1)]) == 0  # no exception
    assert seen["timeout"] == 5.0


def test_one_failing_alert_does_not_stop_the_others_in_the_same_sweep(monkeypatch):
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    codes = iter([500, 200])
    monkeypatch.setattr(monitoring.httpx, "post", lambda u, json=None, timeout=None: _Resp(next(codes)))
    a, b = monitoring.Alert("webhook_dead", "critical", "x", 1), monitoring.Alert("refund_stuck", "critical", "y", 1)
    assert monitoring.notify([a, b]) == 1
    assert monitoring._redis.get("atla:alert:webhook_dead") is None and monitoring._redis.get("atla:alert:refund_stuck")


def test_a_failing_alert_webhook_cannot_crash_the_worker_pass(monkeypatch):
    monkeypatch.setattr(get_settings(), "ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setattr(monitoring.httpx, "post", lambda *a, **k: _Resp(503))
    for name, val in (("retry_due_events", lambda: 0), ("reconcile_provisioning", lambda db, p: 0), ("enforce_entitlements", lambda db, p: {}),
                      ("settle_auto_provisioning", lambda db, p: 0), ("reconcile_topups", lambda db: 0)):
        monkeypatch.setattr(run_worker, name, val)
    monitoring._redis.delete("atla:monitoring:sweep")
    summary = run_worker.run_once(phone_provider=FakePhone())
    assert summary["failed_jobs"] == [] and summary["alerts_sent"] == 0


# ---------------- worker heartbeat: slow vs dead ----------------

def _set(**keys):
    r = monitoring._redis
    now = time.time()
    r.delete(monitoring.HEARTBEAT_KEY, monitoring.LIVENESS_KEY, monitoring.STARTED_KEY, monitoring.PHASE_KEY)
    for name, key in (("last_pass", monitoring.HEARTBEAT_KEY), ("liveness", monitoring.LIVENESS_KEY), ("started", monitoring.STARTED_KEY)):
        if name in keys:
            r.set(key, str(now - keys[name]))
    if "phase" in keys:
        age, budget = keys["phase"]
        r.set(monitoring.PHASE_KEY, f"pass|{now - age}|{budget}")


def _state():
    return monitoring.worker_status()["state"]


def test_a_fresh_start_is_not_ready_until_its_first_pass_completes_and_is_not_an_alarm_yet():
    _set(started=30, liveness=2)
    assert _state() == "starting" and monitoring.worker_status()["alive"] is False
    assert "worker_stopped" not in _keys()
    assert client.get("/health/ready").json()["worker_state"] == "starting"
    assert client.get("/health/ready").status_code == 503
    monitoring.record_worker_pass(failed=False)
    assert _state() == "ok" and client.get("/health/ready").status_code == 200


def test_a_first_pass_that_never_finishes_becomes_stalled_after_the_startup_grace():
    _set(started=monitoring.STARTUP_GRACE_SECONDS + 60, liveness=3)
    assert _state() == "stalled" and _keys()["worker_stopped"] == 1


def test_a_slow_pass_inside_its_budget_stays_ready():
    _set(last_pass=200, liveness=3, phase=(150, 300))
    st = monitoring.worker_status()
    assert st["state"] == "busy" and st["alive"] is True and "worker_stopped" not in _keys()
    assert client.get("/health/ready").status_code == 200


def test_a_pass_beyond_its_own_budget_is_a_stall_not_busy():
    _set(last_pass=2000, liveness=3, phase=(400, 300))
    assert _state() == "stalled" and "worker_stopped" in _keys()
    assert client.get("/health/ready").status_code == 503


def test_each_phase_is_judged_by_its_own_budget():
    _set(last_pass=500, liveness=3, phase=(500, 900))  # the same age is fine under a 900 s budget...
    assert _state() == "busy"
    _set(last_pass=500, liveness=3, phase=(500, 300))  # ...and a stall under a 300 s one
    assert _state() == "stalled"


def test_a_stale_pass_with_a_live_process_and_no_excuse_is_stalled():
    monitoring._redis.delete(monitoring.PHASE_KEY)
    _set(last_pass=monitoring.WORKER_STALE_SECONDS + 100, liveness=3)
    assert _state() == "stalled"


def test_a_stale_pass_and_a_stale_process_is_stopped():
    monitoring._redis.delete(monitoring.PHASE_KEY)
    _set(last_pass=monitoring.WORKER_STALE_SECONDS + 100, liveness=monitoring.LIVENESS_STALE_SECONDS + 100)
    assert _state() == "stopped" and "worker_stopped" in _keys()


def test_a_phase_alone_cannot_hide_a_dead_process():
    _set(last_pass=1000, liveness=monitoring.LIVENESS_STALE_SECONDS + 100, phase=(10, 300))
    assert _state() == "stopped"


def test_a_pass_recorded_by_a_previous_process_does_not_make_a_restarted_worker_ready():
    monitoring._redis.delete(monitoring.PHASE_KEY)
    _set(last_pass=20, started=5, liveness=1)
    assert _state() == "starting" and monitoring.worker_status()["alive"] is False


def test_the_phase_context_marks_its_budget_and_clears_even_on_failure():
    with monitoring.phase("pass", 123):
        assert monitoring._redis.get(monitoring.PHASE_KEY).startswith("pass|") and monitoring._redis.get(monitoring.PHASE_KEY).endswith("|123")
    assert monitoring._redis.get(monitoring.PHASE_KEY) is None
    with pytest.raises(RuntimeError):
        with monitoring.phase("pass", 123):
            raise RuntimeError("boom")
    assert monitoring._redis.get(monitoring.PHASE_KEY) is None


# ---------------- watchdog: a stuck main loop can neither look healthy nor stay alive ----------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _dog(clock, **kw):
    from app.workers.watchdog import Watchdog

    beats, exits, stalls = [], [], []
    d = Watchdog(beat=lambda: beats.append(clock.t), idle_grace=75, exit_fn=lambda: exits.append(1), on_stall=stalls.append, clock=clock, **kw)
    return d, beats, exits, stalls


def test_the_watchdog_beats_while_the_loop_is_inside_its_budget():
    c = _Clock()
    d, beats, exits, _ = _dog(c)
    d.enter("pass", 300)
    c.t += 299
    assert d.check() is True and beats and not exits


def test_a_stuck_pass_stops_the_beat_records_why_and_exits_the_process():
    c = _Clock()
    d, beats, exits, stalls = _dog(c)
    d.enter("pass", 300)
    c.t += 301
    assert d.check() is False
    assert beats == [] and exits == [1] and stalls == ["pass"]
    c.t += 50
    assert d.check() is False and exits == [1]  # exits once, and never beats again


def test_a_loop_that_goes_silent_between_passes_is_also_caught():
    c = _Clock()
    d, beats, exits, stalls = _dog(c)
    d.enter("pass", 300)
    d.leave()
    c.t += 74
    assert d.check() is True
    c.t += 2
    assert d.check() is False and exits and stalls == ["idle"]


def test_progress_resets_the_clock_so_a_healthy_loop_is_never_killed():
    c = _Clock()
    d, beats, exits, _ = _dog(c)
    for _ in range(10):
        d.enter("pass", 300)
        c.t += 200
        d.leave()
        c.t += 15
        assert d.check() is True
    assert not exits


def test_the_watchdog_thread_beats_then_stops_beating_after_a_stall():
    from app.workers.watchdog import Watchdog

    exits = []
    d = Watchdog(beat=monitoring.beat, idle_grace=0.2, interval=0.05, exit_fn=lambda: exits.append(1))
    monitoring._redis.delete(monitoring.LIVENESS_KEY)
    stop = d.start()
    try:
        time.sleep(0.12)
        assert monitoring._redis.get(monitoring.LIVENESS_KEY) is not None
        time.sleep(0.5)  # no enter/leave: the loop is "silent" past its grace
        assert exits == [1]
        before = monitoring._redis.get(monitoring.LIVENESS_KEY)
        time.sleep(0.2)
        assert monitoring._redis.get(monitoring.LIVENESS_KEY) == before
    finally:
        stop.set()


def test_a_watchdog_restart_is_announced_by_the_next_worker():
    monitoring.record_watchdog_exit("pass")
    assert "worker_watchdog_restart" in _keys()
    monitoring._redis.delete(monitoring.WATCHDOG_EXIT_KEY)
    assert "worker_watchdog_restart" not in _keys()


def test_the_worker_loop_wires_guard_fingerprint_watchdog_and_jobs_in_order(monkeypatch):
    calls = []

    class Stop(Exception):
        pass

    from types import SimpleNamespace

    fake = SimpleNamespace(assert_startup_safe=lambda p: calls.append(("guard", p)), WORKER_PASS_BUDGET_SECONDS=300, WORKER_WATCHDOG_EXIT=True)
    monkeypatch.setattr(run_worker, "get_settings", lambda: fake)
    monkeypatch.setattr(monitoring, "publish_config_fingerprint", lambda r: calls.append(("fingerprint", r)))
    monkeypatch.setattr(monitoring, "mark_started", lambda: calls.append(("started",)))
    monkeypatch.setattr(run_worker, "Watchdog", lambda **k: type("D", (), {"start": lambda s: calls.append(("watchdog",)), "enter": lambda s, n, b: calls.append(("enter", n, b)), "leave": lambda s: calls.append(("leave",))})())
    monkeypatch.setattr(run_worker, "start_background_jobs", lambda: calls.append(("jobs",)))
    monkeypatch.setattr(run_worker, "run_once", lambda *a, **k: calls.append(("pass",)) or {})
    monkeypatch.setattr(run_worker.time, "sleep", lambda _: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        run_worker.main()
    order = [c[0] for c in calls]
    assert order[:5] == ["guard", "fingerprint", "started", "watchdog", "jobs"]
    assert ("enter", "pass", 300) in calls and ("pass",) in calls and order.index("pass") < order.index("leave")


def test_an_unsafe_production_configuration_stops_the_worker_before_it_does_anything(monkeypatch):
    from app.config import Settings

    bad = Settings(_env_file=None, ENV="production", APP_SECRET_KEY="short", DATABASE_URL="postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(run_worker, "get_settings", lambda: bad)
    started = []
    monkeypatch.setattr(monitoring, "mark_started", lambda: started.append(1))
    with pytest.raises(RuntimeError, match=r"Unsafe production configuration \(worker\)"):
        run_worker.main()
    assert started == []


# ---------------- background jobs: scheduling, isolation, overlap ----------------

from app.workers import background_jobs  # noqa: E402


def _run(name, fn, interval=3600, retry=60, budget=300):
    return background_jobs.run_job(name, fn, interval=interval, retry=retry, budget=budget)


def test_a_job_runs_once_per_interval_and_records_success():
    calls = []
    assert _run("voice_sync", lambda: calls.append(1)) == "ok"
    assert _run("voice_sync", lambda: calls.append(1)) == "not_due" and len(calls) == 1
    assert monitoring.job_get("voice_sync", "last_success") and monitoring.job_failure_count("voice_sync") == 0


def test_a_failing_job_is_counted_retried_soon_and_never_raises():
    def boom():
        raise RuntimeError("provider down")

    assert _run("billing_reconcile", boom, retry=1) == "failed"
    assert monitoring.job_failure_count("billing_reconcile") == 1 and monitoring.job_get("billing_reconcile", "last_success") is None
    assert _run("billing_reconcile", boom, retry=1) == "not_due"  # backs off for the retry delay
    time.sleep(1.2)
    assert _run("billing_reconcile", boom, retry=1) == "failed" and monitoring.job_failure_count("billing_reconcile") == 2
    assert "job_failing_billing_reconcile" in _keys()
    time.sleep(1.2)
    assert _run("billing_reconcile", lambda: None) == "ok" and monitoring.job_failure_count("billing_reconcile") == 0


def test_two_workers_never_run_the_same_job_at_once():
    import threading

    inside, overlaps, results = [], [], []
    gate = threading.Event()

    def slow():
        inside.append(1)
        gate.wait(2)

    t = threading.Thread(target=lambda: results.append(_run("voice_sync", slow)))
    t.start()
    time.sleep(0.2)
    results.append(_run("voice_sync", lambda: overlaps.append(1)))  # second "worker" while the first is mid-run
    gate.set()
    t.join()
    assert overlaps == [] and sorted(results) == ["locked", "ok"] and len(inside) == 1


def test_a_second_worker_that_waited_for_the_lock_does_not_repeat_a_finished_run():
    calls = []
    assert _run("voice_sync", lambda: calls.append(1)) == "ok"
    assert _run("voice_sync", lambda: calls.append(1)) == "not_due" and len(calls) == 1


def test_a_hung_job_cannot_block_the_worker_pass_and_is_reported_on_its_own(monkeypatch):
    import threading

    release = threading.Event()
    t = threading.Thread(target=lambda: _run("voice_sync", lambda: release.wait(5), budget=60), daemon=True)
    t.start()
    time.sleep(0.2)
    # the time-sensitive pass still runs and records its heartbeat while the sync is stuck
    for name, val in (("retry_due_events", lambda: 0), ("reconcile_provisioning", lambda db, p: 0), ("enforce_entitlements", lambda db, p: {}),
                      ("settle_auto_provisioning", lambda db, p: 0), ("reconcile_topups", lambda db: 0)):
        monkeypatch.setattr(run_worker, name, val)
    t0 = time.time()
    run_worker.run_once(phone_provider=FakePhone())
    assert time.time() - t0 < 3 and monitoring.worker_status()["age_seconds"] < 3
    # monitoring flags the overdue job without touching worker readiness
    monitoring.job_record("voice_sync", "last_start", str(time.time() - monitoring.JOB_HARD_BUDGET_SECONDS["voice_sync"] - 5))
    assert "job_stalled_voice_sync" in _keys()
    release.set()
    t.join()


def test_the_main_loop_no_longer_runs_voice_sync_or_reconciliation_inline():
    import inspect

    src = inspect.getsource(run_worker)
    assert "sync_voice_catalog" not in src and "reconcile_subscriptions" not in src and "start_background_jobs" in src


def test_job_overdue_and_voice_sync_alerts_use_each_jobs_own_interval(monkeypatch):
    monkeypatch.setattr(get_settings(), "BILLING_RECONCILE_INTERVAL_SECONDS", 100)
    monitoring.job_record("billing_reconcile", "last_success", str(time.time() - (3 * 100 + monitoring.JOB_HARD_BUDGET_SECONDS["billing_reconcile"] + 10)))
    keys = _keys()
    assert "job_overdue_billing_reconcile" in keys and "job_overdue_voice_sync" not in keys


# ---------------- scheduled Paddle reconciliation: what it reports and what it may change ----------------

import json as _json  # noqa: E402


def _report_alerts(**over):
    base = {"checked": 5, "drift_repaired": 0, "drift_unresolved": 0, "missing_at_paddle": 0, "read_errors": 0, "duplicate_active_orgs": 0}
    monitoring.job_record("billing_reconcile", "report", _json.dumps({**base, **over}))
    return _keys()


def test_unresolved_drift_missing_subscriptions_and_duplicates_raise_a_critical_alert():
    assert "billing_drift_unresolved" not in _report_alerts()
    assert _report_alerts(drift_unresolved=1)["billing_drift_unresolved"] == 1
    assert _report_alerts(missing_at_paddle=2)["billing_drift_unresolved"] == 2
    assert _report_alerts(duplicate_active_orgs=1)["billing_drift_unresolved"] == 1


def test_a_repaired_drift_warns_that_a_webhook_may_have_been_lost():
    keys = _report_alerts(drift_repaired=2)
    assert keys["billing_drift_repaired"] == 2 and "billing_drift_unresolved" not in keys


def test_reconciliation_that_cannot_read_any_subscription_is_flagged_as_blind():
    assert "billing_reconcile_blind" in _report_alerts(checked=3, read_errors=3)
    assert "billing_reconcile_blind" not in _report_alerts(checked=3, read_errors=1)


def test_the_config_mismatch_condition_is_a_critical_alert_and_clears_when_they_agree():
    monitoring._redis.set("atla:config:api", "aaaa")
    monitoring._redis.set("atla:config:worker", "bbbb")
    assert _keys().get("config_mismatch") == 1
    monitoring._redis.set("atla:config:worker", "aaaa")
    assert "config_mismatch" not in _keys()
