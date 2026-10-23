import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { api, ApiError } from "../src/lib/api.ts";
import type { BusinessProfile } from "../src/lib/api.ts";
import {
  buildProfileUpdate, classifySaveError, draftFromProfile, liveBadge, loadedState, reconcileWithBackend,
  filterVoices, NO_VOICE_FILTERS, reconcileProfileSave, reconcileReceptionistSave, removeItem, voiceFilterOptions, voiceMeta, safePreviewUrl, SAVED, validateProfile, validateReceptionist,
} from "../src/lib/settingsLogic.ts";

const profile = (): BusinessProfile => ({
  id: "b1", name: "Sunrise", industry: "Dental", phone: "+15555550100", address: "1 Main", website_url: "https://s.example",
  description: "d", hours: { "Mon-Fri": "9-5" }, status: "reviewed", other_knowledge_count: 1,
  services: [{ id: "s1", source: "import", text: "Cleaning" }],
  faqs: [{ id: "f1", source: "import", question: "Parking?", answer: "Free" }],
  policies: [{ id: "p1", source: "manual", text: "24h notice" }],
});

// ---- save states ----
test("502 and 409 are 'saved but not applied' (Sync failed), never a plain success", () => {
  for (const status of [502, 409]) {
    const o = classifySaveError(new ApiError(status, "x"));
    assert.equal(o.state, "sync_failed");
    assert.match(o.message!, /saved/i);
    assert.match(o.message!, /apply|applied/i);
  }
});

test("validation/permission/auth failures are 'Not saved' and never claim saved", () => {
  for (const status of [400, 401, 403, 404, 422]) {
    const o = classifySaveError(new ApiError(status, "bad"));
    assert.equal(o.state, "error");
    assert.match(o.message!, /^Not saved\./);
  }
});

