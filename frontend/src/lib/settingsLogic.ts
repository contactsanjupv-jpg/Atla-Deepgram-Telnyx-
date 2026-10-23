/**
 * Pure logic for the Advanced Settings sections (kept free of React so it is unit-tested).
 * The rule everywhere: the screen only says what the BACKEND confirmed.
 */
import { ApiError, describeError } from "./api";
import { SETTINGS_BASE, SETTINGS_BILLING_HREF } from "./settingsRoutes";
import type { BusinessProfile, BusinessProfileUpdate, CallHandling, Voice } from "./api";

export type SaveState = "idle" | "saving" | "saved" | "sync_failed" | "not_applied" | "error";

export interface SaveOutcome {
  state: SaveState;
  message: string | null;
}

/**
 * Classify a failed save. The backend writes to its database BEFORE it contacts the voice
 * provider, so 502 (provider failed) and 409 (another save in progress) both mean
 * "saved, but not applied to your live receptionist". 400/403/404/422 mean nothing was saved.
 */
export function classifySaveError(err: unknown): SaveOutcome {
  if (err instanceof ApiError && (err.status === 502 || err.status === 409)) {
    return {
      state: "sync_failed",
      message:
        err.status === 409
          ? "Your changes are saved, but another save was in progress so they weren't applied yet. Try again in a moment."
          : "Your changes are saved, but we couldn't apply them to your receptionist yet. Callers still hear the previous version.",
    };
  }
  if (err instanceof ApiError && err.status === 422 && err.message) {
    // FastAPI validation text (already joined by the client); drop pydantic's "Value error, " prefix.
    return { state: "error", message: `Not saved. ${err.message.replace(/Value error, /g, "")}` };
  }
  if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
    return { state: "error", message: `Not saved. ${describeError(err, "Something went wrong. Please try again.")}` };
  }
  // Network failure or unexpected 5xx: we cannot know whether the server stored it, so don't claim either way.
  return {
    state: "error",
    message: "We couldn't confirm your changes were saved (connection or server problem). Your entries are still here — please try again.",
  };
}

export const SAVED: SaveOutcome = { state: "saved", message: "Saved and applied." };

/**
 * Receptionist save. Inputs: `raw` (SAVED, or the classified error), `put` (the successful PUT's
 * AgentOut, or null), `fetchedSynced` (the follow-up GET's `synced`, or null if that GET failed).
 * Precedence: the freshly fetched config wins; if it couldn't be fetched, the PUT response's own
 * `synced` flag is used; we never fall back to an unexamined "Saved and applied".
 */
export function reconcileReceptionistSave(
  raw: SaveOutcome,
  put: { synced: boolean } | null,
  fetchedSynced: boolean | null,
): SaveOutcome {
  if (raw.state === "error") return raw;
  if (fetchedSynced !== null) return reconcileWithBackend(raw, fetchedSynced);
  if (raw.state === "sync_failed") {
    return { ...raw, message: `${raw.message} We couldn't re-check the latest state.` };
  }
  if (put === null) {
    return { state: "saved", message: "Saved. We couldn't verify whether your receptionist has the latest changes." };
  }
  return put.synced
    ? { state: "saved", message: "Saved and applied (confirmed by the save response). We couldn't re-check the latest settings." }
    : { state: "not_applied", message: "Saved, but changes not applied to your receptionist yet. We couldn't re-check the latest settings." };
}

/** After any save attempt, trust the freshly loaded backend flag over our own guess. */
export function reconcileWithBackend(outcome: SaveOutcome, backendSynced: boolean | null): SaveOutcome {
  if (backendSynced === null) return outcome; // couldn't re-read: keep the honest local result
  if (outcome.state === "saved" && !backendSynced) {
    return { state: "not_applied", message: "Saved, but changes not applied to your receptionist yet." };
  }
  if (outcome.state === "sync_failed" && backendSynced) {
    return { state: "saved", message: "Saved and applied." };
  }
  return outcome;
}

/** What the backend says about the receptionist after a profile save. */
export type AgentSyncStatus = "none" | "synced" | "unsynced" | "unknown";

export interface ProfileSaveResult {
  outcome: SaveOutcome;
  /** Replace the form with the server copy only when the backend confirmed the profile was persisted. */
  reloadDraft: boolean;
}

/**
 * Decide what a profile save means. `raw` is the immediate result (SAVED or classifySaveError).
 *  - error (validation/permission/network/etc.): NOT persisted -> keep the user's draft, show the error.
 *  - sync_failed (502/409): the backend commits before it contacts the provider -> persisted but not applied.
 *  - saved: depends on whether a receptionist exists and is in sync.
 */
