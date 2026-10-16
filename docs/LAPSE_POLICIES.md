# Lapse policies - temporary defaults awaiting a business decision

Kept as set: `PAST_DUE_GRACE_DAYS=3`, `NUMBER_RELEASE_DELAY_DAYS=7` (config.py; env-overridable). Not changed.
Figures below come from the config values (cost basis 18 c/min, number 200 c/month) and are estimates, not invoices.

## PAST_DUE_GRACE_DAYS (3)
Payment failed; Paddle retries. Service stays on for this many days from the day the subscription became `past_due`;
after it the receptionist is paused and the number unassigned (calls stop being answered).
- Shorter (e.g. 0-1): less unpaid usage, but a card hiccup silences a live business line quickly and customers churn.
- Longer (e.g. 7-14): friendlier, but you carry unpaid exposure. No new monthly allowance is granted while `past_due`
  (grants need an active subscription), so exposure is bounded by minutes already granted plus the overage grace
  (`OVERAGE_GRACE_MINUTES=30` = about $5.40 at 18 c/min). The longer grace mostly extends how long those remaining minutes stay usable.
- Paddle's own retry schedule decides how long a card keeps failing; grace should be at least as long as its first retries.

## NUMBER_RELEASE_DELAY_DAYS (7)
After suspension the number is kept this long, then **released**. Releasing stops the $2/month provider cost but the
number is gone: a returning customer is given a different number (their printed/advertised number is lost).
- Cost of keeping: about $0.07 per suspended customer per day (about $0.47 for 7 days; $2.00 for 30).
- Shorter: saves little money, raises the chance a customer loses their public number over a short lapse.
- Longer (14-30 days): costs up to $2 per lapsed customer per 30 days; protects the customer's number and win-back.
- Releasing is not reversible from our side.

## Decide before production
1. Grace days for failed payments. 2. How long to hold a suspended customer's number. 3. Whether customers are told (email) when service is about to pause.
There is currently no email notification for `past_due` (only the dashboard banner).