test("network failures and unexpected 5xx are errors that make no claim either way about persistence", () => {
  for (const err of [new Error("network"), new TypeError("Failed to fetch"), new ApiError(500, "boom"), new ApiError(503, "x")]) {
    const o = classifySaveError(err);
    assert.equal(o.state, "error");
    assert.match(o.message!, /couldn't confirm/i);
    assert.ok(!/^Not saved/.test(o.message!) && !/saved and applied/i.test(o.message!));
  }
});

test("backend sync flag overrides optimistic local state", () => {
  assert.equal(reconcileWithBackend(SAVED, false).state, "not_applied");        // 'Changes not applied'
  assert.equal(reconcileWithBackend(SAVED, true).state, "saved");
  assert.equal(reconcileWithBackend(classifySaveError(new ApiError(502, "")), false).state, "sync_failed");
  assert.equal(reconcileWithBackend(classifySaveError(new ApiError(502, "")), true).state, "saved"); // another retry landed
  assert.equal(reconcileWithBackend(SAVED, null).state, "saved");               // unreadable: unchanged, not upgraded
});

test("loading an unsynced config shows Changes not applied; synced shows nothing", () => {
  assert.equal(loadedState(false).state, "not_applied");
  assert.equal(loadedState(true).state, "idle");
});

test("never labels live unless the backend said live", () => {
  assert.equal(liveBadge({ receptionist_state: "live" }).tone, "ok");
  for (const s of ["not_live", "unverified", "not_configured"] as const) assert.notEqual(liveBadge({ receptionist_state: s }).tone, "ok");
  assert.ok(!/live/i.test(liveBadge({ receptionist_state: "unverified" }).label) || /unconfirmed/i.test(liveBadge({ receptionist_state: "unverified" }).label));
});

// ---- knowledge preservation in the payload ----
test("untouched profile re-sends every item by id and deletes nothing", () => {
  const p = profile();
  const u = buildProfileUpdate(p, draftFromProfile(p));
  assert.deepEqual(u.delete_ids, []);
  assert.deepEqual(u.services, [{ id: "s1", text: "Cleaning" }]);
  assert.deepEqual(u.faqs, [{ id: "f1", question: "Parking?", answer: "Free" }]);
  assert.deepEqual(u.policies, [{ id: "p1", text: "24h notice" }]);
});

test("only an explicit Remove puts an item in delete_ids", () => {
  const p = profile();
  const d = removeItem(draftFromProfile(p), "faqs", 0);
  assert.deepEqual(d.removedIds, ["f1"]);
  const u = buildProfileUpdate(p, d);
  assert.deepEqual(u.delete_ids, ["f1"]);
  assert.equal(u.faqs.length, 0);
  assert.equal(u.services.length, 1);
  assert.equal(u.policies.length, 1);
  // each kind
  assert.deepEqual(buildProfileUpdate(p, removeItem(draftFromProfile(p), "services", 0)).delete_ids, ["s1"]);
  assert.deepEqual(buildProfileUpdate(p, removeItem(draftFromProfile(p), "policies", 0)).delete_ids, ["p1"]);
});

test("removing an unsaved (new) row records no deletion", () => {
  const p = profile();
  const d = draftFromProfile(p);
  d.services.push({ text: "temp" });
  const after = removeItem(d, "services", d.services.length - 1);
  assert.deepEqual(after.removedIds, []);
  assert.deepEqual(buildProfileUpdate(p, after).delete_ids, []);
});

test("blanking a saved service/policy/FAQ is a validation error, never a deletion", () => {
  const p = profile();
  const svc = draftFromProfile(p); svc.services[0].text = "   ";
  const pol = draftFromProfile(p); pol.policies[0].text = "";
  const faqBoth = draftFromProfile(p); faqBoth.faqs[0].question = ""; faqBoth.faqs[0].answer = " ";
  const faqHalf = draftFromProfile(p); faqHalf.faqs[0].answer = "";
  for (const d of [svc, pol, faqBoth, faqHalf]) {
    assert.ok(validateProfile(d), "must be rejected before any request is built");
  }
  assert.match(validateProfile(svc)!, /Remove/);
  assert.match(validateProfile(pol)!, /Remove/);
  assert.match(validateProfile(faqBoth)!, /Remove/);
  // even if a caller skipped validation, blanking alone never produces a delete id
  for (const d of [svc, pol, faqBoth, faqHalf]) assert.deepEqual(buildProfileUpdate(p, d).delete_ids, []);
});

test("blank NEW rows are ignored (nothing to delete, nothing to save)", () => {
  const p = profile();
  const d = draftFromProfile(p);
  d.services.push({ text: " " }); d.policies.push({ text: "" }); d.faqs.push({ question: "", answer: "" });
  assert.equal(validateProfile(d), null);
  const u = buildProfileUpdate(p, d);
  assert.equal(u.services.length, 1); assert.equal(u.policies.length, 1); assert.equal(u.faqs.length, 1);
  assert.deepEqual(u.delete_ids, []);
});

test("an item missing from the draft without an explicit Remove is NOT deleted", () => {
  const p = profile();
  const d = draftFromProfile(p);
  d.faqs = []; d.services = []; d.policies = [];   // e.g. a bug or stale state, not a Remove click
  assert.deepEqual(buildProfileUpdate(p, d).delete_ids, []);
});

test("Remove buttons in the profile form go through removeItem", () => {
  const src = readFileSync(new URL("../src/components/settings/BusinessProfileSection.tsx", import.meta.url), "utf8");
  for (const k of ["services", "faqs", "policies"]) assert.ok(src.includes(`removeItem(draft, "${k}", i)`), k);
  assert.ok(!/set\("(services|faqs|policies)", draft\.\1\.filter/.test(src), "no silent filter-based removal");
});

test("new items carry no id; blank rows are dropped, not saved", () => {
  const p = profile();
  const d = draftFromProfile(p);
  d.services.push({ text: "  Whitening " }, { text: "   " });
  d.faqs.push({ question: "", answer: "" });
  const u = buildProfileUpdate(p, d);
  assert.deepEqual(u.services.at(-1), { text: "Whitening" });
  assert.equal(u.services.length, 2);
  assert.equal(u.faqs.length, 1);
});

test("edited items keep their id so they are updated in place", () => {
  const p = profile();
  const d = draftFromProfile(p);
  d.faqs[0].answer = "Free + street";
  assert.deepEqual(buildProfileUpdate(p, d).faqs, [{ id: "f1", question: "Parking?", answer: "Free + street" }]);
});

// ---- validation ----
test("profile validation", () => {
  const ok = draftFromProfile(profile());
  assert.equal(validateProfile(ok), null);
  assert.match(validateProfile({ ...ok, name: " " })!, /name/i);
  assert.match(validateProfile({ ...ok, phone: "abc" })!, /phone/i);
  assert.match(validateProfile({ ...ok, website_url: "javascript:alert(1)" })!, /website/i);
  assert.match(validateProfile({ ...ok, website_url: "example.com" })!, /website/i);
  assert.match(validateProfile({ ...ok, hours: [{ label: "Mon", value: "" }] })!, /hours/i);
  assert.match(validateProfile({ ...ok, faqs: [{ question: "q", answer: "" }] })!, /FAQ/);
});

test("receptionist validation", () => {
  const base = { name: "R", greeting: "hi", voice_id: "v", tasks: { transfer_calls: false }, transfer_number: "" };
  assert.equal(validateReceptionist(base), null);
  assert.match(validateReceptionist({ ...base, voice_id: null })!, /voice/i);
  assert.match(validateReceptionist({ ...base, name: "" })!, /name/i);
  assert.match(validateReceptionist({ ...base, greeting: "x".repeat(501) })!, /500/);
  assert.match(validateReceptionist({ ...base, tasks: { transfer_calls: true } })!, /transfer number/i);
  assert.match(validateReceptionist({ ...base, tasks: { transfer_calls: true }, transfer_number: "555-1234" })!, /\+14155551234/);
  assert.equal(validateReceptionist({ ...base, tasks: { transfer_calls: true }, transfer_number: "+1 (415) 555-1234" }), null);
});

test("voice previews play only from https URLs", () => {
  assert.equal(safePreviewUrl("https://cdn.example/a.mp3"), "https://cdn.example/a.mp3");
  for (const bad of ["http://x/a.mp3", "javascript:alert(1)", "data:audio/mp3;base64,AA", "not a url", null]) assert.equal(safePreviewUrl(bad), null);
});

// ---- api client: right verbs/paths, no unsupported fields ----
test("api methods hit the tenant-scoped endpoints with the right verb and body", async () => {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const orig = globalThis.fetch;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body as string) : undefined });
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    await api.getAgentConfig("o", "b");
    await api.getCallHandling("o", "b");
    await api.getBusinessProfile("o", "b");
    await api.updateBusinessProfile("o", "b", buildProfileUpdate(profile(), draftFromProfile(profile())));
    await api.saveAgentConfig("o", "b", { name: "n", greeting: null, personality: "friendly", language: "en-US", voice_id: "v", tasks: {}, transfer_number: null });
  } finally {
    globalThis.fetch = orig;
  }
  const tail = calls.map((c) => `${c.method} ${c.url.replace(/^.*\/api\/v1/, "")}`);
  assert.deepEqual(tail, [
    "GET /orgs/o/agents/b/config",
    "GET /orgs/o/agents/b/call-handling",
    "GET /orgs/o/businesses/b/profile",
    "PATCH /orgs/o/businesses/b/profile",
    "PUT /orgs/o/agents/b",
  ]);
  const put = calls[4].body as Record<string, unknown>;
  assert.equal(put.after_hours_behavior, null);
  assert.equal(put.business_hours, null);
});

