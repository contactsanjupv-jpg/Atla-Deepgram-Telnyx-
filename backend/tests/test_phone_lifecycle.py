"""
Money path: provisioning (idempotent), activation (verified), and what a
lapsed subscription does to live resources. Only the provider is faked.
"""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus, PhoneProvisioning
from app.db.models.voice_agent import Agent, AgentStatus
from app.main import app
from app.services.entitlement import enforce_entitlements
from app.services.phone_provisioning import reconcile_provisioning, release_number
from tests.fakes import FakePhone
from tests.test_call_pipeline import _seed
from tests.test_tenant_isolation import _signup

settings = get_settings()


@pytest.fixture
def phone(monkeypatch):
    fake = FakePhone()
    for target in (
        "app.services.phone_provisioning.RetellPhoneProvider",
        "app.services.activation.RetellPhoneProvider",
        "app.api.v1.phone_numbers.RetellPhoneProvider",
    ):
        monkeypatch.setattr(target, lambda: fake)
    return fake


def _org(paid=True):
    tag = uuid.uuid4().hex[:10]
    c, org = _signup(f"ph-{tag}@phone-test.com", f"Phone Org {tag}")
    if paid:
        _subscribe(org, "active")
    return c, org


def _subscribe(org, status, sub_id=None, changed_at=None):
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    db.add(Subscription(organization_id=uuid.UUID(org), plan_id="starter", status=status, billing_provider="paddle",
                        external_subscription_id=sub_id or f"sub_{uuid.uuid4().hex[:10]}", last_event_at=now,
                        status_changed_at=changed_at or now))
    db.commit()
    db.close()


def _set_sub_status(org, status, changed_at):
    db = SessionLocal()
    sub = db.query(Subscription).filter(Subscription.organization_id == uuid.UUID(org)).one()
    sub.status, sub.status_changed_at = status, changed_at
    db.commit()
    db.close()


def _clone(c):
    tc = TestClient(app, raise_server_exceptions=False)
    tc.cookies.update(c.cookies)
    return tc


def _buy(c, org):
    return c.post(f"/api/v1/orgs/{org}/phone-numbers", json={"country": "US", "area_code": "415"})


def _numbers(org, status=None):
    db = SessionLocal()
    q = db.query(PhoneNumber).filter(PhoneNumber.organization_id == uuid.UUID(org))
    if status:
        q = q.filter(PhoneNumber.status == status)
    rows = q.all()
    db.close()
    return rows


def _live_setup(org, phone_provider):
    """A synced agent + purchased number, as after the purchase step. Returns (agent_row_id, phone_row_id, retell_agent_id)."""
    retell_agent_id, _ = _seed(org, with_phone=False)
    db = SessionLocal()
    agent = db.query(Agent).filter(Agent.retell_agent_id == retell_agent_id).one()
    agent.status = AgentStatus.draft
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id, retell_agent_id


# ============================ provisioning ============================

