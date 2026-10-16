"""Account essentials: password reset/change, account deletion, billing management."""
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.tenancy import Organization, User
from app.main import app
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.providers.billing.paddle_billing_provider import PaddleAPIError
from tests.fakes import FakePhone
from tests.test_agent_config import FakeRetell
from tests.test_billing import SECRET, _event, _post as paddle_post
from tests.test_call_pipeline import KEY, _call, _post as retell_post, _seed
from tests.test_phone_lifecycle import _subscribe
from tests.test_tenant_isolation import _signup

settings = get_settings()
PW = "correct-horse-battery-staple"


def _acct():
    tag = uuid.uuid4().hex[:10]
    email, name = f"acct-{tag}@acct-test.com", f"Acct Org {tag}"
    c, org = _signup(email, name)
    return c, org, email, name


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.account.send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def _token(mail_item) -> str:
    return re.search(r"token=([\w-]+)", mail_item[2]).group(1)


def _login(email, password):
    c = TestClient(app)
    return c, c.post("/api/v1/auth/login", json={"email": email, "password": password})


anon = TestClient(app)


# ============================ password reset ============================

def test_reset_email_is_sent_only_for_real_accounts_with_identical_responses(mail):
    c, org, email, _ = _acct()
    known = anon.post("/api/v1/auth/password-reset/request", json={"email": email})
    unknown = anon.post("/api/v1/auth/password-reset/request", json={"email": f"nobody-{uuid.uuid4().hex[:8]}@acct-test.com"})
    assert known.status_code == unknown.status_code == 204 and known.text == unknown.text
    assert [m[0] for m in mail] == [email] and "/reset-password?token=" in mail[0][2]


def test_reset_token_sets_a_new_password_once_and_signs_out_old_sessions(mail):
    c, org, email, _ = _acct()
    anon.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = _token(mail[0])
    assert c.get("/api/v1/auth/me").status_code == 200

    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "a-brand-new-passphrase"}).status_code == 204
    assert c.get("/api/v1/auth/me").status_code == 401  # the old session is dead
    assert _login(email, PW)[1].status_code == 401
    assert _login(email, "a-brand-new-passphrase")[1].status_code == 200
    # single use
    again = anon.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "yet-another-passphrase"})
    assert again.status_code == 400


def test_invalid_expired_or_weak_reset_attempts_are_rejected(mail):
    c, org, email, _ = _acct()
    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": "x" * 40, "new_password": "a-brand-new-passphrase"}).status_code == 400
    anon.post("/api/v1/auth/password-reset/request", json={"email": email})
    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": _token(mail[0]), "new_password": "short"}).status_code == 422
    assert _login(email, PW)[1].status_code == 200  # nothing changed


def test_a_new_reset_request_invalidates_the_previous_link(mail):
    c, org, email, _ = _acct()
    anon.post("/api/v1/auth/password-reset/request", json={"email": email})
    anon.post("/api/v1/auth/password-reset/request", json={"email": email})
    old, new = _token(mail[0]), _token(mail[1])
    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": old, "new_password": "a-brand-new-passphrase"}).status_code == 400
    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": new, "new_password": "a-brand-new-passphrase"}).status_code == 204


def test_reset_requests_are_rate_limited_without_revealing_it(mail):
    c, org, email, _ = _acct()
    codes = [anon.post("/api/v1/auth/password-reset/request", json={"email": email}).status_code for _ in range(5)]
    assert set(codes) == {204} and len(mail) == 3


def test_reset_tokens_are_stored_hashed_not_raw(mail):
    import redis

    c, org, email, _ = _acct()
    anon.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = _token(mail[0])
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    assert r.get(f"pwreset:{token}") is None and not any(token in k for k in r.scan_iter("pwreset*"))


# ============================ password change ============================

def test_password_change_signs_out_other_devices_but_keeps_this_one():
    c, org, email, _ = _acct()
    other, resp = _login(email, PW)
    assert resp.status_code == 200
    assert c.post("/api/v1/auth/password/change", json={"current_password": PW, "new_password": "a-brand-new-passphrase"}).status_code == 204
    assert c.get("/api/v1/auth/me").status_code == 200  # this device continues
    assert other.get("/api/v1/auth/me").status_code == 401  # the other device is signed out
    assert _login(email, "a-brand-new-passphrase")[1].status_code == 200 and _login(email, PW)[1].status_code == 401


