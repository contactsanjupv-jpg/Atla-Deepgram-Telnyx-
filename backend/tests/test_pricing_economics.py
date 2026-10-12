"""The configured allowances and top-up rate must earn the target margin at the intended price points."""
from app.config import get_settings
from app.services.plans import PLANS
from app.services.pricing import max_included_minutes, plan_economics, topup_economics

s = get_settings()
# The INTENDED Paddle prices (USD cents). The live source of truth is the Paddle price behind each plan;
# `python -m app.workers.pricing_report` re-checks the allowances against the real Paddle prices.
INTENDED = {"starter": 7900, "growth": 19900}


def test_each_plan_allowance_earns_at_least_the_target_margin_at_its_intended_price():
    for plan_id, price in INTENDED.items():
        eco = plan_economics(s, price, PLANS[plan_id].included_minutes)
        assert eco.gross_margin_percent >= s.TARGET_GROSS_MARGIN_PERCENT, (plan_id, eco)
        assert PLANS[plan_id].included_minutes <= max_included_minutes(s, price)


def test_the_allowance_is_not_wastefully_below_what_the_price_can_afford():
    for plan_id, price in INTENDED.items():
        assert PLANS[plan_id].included_minutes >= 0.9 * max_included_minutes(s, price)


def test_a_topup_at_the_minimum_and_maximum_earns_the_target_margin():
    for amount in (s.TOPUP_MIN_CENTS, 2500, 10000, s.TOPUP_MAX_CENTS):
        assert topup_economics(s, amount).gross_margin_percent >= s.TARGET_GROSS_MARGIN_PERCENT, amount


def test_a_minute_costs_more_to_top_up_than_it_costs_us_including_fees():
    assert s.TOPUP_CENTS_PER_MINUTE > s.COST_BASIS_CENTS_PER_MINUTE
    assert s.TOPUP_MIN_CENTS == 2000  # the product rule: $20 minimum


def test_the_cost_basis_covers_what_the_one_real_test_call_cost():
    observed = 12.13 / (44 / 60)  # cents per minute from the real 44 s call (Retell reports combined_cost in cents)
    assert s.COST_BASIS_CENTS_PER_MINUTE >= observed
