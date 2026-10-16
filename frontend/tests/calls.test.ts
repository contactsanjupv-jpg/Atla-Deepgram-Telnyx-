import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_FILTERS,
  PAGE_SIZE,
  activeFilterCount,
  addDays,
  callStatus,
  callerLabel,
  clampOffset,
  endedBecause,
  filterProblem,
  formatCallTime,
  formatDuration,
  formatTotalTime,
  localDate,
  pageInfo,
  phoneLabel,
  presetRange,
  searchTerm,
  sentimentView,
  toQuery,
  type CallFilterState,
} from "../src/lib/calls.ts";

// 2026-10-14 02:30 UTC is still Oct 13 in New York and already Oct 14 in Kolkata.
const NOW = new Date("2026-10-14T02:30:00Z");
const f = (over: Partial<CallFilterState> = {}): CallFilterState => ({ ...DEFAULT_FILTERS, ...over });

test("localDate follows the viewer's timezone, not UTC", () => {
  assert.equal(localDate(NOW, "America/New_York"), "2026-10-13");
  assert.equal(localDate(NOW, "Asia/Kolkata"), "2026-10-14");
  assert.equal(localDate(NOW, "Not/AZone"), "2026-10-14"); // bad zone falls back instead of throwing
});

test("addDays crosses month and year ends", () => {
  assert.equal(addDays("2026-10-14", -6), "2026-10-08");
  assert.equal(addDays("2026-03-01", -1), "2026-02-28");
  assert.equal(addDays("2026-12-31", 1), "2027-01-01");
});

test("presets are inclusive calendar-day ranges in the viewer's timezone", () => {
  assert.deepEqual(presetRange("today", "America/New_York", NOW), { from: "2026-10-13", to: "2026-10-13" });
  assert.deepEqual(presetRange("7d", "America/New_York", NOW), { from: "2026-10-07", to: "2026-10-13" });
  assert.deepEqual(presetRange("30d", "Asia/Kolkata", NOW), { from: "2026-09-15", to: "2026-10-14" });
  assert.deepEqual(presetRange("all", "UTC", NOW), {});
});

test("toQuery with default filters sends only paging, sort and timezone", () => {
  assert.deepEqual(toQuery(DEFAULT_FILTERS, "UTC", 0, NOW), { tz: "UTC", sort: "newest", limit: String(PAGE_SIZE), offset: "0" });
});

test("toQuery omits empties and maps every filter", () => {
  const q = toQuery(f({ q: "  roof   leak ", direction: "inbound", status: "ended", sentiment: "negative", lead: "with", sort: "longest", preset: "7d" }), "America/New_York", 50, NOW);
  assert.deepEqual(q, {
    tz: "America/New_York",
    sort: "longest",
    limit: String(PAGE_SIZE),
    offset: "50",
    q: "roof leak",
    date_from: "2026-10-07",
    date_to: "2026-10-13",
    direction: "inbound",
    status: "ended",
    sentiment: "negative",
    has_lead: "true",
  });
  assert.equal(toQuery(f({ lead: "without" }), "UTC", 0, NOW).has_lead, "false");
});

test("a search shorter than two characters is not sent", () => {
  assert.equal(searchTerm("a"), null);
  assert.equal(searchTerm("   "), null);
  assert.equal(searchTerm(" ab "), "ab");
  assert.equal("q" in toQuery(f({ q: "a" }), "UTC", 0, NOW), false);
});

test("custom dates pass through and allow one open end", () => {
  const both = toQuery(f({ preset: "custom", dateFrom: "2026-09-01", dateTo: "2026-09-30" }), "UTC", 0, NOW);
  assert.equal(both.date_from, "2026-09-01");
  assert.equal(both.date_to, "2026-09-30");
  const fromOnly = toQuery(f({ preset: "custom", dateFrom: "2026-09-01" }), "UTC", 0, NOW);
  assert.equal(fromOnly.date_from, "2026-09-01");
  assert.equal("date_to" in fromOnly, false);
  // custom dates left over from an earlier choice are ignored once another preset is picked
  assert.equal("date_from" in toQuery(f({ preset: "all", dateFrom: "2026-09-01" }), "UTC", 0, NOW), false);
});

test("filterProblem only flags a backwards custom range", () => {
  assert.equal(filterProblem(f({ preset: "custom", dateFrom: "2026-10-02", dateTo: "2026-10-01" })) !== null, true);
  assert.equal(filterProblem(f({ preset: "custom", dateFrom: "2026-10-01", dateTo: "2026-10-01" })), null);
  assert.equal(filterProblem(f({ preset: "custom", dateFrom: "2026-10-01" })), null);
  assert.equal(filterProblem(f({ preset: "7d", dateFrom: "2026-10-02", dateTo: "2026-10-01" })), null);
});

test("activeFilterCount counts narrowing filters, not sort", () => {
  assert.equal(activeFilterCount(DEFAULT_FILTERS), 0);
  assert.equal(activeFilterCount(f({ sort: "oldest" })), 0);
  assert.equal(activeFilterCount(f({ q: "x" })), 0);
  assert.equal(activeFilterCount(f({ q: "roof", preset: "30d", direction: "test", status: "error", sentiment: "positive", lead: "without" })), 6);
});