def test_password_change_needs_the_current_password():
    c, org, email, _ = _acct()
    assert c.post("/api/v1/auth/password/change", json={"current_password": "wrong-password-here", "new_password": "a-brand-new-passphrase"}).status_code == 400
    assert _login(email, PW)[1].status_code == 200
    assert anon.post("/api/v1/auth/password/change", json={"current_password": PW, "new_password": "a-brand-new-passphrase"}).status_code == 401


# ============================ account deletion ============================

class FakeBilling:
    def __init__(self):
        self.cancels, self.fail = [], None
        self.urls = {"update_payment_method": "https://pay.example/update"}

    def cancel_subscription(self, ext, immediately=False):
        if self.fail is not None:
            raise self.fail
        self.cancels.append((ext, immediately))

    def get_management_urls(self, ext):
        if self.fail is not None:
            raise self.fail
        return self.urls


@pytest.fixture
def providers(monkeypatch):
    billing, phones, retell = FakeBilling(), FakePhone(), FakeRetell()
    monkeypatch.setattr("app.services.account_deletion.PaddleBillingProvider", lambda: billing)
    monkeypatch.setattr("app.services.account_deletion.RetellPhoneProvider", lambda: phones)
    monkeypatch.setattr("app.services.account_deletion.RetellAgentProvider", lambda: RetellAgentProvider(client=retell))
    monkeypatch.setattr("app.api.v1.billing.PaddleBillingProvider", lambda: billing)
    monkeypatch.setattr(settings, "RETELL_API_KEY", KEY)
    return SimpleProviders(billing, phones, retell)


class SimpleProviders:
    def __init__(self, billing, phones, retell):
        self.billing, self.phones, self.retell = billing, phones, retell


def _delete(c, org, name, password=PW):
    return c.post(f"/api/v1/orgs/{org}/delete", json={"confirm_name": name, "password": password})


def _rows(org, table):
    db = SessionLocal()
    try:
        return db.execute(text(f"SELECT count(*) FROM {table} WHERE organization_id = :o"), {"o": org}).scalar()
    finally:
        db.close()


def _fully_populated_org():
    c, org, email, name = _acct()
    _subscribe(org, "active", sub_id=f"sub_{uuid.uuid4().hex[:10]}")
    agent, number = _seed(org)
    cid = f"call_{uuid.uuid4().hex[:10]}"
    retell_post("call_analyzed", _call(cid, agent, number, call_analysis={"call_summary": "s", "custom_analysis_data": {"captured_lead": True, "caller_name": "M"}}))
    return c, org, email, name, agent, cid


def test_delete_requires_password_and_the_typed_name_and_deletes_nothing_otherwise(providers):
    c, org, email, name, agent, cid = _fully_populated_org()
    assert _delete(c, org, name, password="not-the-password!!").status_code == 400
    assert _delete(c, org, "wrong name").status_code == 400
    assert _rows(org, "calls") == 1 and providers.billing.cancels == [] and providers.phones.releases == []


def test_deleting_an_account_cancels_billing_releases_numbers_and_purges_everything(providers):
    c, org, email, name, agent, cid = _fully_populated_org()
    assert _rows(org, "calls") == 1 and _rows(org, "leads") == 1 and _rows(org, "usage_ledger") == 1

    assert _delete(c, org, name).status_code == 204

    assert len(providers.billing.cancels) == 1 and providers.billing.cancels[0][1] is True
    assert len(providers.phones.releases) == 1
    deleted_paths = [p for _, p, _ in providers.retell.calls]
    assert any(p.startswith("/delete-agent/") for p in deleted_paths) and any(p.startswith("/delete-retell-llm/") for p in deleted_paths)

    for table in ("businesses", "agents", "calls", "leads", "usage_ledger", "subscriptions", "phone_numbers", "organization_members"):
        assert _rows(org, table) == 0, table
    db = SessionLocal()
    assert db.get(Organization, uuid.UUID(org)) is None
    assert db.query(User).filter(User.email == email).first() is None
    assert db.execute(text("SELECT count(*) FROM webhook_events WHERE external_event_id LIKE :p"), {"p": f"{cid}:%"}).scalar() == 0
    db.close()
    assert c.get("/api/v1/auth/me").status_code == 401  # signed out everywhere


