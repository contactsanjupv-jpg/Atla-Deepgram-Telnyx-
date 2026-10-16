# Atla launch checklist (PASS / FAIL / NOT VERIFIED)

"PASS (automated)" = covered by tests against fakes only, run on `atla_test`. It is not proof of live-provider behaviour.

| Area | Status | Evidence / remaining blocker |
|---|---|---|
| Signup, org, tenant isolation, route auth | PASS (automated) | test_auth, test_tenant_isolation; route scan: every org route enforces membership |
| Receptionist configuration | PASS (automated) | test_agent_config |
| Paddle checkout + subscription sync | PASS (automated) / NOT VERIFIED live | Needs sandbox credentials: docs/PADDLE_SANDBOX_TEST.md |
| Paddle webhook signature, dedupe, replay | PASS (automated) | test_webhook_signature, test_billing |
| Cancel at period end, repeat cancel, UI date | PASS (automated) / NOT VERIFIED live | sandbox step 3-5 |
| Refund lifecycle, duplicate protection, timeout recovery | PASS (automated) / NOT VERIFIED live | sandbox steps 6-9 |
| Migration a9c1e3f52d84 | PASS on fresh + prior-head DBs / NOT VERIFIED on target DB | run `alembic upgrade head` on a copy of production first |
| Auto number provisioning (idempotent, blocked state) | PASS (automated) / NOT VERIFIED live | Retell account needs payment method; HTTP 402 assumed |
| Activation only after verified assignment | PASS (automated) | test_phone_lifecycle |
| Inbound call, lead, usage, credits | PASS (automated) / NOT VERIFIED live | docs/RETELL_VERIFICATION.md (steps 3, 7 cost money) |
| Retell webhook signature | PASS | forged-with-empty-key fixed + regression tests |
| Usage caps, overage, lapse suspension | PASS (automated) | test_entitlements, test_credits_ledger |
| Production config guard (https, secrets present) | PASS | tests/test_production_config.py; fails at start-up |
| CORS, cookies, headers, docs disabled in prod | PASS (code review) | single origin, httponly, secure in prod |
| Monitoring: readiness, alerts, worker heartbeat, push | PASS (automated) / NOT CONFIGURED | docs/MONITORING.md: you must add an uptime monitor, alert webhook, MONITORING_TOKEN, worker supervisor |
| Error tracking (Sentry etc.) | NOT DONE | not integrated |
| SMTP / Redis / HTTPS / worker on the real deployment | NOT VERIFIED | startup guard checks presence only; verify on deploy |
| Lapse policies (3 days / 7 days) | TEMPORARY | docs/LAPSE_POLICIES.md - business decision pending |
| Frontend production build | PASS (you, on your Mac) | no frontend changes since |
