import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_LEAD_FILTERS, PAGE_SIZE, callBlurb, isLeadStatus, leadName, leadPhone, leadsNarrowed, telHref, toLeadQuery } from "../src/lib/leads.ts";

test("default filters send only sort and paging", () => {
  assert.deepEqual(toLeadQuery(DEFAULT_LEAD_FILTERS, 0), { sort: "newest", limit: String(PAGE_SIZE), offset: "0" });
});

test("search and status are sent only when they mean something", () => {
  assert.deepEqual(toLeadQuery({ q: "  maria   lopez ", status: "booked", sort: "oldest" }, 25), {
    sort: "oldest",
    limit: String(PAGE_SIZE),
    offset: "25",
    q: "maria lopez",
    status: "booked",
  });
  assert.equal("q" in toLeadQuery({ ...DEFAULT_LEAD_FILTERS, q: "a" }, 0), false); // under two characters
});

test("only search or a status counts as narrowing, not sort", () => {
  assert.equal(leadsNarrowed(DEFAULT_LEAD_FILTERS), false);
  assert.equal(leadsNarrowed({ ...DEFAULT_LEAD_FILTERS, sort: "oldest" }), false);
  assert.equal(leadsNarrowed({ ...DEFAULT_LEAD_FILTERS, q: "ab" }), true);
  assert.equal(leadsNarrowed({ ...DEFAULT_LEAD_FILTERS, status: "new" }), true);
});

test("a missing name is never replaced by a guess", () => {
  assert.equal(leadName({ name: null }), "Unnamed lead");
  assert.equal(leadName({ name: "   " }), "Unnamed lead");
  assert.equal(leadName({ name: " Maria Lopez " }), "Maria Lopez");
});

test("phone display and tel links", () => {
  assert.equal(leadPhone(null), null);
  assert.equal(leadPhone("+13055551234"), "(305) 555-1234");
  assert.equal(telHref("+13055551234"), "tel:+13055551234");
  assert.equal(telHref("(305) 555-1234"), "tel:3055551234");
  assert.equal(telHref(null), null);
  assert.equal(telHref("--"), null);
});

test("the linked-call line uses only stored values", () => {
  assert.equal(callBlurb({ call_id: null, call_direction: null, call_duration_seconds: null }), null);
  assert.equal(callBlurb({ call_id: "c", call_direction: "inbound", call_duration_seconds: 95 }), "Inbound call · 1:35");
  assert.equal(callBlurb({ call_id: "c", call_direction: "outbound", call_duration_seconds: null }), "Outbound call");
  assert.equal(callBlurb({ call_id: "c", call_direction: "test", call_duration_seconds: 5 }), "Test call · 0:05");
});

test("isLeadStatus", () => {
  assert.equal(isLeadStatus("booked"), true);
  assert.equal(isLeadStatus("weird"), false);
});
