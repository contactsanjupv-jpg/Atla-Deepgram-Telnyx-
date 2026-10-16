"""
Paddle Billing implementation. Creates a transaction server-side and
returns Paddle's checkout.url (your default payment link + ?_ptxn=txn_...).
That page must include Paddle.js, which opens the checkout automatically.

organization_id + plan_id travel as custom_data on the transaction; Paddle
copies it onto the resulting subscription, which is how the webhook maps
an event back to a tenant. The client never supplies either value.
"""
import logging

import httpx

from app.config import get_settings
from app.providers.billing.billing_provider import BillingProvider, CheckoutSession

logger = logging.getLogger("atla.billing.paddle")

_BASE_URLS = {"sandbox": "https://sandbox-api.paddle.com", "live": "https://api.paddle.com"}


class PaddleAPIError(RuntimeError):
    """Subclasses RuntimeError so existing `except RuntimeError` blocks map it to a clean 502."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class PaddleBillingProvider(BillingProvider):
    def __init__(self):
        settings = get_settings()
        if not settings.PADDLE_API_KEY:
            raise RuntimeError("PADDLE_API_KEY is not configured — cannot create checkout sessions.")
        if settings.PADDLE_ENV not in _BASE_URLS:
            raise RuntimeError("PADDLE_ENV must be 'sandbox' or 'live'.")
        self._api_key = settings.PADDLE_API_KEY
        self._base_url = _BASE_URLS[settings.PADDLE_ENV]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def create_checkout_session(
        self,
        *,
        organization_id: str,
        plan_id: str,
        plan_price_id: str,
        customer_email: str,  # unused: Paddle collects the email in its own checkout
        success_url: str,  # unused here: passed to Paddle.js on the frontend checkout page
        cancel_url: str,
    ) -> CheckoutSession:
        body = {
            "items": [{"price_id": plan_price_id, "quantity": 1}],
            "collection_mode": "automatic",
            "custom_data": {"organization_id": organization_id, "plan_id": plan_id},
        }
        try:
            resp = httpx.post(f"{self._base_url}/transactions", headers=self._headers(), json=body, timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            logger.error("Paddle create-transaction failed: %s", resp.status_code)
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        data = resp.json().get("data") or {}
        url = (data.get("checkout") or {}).get("url")
        if not url or not data.get("id"):
            raise PaddleAPIError("Paddle response missing checkout URL — is a default payment link set?")
        return CheckoutSession(checkout_url=url, provider_session_id=data["id"])

    def cancel_subscription(self, external_subscription_id: str, immediately: bool = False) -> None:
        """Default is Paddle's: cancel at the end of the paid period (the customer keeps what they paid for)."""
        try:
            resp = httpx.post(
                f"{self._base_url}/subscriptions/{external_subscription_id}/cancel",
                headers=self._headers(),
                json={"effective_from": "immediately" if immediately else "next_billing_period"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)

    def get_management_urls(self, external_subscription_id: str) -> dict:
        """Paddle omits management_urls from webhooks; they come from GET /subscriptions/{id}."""
        try:
            resp = httpx.get(f"{self._base_url}/subscriptions/{external_subscription_id}", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        return (resp.json().get("data") or {}).get("management_urls") or {}

    def get_subscription(self, external_subscription_id: str) -> dict | None:
        """Paddle's current view of one subscription; None if Paddle has no such subscription."""
        try:
            resp = httpx.get(f"{self._base_url}/subscriptions/{external_subscription_id}", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        return resp.json().get("data") or None

    def get_price(self, price_id: str) -> dict | None:
        """Paddle's price entity (amount, currency, billing cycle); None if Paddle has no such price."""
        try:
            resp = httpx.get(f"{self._base_url}/prices/{price_id}", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        return resp.json().get("data") or None

    def create_topup_transaction(
        self, *, organization_id: str, topup_id: str, amount_minor: int, currency: str, description: str, display_name: str
    ) -> CheckoutSession:
        """
        A ONE-TIME Paddle transaction for a customer-chosen amount (a non-catalog price). Credits are
        never granted from this call: only from Paddle's verified transaction.completed webhook (or the
        reconciler reading the transaction back). custom_data is set server-side, never by the client.
        """
        settings = get_settings()
        price: dict = {
            "description": description[:500],
            "name": display_name[:150],
            "unit_price": {"amount": str(int(amount_minor)), "currency_code": currency},
        }
        if settings.PADDLE_TOPUP_PRODUCT_ID:
            price["product_id"] = settings.PADDLE_TOPUP_PRODUCT_ID
        else:
            price["product"] = {"name": "Atla call credits", "tax_category": settings.PADDLE_TOPUP_TAX_CATEGORY}
        body = {
            "items": [{"quantity": 1, "price": price}],
            "currency_code": currency,
            "collection_mode": "automatic",
            "custom_data": {"organization_id": organization_id, "kind": "topup", "topup_id": topup_id},
        }
        try:
            resp = httpx.post(f"{self._base_url}/transactions", headers=self._headers(), json=body, timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            logger.error("Paddle create top-up transaction failed: %s %s", resp.status_code, resp.text[:500])
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        data = resp.json().get("data") or {}
        url = (data.get("checkout") or {}).get("url")
        if not url or not data.get("id"):
            raise PaddleAPIError("Paddle response missing checkout URL — is a default payment link set?")
        return CheckoutSession(checkout_url=url, provider_session_id=data["id"])

    def get_transaction(self, transaction_id: str) -> dict | None:
        """Paddle's current view of one transaction (status, items, custom_data); None if it doesn't exist."""
        try:
            resp = httpx.get(f"{self._base_url}/transactions/{transaction_id}", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        return resp.json().get("data") or None

    def create_refund(self, transaction_id: str, reason: str) -> dict:
        """
        FULL refund of one transaction (a Paddle adjustment, action=refund, type=full). Only services/refunds.py
        may call this, after its authorization and eligibility checks. Returns Paddle's adjustment
        ({"id", "status", ...}); Paddle may leave it "pending_approval".
        """
        try:
            resp = httpx.post(
                f"{self._base_url}/adjustments",
                headers=self._headers(),
                json={"action": "refund", "type": "full", "transaction_id": transaction_id, "reason": reason[:1000]},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        return resp.json().get("data") or {}

    def list_refund_adjustments(self, transaction_id: str) -> list[dict]:
        """Paddle's refund adjustments for one transaction (used to settle an unconfirmed refund request)."""
        try:
            resp = httpx.get(
                f"{self._base_url}/adjustments",
                headers=self._headers(),
                params={"transaction_id": transaction_id, "action": "refund"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            raise PaddleAPIError(f"Paddle returned {resp.status_code}", resp.status_code)
        return [a for a in (resp.json().get("data") or []) if isinstance(a, dict)]

    def cancel_transaction(self, transaction_id: str) -> bool:
        """
        Best-effort: ask Paddle to cancel a not-yet-paid transaction so the customer can pick another plan.
        Returns True only if Paddle confirms status "canceled". Paddle documents this for BILLED
        transactions; for other unpaid states it may refuse — a False means "still payable, do not assume it is dead".
        """
        try:
            resp = httpx.patch(
                f"{self._base_url}/transactions/{transaction_id}",
                headers=self._headers(),
                json={"status": "canceled"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise PaddleAPIError("Could not reach Paddle") from e
        if resp.status_code >= 400:
            return False
        return (resp.json().get("data") or {}).get("status") == "canceled"

