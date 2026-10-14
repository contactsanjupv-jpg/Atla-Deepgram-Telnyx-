import type { CallRow } from "./api";
import type { Tone } from "./billingState";
import { formatPhone } from "./receptionistStatus";

export const PAGE_SIZE = 25;
export const MIN_SEARCH_CHARS = 2;
/** A call that never reported an end after this long is shown as such instead of "in progress" forever. */
export const STALE_CALL_HOURS = 6;

export type Preset = "all" | "today" | "7d" | "30d" | "custom";
export type Sort = "newest" | "oldest" | "longest";

export interface CallFilterState {
  q: string;
  preset: Preset;
  dateFrom: string; // YYYY-MM-DD, only used when preset === "custom"
  dateTo: string;
  direction: "" | "inbound" | "outbound" | "test";
  status: "" | "registered" | "ongoing" | "ended" | "error";
  sentiment: "" | "positive" | "negative" | "neutral" | "unknown";
  lead: "" | "with" | "without";
  sort: Sort;
}

export const DEFAULT_FILTERS: CallFilterState = {
  q: "",
  preset: "all",
  dateFrom: "",
  dateTo: "",
  direction: "",
  status: "",
  sentiment: "",
  lead: "",
  sort: "newest",
};

export const PRESETS: { id: Preset; label: string }[] = [
  { id: "all", label: "All time" },
  { id: "today", label: "Today" },
  { id: "7d", label: "Last 7 days" },
  { id: "30d", label: "Last 30 days" },
  { id: "custom", label: "Custom dates" },
];

/** Today's calendar date (YYYY-MM-DD) in the given IANA timezone. */
export function localDate(now: Date, tz: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  } catch {
    return now.toISOString().slice(0, 10);
  }
}