def test_eight_concurrent_purchases_buy_at_most_one_number(phone):
    c, org = _org()
    results = []

    def go():
        results.append(_buy(_clone(c), org).status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert phone.purchases == 1
    assert len(_numbers(org, PhoneNumberStatus.active)) == 1
    assert set(results) <= {201, 409} and 201 in results


def test_double_click_returns_the_same_number_without_buying_again(phone):
    c, org = _org()
    a, b = _buy(c, org), _buy(c, org)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert phone.purchases == 1


def test_timeout_after_provider_success_reconciles_instead_of_buying_again(phone):
    c, org = _org()
    phone.timeout_after_success = 1
    first = _buy(c, org)
    assert first.status_code == 409 and "confirming" in first.json()["detail"]
    assert len(phone.numbers) == 1 and _numbers(org) == []  # provider bought it; we don't know yet

    retry = _buy(c, org)
    assert retry.status_code == 201
    assert phone.purchases == 1  # reconciled via nickname — no second purchase
    assert retry.json()["number"] in phone.numbers


def test_db_failure_after_provider_success_recovers_on_retry(phone, monkeypatch):
    c, org = _org()
    from app.services import phone_provisioning

    real = phone_provisioning._adopt
    state = {"fail": True}

    def flaky(db, prov, pn):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("db write failed")
        return real(db, prov, pn)

    monkeypatch.setattr(phone_provisioning, "_adopt", flaky)
    assert _clone(c).post(f"/api/v1/orgs/{org}/phone-numbers", json={}).status_code == 502
    assert phone.purchases == 1 and len(phone.numbers) == 1 and _numbers(org) == []
    assert _buy(c, org).status_code == 201
    assert phone.purchases == 1 and len(_numbers(org, PhoneNumberStatus.active)) == 1


def test_a_definitive_rejection_is_failed_and_a_retry_may_buy(phone):
    c, org = _org()
    phone.reject_purchases = 1
    assert _buy(c, org).status_code == 502
    assert _buy(c, org).status_code == 201
    assert phone.purchases == 2 and len(phone.numbers) == 1


def test_sweeper_adopts_a_number_bought_during_an_ambiguous_timeout(phone):
    c, org = _org()
    phone.timeout_after_success = 1
    _buy(c, org)
    db = SessionLocal()
    assert reconcile_provisioning(db, phone, now=datetime.now(timezone.utc) + timedelta(minutes=3), organization_ids={uuid.UUID(org)}) == 1
    db.close()
    assert len(_numbers(org, PhoneNumberStatus.active)) == 1 and phone.purchases == 1


def test_sweeper_frees_a_stuck_purchase_the_provider_never_made(phone):
    c, org = _org()
    phone.timeout_without_purchase = 1
    assert _buy(c, org).status_code == 409
    db = SessionLocal()
    assert reconcile_provisioning(db, phone, now=datetime.now(timezone.utc) + timedelta(minutes=11), organization_ids={uuid.UUID(org)}) == 1
    prov = db.query(PhoneProvisioning).filter_by(organization_id=uuid.UUID(org)).one()
    assert prov.status == "failed"
    db.close()
    assert _buy(c, org).status_code == 201  # customer can retry without us


def test_after_a_release_the_next_number_gets_a_new_generation(phone):
    c, org = _org()
    first = _buy(c, org).json()
    db = SessionLocal()
    release_number(db, db.get(PhoneNumber, uuid.UUID(first["id"])), phone)
    db.close()
    second = _buy(c, org).json()
    assert second["id"] != first["id"] and phone.purchases == 2
    db = SessionLocal()
    prov = db.query(PhoneProvisioning).filter_by(organization_id=uuid.UUID(org)).one()
    assert prov.generation == 2 and prov.nickname.endswith("-2")
    db.close()


def test_purchase_without_a_subscription_never_reaches_the_provider(phone):
    c, org = _org(paid=False)
    assert _buy(c, org).status_code == 402 and phone.purchases == 0


# ============================ activation ============================

def _bought(phone, c, org):
    number = _buy(c, org).json()
    agent_id, retell_agent_id = _live_setup(org, phone)
    return number["id"], agent_id, retell_agent_id


def _activate(c, org, agent_id, phone_id):
    return c.post(f"/api/v1/orgs/{org}/activate", json={"agent_id": str(agent_id), "phone_number_id": phone_id})


def test_activation_verifies_with_the_provider_and_is_safe_to_repeat(phone):
    c, org = _org()
    phone_id, agent_id, retell_agent_id = _bought(phone, c, org)
    assert _activate(c, org, agent_id, phone_id).json()["status"] == "active"
    assert _activate(c, org, agent_id, phone_id).status_code == 200
    assert len(phone.assigns) == 1  # the second call saw it already routed correctly
    db = SessionLocal()
    assert db.get(Agent, agent_id).status == AgentStatus.active
    assert db.get(PhoneNumber, uuid.UUID(phone_id)).agent_id == agent_id
    db.close()


def test_activation_refuses_to_claim_live_with_an_unsynced_agent(phone):
    c, org = _org()
    phone_id, agent_id, _ = _bought(phone, c, org)
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.version = 3
    db.commit()
    db.close()
    assert _activate(c, org, agent_id, phone_id).status_code == 409
    assert phone.assigns == []
    db = SessionLocal()
    assert db.get(Agent, agent_id).status != AgentStatus.active
    db.close()


def test_activation_is_not_live_if_the_provider_does_not_confirm(phone):
    c, org = _org()
    phone_id, agent_id, _ = _bought(phone, c, org)
    phone.assign_is_noop = True
    assert _activate(c, org, agent_id, phone_id).status_code == 502
    db = SessionLocal()
    assert db.get(Agent, agent_id).status != AgentStatus.active
    assert db.get(PhoneNumber, uuid.UUID(phone_id)).agent_id is None
    db.close()


def test_activation_refuses_a_released_or_vanished_number(phone):
    c, org = _org()
    phone_id, agent_id, _ = _bought(phone, c, org)
    phone.numbers.clear()  # the provider lost it
    assert _activate(c, org, agent_id, phone_id).status_code == 409
    assert _numbers(org, PhoneNumberStatus.active) == []
    assert _activate(c, org, agent_id, phone_id).status_code == 409  # now locally released too


def test_activation_requires_a_subscription_and_the_right_tenant(phone):
    c, org = _org()
    phone_id, agent_id, _ = _bought(phone, c, org)
    other_c, other_org = _org()
    assert _activate(other_c, other_org, agent_id, phone_id).status_code == 404  # someone else's ids
    _set_sub_status(org, "canceled", datetime.now(timezone.utc))
    assert _activate(c, org, agent_id, phone_id).status_code == 402
    assert phone.assigns == []


# ============================ cancellation / past_due ============================

def _live(phone, c, org):
    phone_id, agent_id, _ = _bought(phone, c, org)
    assert _activate(c, org, agent_id, phone_id).status_code == 200
    return uuid.UUID(phone_id), agent_id


def _enforce(phone, org, now=None):
    db = SessionLocal()
    try:
        return enforce_entitlements(db, phone, now=now, organization_ids={uuid.UUID(org)})
    finally:
        db.close()


def _state(agent_id, phone_id):
    db = SessionLocal()
    out = (db.get(Agent, agent_id).status, db.get(PhoneNumber, phone_id).status, db.get(PhoneNumber, phone_id).agent_id)
    db.close()
    return out


def test_paying_customers_are_never_touched(phone):
    c, org = _org()
    phone_id, agent_id = _live(phone, c, org)
    assert _enforce(phone, org) == {"suspended": 0, "released": 0, "errors": 0}
    assert phone.unassigns == [] and _state(agent_id, phone_id)[0] == AgentStatus.active


def test_cancellation_suspends_then_releases_after_the_retention_window_idempotently(phone):
    c, org = _org()
    phone_id, agent_id = _live(phone, c, org)
    t0 = datetime.now(timezone.utc)
    _set_sub_status(org, "canceled", t0)

    assert _enforce(phone, org, now=t0 + timedelta(hours=1))["suspended"] == 1
    assert _state(agent_id, phone_id) == (AgentStatus.paused, PhoneNumberStatus.active, None)
    assert len(phone.unassigns) == 1 and phone.releases == []  # kept during retention

    assert _enforce(phone, org, now=t0 + timedelta(hours=2)) == {"suspended": 0, "released": 0, "errors": 0}
    assert len(phone.unassigns) == 1  # idempotent: nothing repeated

    late = t0 + timedelta(days=settings.NUMBER_RELEASE_DELAY_DAYS, minutes=1)
    assert _enforce(phone, org, now=late)["released"] == 1
    assert _state(agent_id, phone_id)[1] == PhoneNumberStatus.released and len(phone.releases) == 1
    assert _enforce(phone, org, now=late + timedelta(days=1))["released"] == 0 and len(phone.releases) == 1
    db = SessionLocal()
    assert db.query(PhoneProvisioning).filter_by(organization_id=uuid.UUID(org)).one().status == "released"
    db.close()


def test_a_provider_failure_is_retried_next_sweep_and_state_only_changes_on_success(phone):
    c, org = _org()
    phone_id, agent_id = _live(phone, c, org)
    t0 = datetime.now(timezone.utc)
    _set_sub_status(org, "canceled", t0)
    phone.fail_unassign = 1
    assert _enforce(phone, org, now=t0)["errors"] == 1
    assert _state(agent_id, phone_id)[0] == AgentStatus.active  # nothing claimed that did not happen
    assert _enforce(phone, org, now=t0 + timedelta(minutes=1))["suspended"] == 1
    assert _state(agent_id, phone_id)[0] == AgentStatus.paused


def test_past_due_keeps_service_inside_the_grace_window_then_suspends(phone):
    c, org = _org()
    phone_id, agent_id = _live(phone, c, org)
    t0 = datetime.now(timezone.utc)
    _set_sub_status(org, "past_due", t0)
    inside = t0 + timedelta(days=settings.PAST_DUE_GRACE_DAYS) - timedelta(minutes=1)
    assert _enforce(phone, org, now=inside)["suspended"] == 0
    assert _state(agent_id, phone_id)[0] == AgentStatus.active
    after = t0 + timedelta(days=settings.PAST_DUE_GRACE_DAYS, minutes=1)
    assert _enforce(phone, org, now=after)["suspended"] == 1
    assert _state(agent_id, phone_id)[0] == AgentStatus.paused


def test_a_paddle_cancellation_webhook_drives_the_downstream_shutdown(phone, monkeypatch):
    from tests.test_billing import SECRET, _event, _post as paddle_post

    monkeypatch.setattr(settings, "PADDLE_WEBHOOK_SECRET", SECRET)

    c, org = _org(paid=False)
    sub = f"sub_{uuid.uuid4().hex[:10]}"
    # Relative to now: fixed dates would eventually be older than NUMBER_RELEASE_DELAY_DAYS and the number would be released too.
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    created_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(fmt)
    canceled_at = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(fmt)
    assert paddle_post(_event("subscription.created", sub, org, at=created_at)).status_code == 204
    phone_id, agent_id = _live(phone, c, org)
    assert paddle_post(_event("subscription.canceled", sub, org, status="canceled", at=canceled_at)).status_code == 204
    assert _enforce(phone, org)["suspended"] == 1
    assert _state(agent_id, phone_id)[0] == AgentStatus.paused and phone.numbers[_numbers(org)[0].number]["agent"] is None


def test_resubscribing_restores_the_right_to_go_live(phone):
    c, org = _org()
    phone_id, agent_id = _live(phone, c, org)
    t0 = datetime.now(timezone.utc)
    _set_sub_status(org, "canceled", t0)
    _enforce(phone, org, now=t0)
    _subscribe(org, "active")
    assert _enforce(phone, org, now=t0 + timedelta(days=30))["released"] == 0  # paying again: keep the number
    assert _activate(c, org, agent_id, str(phone_id)).status_code == 200
    assert _state(agent_id, phone_id)[0] == AgentStatus.active