export function reconcileProfileSave(raw: SaveOutcome, agent: AgentSyncStatus): ProfileSaveResult {
  if (raw.state === "error") return { outcome: raw, reloadDraft: false };
  if (raw.state === "sync_failed") {
    return { outcome: agent === "synced" ? SAVED : raw, reloadDraft: true };
  }
  switch (agent) {
    case "none":
      return {
        outcome: { state: "saved", message: "Your business profile is saved. You don't have a receptionist yet, so it will be used once you set one up." },
        reloadDraft: true,
      };
    case "unsynced":
      return { outcome: { state: "not_applied", message: "Saved, but changes not applied to your receptionist yet." }, reloadDraft: true };
    case "unknown":
      return { outcome: { state: "saved", message: "Your business profile is saved. We couldn't confirm whether your receptionist has the update yet." }, reloadDraft: true };
    default:
      return { outcome: SAVED, reloadDraft: true };
  }
}

export const SAVE_LABEL: Record<SaveState, string> = {
  idle: "",
  saving: "Saving…",
  saved: "Saved",
  sync_failed: "Sync failed",
  not_applied: "Changes not applied",
  error: "Not saved",
};

/** Status shown on load, before any save: saved config the provider doesn't have yet. */
export function loadedState(synced: boolean): SaveOutcome {
  return synced
    ? { state: "idle", message: null }
    : { state: "not_applied", message: "Your latest saved changes haven't been applied to your receptionist yet. Save again to retry." };
}

// ---------------- call handling ----------------

export function liveBadge(ch: Pick<CallHandling, "receptionist_state">): { label: string; tone: "ok" | "warn" | "off" } {
  switch (ch.receptionist_state) {
    case "live":
      return { label: "Live (confirmed by provider)", tone: "ok" };
    case "unverified":
      return { label: "Status unconfirmed", tone: "warn" };
    case "not_configured":
      return { label: "Not set up", tone: "off" };
    default:
      return { label: "Not live", tone: "off" };
  }
}

export function syncBadge(s: CallHandling["sync_state"]): string {
  return { synced: "Up to date with provider", pending: "Changes not applied", never_synced: "Not yet sent to provider", not_configured: "—" }[s];
}

// ---------------- business profile ----------------

export interface ProfileDraft {
  name: string;
  industry: string;
  phone: string;
  address: string;
  website_url: string;
  description: string;
  hours: { label: string; value: string }[];
  services: { id?: string; text: string }[];
  faqs: { id?: string; question: string; answer: string }[];
  policies: { id?: string; text: string }[];
  /** Ids of loaded items the user explicitly clicked Remove on. The ONLY source of delete_ids. */
  removedIds: string[];
}

export type KnowledgeKind = "services" | "faqs" | "policies";

/** Explicit Remove: drops the row from the draft and records its id (if it was a saved item) for deletion. */
export function removeItem(d: ProfileDraft, kind: KnowledgeKind, index: number): ProfileDraft {
  const row = d[kind][index];
  if (!row) return d;
  const id = (row as { id?: string }).id;
  return {
    ...d,
    [kind]: (d[kind] as unknown[]).filter((_, j) => j !== index),
    removedIds: id && !d.removedIds.includes(id) ? [...d.removedIds, id] : d.removedIds,
  } as ProfileDraft;
}

export function draftFromProfile(p: BusinessProfile): ProfileDraft {
  return {
    name: p.name,
    industry: p.industry ?? "",
    phone: p.phone ?? "",
    address: p.address ?? "",
    website_url: p.website_url ?? "",
    description: p.description ?? "",
    hours: Object.entries(p.hours ?? {}).map(([label, value]) => ({ label, value: String(value) })),
    services: p.services.map((s) => ({ id: s.id, text: s.text })),
    faqs: p.faqs.map((f) => ({ id: f.id, question: f.question, answer: f.answer })),
    policies: p.policies.map((x) => ({ id: x.id, text: x.text })),
    removedIds: [],
  };
}

