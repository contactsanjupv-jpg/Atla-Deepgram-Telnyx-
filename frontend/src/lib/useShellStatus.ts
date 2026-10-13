"use client";

import { useEffect, useState } from "react";
import { api, AgentOut, PhoneNumberOut, SubscriptionOut } from "./api";
import { describeSubscription } from "./billingState";
import { receptionistStatus, type ReceptionistStatus, type StatusTone } from "./receptionistStatus";

interface ShellData {
  agent: AgentOut | null;
  agentKnown: boolean;
  phone: PhoneNumberOut | null;
  phoneKnown: boolean;
  sub: SubscriptionOut | null | undefined; // undefined = couldn't be read
}

export interface PlanChip {
  name: string;
  state: string;
  tone: StatusTone;
}

const FRESH_MS = 10_000;
const cache = new Map<string, { at: number; data: ShellData }>();
const inflight = new Map<string, Promise<ShellData>>();

/** Call on sign-out so the next account never sees the previous one's status. */
export function clearShellStatus() {
  cache.clear();
  inflight.clear();
}

function fetchShell(orgId: string): Promise<ShellData> {
  const running = inflight.get(orgId);
  if (running) return running;
  const promise = Promise.allSettled([api.listAgents(orgId), api.listPhoneNumbers(orgId), api.getSubscription(orgId)])
    .then(([agents, phones, sub]) => {
      const data: ShellData = {
        agentKnown: agents.status === "fulfilled",
        agent: agents.status === "fulfilled" ? (agents.value[0] ?? null) : null,
        phoneKnown: phones.status === "fulfilled",
        phone: phones.status === "fulfilled" ? (phones.value.find((n) => n.status === "active") ?? null) : null,
        sub: sub.status === "fulfilled" ? sub.value : undefined,
      };

      cache.set(orgId, { at: Date.now(), data });
      return data;
    })
    .finally(() => inflight.delete(orgId));
  inflight.set(orgId, promise);
  return promise;
}

/**
 * Receptionist + plan status for the app shell. Served instantly from a short
 * cache while a fresh copy loads in the background, so moving between pages
 * doesn't flash a skeleton or hammer the API.
 */
export function useShellStatus(orgId: string | undefined) {
  const [data, setData] = useState<ShellData | null>(() => (orgId ? (cache.get(orgId)?.data ?? null) : null));

  useEffect(() => {
    if (!orgId) return;
    const cached = cache.get(orgId);
    if (cached && Date.now() - cached.at < FRESH_MS) return;
    let live = true;
    fetchShell(orgId).then((d) => {
      if (live) setData(d);
    });
    return () => {
      live = false;
    };
  }, [orgId]);

    if (!data)
    return {
      loading: true,
      status: null as ReceptionistStatus | null,
      plan: null as PlanChip | null,
      canTest: false,
      agent: null as AgentOut | null,
      phone: null as PhoneNumberOut | null,
      sub: undefined as SubscriptionOut | null | undefined,
      agentKnown: false,
      phoneKnown: false,
    };

  const status = data.agentKnown ? receptionistStatus({ agent: data.agent, phone: data.phone, sub: data.sub }) : null;
  let plan: PlanChip | null = null;
  if (data.sub === null) plan = { name: "No plan", state: "Choose a plan", tone: "neutral" };
  else if (data.sub) {
    const info = describeSubscription(data.sub);
    plan = { name: data.sub.plan_name ?? "Your plan", state: info.label, tone: info.tone };
  }
  return { loading: false, status, plan, canTest: data.agentKnown && data.agent !== null, agent: data.agent, phone: data.phone, sub: data.sub, agentKnown: data.agentKnown, phoneKnown: data.phoneKnown };
}