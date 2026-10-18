# Production monitoring (minimal)

Three layers. None needs a vendor SDK; two need an account you create. **Nothing is "configured" until you do the setup below.**

| Layer | What it catches | Needs |
|---|---|---|
| `GET /health/ready` polled by an external uptime monitor | API down, database down, Redis down, **worker stopped or stalled** (see Worker states) | A free account at UptimeRobot / Better Stack / similar; alert to email or phone |
| Worker push to `ALERT_WEBHOOK_URL` | Business conditions (below), each announced once per 6 h | A Slack/Discord/ntfy incoming-webhook URL |
| `GET /health/alerts` (bearer `MONITORING_TOKEN`) | The same conditions on demand, counts only | Set `MONITORING_TOKEN` (any long random string); unset = endpoint disabled (404) |

The worker cannot report its own death, so the **external** monitor on `/health/ready` is the one that matters most. Do **not** use `/health/ready` as a load-balancer or orchestrator health check for the API: a stopped worker would take the API out of rotation. Pushed alerts also need Redis; during a Redis outage only the external monitor sees problems.
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


## Worker states (`/health/ready` -> `worker_state`)
The worker records: the last completed pass; a liveness beat from a **watchdog thread**; and a phase marker with its **own time budget** while the main loop runs a pass (`WORKER_PASS_BUDGET_SECONDS`, default 300).

| State | Meaning | Ready (200)? |
|---|---|---|
| `ok` | A pass finished in the last 120 s | yes |
| `busy` | Process alive (liveness < 45 s), a previous pass exists, and the current pass is inside its budget | yes |
| `starting` | Process alive, no pass completed since it started, under 420 s | **no** (not operational yet), no alert |
| `stalled` | Process alive but no progress: stale pass with no valid phase, a pass over its budget, or startup over 420 s | no, `worker_stopped` alert |
| `stopped` | No liveness beat for 45 s | no, `worker_stopped` alert |

**The liveness thread cannot hide a stuck loop.** The watchdog (`app/workers/watchdog.py`) beats only while the main loop is within its budget (or within 75 s of silence between passes). When a pass overruns, the beat stops (readiness turns `stalled`), the reason is recorded, and the process exits with code 70 so the supervisor restarts it. The next worker announces it with a `worker_watchdog_restart` warning. `WORKER_WATCHDOG_EXIT=false` disables the exit (the beat still stops). A pass from before a restart is ignored, so a restarted worker is never ready before it completes its own first pass.

**Requires a supervisor that restarts on exit** (docker `restart: always`, systemd `Restart=always`); without one a watchdog exit is an outage until someone restarts the worker.

## Slow jobs run beside the loop, not in it
Voice-catalog sync and Paddle subscription reconciliation run in their own threads (`app/workers/background_jobs.py`), so they can never delay event retries, provisioning, entitlement enforcement, top-up settlement or alerts, and never affect readiness.
- One thread per job (no self-overlap); a Redis lock per job (no overlap across workers); the "next due" key is re-checked after taking the lock.
- Cadence: `VOICE_SYNC_INTERVAL_SECONDS` and `BILLING_RECONCILE_INTERVAL_SECONDS` (default 6 h each); after a failure the reconciliation retries after `BILLING_RECONCILE_RETRY_SECONDS` (15 min), voice sync after 5 min.
- Reconciliation runs in apply mode, through the same stale-safe, idempotent code as the webhook: it only updates subscriptions we already hold, reads Paddle's own record over the authenticated API, skips a repair when local state is newer, ignores a snapshot with a different subscription id, and never touches a subscription that is missing at Paddle (counted and alerted for manual review). It publishes counts only.
- Alerts: `billing_drift_unresolved` (drift that could not be repaired, missing at Paddle, or duplicate active subscriptions), `billing_drift_repaired` (warning: a webhook was probably lost), `billing_reconcile_blind` (could not read any subscription: wrong environment or key), `job_failing_*` (2+ consecutive failures), `job_overdue_*`, `job_stalled_*`, plus `config_mismatch` (API and worker resolved different environment settings).
- Trade-off: a separate worker process would isolate a wedged driver call completely but needs a second supervisor entry. Threads were chosen; the lock TTL, hard budgets and alerts bound the risk. A wedged job thread cannot be killed, but it blocks nothing except its own next run.

## Alert delivery
A webhook response outside 2xx (including 429 and 5xx), a timeout (5 s) or a network error counts as failed: it is logged by status/exception class only (never the URL) and the dedupe key is removed, so the alert is retried on the next sweep instead of being silenced for 6 hours. Failures never crash the worker pass.
