"""
GET /api/v1/plans — public. The one place the frontend learns plan names, what
each plan includes, and what it costs. Prices come from Paddle (never hardcoded);
Paddle/price ids and internal ids are never returned.
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.services.plan_pricing import price_for_plan
from app.services.plans import BASELINE_INCLUDED, FEATURE_LABELS, PLANS, Feature, when_credits_run_out

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


class PlanFeatureOut(BaseModel):
    id: str
    label: str


class PlanPriceOut(BaseModel):
    amount_minor: int  # lowest currency denomination, e.g. cents
    currency: str
    interval: str  # "month" | "year"
    interval_count: int


class PlanOut(BaseModel):
    id: str
    name: str
    features: list[PlanFeatureOut]
    price: PlanPriceOut | None  # None when Paddle can't be read — show no number, never a wrong one
    included_minutes: int  # monthly AI-receptionist minutes included; the same number the credit ledger grants
    baseline: list[str]  # what every plan includes besides `features` (call history, leads, usage)
    when_credits_run_out: str


@router.get("", response_model=list[PlanOut])
def list_plans():
    out = []
    for plan in sorted(PLANS.values(), key=lambda p: p.rank):
        out.append(
            PlanOut(
                id=plan.id,
                name=plan.name,
                features=[PlanFeatureOut(id=f.value, label=FEATURE_LABELS[f]) for f in Feature if f in plan.features],
                price=price_for_plan(plan.id),
                included_minutes=plan.included_minutes,
                baseline=list(BASELINE_INCLUDED),
                when_credits_run_out=when_credits_run_out(),
            )
        )
    return out
