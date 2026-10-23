import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { SubscriptionOut } from "../src/lib/api.ts";
import { describeSubscription } from "../src/lib/billingState.ts";
import { cancelNotice, cancelPhase, createCancelController, PENDING_STALE_AFTER_MS, type CancelResponse } from "../src/lib/cancelFlow.ts";

const NOW = Date.parse("2026-10-23T12:00:00Z");
const sub = (over: Partial<SubscriptionOut> = {}): SubscriptionOut => ({ plan_id: "starter", plan_name: "Starter", status: "active", entitled: true, features: [], current_period_start: "2026-10-07T00:00:00Z", current_period_end: "2026-11-07T00:00:00Z", cancel_effective_at: null, cancel_requested_at: null, service_ends_at: null, ...over });
const describe = (e: unknown) => (e instanceof Error ? e.message : "x");
const END = "2026-11-07T00:00:00Z";
const REQ = "2026-10-23T11:59:00Z";

/** A realistic mocked API: the server keeps the subscription row; reloading = building a fresh controller + reading GET. */
function server(opts: { webhook?: boolean } = {}) {
  const row = sub();
  const calls = { post: 0, get: 0 };
  let failGet = false;
  const api = {
    cancel: async (): Promise<CancelResponse> => {
      calls.post++;
      if (!row.cancel_effective_at && !row.cancel_requested_at) row.cancel_requested_at = REQ; // durable marker
      return { status: row.cancel_effective_at ? "already_scheduled" : "requested", effective_at: null };
    },
    get: async (): Promise<SubscriptionOut> => {
      calls.get++;
      if (failGet) throw new Error("network");
      return { ...row };
    },
  };
  return {
    row, calls, api,
    failGets: (v: boolean) => (failGet = v),
    webhookArrives: () => { row.cancel_effective_at = END; row.cancel_requested_at = null; },
    controller: () => createCancelController({ cancel: api.cancel, refresh: api.get }),
    opts,
  };
}

test("the exact reported sequence: 202 requested, GET still active, reload keeps 'awaiting confirmation' and blocks a second POST", async () => {
  const s = server();
  const first = s.controller();
  const r = await first.submit();
  assert.ok(r);
  assert.equal(r.outcome.kind, "accepted");
  assert.equal(s.calls.post, 1);
  // first GET: still active, no cancel_effective_at, but the SERVER marks it requested
  assert.equal(r.sub?.cancel_effective_at, null);
  assert.equal(cancelPhase(r.sub, true, NOW), "pending");
  const n1 = cancelNotice(r.outcome, r.sub, "pending", describe)!;
  assert.equal(n1.title, "Cancellation requested, awaiting confirmation");
  assert.doesNotMatch(n1.body, /ends on|scheduled/i);

  // ---- page reload: brand-new controller and no session memory ----
  const reloaded = await s.api.get();
  const phase = cancelPhase(reloaded, false, NOW);
  assert.equal(phase, "pending");
  const n2 = cancelNotice(null, reloaded, phase, describe)!;
  assert.equal(n2.title, "Cancellation requested, awaiting confirmation");
  const info = describeSubscription(reloaded, phase === "pending");
  assert.equal(info.label, "Cancellation requested");
  assert.notEqual(info.label, "Active");
  assert.doesNotMatch(info.headline, /ends on/i);
  const canCancel = reloaded.entitled && !reloaded.cancel_effective_at && phase === "none";
  assert.equal(canCancel, false); // Cancel plan stays hidden after reload
  assert.equal(s.calls.post, 1); // and no further POST was made

  // ---- later GET: the server confirms ----
  s.webhookArrives();
  const confirmed = await s.api.get();
  assert.equal(cancelPhase(confirmed, false, NOW), "confirmed");
  const done = describeSubscription(confirmed, false);
  assert.equal(done.label, "Ending");
  assert.match(done.headline, /ends on/i);
  assert.equal(confirmed.status, "active");
});

test("the cancel response's own date is never treated as confirmation", () => {
  const phase = cancelPhase(sub({ cancel_requested_at: REQ }), true, NOW);
  assert.equal(phase, "pending");
  assert.equal(describeSubscription(sub({ cancel_requested_at: REQ }), true).label, "Cancellation requested");
});

