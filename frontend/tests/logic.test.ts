import test from "node:test";
import assert from "node:assert/strict";
import { validateTopup, quickAmounts, creditView, creditBanner, chartBars } from "../src/lib/credits.ts";
import { planHighlights } from "../src/lib/planCompare.ts";
import type { CreditsOut, PlanOut } from "../src/lib/api.ts";

const base: CreditsOut = { entitled: true, plan_name: "Starter", period_start: "x", period_end: "y", included_granted_seconds: 10800, included_used_seconds: 0, included_remaining_seconds: 10800, purchased_balance_seconds: 0, available_seconds: 10800, status: "ok", calls_handled: 0, topup_min_minor: 2000, topup_max_minor: 50000, topup_cents_per_minute: 45, free_test_calls_remaining: 0, when_credits_run_out: "" };

test("top-up minimum $20 and formats", () => {
  assert.equal(validateTopup("19.99", 2000, 50000).ok, false);
  assert.deepEqual(validateTopup("20", 2000, 50000), { ok: true, amountMinor: 2000 });
  assert.deepEqual(validateTopup("$1,250.00", 2000, 500000), { ok: true, amountMinor: 125000 });
  assert.equal(validateTopup("abc", 2000, 50000).ok, false);
  assert.equal(validateTopup("", 2000, 50000).ok, false);
  assert.equal(validateTopup("500.01", 2000, 50000).ok, false);
});
test("quick amounts derive from server minimum", () => {
  assert.deepEqual(quickAmounts(2000, 50000), [2000, 4000, 10000]);
  assert.deepEqual(quickAmounts(2000, 5000), [2000, 4000]);
});
test("credit states", () => {
  assert.equal(creditBanner(base), null);
  assert.equal(creditBanner({ ...base, status: "low" })?.tone, "low");
  assert.equal(creditBanner({ ...base, status: "empty", available_seconds: 0 })?.tone, "empty");
  assert.equal(creditBanner({ ...base, entitled: false, status: "empty" }), null);
  assert.equal(creditView({ ...base, entitled: false, included_granted_seconds: 0 }).kind, "unmetered");
  const v = creditView({ ...base, purchased_balance_seconds: -120 });
  assert.equal(v.kind === "metered" && v.owes, "2 min");
});
test("chart bars scale to real max, zeros are empty", () => {
  const pts = [{ date: "2026-10-01", calls: 0, credit_seconds: 0 }, { date: "2026-10-02", calls: 4, credit_seconds: 0 }, { date: "2026-10-03", calls: 2, credit_seconds: 0 }];
  const r = chartBars(pts, (p) => p.calls);
  assert.deepEqual(r.bars.map((b) => b.height), [0, 1, 0.5]);
  assert.equal(chartBars(pts, (p) => p.credit_seconds).empty, true);
});
test("plan comparison differentiates Growth from Starter using catalog data", () => {
  const mk = (id: string, features: string[], mins: number): PlanOut => ({ id, name: id, features: features.map((f) => ({ id: f, label: f })), included_minutes: mins, baseline: [], when_credits_run_out: "", price: { amount_minor: id === "starter" ? 7900 : 19900, currency: "USD", interval: "month", interval_count: 1 } });
  const h = planHighlights([mk("starter", ["a"], 180), mk("growth", ["a", "transcripts"], 480)]);
  assert.equal(h[1].recommended, true);
  assert.equal(h[1].extraMinutes, 300);
  assert.equal(h[1].added.length, 1);
});
