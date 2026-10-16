from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CheckoutSession:
    checkout_url: str
    provider_session_id: str


class BillingProvider(ABC):
    @abstractmethod
    def create_checkout_session(
        self,
        *,
        organization_id: str,
        plan_id: str,
        plan_price_id: str,
        customer_email: str,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession: ...

    @abstractmethod
    def cancel_subscription(self, external_subscription_id: str, immediately: bool = False) -> None: ...

    @abstractmethod
    def get_management_urls(self, external_subscription_id: str) -> dict: ...

    def get_subscription(self, external_subscription_id: str) -> dict | None:
        """The provider's own current record of a subscription (None if it has none). Providers that cannot
        look one up leave this unimplemented, which callers must treat as "unverified" (never as success)."""
        raise NotImplementedError("This billing provider cannot look up subscriptions")