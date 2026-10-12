import type { PlanFeature, PlanOut } from "./api";

export interface PlanHighlight {
  plan: PlanOut;
  /** Name of the plan this one builds on ("Everything in Starter, plus:"), or null for the first plan. */
  inherits: string | null;
  /** Features this plan has that the previous plan does not. For the first plan: all its features. */
  added: PlanFeature[];
  /** Extra monthly minutes compared with the previous plan (null for the first plan or if not larger). */
  extraMinutes: number | null;
  recommended: boolean;
}

/**
 * Turns the backend catalog into "what each plan adds". The catalog is ordered cheapest first; a plan builds on
 * the one before it. The recommended plan is the one that adds real features over the plan before it.
 */
export function planHighlights(plans: PlanOut[]): PlanHighlight[] {
  const ordered = [...plans];
  return ordered.map((plan, i) => {
    const prev = i > 0 ? ordered[i - 1] : null;
    const prevIds = new Set(prev?.features.map((f) => f.id) ?? []);
    const added = plan.features.filter((f) => !prevIds.has(f.id));
    const extra = prev && plan.included_minutes > prev.included_minutes ? plan.included_minutes - prev.included_minutes : null;
    return {
      plan,
      inherits: prev ? prev.name : null,
      added,
      extraMinutes: extra,
      recommended: prev !== null && (added.length > 0 || extra !== null) && i === ordered.length - 1,
    };
  });
}

export function minutesPerMonth(minutes: number): string {
  return `${minutes.toLocaleString()} minutes of receptionist time every month`;
}