export function validateProfile(d: ProfileDraft): string | null {
  if (!d.name.trim()) return "Business name is required.";
  if (d.name.trim().length > 255) return "Business name is too long.";
  if (d.phone.trim() && (!/^[+\d\s().-]+$/.test(d.phone.trim()) || (d.phone.match(/\d/g) ?? []).length < 7)) {
    return "Enter a valid phone number.";
  }
  if (d.website_url.trim()) {
    try {
      const u = new URL(d.website_url.trim());
      if (!["http:", "https:"].includes(u.protocol)) throw new Error("scheme");
    } catch {
      return "Website must be a full address like https://example.com.";
    }
  }
  if (d.description.length > 4000) return "Description is too long (4000 characters max).";
  for (const h of d.hours) {
    if ((h.label.trim() === "") !== (h.value.trim() === "")) return "Each hours row needs both a day and its hours.";
  }
  const BLANK = "A saved entry can't be left blank. Fill it in, or click Remove to delete it.";
  for (const f of d.faqs) {
    if (!f.question.trim() !== !f.answer.trim()) return "Each FAQ needs both a question and an answer.";
    if (f.id && !f.question.trim() && !f.answer.trim()) return BLANK;
  }
  for (const x of [...d.services, ...d.policies]) {
    if (x.id && !x.text.trim()) return BLANK;
  }
  return null;
}

const nz = (v: string) => (v.trim() === "" ? null : v.trim());

/**
 * Builds the PATCH body. Call validateProfile first: blank saved entries are rejected there. Removal only
 * ever happens through `removedIds` (explicit Remove clicks).
 */
export function buildProfileUpdate(original: BusinessProfile, d: ProfileDraft): BusinessProfileUpdate {
  const hours: Record<string, string> = {};
  for (const h of d.hours) if (h.label.trim() && h.value.trim()) hours[h.label.trim()] = h.value.trim();

  const services = d.services.filter((s) => s.text.trim()).map((s) => ({ ...(s.id ? { id: s.id } : {}), text: s.text.trim() }));
  const policies = d.policies.filter((s) => s.text.trim()).map((s) => ({ ...(s.id ? { id: s.id } : {}), text: s.text.trim() }));
  const faqs = d.faqs
    .filter((f) => f.question.trim() && f.answer.trim())
    .map((f) => ({ ...(f.id ? { id: f.id } : {}), question: f.question.trim(), answer: f.answer.trim() }));

  // Deletion is ONLY what the user explicitly removed (and only ids that were actually loaded).
  // Missing or blanked items are never inferred to be deletions; the backend leaves unmentioned items alone.
  const loaded = new Set([...original.services, ...original.policies, ...original.faqs].map((x) => x.id));
  const stillPresent = new Set([...d.services, ...d.policies, ...d.faqs].map((x) => x.id).filter(Boolean) as string[]);
  const delete_ids = d.removedIds.filter((id) => loaded.has(id) && !stillPresent.has(id));

  return {
    name: d.name.trim(),
    industry: nz(d.industry),
    phone: nz(d.phone),
    address: nz(d.address),
    website_url: nz(d.website_url),
    description: nz(d.description),
    hours: Object.keys(hours).length ? hours : null,
    services,
    faqs,
    policies,
    delete_ids,
  };
}

// ---------------- receptionist ----------------

export const LANGUAGE_OPTIONS = ["en-US", "en-GB", "en-AU", "en-IN", "es-ES", "es-419", "fr-FR", "de-DE", "pt-BR"];

export const TASK_LABELS: Record<string, string> = {
  answer_questions: "Answer questions",
  capture_leads: "Capture leads",
  take_messages: "Take messages",
  transfer_calls: "Transfer calls to a person",
};

export function validateReceptionist(r: {
  name: string;
  greeting: string;
  voice_id: string | null;
  tasks: Record<string, boolean>;
  transfer_number: string;
}): string | null {
  if (!r.name.trim()) return "Receptionist name is required.";
  if (!r.voice_id) return "Choose a voice.";
  if (r.greeting.length > 500) return "Greeting must be 500 characters or fewer.";
  const tn = r.transfer_number.replace(/[\s().-]/g, "");
  if (r.tasks.transfer_calls && !tn) return "Enter a transfer number, or turn off call transfer.";
  if (tn && !/^\+[1-9]\d{6,14}$/.test(tn)) return "Transfer number must be a full number like +14155551234.";
  return null;
}

/** Voice previews are only ever played from https URLs. */
export function safePreviewUrl(previewUrl: string | null): string | null {
  if (!previewUrl) return null;
  try {
    const u = new URL(previewUrl);
    return u.protocol === "https:" ? u.toString() : null;
  } catch {
    return null;
  }
}

// ---------------- voice picker (filtering is display-only: it never changes which voice is saved) ----------------


export interface VoiceFilters {
  search: string;
  gender: string; // "" = any
  accent: string; // "" = any
}

