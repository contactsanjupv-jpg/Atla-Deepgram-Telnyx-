"""
F2 regression: a provider 5xx during the number purchase must not leave a paying customer permanently without a number.

Only the provider is faked. Time is moved by passing `now` or by back-dating `updated_at` in the TEST database.
Guarantees checked: recovery ends `ready` with exactly ONE number; the 30-minute cool-down and the attempt cap still
apply; the provider nickname is looked up before every retry, so a number that exists is adopted, never bought twice;
if that look-up itself fails nothing is bought.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.base import SessionLocal
from app.providers.retell_client import RetellAPIError
from app.services.phone_provisioning import NOT_FOUND_AFTER_RECONCILE, reconcile_provisioning
from app.workers import run_worker
from tests.fakes import FakePhone
from tests.test_auto_provisioning import (  # reuse the existing helpers/fixtures unchanged
    _auto, _later, _numbers, _on, _org, _prov, _state, _webhook,
)

__all__ = ["_on"]  # autouse fixture re-exported so it applies here too


class Flaky5xx(FakePhone):
    """purchase_number answers HTTP 500 WITHOUT buying anything, for the next N calls."""

    def __init__(self):
        super().__init__()
        self.fail_5xx = 0
        self.lookup_errors = 0
        self.nick_calls: list[str] = []  # nickname of every purchase call, so tests can count ONE org's purchases
        self.only: str | None = None     # when set, the 5xx fault applies to this nickname only (the worker sweeps ALL orgs)

    def purchase_number(self, country, area_code, nickname):
        self.nick_calls.append(nickname)
        if self.fail_5xx > 0 and (self.only is None or nickname == self.only):
            self.purchases += 1
            self.fail_5xx -= 1
            raise RetellAPIError(500, "simulated provider failure")
        return super().purchase_number(country, area_code, nickname)

    def find_number_by_nickname(self, nickname):
        if self.lookup_errors > 0:
            self.lookup_errors -= 1
            raise RetellAPIError(503, "list-phone-numbers unavailable")
        return super().find_number_by_nickname(nickname)


@pytest.fixture
def fake(monkeypatch):
    f = Flaky5xx()
    for target in (
        "app.services.auto_provisioning.RetellPhoneProvider",
        "app.services.phone_provisioning.RetellPhoneProvider",
        "app.services.activation.RetellPhoneProvider",
        "app.api.v1.phone_numbers.RetellPhoneProvider",
        "app.workers.run_worker.RetellPhoneProvider",
    ):
        monkeypatch.setattr(target, lambda: f)
    return f


def _reconcile(org, fake, minutes=11):
    db = SessionLocal()
    try:
        return reconcile_provisioning(db, fake, now=datetime.now(timezone.utc) + timedelta(minutes=minutes), organization_ids={uuid.UUID(org)})
    finally:
        db.close()


def _age(org, minutes):
    """Back-date the provisioning row in the TEST database so the worker's real clock sees it as `minutes` old."""
    db = SessionLocal()
    try:
        db.execute(text("update phone_provisionings set updated_at = now() - make_interval(mins => :m) where organization_id = :o"), {"m": minutes, "o": org})
        db.commit()
    finally:
        db.close()


def _failed_after_5xx(fake, org):
    """The C5d situation: the provider answered 5xx, nothing was bought, and reconciliation proved it."""
    fake.fail_5xx = 1
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert _prov(org)[0] == "ambiguous" and fake.purchases == 1 and _numbers(org) == 0
    assert _reconcile(org, fake) == 1
    assert _prov(org) == ("failed", 1, NOT_FOUND_AFTER_RECONCILE)


def test_a_provider_5xx_that_bought_nothing_self_heals_to_ready_with_exactly_one_number(fake):
    org = _org()
    _failed_after_5xx(fake, org)
    st = _state(org)
    assert st["state"] == "blocked" and st["retry_at"] is not None  # a retry is scheduled, not "contact support forever"
    assert _auto(org, fake, _later(45)) == "purchased"
    assert _numbers(org) == 1 and fake.purchases == 2 and len(fake.numbers) == 1
    assert _prov(org)[0] == "purchased" and _state(org)["state"] == "ready"


def test_the_cool_down_still_applies_after_a_reconciled_failure(fake):
    org = _org()
    _failed_after_5xx(fake, org)
    assert _auto(org, fake, _later(5)) == "skipped" and fake.purchases == 1
    assert _auto(org, fake, _later(29)) == "skipped" and fake.purchases == 1
    assert _auto(org, fake, _later(31)) == "purchased" and fake.purchases == 2