export function addDays(date: string, days: number): string {
  const [y, m, d] = date.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/** The inclusive calendar-day range a preset stands for, in the viewer's timezone. */
export function presetRange(preset: Preset, tz: string, now: Date): { from?: string; to?: string } {
  const today = localDate(now, tz);
  if (preset === "today") return { from: today, to: today };
  if (preset === "7d") return { from: addDays(today, -6), to: today };
  if (preset === "30d") return { from: addDays(today, -29), to: today };
  return {};
}

/** Why the filters can't be applied yet, or null. */
export function filterProblem(f: CallFilterState): string | null {
  if (f.preset === "custom" && f.dateFrom && f.dateTo && f.dateFrom > f.dateTo) return "The start date is after the end date.";
  return null;
}

export function searchTerm(q: string): string | null {
  const term = q.replace(/\s+/g, " ").trim();
  return term.length >= MIN_SEARCH_CHARS ? term : null;
}

/** The query string for GET /calls/browse. Empty filters are omitted; the server re-validates everything. */
export function toQuery(f: CallFilterState, tz: string, offset: number, now: Date = new Date()): Record<string, string> {
  const out: Record<string, string> = { tz, sort: f.sort, limit: String(PAGE_SIZE), offset: String(offset) };
  const term = searchTerm(f.q);
  if (term) out.q = term;
  const range = f.preset === "custom" ? { from: f.dateFrom || undefined, to: f.dateTo || undefined } : presetRange(f.preset, tz, now);
  if (range.from) out.date_from = range.from;
  if (range.to) out.date_to = range.to;
  if (f.direction) out.direction = f.direction;
  if (f.status) out.status = f.status;
  if (f.sentiment) out.sentiment = f.sentiment;
  if (f.lead) out.has_lead = f.lead === "with" ? "true" : "false";
  return out;
}

/** How many filters (not counting sort) are narrowing the list. */
export function activeFilterCount(f: CallFilterState): number {
  return [searchTerm(f.q) !== null, f.preset !== "all", f.direction !== "", f.status !== "", f.sentiment !== "", f.lead !== ""].filter(Boolean).length;
}

export interface PageInfo {
  from: number;
  to: number;
  hasPrev: boolean;
  hasNext: boolean;
}

export function pageInfo(total: number, offset: number, limit: number, shown: number): PageInfo {
  if (total === 0 || shown === 0) return { from: 0, to: 0, hasPrev: offset > 0, hasNext: false };
  return { from: offset + 1, to: offset + shown, hasPrev: offset > 0, hasNext: offset + shown < total };
}

/** The last page that has rows, for when a filter change or deletion leaves an offset past the end. */
export function clampOffset(offset: number, total: number, limit: number): number {
  if (total <= 0) return 0;
  return Math.min(offset, Math.floor((total - 1) / limit) * limit);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

/** "45 sec", "12 min", "3 h 12 min" — for totals. */
export function formatTotalTime(seconds: number): string {
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

/** "Today, 3:42 PM", "Yesterday, 9:05 AM", "Oct 9, 3:42 PM". Null when there is no real timestamp. */
export function formatCallTime(iso: string | null | undefined, tz: string, now: Date = new Date()): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const time = new Intl.DateTimeFormat(undefined, { timeZone: tz, hour: "numeric", minute: "2-digit" }).format(at);
  const day = localDate(at, tz);
  const today = localDate(now, tz);
  if (day === today) return `Today, ${time}`;
  if (day === addDays(today, -1)) return `Yesterday, ${time}`;
  const date = new Intl.DateTimeFormat(undefined, { timeZone: tz, month: "short", day: "numeric", year: day.slice(0, 4) === today.slice(0, 4) ? undefined : "numeric" }).format(at);
  return `${date}, ${time}`;
}

export function callerLabel(c: Pick<CallRow, "direction" | "caller_number">): string {
  if (c.direction === "test") return "Test call";
  return c.caller_number ? formatPhone(c.caller_number) : "Unknown caller";
}

export const DIRECTION_LABEL: Record<string, string> = { inbound: "Inbound", outbound: "Outbound", test: "Test (browser)" };

// Retell's documented disconnection reasons, in plain words. Anything not listed is shown humanized, never hidden.
const ENDED_BECAUSE: Record<string, string> = {
  user_hangup: "Caller hung up",
  agent_hangup: "Receptionist ended the call",
  call_transfer: "Transferred",
  voicemail_reached: "Reached voicemail",
  inactivity: "No one was speaking",
  max_duration_reached: "Reached the maximum call length",
  concurrency_limit_reached: "Too many calls at once",
  dial_busy: "Line was busy",
  dial_failed: "Couldn't connect",
  dial_no_answer: "No answer",
  invalid_destination: "Invalid number",
  user_declined: "Caller declined",
  marked_as_spam: "Marked as spam",
  scam_detected: "Flagged as a scam call",
  telephony_provider_permission_denied: "Phone network problem",
  telephony_provider_unavailable: "Phone network problem",
  sip_routing_error: "Phone network problem",
  no_valid_payment: "Couldn't be completed",
};

export function endedBecause(reason: string | null | undefined): string | null {
  if (!reason) return null;
  if (ENDED_BECAUSE[reason]) return ENDED_BECAUSE[reason];
  if (reason.startsWith("error_")) return "Technical problem";
  const words = reason.replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : null;
}

export interface CallStatusView {
  label: string;
  tone: Tone;
}

const NOT_CONNECTED = new Set(["dial_busy", "dial_no_answer", "user_declined"]);
const FAILED = new Set(["dial_failed", "invalid_destination", "telephony_provider_permission_denied", "telephony_provider_unavailable", "sip_routing_error"]);

/** What happened to the call, from the status and end reason we stored — nothing inferred beyond that. */
export function callStatus(c: Pick<CallRow, "status" | "disconnect_reason" | "started_at">, now: number = Date.now()): CallStatusView {
  const reason = c.disconnect_reason ?? "";
  switch (c.status) {
    case "error":
      return { label: "Failed", tone: "bad" };
    case "ended":
      if (reason.startsWith("error_") || FAILED.has(reason)) return { label: "Failed", tone: "bad" };
      if (NOT_CONNECTED.has(reason)) return { label: "Not connected", tone: "warn" };
      if (reason === "voicemail_reached") return { label: "Voicemail", tone: "neutral" };
      return { label: "Completed", tone: "ok" };
    case "ongoing":
    case "registered": {
      const started = c.started_at ? new Date(c.started_at).getTime() : NaN;
      if (!Number.isNaN(started) && now - started > STALE_CALL_HOURS * 3600_000) return { label: "No end recorded", tone: "warn" };
      return { label: c.status === "ongoing" ? "In progress" : "Connecting", tone: "neutral" };
    }
    default:
      return { label: "Unknown", tone: "neutral" };
  }
}

export function sentimentView(sentiment: string | null | undefined): { label: string; tone: Tone } | null {
  switch ((sentiment ?? "").toLowerCase()) {
    case "positive":
      return { label: "Positive", tone: "ok" };
    case "negative":
      return { label: "Negative", tone: "bad" };
    case "neutral":
      return { label: "Neutral", tone: "neutral" };
    default:
      return null; // "unknown" or nothing recorded: say nothing rather than guess
  }
}

export const LEAD_STATUS_LABEL: Record<string, string> = { new: "New", contacted: "Contacted", booked: "Booked", closed: "Closed" };