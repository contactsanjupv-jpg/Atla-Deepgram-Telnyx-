from datetime import datetime

from pydantic import BaseModel


class CreateCheckoutSessionRequest(BaseModel):
    plan: str  # "starter" | "growth"


class CreateCheckoutSessionResponse(BaseModel):
    checkout_url: str


class SubscriptionFeatureOut(BaseModel):
    id: str
    label: str


class SubscriptionOut(BaseModel):
    plan_id: str
    plan_name: str | None  # None when the plan isn't one we recognise
    status: str
    # The server's verdict: are paid features unlocked right now? (The frontend never decides this.)
    entitled: bool
    # What the current plan includes right now. Empty when not entitled.
    features: list[SubscriptionFeatureOut]
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    # Set when the customer has cancelled but service continues until this date.
    cancel_effective_at: datetime | None = None
    # Set when a cancel request was accepted but the provider hasn't confirmed it in our state yet.
    cancel_requested_at: datetime | None = None
    # past_due only: when the receptionist stops answering calls if payment isn't fixed.
    service_ends_at: datetime | None = None