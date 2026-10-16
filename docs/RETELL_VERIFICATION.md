# Retell: cheapest safe verification sequence

Status: **NOT VERIFIED**. No Retell credentials in this environment. Nothing below has been run. Do not run billable steps without explicit approval.

## Account requirement
The Retell account that owns `RETELL_API_KEY` needs a **payment method or prepaid balance** before it can buy a phone number
(provider cost, separate from customer payments in Paddle). Without it the application records the purchase as
`blocked_payment` and shows "blocked", never "live".

## Sequence (cost low to high)
| Step | Charge? | What it proves |
|---|---|---|
| 1. Set `RETELL_API_KEY`; `GET /list-phone-numbers` style read via the app's `find_number_by_nickname` | none | key valid, API reachable |
| 2. Create agent + LLM (activation of the receptionist configuration, no number) | none to minimal | agent config accepted |
| 3. Free browser test call (one allowed per org/user) | small per-minute call cost (about 18 c/min basis) | call webhooks arrive, signature valid, call + usage rows written |
| 4. With NO payment method: trigger purchase | none (rejected) | record the real HTTP status/body; confirm `blocked_payment` or `failed`; fix `PAYMENT_REQUIRED` if not 402 |
| 5. Add payment method; trigger purchase once | **buys a real number (about $2/month plus any setup fee)** | provisioning, nickname look-up, idempotent retry (replay must not buy again) |
| 6. Assign agent, activate | included | `get_number` shows inbound agent = ours before anything is shown live |
| 7. Place one inbound call to the number | **billable call minutes (+ carrier)** | call_started/ended/analyzed, lead capture, usage and credits |
| 8. Release the number afterwards | stops the monthly cost | release path |

Steps 5 and 7 cost money and need your explicit approval.

## Honesty guarantees already in code (tested with fakes)
- "Live" only after the provider confirms the number is assigned to the agent (`activation`).
- Provisioning failure states are shown as blocked/failed with no live or refund claim (`GET .../phone-numbers/provisioning`).
- A replayed Paddle webhook cannot buy a second number.
