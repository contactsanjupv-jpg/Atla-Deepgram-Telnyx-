"""
Level 4: phone purchase and activation input validation.

* A bad area code / country is a 422 BEFORE anything is written, rate-limited or sent to the provider (it used to be
  accepted and stored, a 500 from a column overflow, or - with the real provider - a 500 that left a provisioning row
  stuck in "purchasing").
* Malformed ids on /activate are 422 (never 500); well-formed unknown ids are 404; released / inactive numbers are
  refused by the backend (409) without touching the provider.
"""
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus, PhoneProvisioning
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellClient
from app.services import auto_provisioning
from app.services.phone_provisioning import InvalidProvisioningRequest, provision_number
from tests.fakes import FakePhone
from tests.test_phone_lifecycle import _activate, _clone, _live_setup, _org


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


def _buy(c, org, **body):
    return _clone(c).post(f"/api/v1/orgs/{org}/phone-numbers", json=body)


def _prov(org):
    db = SessionLocal()
    try:
        return db.query(PhoneProvisioning).filter(PhoneProvisioning.organization_id == uuid.UUID(org)).first()
    finally:
        db.close()


BAD_AREA_CODES = [
    "abc", "12345678901", "41", "4155", "+415", "4_15", "٤١٥", "0415", "115", "211", "911", "415 415", "41a", "4.1", "-415",
]


@pytest.mark.parametrize("area", BAD_AREA_CODES)
def test_bad_area_code_is_a_422_and_nothing_is_written_or_bought(phone, area):
    c, org = _org()
    r = _buy(c, org, country="US", area_code=area)

    assert r.status_code == 422, r.text
    assert "area code" in r.text.lower() and "Value error" not in r.text  # readable in the wizard
    assert _prov(org) is None and phone.purchases == 0


@pytest.mark.parametrize("country", ["", "U", "USA", "UNITEDSTATES", "U1", "ÜS", "  "])
def test_bad_country_is_a_422_not_a_500(phone, country):
    c, org = _org()
    r = _buy(c, org, country=country, area_code="415")

    assert r.status_code == 422, r.text
    assert _prov(org) is None and phone.purchases == 0


@pytest.mark.parametrize("value", [415, 415.0, ["415"], {"a": 1}, True])
def test_non_string_area_code_is_a_422(phone, value):
    c, org = _org()

    assert _buy(c, org, country="US", area_code=value).status_code == 422
    assert phone.purchases == 0


@pytest.mark.parametrize("sent,expected", [("415", "415"), (" 415 ", "415"), ("212", "212"), (None, None), ("", None), ("   ", None)])
def test_valid_or_blank_area_code_still_buys_one_number(phone, sent, expected):
    c, org = _org()
    r = _buy(c, org, country="US", area_code=sent)

    assert r.status_code == 201, r.text
    assert r.json()["area_code"] == expected
    assert phone.purchases == 1


def test_country_is_normalised_and_defaults_to_us(phone):
    c, org = _org()
    r = _clone(c).post(f"/api/v1/orgs/{org}/phone-numbers", json={"country": " ca "})
    assert r.status_code == 201
    assert _prov(org).country == "CA"  # what we asked the provider for (the fake echoes a fixed country)

    c2, org2 = _org()
    r2 = _clone(c2).post(f"/api/v1/orgs/{org2}/phone-numbers", json={})
    assert r2.status_code == 201
    assert _prov(org2).country == "US"


def test_a_rejected_request_does_not_use_up_the_daily_purchase_limit(phone):
    c, org = _org()
    for _ in range(12):  # the limit is 10 attempts a day
        assert _buy(c, org, country="US", area_code="abc").status_code == 422

    assert _buy(c, org, country="US", area_code="415").status_code == 201