export const NO_VOICE_FILTERS: VoiceFilters = { search: "", gender: "", accent: "" };

const norm = (v: string | null | undefined) => (v ?? "").trim().toLowerCase();

export function voiceMeta(v: Pick<Voice, "accent" | "gender" | "age_style" | "provider">): string {
  return [v.accent, v.gender, v.age_style].filter(Boolean).join(" · ") || v.provider;
}

/** Distinct, sorted filter values taken from the real catalogue (nothing invented). */
export function voiceFilterOptions(voices: Voice[]): { genders: string[]; accents: string[] } {
  const uniq = (xs: (string | null)[]) => {
    const seen = new Map<string, string>(); // case-insensitive; the first spelling in the catalogue wins
    for (const x of xs) if (x && x.trim() && !seen.has(norm(x))) seen.set(norm(x), x.trim());
    return [...seen.values()].sort((a, b) => a.localeCompare(b));
  };
  return { genders: uniq(voices.map((v) => v.gender)), accents: uniq(voices.map((v) => v.accent)) };
}

/** Pure subset of the catalogue; the caller still holds the full list, so no voice is ever removed. */
export function filterVoices(voices: Voice[], f: VoiceFilters): Voice[] {
  const q = norm(f.search);
  return voices.filter(
    (v) =>
      (!q || [v.name, v.accent, v.gender, v.age_style, v.provider].some((x) => norm(x).includes(q))) &&
      (!f.gender || norm(v.gender) === norm(f.gender)) &&
      (!f.accent || norm(v.accent) === norm(f.accent)),
  );
}

// ---------------- settings views (each category is its own route) ----------------

export type SettingsViewId = "overview" | "business" | "receptionist" | "voice" | "billing" | "security" | "delete-account";

export interface SettingsView {
  id: SettingsViewId;
  href: string;
  label: string;
  description: string;
  needsBusiness: boolean;
}

const BASE = SETTINGS_BASE;

export const SETTINGS_VIEWS: SettingsView[] = [
  { id: "overview", href: BASE, label: "Overview", description: "Your receptionist's status and what to do next.", needsBusiness: true },
  { id: "business", href: `${BASE}/business`, label: "Business", description: "Business details, hours, services, FAQs and policies.", needsBusiness: true },
  { id: "receptionist", href: `${BASE}/receptionist`, label: "Receptionist", description: "Name, greeting, personality, language and what it does on a call.", needsBusiness: true },
  { id: "voice", href: `${BASE}/voice`, label: "Voice", description: "Choose and preview your receptionist's voice.", needsBusiness: true },
  { id: "billing", href: `${BASE}/billing`, label: "Billing", description: "Your plan, payment method and usage.", needsBusiness: false },
  { id: "security", href: `${BASE}/security`, label: "Security", description: "Change your password.", needsBusiness: false },
  { id: "delete-account", href: `${BASE}/delete-account`, label: "Delete account", description: "Permanently delete your account and data.", needsBusiness: false },
];

export { SETTINGS_BILLING_HREF };

/** The view a pathname belongs to (trailing slash tolerated); null when it isn't a settings view. */
export function activeSettingsView(pathname: string): SettingsView | null {
  const p = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  return SETTINGS_VIEWS.find((v) => v.href === p) ?? null;
}

export interface NextStep {
  id: string;
  text: string;
  action: { label: string; href: string } | null;
}

/** Short, honest to-do list for the Overview, derived only from what the backend reported. */
export function nextSteps(ch: CallHandling | null): NextStep[] {
  if (!ch) return [];
  const steps: NextStep[] = [];
  if (ch.receptionist_state === "not_configured") {
    steps.push({ id: "setup", text: "Your receptionist isn't set up yet.", action: { label: "Finish setup", href: "/onboarding" } });
    return steps;
  }
  if (!ch.business_number) {
    steps.push({ id: "number", text: ch.number_message, action: null });
  }
  if (ch.sync_state === "pending" || ch.sync_state === "never_synced") {
    steps.push({
      id: "sync",
      text: "Your latest saved changes haven't been applied to your receptionist. Save it again to retry.",
      action: { label: "Open Receptionist", href: `${BASE}/receptionist` },
    });
  }
  if (ch.receptionist_state === "unverified") {
    steps.push({ id: "unverified", text: ch.receptionist_message, action: null });
  } else if (ch.receptionist_state === "not_live" && ch.business_number) {
    steps.push({ id: "golive", text: "Your receptionist isn't taking calls yet.", action: { label: "Go live", href: "/dashboard/go-live" } });
  }
  return steps;
}
