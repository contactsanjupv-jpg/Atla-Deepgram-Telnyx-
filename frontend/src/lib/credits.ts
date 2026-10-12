import type { CreditsOut, DailyPoint } from "./api";

/** Customer-facing credit unit: minutes of receptionist time. The database stores seconds. */
export function minutesLabel(seconds: number): string {
  const minutes = Math.floor(Math.abs(seconds) / 60);
  return `${minutes.toLocaleString()} min`;
}

export function formatDollars(amountMinor: number, currency = "USD"): string {
  const whole = amountMinor % 100 === 0;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(amountMinor / 100);
}

export type CreditView =
  | { kind: "unmetered" } // no plan / no grant yet: there is no balance to show
  | { kind: "metered"; tone: "ok" | "low" | "empty"; remaining: string; used: string; granted: string; usedPercent: number; owes: string | null; purchased: string };

/** What the dashboard says about the balance. Straight from the server's numbers; nothing is estimated. */
export function creditView(c: CreditsOut): CreditView {
  if (!c.entitled && c.included_granted_seconds === 0 && c.purchased_balance_seconds === 0) return { kind: "unmetered" };
  const granted = c.included_granted_seconds;
  const usedPercent = granted > 0 ? Math.min(100, Math.round((c.included_used_seconds / granted) * 100)) : 0;
  return {
    kind: "metered",
    tone: c.status,
    remaining: minutesLabel(Math.max(0, c.available_seconds)),
    used: minutesLabel(c.included_used_seconds),
    granted: minutesLabel(granted),
    usedPercent,
    owes: c.purchased_balance_seconds < 0 ? minutesLabel(c.purchased_balance_seconds) : null,
    purchased: minutesLabel(Math.max(0, c.purchased_balance_seconds)),
  };
}

export function creditBanner(c: CreditsOut): { tone: "low" | "empty"; title: string; body: string } | null {
  if (!c.entitled) return null;
  if (c.status === "empty") {
    return {
      tone: "empty",
      title: "You're out of credits",
      body: "Your receptionist keeps answering for a short grace period, then pauses until you add credits.",
    };
  }
  if (c.status === "low") {
    return { tone: "low", title: "You're running low on credits", body: `About ${minutesLabel(c.available_seconds)} left. Add credits so calls are never missed.` };
  }
  return null;
}

export type TopupCheck = { ok: true; amountMinor: number } | { ok: false; message: string };

/** Client-side convenience only — the server re-validates every amount. Accepts "20", "20.5", "$1,250.00". */
export function validateTopup(input: string, minMinor: number, maxMinor: number): TopupCheck {
  const cleaned = input.replace(/[$,\s]/g, "");
  if (cleaned === "") return { ok: false, message: "Enter an amount." };
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return { ok: false, message: "Enter a dollar amount, like 25 or 25.50." };
  const minor = Math.round(Number(cleaned) * 100);
  if (minor < minMinor) return { ok: false, message: `The minimum top-up is ${formatDollars(minMinor)}.` };
  if (minor > maxMinor) return { ok: false, message: `The maximum top-up is ${formatDollars(maxMinor)}.` };
  return { ok: true, amountMinor: minor };
}

/** Quick-pick amounts derived from the server's minimum (never hardcoded). */
export function quickAmounts(minMinor: number, maxMinor: number): number[] {
  return [1, 2, 5].map((m) => minMinor * m).filter((a) => a <= maxMinor);
}

export interface ChartBar {
  label: string; // YYYY-MM-DD
  value: number;
  height: number; // 0..1 of the plot height
}

/** Bars scaled to the real maximum. All-zero data returns bars of height 0 (the caller shows the empty state). */
export function chartBars(points: DailyPoint[], pick: (p: DailyPoint) => number): { bars: ChartBar[]; max: number; empty: boolean } {
  const values = points.map(pick);
  const max = Math.max(0, ...values);
  return {
    bars: points.map((p, i) => ({ label: p.date, value: values[i], height: max > 0 ? values[i] / max : 0 })),
    max,
    empty: max === 0,
  };
}

export function dayLabel(date: string): string {
  const [y, m, d] = date.split("-").map(Number);
  if (!y || !m || !d) return date;
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export const EVENT_LABEL: Record<string, string> = {
  plan_grant: "Monthly minutes added",
  topup: "Credits purchased",
  refund: "Refund",
  chargeback: "Chargeback",
  adjustment: "Adjustment",
};

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
