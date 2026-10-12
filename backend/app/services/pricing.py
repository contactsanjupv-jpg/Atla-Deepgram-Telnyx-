"""
Unit economics: how many included minutes a plan price can afford, and what a top-up minute must cost.

Pure arithmetic over INTERNAL inputs (config.py). Nothing here is ever sent to a customer.

    revenue            = the Paddle price
    variable cost      = minutes x COST_BASIS_CENTS_PER_MINUTE
                         + PHONE_NUMBER_CENTS_PER_MONTH            (one number per customer)
                         + Paddle fee (PADDLE_FEE_PERCENT of the price + PADDLE_FEE_FIXED_CENTS)
    gross margin       = (revenue - variable cost) / revenue

Provider cost unit: Retell documents call_cost.combined_cost "in cents" (API reference, Get Call).
Retell's published rates as read on 2026-10-11: voice infrastructure $0.055/min, telephony $0.015/min,
phone number $2.00/month, plus the LLM you select (billed per minute). The cost basis must cover
infrastructure + telephony + YOUR LLM; the measured average from real calls is printed by
`python -m app.workers.pricing_report` so the basis can be checked against reality.
"""
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Economics:
    revenue_minor: int
    minutes: int
    provider_cost_minor: int
    phone_cost_minor: int
    paddle_fee_minor: int
    gross_margin_percent: float


def paddle_fee_minor(settings: Settings, revenue_minor: int) -> int:
    return -(-revenue_minor * settings.PADDLE_FEE_PERCENT // 100) + settings.PADDLE_FEE_FIXED_CENTS  # ceil(percent) + fixed


def plan_economics(settings: Settings, price_minor: int, included_minutes: int) -> Economics:
    provider = included_minutes * settings.COST_BASIS_CENTS_PER_MINUTE
    phone = settings.PHONE_NUMBER_CENTS_PER_MONTH
    fee = paddle_fee_minor(settings, price_minor)
    margin = (price_minor - provider - phone - fee) / price_minor * 100 if price_minor else 0.0
    return Economics(price_minor, included_minutes, provider, phone, fee, round(margin, 2))


def max_included_minutes(settings: Settings, price_minor: int) -> int:
    """The most minutes this price can include while still earning TARGET_GROSS_MARGIN_PERCENT."""
    budget = price_minor * (100 - settings.TARGET_GROSS_MARGIN_PERCENT) / 100
    budget -= paddle_fee_minor(settings, price_minor) + settings.PHONE_NUMBER_CENTS_PER_MONTH
    return max(0, int(budget // settings.COST_BASIS_CENTS_PER_MINUTE))


def topup_economics(settings: Settings, amount_minor: int) -> Economics:
    """A top-up has no phone number cost: just the minutes it buys and Paddle's fee."""
    minutes = amount_minor // settings.TOPUP_CENTS_PER_MINUTE
    provider = minutes * settings.COST_BASIS_CENTS_PER_MINUTE
    fee = paddle_fee_minor(settings, amount_minor)
    margin = (amount_minor - provider - fee) / amount_minor * 100 if amount_minor else 0.0
    return Economics(amount_minor, minutes, provider, 0, fee, round(margin, 2))
