import type { SubscriptionOut } from "./api";
import { formatDate } from "./billing";

export type Tone = "ok" | "warn" | "bad" | "neutral";

export interface StatusInfo {
  label: string;
  tone: Tone;
  headline: string;
  detail: string;
}

/** Translate the server's billing state into plain language. Entitlement itself comes from the server (`entitled`). */
export function describeSubscription(sub: SubscriptionOut | null, cancelPending = false): StatusInfo {
  if (sub === null) {
    return {
      label: "No plan",
      tone: "neutral",
      headline: "You don't have a plan yet",
      detail: "Choose a plan to get a phone number and put your receptionist on real calls.",
    };
  }
  const end = formatDate(sub.current_period_end);
  const cancelOn = formatDate(sub.cancel_effective_at);

  if (sub.entitled && cancelOn) {
    return {
      label: "Ending",
      tone: "warn",
      headline: `Your plan ends on ${cancelOn}`,
      detail: "Your receptionist keeps answering calls until then. You won't be charged again.",
    };
  }
  if (sub.entitled && (cancelPending || sub.cancel_requested_at) && !cancelOn) {
    return {
      label: "Cancellation requested",
      tone: "warn",
      headline: "Cancellation requested, awaiting confirmation",
      detail: `Your receptionist keeps answering calls. We'll show the confirmed end date here once the billing provider confirms it${end ? ` (your current billing period runs until ${end})` : ""}.`,
    };
  }
  if (sub.entitled && sub.status === "trialing") {
    return {
      label: "Trial",
      tone: "ok",
      headline: "Your trial is active",
      detail: end ? `Your trial runs until ${end}.` : "Your receptionist is live.",
    };
  }
  if (sub.entitled) {
    return {
      label: "Active",
      tone: "ok",
      headline: "Your plan is active",
      detail: end ? `Your plan renews on ${end}.` : "Your receptionist is live.",
    };
  }
  if (sub.status === "past_due") {
    const stops = sub.service_ends_at ? new Date(sub.service_ends_at) : null;
    const stillLive = stops !== null && stops.getTime() > Date.now();
    return {
      label: "Payment needed",
      tone: "bad",
      headline: "Your payment needs attention",
      detail: stillLive
        ? `Update your payment method to keep your receptionist active. It stays live until ${formatDate(sub.service_ends_at)}.`
        : "Your receptionist has stopped answering calls. Update your payment method to bring it back.",
    };
  }
  if (sub.status === "paused") {
    return {
      label: "Paused",
      tone: "bad",
      headline: "Your subscription is paused",
      detail: "Your receptionist isn't answering calls while your subscription is paused.",
    };
  }
  if (sub.status === "canceled") {
    return {
      label: "Ended",
      tone: "neutral",
      headline: "Your subscription has ended",
      detail: "Your receptionist is no longer answering calls. Choose a plan to start again.",
    };
  }
  if ((sub.status === "active" || sub.status === "trialing") && sub.plan_name === null) {
    return {
      label: "Needs review",
      tone: "warn",
      headline: "We couldn't confirm your plan details",
      detail: "Your payment is active, but we can't match it to a plan right now. Please try again shortly.",
    };
  }
  return { label: "Inactive", tone: "neutral", headline: "Your plan isn't active", detail: "Choose a plan to start again." };
}