import test from "node:test";
import assert from "node:assert/strict";
import type { AgentOut, CreditsOut, PhoneNumberOut, SubscriptionOut } from "../src/lib/api.ts";
import type { ReceptionistStatus } from "../src/lib/receptionistStatus.ts";
import { compare, hourLabel, attentionItems, relativeTime, hasAnyActivity, RANGES } from "../src/lib/dashboard.ts";

const NOW = Date.parse("2026-10-12T12:00:00Z");
const sub = (over: Partial<SubscriptionOut> = {}): SubscriptionOut => ({ plan_id: "starter", plan_name: "Starter", status: "active", entitled: true, features: [], current_period_start: null, current_period_end: "2026-11-01T00:00:00Z", cancel_effective_at: null, service_ends_at: null, ...over });
const agent: AgentOut = { id: "a", name: "Ava", status: "active", retell_agent_id: "r", synced: true };
const phone: PhoneNumberOut = { id: "p", number: "+13055551234", area_code: "305", country: "US", status: "active" };
const credits = (over: Partial<CreditsOut> = {}): CreditsOut => ({ entitled: true, plan_name: "Starter", period_start: null, period_end: null, included_granted_seconds: 10800, included_used_seconds: 4800, included_remaining_seconds: 6000, purchased_balance_seconds: 0, available_seconds: 6000, status: "ok", calls_handled: 0, topup_min_minor: 2000, topup_max_minor: 50000, topup_cents_per_minute: 45, free_test_calls_remaining: 0, when_credits_run_out: "", ...over });
const base: Parameters<typeof attentionItems>[0] = { status: null, sub: sub(), agent, phone, credits: credits(), awaitingLeads: 0, oldestAwaitingAt: null, now: NOW };

test("ranges are Today / 7 days / 30 days and say what they are compared with", () => {
  assert.deepEqual(RANGES.map((r) => r.id), ["today", "7d", "30d"]);
  assert.equal(RANGES[0].compareTo, "yesterday");
});

test("comparison never invents a percentage", () => {
  assert.equal(compare(0, 0, "yesterday").direction, "none");
  assert.deepEqual(compare(5, 0, "yesterday"), { direction: "new", text: "None yesterday" });
  assert.equal(compare(5, 0, "the previous 7 days").text, "None in the previous 7 days");
  assert.deepEqual(compare(12, 10, "the previous 7 days"), { direction: "up", text: "+20% vs the previous 7 days" });
  assert.deepEqual(compare(5, 10, "yesterday"), { direction: "down", text: "−50% vs yesterday" });
  assert.equal(compare(10, 10, "yesterday").direction, "flat");
  assert.equal(compare(0, 4, "yesterday").text, "−100% vs yesterday");
});

test("hour labels", () => {
  assert.equal(hourLabel("2026-10-12T00:00:00"), "12 AM");
  assert.equal(hourLabel("2026-10-12T12:00:00"), "12 PM");
  assert.equal(hourLabel("2026-10-12T14:00:00"), "2 PM");
  assert.equal(hourLabel("2026-10-12T23:00:00"), "11 PM");
});

test("an all-zero series is not activity", () => {
  assert.equal(hasAnyActivity([{ start: "x", calls: 0, call_seconds: 0, leads: 0 }]), false);
  assert.equal(hasAnyActivity([{ start: "x", calls: 0, call_seconds: 0, leads: 1 }]), true);
});

test("a healthy, fully set up account has nothing to do", () => {
  const liveStatus: ReceptionistStatus = { state: "live", label: "Live", detail: "x", tone: "ok", action: null };
  assert.deepEqual(attentionItems({ ...base, status: liveStatus }), []);
});

test("a receptionist that is not live yet surfaces its next step", () => {
  const status: ReceptionistStatus = { state: "ready", label: "Ready to go live", detail: "x", tone: "neutral", action: { label: "Go live", href: "/dashboard/go-live" } };
  const items = attentionItems({ ...base, status });
  assert.equal(items[0].id, "receptionist");
  assert.equal(items[0].cta.href, "/dashboard/go-live");
});

test("billing trouble is shown once, not twice", () => {
  const status: ReceptionistStatus = { state: "billing", label: "Not answering calls", detail: "Your payment is overdue.", tone: "bad", action: { label: "Fix billing", href: "/dashboard/settings" } };
  const items = attentionItems({ ...base, status, sub: sub({ status: "past_due", entitled: false, service_ends_at: "2026-10-01T00:00:00Z" }) });
  assert.equal(items.filter((i) => i.cta.href === "/dashboard/settings").length, 1);
  assert.equal(items[0].tone, "bad");
});

test("a plan about to end is flagged when the receptionist itself is fine", () => {
  const status: ReceptionistStatus = { state: "live", label: "Live", detail: "x", tone: "ok", action: null };
  const items = attentionItems({ ...base, status, sub: sub({ cancel_effective_at: "2026-10-20T00:00:00Z" }) });
  assert.equal(items.length, 1);
  assert.equal(items[0].id, "plan");
});

test("low and empty credits are flagged; empty outranks low and waiting leads", () => {
  const status: ReceptionistStatus = { state: "live", label: "Live", detail: "x", tone: "ok", action: null };
  const low = attentionItems({ ...base, status, credits: credits({ status: "low" }) });
  assert.equal(low[0].id, "credits");
  assert.equal(low[0].tone, "warn");
  const empty = attentionItems({ ...base, status, credits: credits({ status: "empty", available_seconds: 0 }), awaitingLeads: 2 });
  assert.equal(empty[0].tone, "bad");
  assert.equal(empty[0].id, "credits");
  assert.equal(empty[1].id, "leads");
  assert.equal(attentionItems({ ...base, status, credits: credits({ status: "low", entitled: false }) }).length, 0);
});

test("waiting leads: singular/plural, and only old ones are a warning", () => {
  const status: ReceptionistStatus = { state: "live", label: "Live", detail: "x", tone: "ok", action: null };
  const fresh = attentionItems({ ...base, status, awaitingLeads: 1, oldestAwaitingAt: "2026-10-12T10:00:00Z" })[0];
  assert.equal(fresh.title, "1 lead is waiting for a follow-up");
  assert.equal(fresh.tone, "neutral");
  const old = attentionItems({ ...base, status, awaitingLeads: 3, oldestAwaitingAt: "2026-10-09T10:00:00Z" })[0];
  assert.equal(old.title, "3 leads are waiting for a follow-up");
  assert.equal(old.tone, "warn");
});

test("a paying customer with no number is told, one without a plan is not nagged about a number", () => {
  const status: ReceptionistStatus = { state: "live", label: "Live", detail: "x", tone: "ok", action: null };
  assert.equal(attentionItems({ ...base, status, phone: null }).some((i) => i.id === "phone"), true);
  assert.equal(attentionItems({ ...base, status, phone: null, sub: null }).some((i) => i.id === "phone"), false);
  assert.equal(attentionItems({ ...base, status, phone: null, agent: null }).some((i) => i.id === "phone"), false);
});

test("unknown data produces no claims", () => {
  assert.deepEqual(attentionItems({ ...base, sub: undefined, credits: null }), []);
});

test("relative time", () => {
  assert.equal(relativeTime(null, NOW), null);
  assert.equal(relativeTime("nonsense", NOW), null);
  assert.equal(relativeTime("2026-10-12T11:59:40Z", NOW), "Just now");
  assert.equal(relativeTime("2026-10-12T11:30:00Z", NOW), "30 min ago");
  assert.equal(relativeTime("2026-10-12T09:00:00Z", NOW), "3 h ago");
});