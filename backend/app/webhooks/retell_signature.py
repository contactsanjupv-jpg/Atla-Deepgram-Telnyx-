"""
Retell webhook signature verification.

Per Retell's documented scheme (docs.retellai.com/features/secure-webhook):
  - Header: X-Retell-Signature: v={unix_ms_timestamp},d={hex_digest}
  - digest = HMAC-SHA256(raw_body + timestamp, key=RETELL_API_KEY)
    (the signing "secret" is the Retell API key itself — the one flagged
    with a webhook badge in their dashboard — there is no separate
    webhook-specific secret)
  - Verify against the RAW body bytes, never a re-serialized JSON string
  - Reject anything outside a ~5 minute window to stop replay
  - Constant-time comparison
"""
import hashlib
import hmac
import re
import time

_SIGNATURE_RE = re.compile(r"^v=(\d+),d=([0-9a-f]+)$")


class InvalidRetellSignature(Exception):
    pass


def verify_retell_signature(
    raw_body: bytes,
    signature_header: str | None,
    api_key: str,
    max_skew_seconds: int = 300,
) -> None:
    """Raises InvalidRetellSignature if the request should be rejected.
    Callers MUST pass the untouched raw request body — verifying against a
    JSON.dumps() of the parsed body will intermittently fail (key ordering,
    whitespace) and worse, can be tricked by a payload that reserializes
    differently than what was actually signed."""
    if not api_key:
        # HMAC with an empty key is computable by anyone: an unconfigured server must reject everything,
        # never accept a "valid" signature made with a public (empty) secret.
        raise InvalidRetellSignature("RETELL_API_KEY is not configured")
    if not signature_header:
        raise InvalidRetellSignature("Missing X-Retell-Signature header")

    match = _SIGNATURE_RE.match(signature_header)
    if not match:
        raise InvalidRetellSignature("Malformed X-Retell-Signature header")

    timestamp_ms, digest_hex = match.group(1), match.group(2)

    now_ms = int(time.time() * 1000)
    skew_seconds = abs(now_ms - int(timestamp_ms)) / 1000
    if skew_seconds > max_skew_seconds:
        raise InvalidRetellSignature(f"Signature timestamp outside allowed window ({skew_seconds:.0f}s)")

    signed_payload = raw_body + timestamp_ms.encode()
    expected_digest = hmac.new(api_key.encode(), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_digest, digest_hex):
        raise InvalidRetellSignature("Signature mismatch")
