"""
`status=0` means "no HTTP response from Retell". The log must say WHY (exception class + a short sanitized
message: DNS, TLS, refused, timeout, proxy) without ever including the API key, and the money-safety
behaviour must not change: connect errors are retried and not ambiguous; a POST that was sent and got no
answer is never repeated and is ambiguous.
"""
import logging
import uuid

import httpx
import pytest

from app.db.base import SessionLocal
from app.db.models.telephony import PhoneProvisioning
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellAPIError, RetellClient
from app.services.phone_provisioning import ProvisioningFailed, ProvisioningPending, provision_number
from tests.billing_helpers import new_org

SECRET = "key_live_super_secret_123"


class _Net:
    attempts = 0
    error: Exception = httpx.ConnectError("x")

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, *a, **k):
        type(self).attempts += 1
        raise type(self).error


@pytest.fixture
def net(monkeypatch):
    monkeypatch.setattr("app.providers.retell_client.httpx.Client", _Net)
    monkeypatch.setattr("app.providers.retell_client.time.sleep", lambda s: None)
    _Net.attempts = 0
    return _Net


def test_connect_failure_names_the_exception_and_reason_but_never_the_key(net):
    net.error = httpx.ConnectError(f"[Errno -2] Name or service not known (Authorization: Bearer {SECRET} key={SECRET})")
    with pytest.raises(RetellAPIError) as exc:
        RetellClient(api_key=SECRET).request("POST", "/create-phone-number", json={})
    err = exc.value
    assert err.status_code == 0 and err.ambiguous is False and net.attempts == 3  # retry behaviour unchanged
    assert "ConnectError" in str(err) and "Name or service not known" in str(err)
    assert SECRET not in str(err) and "[redacted]" in str(err)
    assert isinstance(err.__cause__, httpx.ConnectError)  # the original stays chained


def test_a_sent_post_with_no_answer_is_still_ambiguous_and_never_repeated(net):
    net.error = httpx.ReadTimeout("The read operation timed out")
    with pytest.raises(RetellAPIError) as exc:
        RetellClient(api_key=SECRET).request("POST", "/create-phone-number", json={})
    assert exc.value.ambiguous is True and net.attempts == 1
    assert "ReadTimeout" in str(exc.value)


def test_message_is_capped_and_tolerates_an_empty_reason(net):
    net.error = httpx.ConnectError("y" * 5000)
    with pytest.raises(RetellAPIError) as exc:
        RetellClient(api_key=SECRET).request("GET", "/list-phone-numbers")
    assert len(str(exc.value)) < 400
    net.error = httpx.ConnectTimeout("")
    with pytest.raises(RetellAPIError) as exc:
        RetellClient(api_key=SECRET).request("GET", "/list-phone-numbers")
    assert "ConnectTimeout" in str(exc.value)


def _prov_status(org):
    db = SessionLocal()
    try:
        return db.query(PhoneProvisioning).filter_by(organization_id=uuid.UUID(org)).one().status
    finally:
        db.close()


def test_a_failed_number_purchase_logs_why_the_provider_was_unreachable(net, caplog):
    net.error = httpx.ConnectError(f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed ({SECRET})")
    _c, org = new_org()
    db = SessionLocal()
    try:
        with caplog.at_level(logging.ERROR, logger="atla.provisioning"):
            with pytest.raises(ProvisioningFailed):
                provision_number(db, uuid.UUID(org), "US", "415", provider=RetellPhoneProvider(client=RetellClient(api_key=SECRET)))
    finally:
        db.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Number purchase failed" in text and "status=0" in text
    assert "ConnectError" in text and "CERTIFICATE_VERIFY_FAILED" in text
    assert SECRET not in text
    assert _prov_status(org) == "failed"  # a connect error means nothing was bought: not ambiguous


def test_an_ambiguous_purchase_is_still_treated_as_ambiguous_and_logs_its_cause(net, caplog):
    net.error = httpx.ReadTimeout("timed out waiting for Retell")
    _c, org = new_org()
    db = SessionLocal()
    try:
        with caplog.at_level(logging.ERROR, logger="atla.provisioning"):
            with pytest.raises(ProvisioningPending):
                provision_number(db, uuid.UUID(org), "US", None, provider=RetellPhoneProvider(client=RetellClient(api_key=SECRET)))
    finally:
        db.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ReadTimeout" in text and SECRET not in text
    assert _prov_status(org) == "ambiguous" and net.attempts == 1