def test_the_maximum_attempt_limit_still_stops_retries(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_MAX_ATTEMPTS", 2)
    org = _org()
    _failed_after_5xx(fake, org)               # attempt 1
    fake.fail_5xx = 1
    assert _auto(org, fake, _later(45)) == "pending" and _prov(org)[1] == 2   # attempt 2 -> 5xx again
    assert _reconcile(org, fake) == 1 and _prov(org)[0] == "failed"
    assert _auto(org, fake, _later(90)) == "skipped" and fake.purchases == 2 and _numbers(org) == 0
    assert _state(org)["retry_at"] is None


def test_a_number_that_appeared_late_at_the_provider_is_adopted_not_bought_again(fake):
    org = _org()
    _failed_after_5xx(fake, org)
    nickname = _nick(org)
    fake.numbers["+15550001111"] = {"nickname": nickname, "agent": None}  # the "failed" purchase did land after all
    assert _auto(org, fake, _later(45)) == "purchased"
    assert fake.purchases == 1 and _numbers(org) == 1 and _state(org)["state"] == "ready"


def test_if_the_nickname_lookup_fails_nothing_is_bought_and_state_is_preserved(fake):
    org = _org()
    _failed_after_5xx(fake, org)
    fake.lookup_errors = 1
    assert _auto(org, fake, _later(45)) == "pending" and fake.purchases == 1 and _numbers(org) == 0
    assert _prov(org) == ("failed", 1, NOT_FOUND_AFTER_RECONCILE)      # untouched: not consumed an attempt
    assert _auto(org, fake, _later(45)) == "purchased" and fake.purchases == 2 and _numbers(org) == 1


def test_a_connection_failure_retry_also_looks_the_nickname_up_first(fake):
    org = _org()
    real = fake.purchase_number
    state = {"n": 0}

    def drop(country, area_code, nickname):
        state["n"] += 1
        if state["n"] == 1:
            raise RetellAPIError(0, "Could not connect to Retell", ambiguous=False)
        return real(country, area_code, nickname)

    fake.purchase_number = drop
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    assert _prov(org) == ("failed", 1, "status=0")
    fake.numbers["+15550002222"] = {"nickname": _nick(org), "agent": None}  # present after all
    assert _auto(org, fake, _later()) == "purchased"
    assert state["n"] == 1 and _numbers(org) == 1  # adopted; the second purchase call was never made


def test_repeated_sweeps_and_replayed_webhooks_during_recovery_never_create_a_second_number(fake):
    org = _org()
    _failed_after_5xx(fake, org)
    sid = f"sub_{uuid.uuid4().hex[:10]}"
    for _ in range(3):
        _webhook(org, sid)                      # replayed activations (cool-down: no provider call)
        _auto(org, fake, _later(5))
    assert fake.purchases == 1
    assert _auto(org, fake, _later(45)) == "purchased"
    for _ in range(5):
        _auto(org, fake, _later(200))
        _webhook(org, sid)
    assert fake.purchases == 2 and _numbers(org) == 1 and len(fake.numbers) == 1


def test_the_real_worker_loop_recovers_and_survives_restarts(fake, monkeypatch):
    """run_worker.run_once is the worker's own pass; each call builds fresh sessions, as a restarted process would.
    It sweeps EVERY org in the shared test database, so all counts below are for this org's nickname only.

    The sweep handles at most SWEEP_LIMIT orgs per pass, in no defined order. The shared test database holds every
    paid org without a number that the other tests left behind (hundreds by this point in a full run), so with the
    production limit this org's retry could fall outside the window of the pass under test and the test passed or
    failed by chance. Lifting the limit makes every pass cover every org, so the outcome no longer depends on how
    many other tests ran. (That the limit still drains a backlog, 25 orgs per pass, is a production property.)"""
    from app.services import auto_provisioning

    monkeypatch.setattr(auto_provisioning, "SWEEP_LIMIT", 100_000)
    org = _org()
    fake.fail_5xx = 1
    fake.only = f"atla-{org}-1"  # deterministic nickname of this org's first number
    _webhook(org, f"sub_{uuid.uuid4().hex[:10]}")
    nick = _nick(org)
    mine = lambda: fake.nick_calls.count(nick)  # noqa: E731
    owned = lambda: sum(1 for n in fake.numbers.values() if n["nickname"] == nick)  # noqa: E731
    assert _prov(org)[0] == "ambiguous"
    run_worker.run_once(fake); assert _prov(org)[0] == "ambiguous"        # too early to settle
    _age(org, 11); run_worker.run_once(fake)
    assert _prov(org) == ("failed", 1, NOT_FOUND_AFTER_RECONCILE)
    run_worker.run_once(fake); assert mine() == 1                          # cool-down: sweep does not retry yet
    _age(org, 31); run_worker.run_once(fake)                               # <- "restart" then the retry
    assert _numbers(org) == 1 and mine() == 2 and _prov(org)[0] == "purchased"
    for _ in range(3):
        run_worker.run_once(fake)
    assert _numbers(org) == 1 and mine() == 2 and owned() == 1 and _state(org)["state"] == "ready"


def _nick(org):
    db = SessionLocal()
    try:
        return db.execute(text("select nickname from phone_provisionings where organization_id=:o"), {"o": org}).scalar_one()
    finally:
        db.close()