def test_a_provider_failure_deletes_nothing_and_a_retry_completes(providers):
    c, org, email, name, agent, cid = _fully_populated_org()
    providers.billing.fail = PaddleAPIError("boom", 500)
    resp = _delete(c, org, name)
    assert resp.status_code == 502 and "nothing was deleted" in resp.json()["detail"]
    assert "paddle" not in resp.text.lower() and _rows(org, "calls") == 1 and _rows(org, "subscriptions") == 1

    providers.billing.fail = None
    assert _delete(c, org, name).status_code == 204
    assert _rows(org, "subscriptions") == 0


def test_a_paddle_4xx_on_cancel_is_never_assumed_to_mean_already_cancelled(providers):
    """Replaces the old test that asserted the unsafe assumption. Paddle's own record must say "canceled"
    (see tests/test_account_deletion_billing_safety.py); here the provider cannot confirm it, so nothing is deleted."""
    c, org, email, name, agent, cid = _fully_populated_org()
    providers.billing.fail = PaddleAPIError("Paddle returned 401", 401)
    resp = _delete(c, org, name)
    assert resp.status_code == 502 and "nothing was deleted" in resp.json()["detail"]
    assert _rows(org, "calls") == 1 and _rows(org, "subscriptions") == 1 and providers.phones.releases == []


def test_other_accounts_cannot_delete_mine(providers):
    c1, org1, email1, name1 = _acct()
    c2, org2, email2, name2 = _acct()
    assert _delete(c2, org1, name1).status_code == 404
    assert _delete(anon, org1, name1).status_code == 401
    db = SessionLocal()
    assert db.get(Organization, uuid.UUID(org1)) is not None
    db.close()


# ============================ billing management ============================

def _paddle_secret(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_WEBHOOK_SECRET", SECRET)


def test_cancel_requests_end_of_period_cancellation_and_state_follows_the_webhook(providers, monkeypatch):
    _paddle_secret(monkeypatch)
    c, org, *_ = _acct()
    sub = f"sub_{uuid.uuid4().hex[:10]}"
    paddle_post(_event("subscription.created", sub, org, at="2026-10-01T00:00:00.000000Z"))

    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").status_code == 202
    assert providers.billing.cancels == [(sub, False)]  # at period end, not immediately
    assert c.get(f"/api/v1/orgs/{org}/billing/subscription").json()["status"] == "active"  # not changed locally

    evt = _event("subscription.updated", sub, org, at="2026-10-02T00:00:00.000000Z")
    evt["data"]["scheduled_change"] = {"action": "cancel", "effective_at": "2026-11-01T00:00:00.000000Z"}
    paddle_post(evt)
    body = c.get(f"/api/v1/orgs/{org}/billing/subscription").json()
    assert body["status"] == "active" and body["cancel_effective_at"].startswith("2026-11-01")
    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").json() == {"status": "already_scheduled"}
    assert len(providers.billing.cancels) == 1

    undo = _event("subscription.updated", sub, org, at="2026-10-03T00:00:00.000000Z")  # customer resumed
    paddle_post(undo)
    assert c.get(f"/api/v1/orgs/{org}/billing/subscription").json()["cancel_effective_at"] is None


def test_cancel_and_manage_need_a_subscription_and_the_owner(providers):
    c, org, *_ = _acct()
    assert c.post(f"/api/v1/orgs/{org}/billing/cancel").status_code == 409
    assert c.get(f"/api/v1/orgs/{org}/billing/manage").status_code == 409
    other, other_org, *_ = _acct()
    assert other.post(f"/api/v1/orgs/{org}/billing/cancel").status_code == 404


def test_manage_returns_the_payment_update_link_and_hides_provider_errors(providers, monkeypatch):
    _paddle_secret(monkeypatch)
    c, org, *_ = _acct()
    paddle_post(_event("subscription.created", f"sub_{uuid.uuid4().hex[:10]}", org))
    assert c.get(f"/api/v1/orgs/{org}/billing/manage").json() == {"update_payment_method_url": "https://pay.example/update"}
    providers.billing.fail = PaddleAPIError("Paddle returned 500 secret", 500)
    resp = c.get(f"/api/v1/orgs/{org}/billing/manage")
    assert resp.status_code == 502 and "paddle" not in resp.text.lower() and "secret" not in resp.text.lower()
