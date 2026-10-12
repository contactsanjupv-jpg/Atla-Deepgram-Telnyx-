"""
Prints the numbers behind the plan allowances and top-up rate, and what free test calls really cost.

    python -m app.workers.pricing_report [--starter-price-cents 7900 --growth-price-cents 19900]

Plan prices are read from Paddle (the price behind each PADDLE_*_PRICE_ID). If Paddle can't be read
you may pass them explicitly. INTERNAL: shows provider cost and margin — never expose this output.
Exit code 1 means a configured allowance would earn less than TARGET_GROSS_MARGIN_PERCENT.
"""
import argparse

from sqlalchemy import func

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import UsageLedgerEntry
from app.services.plan_pricing import price_for_plan
from app.services.plans import PLANS
from app.services.pricing import max_included_minutes, plan_economics, topup_economics


def measured_cost_by_kind(db) -> list[tuple[str, int, int, float | None]]:
    """(kind, calls, billable seconds, average provider cents per minute). Provider cost unit: CENTS."""
    rows = (
        db.query(
            UsageLedgerEntry.kind,
            func.count(),
            func.coalesce(func.sum(UsageLedgerEntry.billable_seconds), 0),
            func.coalesce(func.sum(UsageLedgerEntry.provider_cost), 0),
        )
        .group_by(UsageLedgerEntry.kind)
        .all()
    )
    out = []
    for kind, calls, seconds, cost in rows:
        seconds = int(seconds)
        out.append((kind, int(calls), seconds, float(cost) / (seconds / 60) if seconds else None))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--starter-price-cents", type=int)
    parser.add_argument("--growth-price-cents", type=int)
    args = parser.parse_args()
    s = get_settings()
    explicit = {"starter": args.starter_price_cents, "growth": args.growth_price_cents}
    configured = {"starter": s.STARTER_INCLUDED_MINUTES, "growth": s.GROWTH_INCLUDED_MINUTES}

    print(f"Cost basis {s.COST_BASIS_CENTS_PER_MINUTE}c/min | Paddle {s.PADDLE_FEE_PERCENT}% + {s.PADDLE_FEE_FIXED_CENTS}c | "
          f"phone {s.PHONE_NUMBER_CENTS_PER_MONTH}c/mo | target margin {s.TARGET_GROSS_MARGIN_PERCENT}%")
    below_target = False
    for plan_id in PLANS:
        price = explicit.get(plan_id)
        source = "given"
        if price is None:
            paddle = price_for_plan(plan_id)
            price, source = (paddle["amount_minor"], "Paddle") if paddle else (None, "unavailable")
        if price is None:
            print(f"{plan_id}: price unavailable from Paddle — pass --{plan_id}-price-cents")
            continue
        eco = plan_economics(s, price, configured[plan_id])
        ceiling = max_included_minutes(s, price)
        flag = "" if eco.gross_margin_percent >= s.TARGET_GROSS_MARGIN_PERCENT else "  <-- BELOW TARGET"
        below_target |= bool(flag)
        print(f"{plan_id}: price {price}c ({source}) | included {configured[plan_id]} min (max affordable {ceiling}) | "
              f"margin {eco.gross_margin_percent}%{flag}")

    for amount in (s.TOPUP_MIN_CENTS, s.TOPUP_MAX_CENTS):
        eco = topup_economics(s, amount)
        flag = "" if eco.gross_margin_percent >= s.TARGET_GROSS_MARGIN_PERCENT else "  <-- BELOW TARGET"
        below_target |= bool(flag)
        print(f"top-up {amount}c buys {eco.minutes} min at {s.TOPUP_CENTS_PER_MINUTE}c/min | margin {eco.gross_margin_percent}%{flag}")

    db = SessionLocal()
    try:
        print("Measured provider cost (cents per minute, from real calls):")
        for kind, calls, seconds, cpm in measured_cost_by_kind(db):
            rate = f"{cpm:.2f}c/min" if cpm is not None else "n/a"
            print(f"  {kind}: {calls} calls, {seconds}s, {rate}")
    finally:
        db.close()
    return 1 if below_target else 0


if __name__ == "__main__":
    raise SystemExit(main())
