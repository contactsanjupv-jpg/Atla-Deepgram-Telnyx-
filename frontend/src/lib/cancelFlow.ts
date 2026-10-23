import type { SubscriptionOut } from "./api";
import { formatDate } from "./billing";

/** Body of `POST /billing/cancel` (202). `effective_at` is the provider's date and can be null; it is NOT confirmation. */
export interface CancelResponse {
  status: string;
  effective_at: string | null;
}

/** What this browser session just did. Durable state lives on the server (`cancel_requested_at` / `cancel_effective_at`). */
export type CancelOutcome = { kind: "accepted"; alreadyScheduled: boolean; refreshFailed: boolean } | { kind: "error"; error: unknown };

/** How long an accepted request may stay unconfirmed before we say so and offer a retry. */
export const PENDING_STALE_AFTER_MS = 10 * 60 * 1000;

export type CancelPhase = "none" | "pending" | "stale" | "confirmed";

/**
 * THE RULE. A cancellation is confirmed if and only if the server's subscription says so:
 * `cancel_effective_at` is set (the billing provider's confirmed date) while the plan is entitled.
 * Otherwise it is pending when the server holds `cancel_requested_at` (durable, survives reload) or this
 * session just had a request accepted but could not yet read the server state. A response date from the
 * cancel call alone never counts as confirmation.
 */
export function cancelPhase(sub: SubscriptionOut | null, accepted: boolean, nowMs: number): CancelPhase {
  if (sub === null) return "none";
  if (sub.cancel_effective_at) return "confirmed";
  const requestedAt = sub.cancel_requested_at ? Date.parse(sub.cancel_requested_at) : NaN;
  if (!sub.entitled && !accepted) return "none";
  if (Number.isNaN(requestedAt)) return accepted ? "pending" : "none";
  return nowMs - requestedAt > PENDING_STALE_AFTER_MS ? "stale" : "pending";
}

export type NoticeTone = "ok" | "warn" | "bad";
export interface CancelNotice {
  tone: NoticeTone;
  title: string;
  body: string;
  /** Safe recovery action offered with the notice. */
  action: "refresh" | "retry" | null;
}

const KEEP = "Your receptionist keeps answering calls until then and you won't be charged again. Cancelling doesn't refund the current period.";

/** The one message to show, from the server state plus what this session just did. null = nothing to say. */
export function cancelNotice(
  outcome: CancelOutcome | null,
  sub: SubscriptionOut | null,
  phase: CancelPhase,
  describe: (err: unknown) => string,
): CancelNotice | null {
  if (outcome?.kind === "error") {
    return { tone: "bad", title: "We couldn't cancel your plan", body: describe(outcome.error), action: null };
  }
  if (phase === "confirmed" && sub && outcome?.kind === "accepted") {
    const date = formatDate(sub.cancel_effective_at) ?? "the end of your paid period";
    const already = outcome?.kind === "accepted" && outcome.alreadyScheduled;
    return {
      tone: "ok",
      title: already ? "Your cancellation was already scheduled" : "Cancellation scheduled",
      body: `Your plan ends on ${date}. ${KEEP}`,
      action: null,
    };
  }
  if (phase === "stale") {
    return {
      tone: "warn",
      title: "Cancellation requested, still not confirmed",
      body: "We haven't received confirmation from the billing provider yet. Your plan is still active and nothing has changed. Check again, or send the request again (it can't cancel twice).",
      action: "retry",
    };
  }
  if (phase === "pending") {
    const failed = outcome?.kind === "accepted" && outcome.refreshFailed;
    return {
      tone: "warn",
      title: "Cancellation requested, awaiting confirmation",
      body: failed
        ? "Your request was accepted, but we couldn't refresh your billing details just now. Your plan stays active until the cancellation is confirmed. Check again in a moment."
        : "Your request was accepted. Your plan stays active, and the end date will appear here as soon as the billing provider confirms it.",
      action: "refresh",
    };
  }
  return null;
}

export interface CancelDeps {
  cancel: () => Promise<CancelResponse>;
  /** Latest subscription (null = none). Throws when it can't be loaded. */
  refresh: () => Promise<SubscriptionOut | null>;
}

export interface CancelResult {
  outcome: CancelOutcome;
  sub: SubscriptionOut | null;
}

/**
 * One cancellation at a time, and no second request once one was accepted. `submit()` returns null when it ignored
 * the call. `allowRetry()` re-opens it, used only for a request the server still shows as unconfirmed after a while.
 */
export function createCancelController(deps: CancelDeps) {
  let inFlight = false;
  let accepted = false;
  return {
    get busy() {
      return inFlight;
    },
    allowRetry() {
      if (!inFlight) accepted = false;
    },
    async submit(): Promise<CancelResult | null> {
      if (inFlight || accepted) return null;
      inFlight = true;
      try {
        let response: CancelResponse;
        try {
          response = await deps.cancel();
        } catch (error) {
          return { outcome: { kind: "error", error }, sub: null };
        }
        accepted = true;
        let sub: SubscriptionOut | null = null;
        let refreshFailed = false;
        try {
          sub = await deps.refresh();
        } catch {
          refreshFailed = true;
        }
        return { outcome: { kind: "accepted", alreadyScheduled: response.status === "already_scheduled", refreshFailed }, sub };
      } finally {
        inFlight = false;
      }
    },
  };
}
