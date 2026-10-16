# Production monitoring (minimal)

Three layers. None needs a vendor SDK; two need an account you create. **Nothing is "configured" until you do the setup below.**

| Layer | What it catches | Needs |
|---|---|---|
| `GET /health/ready` polled by an external uptime monitor | API down, database down, Redis down, **worker stopped** (no heartbeat for 120 s) | A free account at UptimeRobot / Better Stack / similar; alert to email or phone |
| Worker push to `ALERT_WEBHOOK_URL` | Business conditions (below), each announced once per 6 h | A Slack/Discord/ntfy incoming-webhook URL |
| `GET /health/alerts` (bearer `MONITORING_TOKEN`) | The same conditions on demand, counts only | Set `MONITORING_TOKEN` (any long random string); unset = endpoint disabled (404) |

The worker cannot report its own death, so the **external** monitor on `/health/ready` is the one that matters most.
Also run the worker under a supervisor that restarts it (docker `restart: always`, systemd `Restart=always`).

## Alert conditions
| Key | Meaning | First action |
|---|---|---|
| `worker_stopped` / `worker_failing` | No pass for 120 s / 3+ consecutive failing passes | Check worker logs for `Worker pass failed` / `sweep failed` |
| `webhook_rejected_paddle` / `_retell` | 10+ bad signatures in an hour: wrong secret or key, so genuine events are being rejected | Compare `PADDLE_WEBHOOK_SECRET` with the endpoint secret in Paddle; check `RETELL_API_KEY` |
| `webhook_failed_paddle` | 3+ Paddle events failed to process in an hour (Paddle retries) | Logs: `Paddle webhook processing failed` |
| `webhook_dead` / `webhook_stuck` | Events gave up after 8 attempts / sit unprocessed over 15 min | Inspect `webhook_events.last_error`; fix cause; re-queue |
| `provisioning_stuck` | A number purchase has been in progress over 15 min | Worker reconciles; if persistent, check Retell |
| `provisioning_blocked_payment` | A paying customer has no number: **Retell account needs a payment method or balance** | Fund Retell; the worker retries within 30 min |
| `provisioning_failed` | A purchase failed (not payment) | Log line `Number purchase failed ... status=`; see docs/RETELL_VERIFICATION.md |
| `refund_stuck` | Refund `requested`/`unconfirmed` over 15 min | Re-submit from the admin refund route; it reconciles with Paddle first |
| `refund_pending_approval` | Submitted refund waiting 48 h for Paddle approval | Check Paddle dashboard |

Database or Redis failure: `/health/ready` returns 503 with booleans only.

## Logging rules
Alert logs are `ALERT [severity] key count=N`: counts and categories only. No secrets, webhook URLs, customer ids, payment
data or call content. Tests assert the alert webhook URL never appears in logs.

## Not configured / not verified
Uptime monitor account, alert webhook, `MONITORING_TOKEN`, worker supervisor and log aggregation are all yours to set up.
Error-tracking (e.g. Sentry) is not integrated.