test("pageInfo and clampOffset", () => {
  assert.deepEqual(pageInfo(60, 0, 25, 25), { from: 1, to: 25, hasPrev: false, hasNext: true });
  assert.deepEqual(pageInfo(60, 50, 25, 10), { from: 51, to: 60, hasPrev: true, hasNext: false });
  assert.deepEqual(pageInfo(0, 0, 25, 0), { from: 0, to: 0, hasPrev: false, hasNext: false });
  assert.equal(clampOffset(75, 60, 25), 50);
  assert.equal(clampOffset(25, 60, 25), 25);
  assert.equal(clampOffset(50, 50, 25), 25);
  assert.equal(clampOffset(50, 0, 25), 0);
});

test("durations", () => {
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(65), "1:05");
  assert.equal(formatDuration(3725), "1:02:05");
  assert.equal(formatTotalTime(45), "45 sec");
  assert.equal(formatTotalTime(720), "12 min");
  assert.equal(formatTotalTime(3600), "1 h");
  assert.equal(formatTotalTime(11520), "3 h 12 min");
});

test("formatCallTime says Today / Yesterday in the viewer's timezone", () => {
  assert.equal(formatCallTime(null, "UTC", NOW), null);
  assert.equal(formatCallTime("garbage", "UTC", NOW), null);
  assert.match(formatCallTime("2026-10-14T01:00:00Z", "America/New_York", NOW) ?? "", /^Today, /); // Oct 13 evening there, "now" is also Oct 13
  assert.match(formatCallTime("2026-10-13T01:00:00Z", "America/New_York", NOW) ?? "", /^Yesterday, /);
  const older = formatCallTime("2026-10-01T15:00:00Z", "UTC", NOW) ?? "";
  assert.doesNotMatch(older, /Today|Yesterday/);
  assert.doesNotMatch(older, /2026/); // same year: no year shown
  assert.match(formatCallTime("2025-10-01T15:00:00Z", "UTC", NOW) ?? "", /2025/);
});

test("callerLabel", () => {
  assert.equal(callerLabel({ direction: "test", caller_number: null }), "Test call");
  assert.equal(callerLabel({ direction: "inbound", caller_number: null }), "Unknown caller");
  assert.notEqual(callerLabel({ direction: "inbound", caller_number: "+13055551234" }), "Unknown caller");
});

test("endedBecause maps known reasons and never hides unknown ones", () => {
  assert.equal(endedBecause(null), null);
  assert.equal(endedBecause("user_hangup"), "Caller hung up");
  assert.equal(endedBecause("error_llm_websocket_open"), "Technical problem");
  assert.equal(endedBecause("some_new_reason"), "Some new reason");
});

test("callStatus from the stored status and end reason", () => {
  const now = NOW.getTime();
  const c = (status: string, disconnect_reason: string | null = null, started_at: string | null = null) => ({ status, disconnect_reason, started_at });
  assert.deepEqual(callStatus(c("ended", "user_hangup"), now), { label: "Completed", tone: "ok" });
  assert.deepEqual(callStatus(c("ended", "voicemail_reached"), now), { label: "Voicemail", tone: "neutral" });
  assert.deepEqual(callStatus(c("ended", "dial_no_answer"), now), { label: "Not connected", tone: "warn" });
  assert.deepEqual(callStatus(c("ended", "dial_failed"), now), { label: "Failed", tone: "bad" });
  assert.deepEqual(callStatus(c("ended", "error_retell"), now), { label: "Failed", tone: "bad" });
  assert.deepEqual(callStatus(c("error"), now), { label: "Failed", tone: "bad" });
  assert.equal(callStatus(c("ongoing", null, "2026-10-14T02:00:00Z"), now).label, "In progress");
  assert.equal(callStatus(c("registered", null, "2026-10-14T02:00:00Z"), now).label, "Connecting");
  // never ended and older than 6 hours: say so instead of "in progress" forever
  assert.deepEqual(callStatus(c("ongoing", null, "2026-10-13T10:00:00Z"), now), { label: "No end recorded", tone: "warn" });
  assert.equal(callStatus(c("something_else"), now).label, "Unknown");
});

test("sentimentView stays silent when unknown", () => {
  assert.deepEqual(sentimentView("Positive"), { label: "Positive", tone: "ok" });
  assert.deepEqual(sentimentView("negative"), { label: "Negative", tone: "bad" });
  assert.deepEqual(sentimentView("Neutral"), { label: "Neutral", tone: "neutral" });
  assert.equal(sentimentView("Unknown"), null);
  assert.equal(sentimentView(null), null);
});

test("phoneLabel shows the stored number and never invents one", () => {
  assert.equal(phoneLabel({ direction: "inbound", caller_number: "+13055551234" }), "(305) 555-1234");
  assert.equal(phoneLabel({ direction: "inbound", caller_number: null }), "Not available");
  assert.equal(phoneLabel({ direction: "test", caller_number: null }), "None (browser test call)");
});
