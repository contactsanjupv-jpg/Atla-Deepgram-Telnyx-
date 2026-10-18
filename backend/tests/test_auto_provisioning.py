"""
Automatic number purchase from a confirmed (active) Paddle subscription. Only the provider is faked. HTTP 402 as
Retell's "no payment method" answer is an ASSUMPTION; nothing here claims it was observed live.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry
from app.db.models.telephony import PhoneNumber, PhoneProvisioning
from app.db.models.voice_agent import Agent
from app.providers.retell_client import RetellAPIError
from app.services.auto_provisioning import auto_provision_for_org, provisioning_state, settle_auto_provisioning
from app.services.credits import credit_summary
from app.workers import run_worker
from tests.fakes import FakePhone
from tests.test_billing import SECRET, _event, _new_org, _post
from tests.test_phone_lifecycle import _subscribe


class PayFake(FakePhone):
    def __init__(self):
        super().__init__()
        self.payment_required = 0  # next N purchases answer HTTP 402

    def purchase_number(self, country, area_code, nickname):
        if self.payment_required > 0:
            self.purchases += 1
            self.payment_required -= 1
            raise RetellAPIError(402, "payment required", {"error": "no payment method"})
        return super().purchase_number(country, area_code, nickname)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AUTO_PROVISION_NUMBERS", True)
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_API_KEY", "test_key")
    monkeypatch.setattr(s, "PADDLE_ENV", "sandbox")
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


@pytest.fixture
def fake(monkeypatch):
    f = PayFake()
    for target in (
        "app.services.auto_provisioning.RetellPhoneProvider",
        "app.services.phone_provisioning.RetellPhoneProvider",
        "app.services.activation.RetellPhoneProvider",
        "app.api.v1.phone_numbers.RetellPhoneProvider",
        "app.workers.run_worker.RetellPhoneProvider",
    ):
        monkeypatch.setattr(target, lambda: f)
    return f


def _org():
    _c, org = _new_org()
    return org


def _webhook(org, sub_id, status="active", event_type="subscription.activated", **kw):
    r = _post(_event(event_type, sub_id, org, status=status, **kw))
    assert r.status_code in (200, 204), r.text
    return r


def _numbers(org):
    db = SessionLocal()
    try:
        return db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org)).count()
    finally:
        db.close()


def _prov(org):
    db = SessionLocal()
    try:
        row = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == uuid.UUID(org)).first()
        return (row.status, row.attempts, row.last_error) if row else None
    finally:
        db.close()


def _state(org, now=None):
    db = SessionLocal()
    try:
        return provisioning_state(db, uuid.UUID(org), now)
    finally:
        db.close()


def _auto(org, fake, now=None):
    db = SessionLocal()
    try:
        return auto_provision_for_org(db, uuid.UUID(org), fake, now)
    finally:
        db.close()


def _later(minutes=31):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _live_agents(org):
    db = SessionLocal()
    try:
        return db.query(Agent).filter(Agent.organization_id == uuid.UUID(org), Agent.status == "active").count()
    finally:
        db.close()


# ---------- success ----------

def test_a_confirmed_active_subscription_buys_one_number_and_is_not_live(fake):
    org, sid = _org(), f"sub_{uuid.uuid4().hex[:10]}"
    _webhook(org, sid)
    assert fake.purchases == 1 and _numbers(org) == 1 and _prov(org)[0] == "purchased"
    st = _state(org)
    assert st["state"] == "ready" and st["number"]
    assert fake.assigns == [] and _live_agents(org) == 0  # nothing assigned, nothing live: activation is a separate verified step


def test_credits_are_untouched_by_provisioning(fake):
    org = _org()
    fake.payment_required = 1
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    db = SessionLocal()
    uid = uuid.UUID(org)
    before = credit_summary(db, uid).available_seconds
    rows = db.query(CreditLedgerEntry).filter(CreditLedgerEntry.organization_id == uid).count()
    db.close()
    assert _auto(org, fake, _later()) == "purchased"
    db = SessionLocal()
    assert credit_summary(db, uid).available_seconds == before
    assert db.query(CreditLedgerEntry).filter(CreditLedgerEntry.organization_id == uid).count() == rows
    db.close()


# ---------- replays / duplicates ----------

def test_replayed_and_duplicate_webhooks_never_buy_a_second_number(fake):
    org, sid = _org(), f"sub_{uuid.uuid4().hex[:10]}"
    ev = _event("subscription.activated", sid, org)
    for _ in range(3):
        assert _post(ev).status_code in (200, 204)  # the identical event, replayed
    _webhook(org, sid, event_type="subscription.updated")  # a different event for the same subscription
    _webhook(org, sid, event_type="subscription.updated")
    assert fake.purchases == 1 and _numbers(org) == 1


# ---------- trigger conditions ----------

@pytest.mark.parametrize("status", ["trialing", "past_due", "paused", "canceled"])
def test_only_an_active_subscription_triggers_a_purchase(fake, status):
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}", status=status, event_type="subscription.updated")
    assert fake.purchases == 0 and _prov(org) is None and _state(org)["state"] == "none"


def test_a_webhook_with_a_bad_signature_buys_nothing(fake):
    org = _org()
    r = _post(_event("subscription.activated", "sub_x", org), signature="ts=1;h1=bad")
    assert r.status_code in (400, 401) and fake.purchases == 0


def test_a_webhook_for_an_unknown_organization_buys_nothing(fake):
    r = _post(_event("subscription.activated", f"sub_{uuid.uuid4().hex[:8]}", str(uuid.uuid4())))
    assert fake.purchases == 0 and r.status_code in (200, 204, 400, 404, 422)


def test_the_switch_turns_it_off(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_NUMBERS", False)
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert fake.purchases == 0


# ---------- provider rejects for payment ----------

def test_payment_required_keeps_the_subscription_shows_blocked_and_claims_nothing(fake):
    fake.payment_required = 99
    org, sid = _org(), f"sub_{uuid.uuid4().hex[:10]}"
    _webhook(org, sid)
    assert _prov(org)[0] == "blocked_payment" and _numbers(org) == 0
    st = _state(org)
    assert st["state"] == "blocked" and st["number"] is None and st["retry_at"] is not None
    text = st["message"].lower()
    assert "not live" in text and "refund" not in text
    assert _live_agents(org) == 0
    db = SessionLocal()
    from app.services.billing_state import current_subscription
    assert current_subscription(db, uuid.UUID(org)).status == "active"  # the paid state is preserved
    db.close()


def test_duplicate_webhooks_during_the_cooldown_make_no_provider_call(fake):
    fake.payment_required = 99
    org, sid = _org(), f"sub_{uuid.uuid4().hex[:10]}"
    _webhook(org, sid)
    assert fake.purchases == 1
    for _ in range(4):
        _webhook(org, sid, event_type="subscription.updated")
    assert fake.purchases == 1 and _auto(org, fake) == "skipped"


def test_a_definitive_non_payment_rejection_is_not_retried_automatically(fake):
    fake.reject_purchases = 5  # HTTP 400
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert _prov(org)[0] == "failed" and fake.purchases == 1
    assert _auto(org, fake, _later(24 * 60)) == "skipped" and fake.purchases == 1
    assert _state(org)["state"] == "blocked"


# ---------- retry / recovery ----------

def test_recovery_after_the_provider_account_is_funded_via_a_later_webhook(fake):
    fake.payment_required = 1
    org, sid = _org(), f"sub_{uuid.uuid4().hex[:10]}"
    _webhook(org, sid)
    assert _numbers(org) == 0
    assert _auto(org, fake, _later()) == "purchased"  # the provider now accepts
    assert _numbers(org) == 1 and _prov(org)[0] == "purchased" and fake.purchases == 2
    assert _auto(org, fake, _later(120)) == "already_provisioned" and fake.purchases == 2


def test_the_worker_sweep_recovers_a_blocked_org_once_and_respects_the_cooldown(fake):
    fake.payment_required = 1
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    ids = {uuid.UUID(org)}
    db = SessionLocal()
    try:
        assert settle_auto_provisioning(db, fake, organization_ids=ids) == 0 and fake.purchases == 1  # still cooling down
        assert settle_auto_provisioning(db, fake, now=_later(), organization_ids=ids) == 1
        assert settle_auto_provisioning(db, fake, now=_later(90), organization_ids=ids) == 0
    finally:
        db.close()
    assert fake.purchases == 2 and _numbers(org) == 1


def test_run_once_includes_the_provisioning_sweep(fake):
    summary = run_worker.run_once(phone_provider=fake)
    assert "auto_provisioning" in summary


def test_the_attempt_cap_stops_endless_retries(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_MAX_ATTEMPTS", 2)
    fake.payment_required = 99
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert _auto(org, fake, _later()) == "blocked_payment"  # attempt 2
    assert _auto(org, fake, _later(120)) == "skipped" and fake.purchases == 2
    assert _state(org)["retry_at"] is None


def test_a_connection_failure_that_never_reached_the_provider_is_retried(fake, monkeypatch):
    org = _org()
    calls = {"n": 0}
    real = fake.purchase_number

    def flaky(country, area_code, nickname):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetellAPIError(0, "Could not connect to Retell", ambiguous=False)
        return real(country, area_code, nickname)

    monkeypatch.setattr(fake, "purchase_number", flaky)
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert _prov(org) == ("failed", 1, "status=0") and _numbers(org) == 0
    assert _auto(org, fake, _later()) == "purchased" and _numbers(org) == 1


def test_an_ambiguous_purchase_is_left_to_reconciliation_not_re_bought(fake):
    fake.timeout_after_success = 1  # the provider bought it, the reply was lost
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert _prov(org)[0] == "ambiguous" and fake.purchases == 1
    assert _auto(org, fake, _later(60)) == "skipped" and fake.purchases == 1
    from app.services.phone_provisioning import reconcile_provisioning
    db = SessionLocal()
    try:
        assert reconcile_provisioning(db, fake, now=_later(5), organization_ids={uuid.UUID(org)}) == 1
    finally:
        db.close()
    assert _numbers(org) == 1 and fake.purchases == 1


# ---------- status route ----------

def test_the_status_route_is_scoped_and_honest(fake):
    from tests.test_phone_lifecycle import _org as signup_org
    c, org = signup_org(paid=False)
    r = c.get(f"/api/v1/orgs/{org}/phone-numbers/provisioning")
    assert r.status_code == 200 and r.json()["state"] == "none"
    _subscribe(org, "active")
    fake.payment_required = 1
    assert _auto(org, fake) == "blocked_payment"
    j = c.get(f"/api/v1/orgs/{org}/phone-numbers/provisioning").json()
    assert j["state"] == "blocked" and "refund" not in j["message"].lower()


# ---------- a number released for a LAPSED subscription is re-bought when the subscription is active again ----------

def _release(org, fake, reason):
    from app.services.phone_provisioning import active_number, release_number
    db = SessionLocal()
    try:
        release_number(db, active_number(db, uuid.UUID(org)), fake, reason=reason)
    finally:
        db.close()


def _bought(fake):
    org = _org()
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert fake.purchases == 1 and _prov(org)[0] == "purchased"
    return org


def _generation(org):
    db = SessionLocal()
    try:
        row = db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == uuid.UUID(org)).one()
        return row.generation, row.nickname
    finally:
        db.close()


def test_a_number_released_by_the_lapse_sweep_is_replaced_under_a_new_generation_when_active_again(fake):
    org = _bought(fake)
    _release(org, fake, "entitlement")
    assert _prov(org)[0] == "released" and _state(org)["state"] == "provisioning"
    assert _auto(org, fake) == "purchased"
    assert fake.purchases == 2 and _generation(org)[0] == 2 and _generation(org)[1].endswith("-2")
    assert _prov(org)[0] == "purchased" and _state(org)["state"] == "ready"
    assert _live_agents(org) == 0  # still not live: activation stays a separate verified step


def test_a_second_pass_never_buys_again_once_the_replacement_exists(fake):
    org = _bought(fake)
    _release(org, fake, "entitlement")
    assert _auto(org, fake) == "purchased"
    assert _auto(org, fake) == "already_provisioned" and _auto(org, fake, _later(600)) == "already_provisioned"
    db = SessionLocal()
    try:
        assert settle_auto_provisioning(db, fake, organization_ids={uuid.UUID(org)}) == 0
    finally:
        db.close()
    assert fake.purchases == 2


def test_a_release_the_customer_asked_for_never_buys_a_replacement(fake):
    org = _bought(fake)
    _release(org, fake, "customer")
    assert _auto(org, fake, _later(600)) == "skipped" and fake.purchases == 1


def test_a_release_from_account_deletion_never_buys_a_replacement(fake):
    org = _bought(fake)
    _release(org, fake, "account_deleted")
    assert _auto(org, fake, _later(600)) == "skipped" and fake.purchases == 1


def test_a_released_number_is_not_replaced_while_the_subscription_is_not_active(fake):
    from tests.test_phone_lifecycle import _set_sub_status
    org = _bought(fake)
    _release(org, fake, "entitlement")
    _set_sub_status(org, "past_due", datetime.now(timezone.utc))
    assert _auto(org, fake) == "not_eligible" and fake.purchases == 1


def test_the_worker_sweep_replaces_the_released_number_exactly_once(fake):
    org = _bought(fake)
    _release(org, fake, "entitlement")
    db = SessionLocal()
    try:
        assert settle_auto_provisioning(db, fake, organization_ids={uuid.UUID(org)}) == 1
        assert settle_auto_provisioning(db, fake, organization_ids={uuid.UUID(org)}) == 0
    finally:
        db.close()
    assert fake.purchases == 2 and _numbers(org) == 2  # the released row is history; one active number


def test_an_ambiguous_replacement_purchase_is_adopted_not_re_bought(fake):
    org = _bought(fake)
    _release(org, fake, "entitlement")
    fake.timeout_after_success = 1  # the provider buys the replacement but the reply is lost
    assert _auto(org, fake) == "pending" and fake.purchases == 2 and _prov(org)[0] == "ambiguous"
    assert _auto(org, fake, _later(600)) == "skipped" and fake.purchases == 2  # still never re-bought
    from app.services.phone_provisioning import reconcile_provisioning
    db = SessionLocal()
    try:
        assert reconcile_provisioning(db, fake, now=_later(5), organization_ids={uuid.UUID(org)}) == 1
    finally:
        db.close()
    assert fake.purchases == 2 and _prov(org)[0] == "purchased"


def test_the_full_cycle_lapse_release_then_resubscribe_ends_with_one_new_number(fake):
    from app.services.entitlement import enforce_entitlements
    from tests.test_phone_lifecycle import _set_sub_status
    org = _bought(fake)
    _set_sub_status(org, "canceled", datetime.now(timezone.utc) - timedelta(days=30))
    db = SessionLocal()
    try:
        assert enforce_entitlements(db, fake, organization_ids={uuid.UUID(org)})["released"] == 1
    finally:
        db.close()
    assert _prov(org) == ("released", 1, "released_by=entitlement")
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")  # the customer subscribes again
    assert fake.purchases == 2 and _prov(org)[0] == "purchased" and _generation(org)[0] == 2
