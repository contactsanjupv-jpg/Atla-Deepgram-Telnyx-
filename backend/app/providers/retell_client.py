"""
Shared low-level HTTP client for every Retell*Provider. Centralizes auth,
base URL, timeout, and error mapping so individual providers stay thin.

Retry policy (money-critical): a request is only retried when it is SAFE
to repeat.
  - Connection errors (nothing reached Retell): safe for every method.
  - Timeouts / dropped connections AFTER the request was sent: only safe for
    GET/DELETE. For POST/PATCH the outcome is AMBIGUOUS (Retell may have
    done the work), so we raise RetellAPIError(ambiguous=True) and let the
    caller reconcile instead of blindly repeating a purchase.
"""
import re
import time

import httpx

from app.config import get_settings

_MAX_ATTEMPTS = 3
_SAFE_TO_REPEAT_METHODS = {"GET", "DELETE"}


def _safe_reason(exc: Exception, api_key: str | None = None) -> str:
    """Exception class + short message for LOGS: enough to tell DNS / TLS / refused / timeout / proxy apart.
    The key is never part of an httpx transport message (it only travels in a header), but it is stripped
    anyway, along with any bearer token, and the text is capped."""
    text = " ".join(str(exc).split())
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)[:200]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class RetellAPIError(Exception):
    def __init__(self, status_code: int, message: str, body: dict | None = None, ambiguous: bool = False):
        self.status_code = status_code  # 0 = never got an HTTP response
        self.body = body
        self.ambiguous = ambiguous  # True = Retell MAY have processed the request
        super().__init__(f"Retell API error {status_code}: {message}")


class RetellClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.RETELL_API_KEY
        self.base_url = base_url or settings.RETELL_API_BASE_URL
        if not self.api_key:
            # Fail loudly and early rather than silently making
            # unauthenticated calls that will 401 downstream.
            raise RuntimeError(
                "RETELL_API_KEY is not configured. Set it in the environment before using RetellClient."
            )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        method = method.upper()
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.request(method, url, headers=self._headers(), json=json, params=params)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_error = e  # request never reached Retell — always safe to repeat
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise RetellAPIError(0, f"Could not connect to Retell ({_safe_reason(e, self.api_key)})", ambiguous=False) from e
            except httpx.TransportError as e:
                # Sent, but we never saw a response: Retell may have acted on it.
                last_error = e
                if method in _SAFE_TO_REPEAT_METHODS and attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise RetellAPIError(
                    0, f"No response from Retell ({_safe_reason(e, self.api_key)})", ambiguous=method not in _SAFE_TO_REPEAT_METHODS
                ) from e

            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                raise RetellAPIError(resp.status_code, resp.text, body)
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

        raise RetellAPIError(0, "Retell request failed", ambiguous=False) from last_error