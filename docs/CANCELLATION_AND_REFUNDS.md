# Cancellation and refunds

Two separate actions. Cancelling never refunds, and a refund never happens because someone cancelled.

## Cancel plan (customer, organization owner)
`POST /api/v1/orgs/{org}/billing/cancel`
- Checks Paddle's own record first. If a cancellation is already scheduled it returns `already_scheduled` plus the
  confirmed `effective_at`, without calling Paddle to cancel again. A request that fails on our side is reported as
  success only if Paddle's record then shows the cancellation in effect.
- Otherwise it asks Paddle to cancel `effective_from: next_billing_period` (never `immediately`) and returns the
  effective date read back from Paddle.
- No refund call, no adjustment, no credit change.
- Our subscription row changes only through Paddle's webhook (`subscription_sync`): `cancel_effective_at` is set
  from `scheduled_change` and cleared if Paddle removes it. Status stays `active` until Paddle sends `canceled`,
  so entitlements, included minutes and purchased credits continue through the paid-through date under the normal
  usage limits. Paddle does not renew a subscription with a scheduled cancellation.
- Settings (BillingPanel) shows "Ends on <date>".

## Refunds (platform admin only)
`POST /api/v1/admin/refunds` `{transaction_id, basis, reason}` - `services/refunds.py`
- Only platform admins (`admin_users`). There is no customer route and no code path from cancel to refund.
- Eligibility: a written reason; a basis (`legal_requirement`, `provider_or_dispute`, `service_failure`,
  `billing_error`, `goodwill`); Paddle reports the transaction `completed`; it belongs to a subscription we know;
  one request per transaction.
- Deliberately NO time window and no "non-refundable" rule: refunds required by law or by Paddle's terms must stay
  possible. Whether a given customer is legally entitled is decided by the admin (basis `legal_requirement`), not
  guessed by code.
- The request row is stored before Paddle is called; every step is written to `audit_logs`.
- Provider confirmation: `approved` only when Paddle's adjustment says approved (response or the
  `adjustment.updated` webhook); until then `submitted`. A refusal by Paddle (4xx) is `failed`; a timeout or 5xx is
  `unconfirmed`. Both can be retried, but a retry first asks Paddle for an existing refund adjustment and adopts it
  rather than posting again (and does not post at all if Paddle cannot be asked). The row is locked during a retry.
  A `rejected` refund is final.
- Full refunds only. Partial refunds are done in the Paddle dashboard; they arrive as adjustment webhooks (top-up
  adjustments still claw credits back in `topups.apply_adjustment`).
- A refund does not cancel the subscription and does not change credits. Cancel separately if wanted.

### Operating procedure for an administrator (current policy, deliberately unchanged)
A refund returns money only. The customer keeps service, credits and their number until the subscription ends.
- Refund **and** end service: first cancel the subscription in the Paddle dashboard (or ask the owner to use Cancel plan; for an immediate stop use the Paddle dashboard), then request the refund. Service then ends when Paddle reports `canceled`; the number is released after `NUMBER_RELEASE_DELAY_DAYS`.
- Refund and keep service (goodwill, billing error): request the refund only.
- Top-up purchases (no subscription) cannot be refunded through this route; refund them in the Paddle dashboard (the adjustment webhook claws the credits back).
- A `rejected` refund is final in our records; use the Paddle dashboard.

## Legal / provider notes (to confirm, not legal advice)
- Paddle is the merchant of record: it sells to the customer, collects and remits tax, and its buyer terms and
  consumer-law obligations (statutory withdrawal rights, etc.) apply to the transaction. Confirm the current terms
  with Paddle and counsel before launch; the customer-facing refund policy page should match them.
- Retell/telephony cost is a separate provider cost and is never part of a customer refund flow.

## Not verified
Real Paddle sandbox behaviour has NOT been verified (no credentials in the build environment): the cancel
response and `scheduled_change` shape, the `/adjustments` request (`type: full`) and approval states, and the
`adjustment.updated` payload. Verify each in the sandbox before deploying; see the checklist in the handover.
