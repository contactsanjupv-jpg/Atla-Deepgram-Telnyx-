import type { AgentOut, CreditsOut, DashboardBucket, PhoneNumberOut, SubscriptionOut } from "./api";
import { SETTINGS_BILLING_HREF } from "./settingsRoutes";
import { describeSubscription } from "./billingState";
import { creditBanner } from "./credits";
import type { ReceptionistStatus } from "./receptionistStatus";

export type DashboardRange = "today" | "7d" | "30d";

export const RANGES: { id: DashboardRange; label: string; compareTo: string }[] = [
  { id: "today", label: "Today", compareTo: "yesterday" },
  { id: "7d", label: "7 days", compareTo: "the previous 7 days" },
  { id: "30d", label: "30 days", compareTo: "the previous 30 days" },
];

export interface Delta {
  direction: "up" | "down" | "flat" | "new" | "none";
  text: string;
}

/**
 * How this period compares with the one before it. A percentage is only shown when the previous period
 * had something to compare against; going from nothing to something is reported as such, never as a %.
 */
export function compare(current: number, previous: number, compareTo: string): Delta {
  if (previous === 0 && current === 0) return { direction: "none", text: "" };
  if (previous === 0) return { direction: "new", text: compareTo === "yesterday" ? "None yesterday" : `None in ${compareTo}` };
  const pct = Math.round(((current - previous) / previous) * 100);
  if (pct === 0) return { direction: "flat", text: `Same as ${compareTo}` };
  return { direction: pct > 0 ? "up" : "down", text: `${pct > 0 ? "+" : "−"}${Math.abs(pct)}% vs ${compareTo}` };
}

/** "2026-10-12T14:00:00" -> "2 PM". Parsed from the string: the server already bucketed in the viewer's timezone. */
export function hourLabel(start: string): string {
  const hour = Number(start.slice(11, 13));
  if (Number.isNaN(hour)) return start;
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  return `${h12} ${hour < 12 ? "AM" : "PM"}`;
}

export function hasAnyActivity(series: DashboardBucket[]): boolean {
  return series.some((b) => b.calls > 0 || b.leads > 0);
}

export type AttentionTone = "bad" | "warn" | "neutral";

export interface AttentionItem {
  id: string;
  tone: AttentionTone;
  title: string;
  detail: string;
  cta: { label: string; href: string };
}

export const LEAD_STALE_HOURS = 24;

const ORDER: Record<AttentionTone, number> = { bad: 0, warn: 1, neutral: 2 };

/**
 * Things the customer can actually act on, each derived from data the backend returned.
 * Nothing is listed unless it is true right now, and an empty list means there is nothing to do.
 */
export function attentionItems(input: {
  status: ReceptionistStatus | null;
  sub: SubscriptionOut | null | undefined;
  agent: AgentOut | null;
  phone: PhoneNumberOut | null;
  credits: CreditsOut | null;
  awaitingLeads: number;
  oldestAwaitingAt: string | null;
  now?: number;
}): AttentionItem[] {
  const { status, sub, agent, phone, credits, awaitingLeads, oldestAwaitingAt } = input;
  const now = input.now ?? Date.now();
  const items: AttentionItem[] = [];

  if (status?.action) {
    items.push({
      id: "receptionist",
      tone: status.tone === "bad" ? "bad" : status.tone === "warn" ? "warn" : "neutral",
      title: status.label,
      detail: status.detail,
      cta: status.action,
    });
  }

  const billingAlreadyShown = status?.action?.href === SETTINGS_BILLING_HREF;
  if (sub && !billingAlreadyShown) {
    const info = describeSubscription(sub);
    if (info.tone === "warn" || info.tone === "bad") {
      items.push({ id: "plan", tone: info.tone, title: info.headline, detail: info.detail, cta: { label: "Manage plan", href: SETTINGS_BILLING_HREF } });
    }
  }

  if (credits) {
    const banner = creditBanner(credits);
    if (banner) {
      items.push({ id: "credits", tone: banner.tone === "empty" ? "bad" : "warn", title: banner.title, detail: banner.body, cta: { label: "Buy more credits", href: "#credits" } });
    }
  }

  if (awaitingLeads > 0) {
    const oldest = oldestAwaitingAt ? new Date(oldestAwaitingAt) : null;
    const stale = oldest !== null && !Number.isNaN(oldest.getTime()) && now - oldest.getTime() > LEAD_STALE_HOURS * 3600_000;
    items.push({
      id: "leads",
      tone: stale ? "warn" : "neutral",
      title: `${awaitingLeads} ${awaitingLeads === 1 ? "lead is" : "leads are"} waiting for a follow-up`,
      detail: oldest && !Number.isNaN(oldest.getTime()) ? `The oldest came in ${oldest.toLocaleDateString(undefined, { month: "short", day: "numeric" })}.` : "Reach out while they're still interested.",
      cta: { label: "Review leads", href: "/dashboard/leads" },
    });
  }

  if (agent && sub?.entitled && !phone) {
    items.push({ id: "phone", tone: "warn", title: "No business number connected", detail: "Your receptionist has no number to answer yet.", cta: { label: "Set up your number", href: "/dashboard/go-live" } });
  }

  return items.sort((a, b) => ORDER[a.tone] - ORDER[b.tone]);
}

/** "2 min ago", "3 h ago", "Oct 9" — from a real timestamp; null when there isn't one. */
export function relativeTime(iso: string | null | undefined, now = Date.now()): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const mins = Math.floor((now - t) / 60_000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)} h ago`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}