def test_real_provider_never_sees_a_bad_area_code_and_leaves_no_stuck_row(monkeypatch):
    """The original failure: int('abc') inside the provider, after the row was committed as 'purchasing'."""
    seen = []

    class Wire(RetellClient):
        def __init__(self):
            pass

        def request(self, method, path, json=None, params=None):
            seen.append(json)
            return {"phone_number": f"+1555{uuid.uuid4().int % 10_000_000:07d}"}

    for target in ("app.services.phone_provisioning.RetellPhoneProvider", "app.api.v1.phone_numbers.RetellPhoneProvider"):
        monkeypatch.setattr(target, lambda: RetellPhoneProvider(Wire()))
    c, org = _org()

    assert _buy(c, org, country="US", area_code="abc").status_code == 422
    assert seen == [] and _prov(org) is None

    assert _buy(c, org, country="US", area_code=" 415 ").status_code == 201
    assert seen[0]["area_code"] == 415 and seen[0]["country_code"] == "US"
    assert _prov(org).status == "purchased"


def test_service_refuses_bad_input_before_writing_anything(phone):
    _c, org = _org()
    db = SessionLocal()
    try:
        with pytest.raises(InvalidProvisioningRequest):
            provision_number(db, uuid.UUID(org), "US", "abc", phone)
        with pytest.raises(InvalidProvisioningRequest):
            provision_number(db, uuid.UUID(org), "USA", None, phone)
    finally:
        db.close()
    assert _prov(org) is None and phone.purchases == 0


def test_misconfigured_auto_provision_country_fails_cleanly_without_a_stuck_row(phone, monkeypatch):
    _c, org = _org()
    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_NUMBERS", True)  # off by default in the test environment
    monkeypatch.setattr(get_settings(), "AUTO_PROVISION_COUNTRY", "USA")
    db = SessionLocal()
    try:
        outcome = auto_provisioning.auto_provision_for_org(db, uuid.UUID(org), phone)
    finally:
        db.close()

    assert outcome == "failed"
    assert _prov(org) is None and phone.purchases == 0


# ---------------------------- activation ----------------------------

@pytest.mark.parametrize(
    "agent_id,phone_id",
    [("not-a-uuid", "x"), ("", ""), (str(uuid.uuid4()), "not-a-uuid"), ("1" * 40, str(uuid.uuid4())), ("g" * 36, "g" * 36), (" ", " ")],
)
def test_malformed_activation_ids_are_422_not_500(phone, agent_id, phone_id):
    c, org = _org()
    r = _clone(c).post(f"/api/v1/orgs/{org}/activate", json={"agent_id": agent_id, "phone_number_id": phone_id})

    assert r.status_code == 422, r.text


@pytest.mark.parametrize("body", [{}, {"agent_id": None, "phone_number_id": None}, {"agent_id": 5, "phone_number_id": 6}, {"agent_id": [], "phone_number_id": {}}])
def test_malformed_activation_body_is_422(phone, body):
    c, org = _org()

    assert _clone(c).post(f"/api/v1/orgs/{org}/activate", json=body).status_code == 422


def test_well_formed_but_unknown_ids_are_404(phone):
    c, org = _org()
    r = _clone(c).post(f"/api/v1/orgs/{org}/activate", json={"agent_id": str(uuid.uuid4()), "phone_number_id": str(uuid.uuid4())})

    assert r.status_code == 404


@pytest.mark.parametrize("state", [PhoneNumberStatus.released, PhoneNumberStatus.inactive])
def test_released_or_inactive_number_cannot_be_activated_and_the_provider_is_not_touched(phone, state):
    c, org = _org()
    agent_id, _retell_agent = _live_setup(org, phone)
    number = _buy(c, org, country="US", area_code="415").json()
    db = SessionLocal()
    db.query(PhoneNumber).filter(PhoneNumber.id == uuid.UUID(number["id"])).update({"status": state})
    db.commit()
    db.close()

    r = _activate(_clone(c), org, agent_id, number["id"])

    assert r.status_code == 409 and "no longer active" in r.json()["detail"]
    assert phone.assigns == []  # nothing was assigned at the provider