// ---- page-level guards (source checks; there is no DOM runner in this project) ----
const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");

test("the UI never hard-codes a 'live' claim: it renders the backend state", () => {
  const src = read("../src/components/settings/CallHandlingSection.tsx");
  assert.ok(!/["'`>]\s*Your receptionist is live/i.test(src));
  assert.ok(src.includes("receptionist_state") && src.includes("receptionist_message"));
});

// ---- finding 2: no receptionist yet ----
test("profile saved with no receptionist is NOT 'Saved and applied'", () => {
  const r = reconcileProfileSave(SAVED, "none");
  assert.equal(r.outcome.state, "saved");
  assert.ok(!/applied/i.test(r.outcome.message!) || /not|isn't|don't/i.test(r.outcome.message!));
  assert.match(r.outcome.message!, /don't have a receptionist yet/);
  assert.match(r.outcome.message!, /once you set one up/);
  assert.equal(r.reloadDraft, true);
});

test("profile saved: 'Saved and applied' only when the backend says the receptionist is synced", () => {
  assert.equal(reconcileProfileSave(SAVED, "synced").outcome.message, "Saved and applied.");
  assert.equal(reconcileProfileSave(SAVED, "unsynced").outcome.state, "not_applied");
  const unk = reconcileProfileSave(SAVED, "unknown").outcome;
  assert.equal(unk.state, "saved");
  assert.ok(!/applied\.$/.test(unk.message!) && /couldn't confirm/.test(unk.message!));
});

// ---- finding 3: failed requests keep the draft; persisted-but-unsynced is distinct ----
test("validation / authorization / network failures keep the draft and show the error", () => {
  for (const err of [new ApiError(422, "Enter a valid phone number"), new ApiError(403, "Insufficient permissions"),
    new ApiError(404, "Knowledge item not found"), new ApiError(400, "type mismatch"), new TypeError("Failed to fetch"), new ApiError(500, "x")]) {
    for (const agent of ["none", "synced", "unsynced", "unknown"] as const) {
      const r = reconcileProfileSave(classifySaveError(err), agent);
      assert.equal(r.reloadDraft, false, `${(err as Error).message}/${agent}`);   // draft NOT replaced
      assert.equal(r.outcome.state, "error");
      assert.ok(r.outcome.message && r.outcome.message.length > 0);
    }
  }
});

test("saved-but-provider-failed (502/409) is a distinct state that does reload the persisted copy", () => {
  for (const status of [502, 409]) {
    const r = reconcileProfileSave(classifySaveError(new ApiError(status, "x")), "unsynced");
    assert.equal(r.outcome.state, "sync_failed");
    assert.equal(r.reloadDraft, true);
    assert.match(r.outcome.message!, /saved/i);
  }
});

test("BusinessProfileSection wires the reconciliation: error path returns before any reload", () => {
  const src = readFileSync(new URL("../src/components/settings/BusinessProfileSection.tsx", import.meta.url), "utf8");
  const save = src.slice(src.indexOf("async function save"));
  const errBranch = save.indexOf('raw.state === "error"');
  const firstLoad = save.indexOf("await load()");
  assert.ok(errBranch > -1 && firstLoad > errBranch, "error branch must precede (and skip) the reload");
  assert.match(save.slice(errBranch, firstLoad), /return;/);
  assert.ok(src.includes("reconcileProfileSave") && !src.includes("reconcileWithBackend"));
  assert.ok(save.includes("err.status === 404"), "no receptionist (404) must be recognised");
});

// ---- receptionist save: PUT response is examined, not discarded ----
test("PUT ok + follow-up GET failed: uses the PUT response's synced flag, never an unexamined 'Saved and applied.'", () => {
  const ok = reconcileReceptionistSave(SAVED, { synced: true }, null);
  assert.equal(ok.state, "saved");
  assert.notEqual(ok.message, "Saved and applied.");
  assert.match(ok.message!, /confirmed by the save response/);
  assert.match(ok.message!, /couldn't re-check/);

  const unsynced = reconcileReceptionistSave(SAVED, { synced: false }, null);
  assert.equal(unsynced.state, "not_applied");
  assert.match(unsynced.message!, /not applied/i);

  const none = reconcileReceptionistSave(SAVED, null, null);
  assert.equal(none.state, "saved");
  assert.match(none.message!, /couldn't verify/);
  assert.ok(!/applied/i.test(none.message!.replace(/whether your receptionist has the latest changes/, "")));
});

test("PUT ok but response says unsynced, GET agrees or is unavailable: Changes not applied", () => {
  assert.equal(reconcileReceptionistSave(SAVED, { synced: false }, false).state, "not_applied");
  assert.equal(reconcileReceptionistSave(SAVED, { synced: false }, null).state, "not_applied");
});

test("fresh GET takes precedence over the PUT response", () => {
  assert.equal(reconcileReceptionistSave(SAVED, { synced: true }, false).state, "not_applied");
  assert.equal(reconcileReceptionistSave(SAVED, { synced: false }, true).message, "Saved and applied.");
});

test("provider-sync failure (502/409): Sync failed, whether or not the follow-up GET works", () => {
  for (const status of [502, 409]) {
    const raw = classifySaveError(new ApiError(status, "x"));
    const withGet = reconcileReceptionistSave(raw, null, false);
    assert.equal(withGet.state, "sync_failed");
    const noGet = reconcileReceptionistSave(raw, null, null);
    assert.equal(noGet.state, "sync_failed");
    assert.match(noGet.message!, /couldn't re-check/);
  }
});

test("failed PUT stays an error regardless of the follow-up read", () => {
  const raw = classifySaveError(new ApiError(422, "bad"));
  for (const g of [true, false, null]) assert.equal(reconcileReceptionistSave(raw, null, g).state, "error");
});

test("the shared save helper captures the PUT response and reconciles it; both views use it", () => {
  const helper = readFileSync(new URL("../src/lib/agentConfigSave.ts", import.meta.url), "utf8");
  assert.match(helper, /put = await api\.saveAgentConfig\(/);
  assert.ok(helper.includes("reconcileReceptionistSave(result, put, synced)"));
  assert.ok(!/await api\.saveAgentConfig\(/.test(helper.replace("put = await api.saveAgentConfig(", "")), "response must not be discarded");
  for (const f of ["ReceptionistSection", "VoiceSection"]) {
    const src = readFileSync(new URL(`../src/components/settings/${f}.tsx`, import.meta.url), "utf8");
    assert.ok(src.includes("saveAgentSettings("), f);
    assert.ok(!src.includes("api.saveAgentConfig"), `${f} must not bypass the shared save path`);
  }
});

test("422 shows the server's validation message (without pydantic's prefix) instead of a generic error", () => {
  const o = classifySaveError(new ApiError(422, "Value error, Enter a valid phone number; Value error, Website must be a full http(s) address"));
  assert.equal(o.state, "error");
  assert.match(o.message!, /^Not saved\. Enter a valid phone number; Website must be/);
  assert.ok(!/Value error/.test(o.message!) && !/Something went wrong/.test(o.message!));
});

test("the status line does not repeat 'Not saved' (label + message)", () => {
  const ui = readFileSync(new URL("../src/components/settings/ui.tsx", import.meta.url), "utf8");
  assert.ok(ui.includes('replace(/^Not saved\\.\\s*/, "")'));
});

// ---- voice picker data logic ----
test("voice filters narrow the view only; options come from real catalogue values", () => {
  const mk = (id: string, name: string, gender: string | null, accent: string | null) =>
    ({ id, retell_voice_id: "r" + id, name, provider: "elevenlabs", gender, accent, age_style: null, preview_url: null });
  const vs = [mk("1", "Ava", "female", "American"), mk("2", "Ben", "male", "British"), mk("3", "Cleo", "Female", "British"), mk("4", "Dev", null, null)];
  const opts = voiceFilterOptions(vs);
  assert.deepEqual(opts.genders, ["female", "male"]);              // case-insensitive de-dupe, nothing invented
  assert.deepEqual(opts.accents, ["American", "British"]);
  assert.equal(filterVoices(vs, NO_VOICE_FILTERS).length, 4);      // no filter = every voice
  assert.deepEqual(filterVoices(vs, { search: "", gender: "female", accent: "" }).map((v) => v.id), ["1", "3"]);
  assert.deepEqual(filterVoices(vs, { search: "", gender: "female", accent: "British" }).map((v) => v.id), ["3"]);
  assert.deepEqual(filterVoices(vs, { search: "ben", gender: "", accent: "" }).map((v) => v.id), ["2"]);
  assert.deepEqual(filterVoices(vs, { search: "BRIT", gender: "", accent: "" }).map((v) => v.id), ["2", "3"]);
  assert.equal(vs.length, 4);                                       // input untouched
  assert.equal(voiceMeta(vs[3]), "elevenlabs");
});

test("VoicePicker keeps every voice reachable and the id contract (internal Voice.id)", () => {
  const src = readFileSync(new URL("../src/components/settings/VoicePicker.tsx", import.meta.url), "utf8");
  assert.ok(src.includes("onChange(v.id)") && src.includes('value === v.id'));
  assert.ok(src.includes("filterVoices(voices, filters)") && src.includes("safePreviewUrl"));
  assert.ok(!/slice\(0,\s*\d+\)/.test(src), "the catalogue must not be truncated");
});


// ======================= settings views: one route per category =======================
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { activeSettingsView, nextSteps, SETTINGS_VIEWS } from "../src/lib/settingsLogic.ts";
import type { CallHandling } from "../src/lib/api.ts";

const root = new URL("../src/", import.meta.url);
const src = (p: string) => readFileSync(new URL(p, root), "utf8");

// view -> [route file, view component, markers that MUST be in the view, markers that MUST NOT be]
const FORMS = {
  business: "BusinessProfileSection", receptionist: "ReceptionistSection", voice: "VoiceSection",
  billing: "BillingPanel", security: "api.changePassword", danger: "api.deleteAccount", voicePicker: "VoicePicker",
} as const;
const MATRIX: { id: string; route: string; view: string; owns: string[] }[] = [
  { id: "overview", route: "app/dashboard/settings/page.tsx", view: "OverviewView", owns: ["CallHandlingSection"] },
  { id: "business", route: "app/dashboard/settings/business/page.tsx", view: "BusinessView", owns: [FORMS.business] },
  { id: "receptionist", route: "app/dashboard/settings/receptionist/page.tsx", view: "ReceptionistView", owns: [FORMS.receptionist] },
  { id: "voice", route: "app/dashboard/settings/voice/page.tsx", view: "VoiceView", owns: [FORMS.voice] },
  { id: "billing", route: "app/dashboard/settings/billing/page.tsx", view: "BillingView", owns: [FORMS.billing] },
  { id: "security", route: "app/dashboard/settings/security/page.tsx", view: "SecurityView", owns: [FORMS.security] },
  { id: "delete-account", route: "app/dashboard/settings/delete-account/page.tsx", view: "DeleteAccountView", owns: [FORMS.danger] },
];
const ALL_FORM_MARKERS = [FORMS.business, FORMS.receptionist, FORMS.voice, FORMS.billing, FORMS.security, FORMS.danger, FORMS.voicePicker, "BillingPanel"];

test("every settings category has its own route file that renders exactly its own view", () => {
  for (const m of MATRIX) {
    assert.ok(existsSync(new URL(m.route, root)), `${m.route} missing`);
    const page = src(m.route);
    assert.ok(page.includes(`<${m.view} />`), `${m.route} must render <${m.view} />`);
    assert.ok(!MATRIX.some((o) => o.view !== m.view && page.includes(`<${o.view}`)), `${m.route} must not render another view`);
    assert.match(page, /export const metadata/);
  }
});

test("each view mounts only its own forms (no other category's form is rendered inside it)", () => {
  for (const m of MATRIX) {
    const view = src(`components/settings/views/${m.view}.tsx`);
    for (const own of m.owns) assert.ok(view.includes(own) || src("components/settings/" + own + ".tsx").length > 0, `${m.view} should own ${own}`);
    const owned = new Set(m.owns);
    for (const marker of ALL_FORM_MARKERS) {
      if (owned.has(marker)) continue;
      // overview may LINK to others but must not render their forms; compare against JSX usage / api calls
      const used = new RegExp(`<${marker}[\\s/>]`).test(view) || (marker.startsWith("api.") && view.includes(marker));
      assert.ok(!used, `${m.view} must not render/use ${marker}`);
    }
  }
});

test("the Business view contains no receptionist, voice, billing, password or deletion form (and vice versa)", () => {
  const strict = {
    BusinessView: ["ReceptionistSection", "VoiceSection", "VoicePicker", "BillingPanel", "changePassword", "deleteAccount"],
    ReceptionistView: ["BusinessProfileSection", "VoiceSection", "VoicePicker", "BillingPanel", "changePassword", "deleteAccount"],
    VoiceView: ["BusinessProfileSection", "ReceptionistSection", "BillingPanel", "changePassword", "deleteAccount"],
    BillingView: ["BusinessProfileSection", "ReceptionistSection", "VoiceSection", "VoicePicker", "changePassword", "deleteAccount"],
    SecurityView: ["BusinessProfileSection", "ReceptionistSection", "VoiceSection", "BillingPanel", "deleteAccount"],
    DeleteAccountView: ["BusinessProfileSection", "ReceptionistSection", "VoiceSection", "BillingPanel", "changePassword"],
    OverviewView: ["BusinessProfileSection", "ReceptionistSection", "VoiceSection", "VoicePicker", "BillingPanel", "changePassword", "deleteAccount", "<form"],
  } as Record<string, string[]>;
  for (const [view, banned] of Object.entries(strict)) {
    const code = src(`components/settings/views/${view}.tsx`);
    for (const b of banned) assert.ok(!code.includes(b), `${view} contains ${b}`);
  }
});

test("the Voice section holds the catalogue; the Receptionist section does not load or render voices", () => {
  const rec = src("components/settings/ReceptionistSection.tsx");
  assert.ok(!rec.includes("VoicePicker") && !rec.includes("listVoices"));
  const voice = src("components/settings/VoiceSection.tsx");
  assert.ok(voice.includes("<VoicePicker") && voice.includes("listVoices"));
  // the Voice view saves only the voice id; the Receptionist view never sends one
  assert.ok(voice.includes("{ voice_id: voiceId }"));
  const call = rec.slice(rec.indexOf("saveAgentSettings("), rec.indexOf("});", rec.indexOf("saveAgentSettings(")));
  assert.ok(call.length > 20 && !/voice_id/.test(call), "Receptionist save must not send a voice id");
});

test("no category is hidden with CSS: the layout renders children, not a stack of sections", () => {
  const layout = src("app/dashboard/settings/layout.tsx");
  assert.ok(layout.includes("{children}"));
  for (const c of ["BusinessProfileSection", "ReceptionistSection", "VoiceSection", "BillingPanel", "SecurityView", "DeleteAccountView"]) assert.ok(!layout.includes(c), `layout mounts ${c}`);
  for (const f of ["app/dashboard/settings/layout.tsx", "components/settings/SettingsNav.tsx"]) assert.ok(!/\bhidden\b[^"]*(section|view)/i.test(src(f)));
});

test("navigation uses real links to routes (back/forward + deep links), never in-page anchors", () => {
  const nav = src("components/settings/SettingsNav.tsx");
  assert.ok(nav.includes('from "next/link"') && nav.includes("href={v.href}") && nav.includes('aria-current={isActive ? "page" : undefined}'));
  assert.ok(!nav.includes('href={`#'));
  const hrefs = SETTINGS_VIEWS.map((v) => v.href);
  assert.equal(new Set(hrefs).size, hrefs.length);
  assert.ok(hrefs.every((h) => h.startsWith("/dashboard/settings")) && !hrefs.some((h) => h.includes("#")));
  for (const v of SETTINGS_VIEWS) assert.ok(MATRIX.some((m) => m.id === v.id), `nav item ${v.id} has no route`);
  assert.deepEqual(SETTINGS_VIEWS.map((v) => v.label), ["Overview", "Business", "Receptionist", "Voice", "Billing", "Security", "Delete account"]);
});

test("activeSettingsView maps URLs to exactly one view", () => {
  for (const v of SETTINGS_VIEWS) {
    assert.equal(activeSettingsView(v.href)?.id, v.id);
    assert.equal(activeSettingsView(v.href + "/")?.id, v.id);
  }
  assert.equal(activeSettingsView("/dashboard/settings/unknown"), null);
  assert.equal(activeSettingsView("/dashboard/calls"), null);
});

test("Business, Billing, Security and Delete views do not wait on the business lookup", () => {
  for (const id of ["billing", "security", "delete-account"]) assert.equal(SETTINGS_VIEWS.find((v) => v.id === id)!.needsBusiness, false);
  for (const view of ["BillingView", "SecurityView", "DeleteAccountView"]) assert.ok(!src(`components/settings/views/${view}.tsx`).includes("RequireBusiness"));
});

test("no after-hours / business-hours-behaviour control anywhere in the settings UI", () => {
  const walk = (dir: URL): string[] => readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(new URL(e.name + "/", dir)) : [fileURLToPath(new URL(e.name, dir))]));
  for (const f of [...walk(new URL("components/settings/", root)), ...walk(new URL("app/dashboard/settings/", root))]) {
    assert.ok(!/after.?hours/i.test(readFileSync(f, "utf8")), `${f} mentions after-hours`);
  }
});

test("existing account features survive intact in their views", () => {
  const sec = src("components/settings/views/SecurityView.tsx");
  assert.ok(sec.includes("api.changePassword") && sec.includes("Change password") && sec.includes("at least 10 characters"));
  const del = src("components/settings/views/DeleteAccountView.tsx");
  assert.ok(del.includes("api.deleteAccount(org.id, delName, delPw)") && del.includes("Delete my account"));
  assert.ok(src("components/settings/views/BillingView.tsx").includes("<BillingPanel orgId={org.id} role={org.role} />"));
});

// ---- overview next steps ----
const ch = (o: Partial<CallHandling> = {}): CallHandling => ({
  business_number: "+15555550123", number_state: "ready", number_message: "ok", receptionist_state: "live",
  receptionist_message: "live", sync_state: "synced", version: 1, synced_version: 1, ...o,
});

test("overview next steps come only from what the backend reported", () => {
  assert.deepEqual(nextSteps(null), []);
  assert.deepEqual(nextSteps(ch()), []);                                            // all good: nothing to do
  assert.equal(nextSteps(ch({ receptionist_state: "not_configured", sync_state: "not_configured", business_number: null }))[0].action?.href, "/onboarding");
  const pending = nextSteps(ch({ sync_state: "pending" }));
  assert.equal(pending[0].action?.href, "/dashboard/settings/receptionist");
  assert.equal(nextSteps(ch({ receptionist_state: "not_live" }))[0].action?.href, "/dashboard/go-live");
  const noNum = nextSteps(ch({ business_number: null, receptionist_state: "not_live", number_message: "We are setting up your number." }));
  assert.equal(noNum[0].text, "We are setting up your number.");
  assert.ok(!noNum.some((s) => s.id === "golive"), "no 'go live' prompt without a number");
  const unv = nextSteps(ch({ receptionist_state: "unverified", receptionist_message: "couldn't confirm" }));
  assert.ok(unv.some((s) => s.id === "unverified") && !unv.some((s) => s.id === "golive"));
});
