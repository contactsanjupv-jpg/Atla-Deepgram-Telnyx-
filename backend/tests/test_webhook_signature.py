import hashlib
import hmac
import time

import pytest

from app.webhooks.retell_signature import InvalidRetellSignature, verify_retell_signature

API_KEY = "test-retell-api-key"


def _sign(body: bytes, timestamp_ms: int, key: str = API_KEY) -> str:
    digest = hmac.new(key.encode(), body + str(timestamp_ms).encode(), hashlib.sha256).hexdigest()
    return f"v={timestamp_ms},d={digest}"


def test_valid_signature_passes():
    body = b'{"event":"call_ended","call":{"call_id":"abc123"}}'
    header = _sign(body, int(time.time() * 1000))
    verify_retell_signature(body, header, API_KEY)  # should not raise


def test_missing_header_rejected():
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(b"{}", None, API_KEY)


def test_malformed_header_rejected():
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(b"{}", "not-a-valid-signature", API_KEY)


def test_wrong_key_rejected():
    body = b'{"event":"call_ended"}'
    header = _sign(body, int(time.time() * 1000), key="a-different-key")
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(body, header, API_KEY)


def test_tampered_body_rejected():
    body = b'{"event":"call_ended","call":{"call_id":"abc123"}}'
    header = _sign(body, int(time.time() * 1000))
    tampered_body = b'{"event":"call_ended","call":{"call_id":"XXXXXX"}}'
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(tampered_body, header, API_KEY)


def test_replayed_old_timestamp_rejected():
    """A signature that was valid 10 minutes ago must be rejected now —
    this is the replay-attack guard."""
    body = b'{"event":"call_ended"}'
    ten_minutes_ago_ms = int((time.time() - 600) * 1000)
    header = _sign(body, ten_minutes_ago_ms)
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(body, header, API_KEY, max_skew_seconds=300)


def test_future_timestamp_outside_skew_rejected():
    body = b'{"event":"call_ended"}'
    future_ms = int((time.time() + 600) * 1000)
    header = _sign(body, future_ms)
    with pytest.raises(InvalidRetellSignature):
        verify_retell_signature(body, header, API_KEY, max_skew_seconds=300)

# ---------------- proven against Retell's OWN signer (retell-sdk 6.0.1, retell/lib/webhook_auth.py) ----------------
# The vector below was produced by `symmetric["sign"](body, key, timestamp)` from Retell's official
# Python SDK. If our verifier ever stops accepting it, real Retell webhooks would 401.

OFFICIAL_BODY = (
    '{"event":"call_ended","call":{"call_id":"call_abc123","agent_id":"agent_x","duration_ms":61499,'
    '"note":"café — ünïcode"}}'
).encode("utf-8")
OFFICIAL_KEY = "key_0123456789abcdef"
OFFICIAL_SIGNATURE = "v=1800000000000,d=042fd0d193fcac1055a884ba6717ac5376b0043f13ce62109e8303088233fcfd"


def test_our_verifier_accepts_a_signature_made_by_retells_official_signer():
    from app.webhooks.retell_signature import verify_retell_signature

    # max_skew is huge only because the vector's timestamp is fixed in the past
    verify_retell_signature(OFFICIAL_BODY, OFFICIAL_SIGNATURE, OFFICIAL_KEY, max_skew_seconds=10**10)


def test_the_official_vector_is_rejected_with_the_wrong_key_or_a_changed_body():
    from app.webhooks.retell_signature import InvalidRetellSignature, verify_retell_signature

    for body, key in ((OFFICIAL_BODY, "key_not_the_webhook_key"), (OFFICIAL_BODY + b" ", OFFICIAL_KEY)):
        with pytest.raises(InvalidRetellSignature, match="Signature mismatch"):
            verify_retell_signature(body, OFFICIAL_SIGNATURE, key, max_skew_seconds=10**10)


def test_a_rejected_webhook_is_a_bare_401_but_the_reason_is_logged_without_the_key(monkeypatch, caplog):
    import logging

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    monkeypatch.setattr(get_settings(), "RETELL_API_KEY", "key_SUPERSECRET_9f3a")
    caplog.set_level(logging.WARNING, logger="atla.webhooks.retell")
    ts = int(__import__("time").time() * 1000)
    resp = TestClient(app).post(
        "/webhooks/retell", content=b'{"event":"call_ended"}', headers={"x-retell-signature": f"v={ts},d={'0' * 64}"}
    )
    assert resp.status_code == 401 and resp.content == b""
    assert "Signature mismatch" in caplog.text and "api_key_configured=True" in caplog.text
    assert "SUPERSECRET" not in caplog.text and "9f3a" not in caplog.text and "last4" not in caplog.text