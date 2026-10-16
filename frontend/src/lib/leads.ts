import type { LeadRow } from "./api";
import { PAGE_SIZE, formatDuration, searchTerm } from "./calls";
import { formatPhone } from "./receptionistStatus";

export { PAGE_SIZE, MIN_SEARCH_CHARS, searchTerm, clampOffset, pageInfo } from "./calls";

export const LEAD_STATUSES = ["new", "contacted", "booked", "closed"] as const;
export type LeadStatusId = (typeof LEAD_STATUSES)[number];
export type LeadSort = "newest" | "oldest";

export const LEAD_STATUS_TEXT: Record<LeadStatusId, string> = { new: "New", contacted: "Contacted", booked: "Booked", closed: "Closed" };

export interface LeadFilterState {
  q: string;
  status: "" | LeadStatusId;
  sort: LeadSort;
}

export const DEFAULT_LEAD_FILTERS: LeadFilterState = { q: "", status: "", sort: "newest" };

/** The query string for GET /leads/browse. Empty filters are omitted; the server re-validates everything. */
export function toLeadQuery(f: LeadFilterState, offset: number): Record<string, string> {
  const out: Record<string, string> = { sort: f.sort, limit: String(PAGE_SIZE), offset: String(offset) };
  const term = searchTerm(f.q);
  if (term) out.q = term;
  if (f.status) out.status = f.status;
  return out;
}

/** Search or a status tab is narrowing the list (sort alone is not a filter). */
export function leadsNarrowed(f: LeadFilterState): boolean {
  return searchTerm(f.q) !== null || f.status !== "";
}

/** The lead's own name as captured on the call. Never a guess: missing stays "Unnamed lead". */
export function leadName(l: Pick<LeadRow, "name">): string {
  return l.name?.trim() || "Unnamed lead";
}

/** Digits for a tel: link, or null when the stored phone has none. */
export function telHref(phone: string | null): string | null {
  if (!phone) return null;
  const digits = phone.replace(/[^\d+]/g, "");
  return digits.replace(/\D/g, "").length >= 3 ? `tel:${digits}` : null;
}

export function leadPhone(phone: string | null): string | null {
  return phone ? formatPhone(phone) : null;
}

/** "Inbound call · 1:35" — what the linked call was, from stored values only. */
export function callBlurb(l: Pick<LeadRow, "call_id" | "call_direction" | "call_duration_seconds">): string | null {
  if (!l.call_id) return null;
  const kind = l.call_direction === "outbound" ? "Outbound call" : l.call_direction === "test" ? "Test call" : "Inbound call";
  return l.call_duration_seconds != null ? `${kind} · ${formatDuration(l.call_duration_seconds)}` : kind;
}

export function isLeadStatus(v: string): v is LeadStatusId {
  return (LEAD_STATUSES as readonly string[]).includes(v);
}
