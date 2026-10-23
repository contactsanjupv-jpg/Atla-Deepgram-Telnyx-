import type { AgentOut, PhoneNumberOut, SubscriptionOut } from "./api";
import { SETTINGS_BILLING_HREF } from "./settingsRoutes";

export type StatusTone = "ok" | "warn" | "bad" | "neutral";

export interface ReceptionistStatus {
  state: "live" | "billing" | "ready" | "setup";
  label: string;
  detail: string;
  tone: StatusTone;
  action: { label: string; href: string } | null;
}

/** "+13055551234" -> "(305) 555-1234"; anything else is shown as stored. */
export function formatPhone(number: string): string {
  const digits = number.replace(/\D/g, "");
  if (number.startsWith("+1") && digits.length === 11) {
    return `(${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7)}`;
  }
  return number;
}

/**
 * What the sidebar says about the receptionist. Only states the data can prove:
 *  - live      the agent is active and the plan hasn't stopped service
 *  - billing   the agent is active but the plan is paused / ended / overdue past the grace period
 *  - ready     built and saved, but not answering real calls yet
 *  - setup     not built, or changes not saved yet
 * `sub`: null = no subscription, undefined = couldn't be read (no billing claim is made).
 */
export function receptionistStatus(input: {
  agent: AgentOut | null;
  phone: PhoneNumberOut | null;
  sub: SubscriptionOut | null | undefined;
  now?: number;
}): ReceptionistStatus {
  const { agent, phone, sub } = input;
  const now = input.now ?? Date.now();

  if (!agent) {
    return {
      state: "setup",
      label: "Setup needed",
      detail: "Finish setting up your receptionist.",
      tone: "neutral",
      action: { label: "Continue setup", href: "/onboarding" },
    };
  }

  if (agent.status === "active") {
    const graceEnds = sub?.service_ends_at ? new Date(sub.service_ends_at).getTime() : null;
    const inGrace = sub?.status === "past_due" && graceEnds !== null && graceEnds > now;
    const stopped =
      sub !== undefined &&
      (sub === null || sub.status === "paused" || sub.status === "canceled" || (sub.status === "past_due" && !inGrace));

    if (stopped) {
      return {
        state: "billing",
        label: "Not answering calls",
        detail:
          sub?.status === "paused"
            ? "Your subscription is paused."
            : sub?.status === "past_due"
              ? "Your payment is overdue."
              : "Your plan isn't active.",
        tone: "bad",
        action: { label: "Fix billing", href: SETTINGS_BILLING_HREF },
      };
    }
    if (inGrace) {
      return {
        state: "live",
        label: "Live",
        detail: "Your payment needs attention.",
        tone: "warn",
        action: { label: "Fix billing", href: SETTINGS_BILLING_HREF },
      };
    }
    return {
      state: "live",
      label: "Live",
      detail: phone ? formatPhone(phone.number) : "Answering calls",
      tone: "ok",
      action: null,
    };
  }

  if (!agent.synced) {
    return {
      state: "setup",
      label: "Setup needed",
      detail: "Your latest changes haven't been saved to your receptionist yet.",
      tone: "neutral",
      action: { label: "Continue setup", href: "/onboarding" },
    };
  }

  return {
    state: "ready",
    label: "Ready to go live",
    detail: "Not answering real calls yet.",
    tone: "neutral",
    action: { label: "Go live", href: "/dashboard/go-live" },
  };
}