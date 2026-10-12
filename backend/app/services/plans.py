"""
The ONE authoritative definition of plans and what each plan includes.

* Plan <-> Paddle price mapping comes from config (PADDLE_*_PRICE_ID); a Paddle
  subscription's plan is always derived from its actual PRICE, never from
  client-influenced data.
* Features: Growth = Starter + GROWTH_ONLY. Inheritance is by construction, so
  Starter can never hold something Growth lacks.
* Every server-side feature gate goes through `require_feature` (auth/deps.py),
  which calls into here. Do not compare plan names anywhere else.

Growth-only features are each backed by a real, tested endpoint behind
require_feature: full transcripts, transcript search, analytics. Do not list a feature here
unless its route is gated by it.

Included minutes come from settings (STARTER/GROWTH_INCLUDED_MINUTES), derived by
`python -m app.workers.pricing_report`. The displayed PRICE is never stored here: it is read
from the Paddle price behind each plan (services/plan_pricing.py), so it cannot drift from billing.
"""
import logging
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.billing_state import ACTIVE_STATUSES, current_subscription

logger = logging.getLogger("atla.plans")

UNKNOWN_PLAN = "unknown"


class Feature(str, Enum):
    phone_number = "phone_number"  # buy / keep a real business number (costs us money)
    go_live = "go_live"  # route real calls to the receptionist (costs us money)
    transcripts = "transcripts"  # read the full conversation, not just the summary (Growth)
    transcript_search = "transcript_search"  # search what callers said across all calls (Growth)
    analytics = "analytics"  # call / lead / usage trends over time (Growth)


FEATURE_LABELS: dict[Feature, str] = {
    Feature.phone_number: "A dedicated business phone number",
    Feature.go_live: "Your receptionist answers real calls",
    Feature.transcripts: "Full conversation transcripts, with who said what and when",
    Feature.transcript_search: "Search everything your callers said",
    Feature.analytics: "Call, lead and usage analytics over time",
}

STARTER_FEATURES: frozenset[Feature] = frozenset({Feature.phone_number, Feature.go_live})
GROWTH_ONLY_FEATURES: frozenset[Feature] = frozenset(
    {Feature.transcripts, Feature.transcript_search, Feature.analytics}
)

# True for every plan today and not gated (they do not cost us more per customer): shown on both cards.
BASELINE_INCLUDED: tuple[str, ...] = (
    "Call history with an AI summary of every call",
    "Lead capture and lead management",
    "Credits used, credits remaining and calls handled",
)

def when_credits_run_out() -> str:
    """Customer-facing policy text, built from the real settings so it can never disagree with them."""
    s = get_settings()
    return (
        f"When your included minutes run out, you can buy more any time (from ${s.TOPUP_MIN_CENTS / 100:,.0f}). "
        "Your receptionist keeps answering for a short grace period, then pauses until you add credits."
    )


@dataclass(frozen=True)
class Plan:
    id: str
    name: str  # customer-facing
    rank: int
    features: frozenset[Feature]
    included_minutes: int = 0  # monthly allowance (settings); never rolls over


_settings = get_settings()

PLANS: dict[str, Plan] = {
    "starter": Plan("starter", "Starter", 1, STARTER_FEATURES, _settings.STARTER_INCLUDED_MINUTES),
    "growth": Plan("growth", "Growth", 2, STARTER_FEATURES | GROWTH_ONLY_FEATURES, _settings.GROWTH_INCLUDED_MINUTES),
}

_PRICE_SETTING = {"starter": "PADDLE_STARTER_PRICE_ID", "growth": "PADDLE_GROWTH_PRICE_ID"}


def price_id_for_plan(plan_id: str) -> str:
    name = _PRICE_SETTING.get(plan_id)
    return (getattr(get_settings(), name, "") or "") if name else ""


def plan_id_for_price(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for plan_id in _PRICE_SETTING:
        configured = price_id_for_plan(plan_id)
        if configured and configured == price_id:
            return plan_id
    return None


def plan_id_from_items(items) -> str | None:
    """
    Plan implied by a Paddle subscription's items.
      None        -> payload carried no price information at all
      "unknown"   -> prices present but not (only) ours: fail closed, no features
      "starter"/"growth" -> every price maps to that one plan
    """
    if not isinstance(items, list) or not items:
        return None
    resolved: set[str] = set()
    for item in items:
        price = item.get("price") if isinstance(item, dict) else None
        price_id = price.get("id") if isinstance(price, dict) else None
        if price_id:
            resolved.add(plan_id_for_price(price_id) or UNKNOWN_PLAN)
    if not resolved:
        return None
    return next(iter(resolved)) if len(resolved) == 1 else UNKNOWN_PLAN


def active_plan(db: Session, organization_id: uuid.UUID) -> Plan | None:
    """The plan the org is entitled to RIGHT NOW (trialing/active only). Unknown plan ids get nothing."""
    sub = current_subscription(db, organization_id)
    if sub is None or sub.status not in ACTIVE_STATUSES:
        return None
    plan = PLANS.get(sub.plan_id)
    if plan is None:
        logger.error("Active subscription %s has unrecognised plan_id %r — granting no features", sub.id, sub.plan_id)
    return plan


def plan_allows(db: Session, organization_id: uuid.UUID, feature: Feature) -> bool:
    plan = active_plan(db, organization_id)
    return plan is not None and feature in plan.features


def lowest_plan_with(feature: Feature) -> Plan | None:
    for plan in sorted(PLANS.values(), key=lambda p: p.rank):
        if feature in plan.features:
            return plan
    return None