test("confirmed iff the server sets cancel_effective_at; the confirmed notice shows that date", async () => {
  const confirmedSub = sub({ cancel_effective_at: END });
  assert.equal(cancelPhase(confirmedSub, false, NOW), "confirmed");
  assert.equal(cancelPhase(sub(), false, NOW), "none");
  const n = cancelNotice({ kind: "accepted", alreadyScheduled: false, refreshFailed: false }, confirmedSub, "confirmed", describe)!;
  assert.equal(n.tone, "ok");
  assert.match(n.body, /2026/);
  assert.match(n.body, /doesn't refund/);
  assert.equal(cancelNotice({ kind: "accepted", alreadyScheduled: true, refreshFailed: false }, confirmedSub, "confirmed", describe)!.title, "Your cancellation was already scheduled");
});

test("refresh failure after acceptance keeps the accepted request, never claims completion and never reverts to Active", async () => {
  const s = server();
  s.failGets(true);
  const r = await s.controller().submit();
  assert.ok(r);
  assert.deepEqual(r.outcome, { kind: "accepted", alreadyScheduled: false, refreshFailed: true });
  assert.equal(r.sub, null);
  const phase = cancelPhase(null, true, NOW); // no server data; session knows it was accepted
  assert.equal(phase, "none"); // no sub to judge -> panel keeps the previous sub, below
  const prev = sub();
  const p2 = cancelPhase(prev, true, NOW); // previously loaded sub still says active, but the request was accepted
  assert.equal(p2, "pending");
  const n = cancelNotice(r.outcome, prev, p2, describe)!;
  assert.match(n.body, /couldn't refresh/);
  assert.doesNotMatch(n.title, /scheduled/i);
  assert.equal(describeSubscription(prev, true).label, "Cancellation requested");
});

test("a pending request that never confirms becomes a persistent 'still not confirmed' state with safe recovery", () => {
  const old = sub({ cancel_requested_at: new Date(NOW - PENDING_STALE_AFTER_MS - 1000).toISOString() });
  const phase = cancelPhase(old, false, NOW);
  assert.equal(phase, "stale");
  const n = cancelNotice(null, old, phase, describe)!;
  assert.equal(n.action, "retry");
  assert.match(n.body, /still active/);
  assert.match(n.body, /can't cancel twice/);
  assert.equal(describeSubscription(old, true).label, "Cancellation requested");
  // retry re-opens exactly one new submission
  const calls = { n: 0 };
  const c = createCancelController({ cancel: async () => { calls.n++; return { status: "already_scheduled", effective_at: null }; }, refresh: async () => old });
  return c.submit().then(async (r1) => {
    assert.ok(r1);
    assert.equal(await c.submit(), null);
    c.allowRetry();
    assert.ok(await c.submit());
    assert.equal(calls.n, 2);
  });
});

test("unsuccessful cancel request: error notice, nothing recorded as accepted, retry possible", async () => {
  let fail = true;
  const posts = { n: 0 };
  const c = createCancelController({
    cancel: async () => { posts.n++; if (fail) throw new Error("We couldn't cancel right now. Please try again."); return { status: "requested", effective_at: null }; },
    refresh: async () => sub({ cancel_requested_at: REQ }),
  });
  const r = await c.submit();
  assert.equal(r?.outcome.kind, "error");
  const n = cancelNotice(r!.outcome, sub(), "none", describe)!;
  assert.equal(n.tone, "bad");
  assert.match(n.body, /couldn't cancel/);
  fail = false;
  assert.equal((await c.submit())?.outcome.kind, "accepted");
  assert.equal(posts.n, 2);
});

test("duplicate submissions while in flight send exactly one request; none after acceptance", async () => {
  let release!: () => void;
  const gate = new Promise<void>((res) => { release = res; });
  const posts = { n: 0 };
  const c = createCancelController({ cancel: async () => { posts.n++; await gate; return { status: "requested", effective_at: null }; }, refresh: async () => sub({ cancel_requested_at: REQ }) });
  const first = c.submit();
  assert.equal(c.busy, true);
  assert.equal(await c.submit(), null);
  assert.equal(await c.submit(), null);
  release();
  assert.ok(await first);
  assert.equal(await c.submit(), null);
  assert.equal(posts.n, 1);
});

test("an already-scheduled cancellation on load shows Ending with the server date and no Cancel button", () => {
  const s = sub({ cancel_effective_at: END });
  assert.equal(describeSubscription(s, false).label, "Ending");
  assert.equal(cancelPhase(s, false, NOW), "confirmed");
  assert.equal(cancelNotice(null, s, "confirmed", describe), null); // no banner on a plain reload; header says it
});

test("panel wiring", () => {
  const src = readFileSync(fileURLToPath(new URL("../src/components/billing/BillingPanel.tsx", import.meta.url)), "utf8");
  assert.match(src, /createCancelController/);
  assert.match(src, /"Cancelling…"/);
  assert.match(src, /data-testid="cancel-notice"/);
  assert.match(src, /phase === "none"/);
  assert.doesNotMatch(src, /localStorage|sessionStorage/); // server state is the source of truth
});
