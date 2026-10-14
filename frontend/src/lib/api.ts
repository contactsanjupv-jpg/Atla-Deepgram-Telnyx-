/**
 * Thin fetch wrapper for the Atla backend. Credentials are always included
 * so the session cookie round-trips — the backend is what enforces auth,
 * this file just makes sure the browser sends what it has.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    Object.setPrototypeOf(this, ApiError.prototype);
  }
}

/**
 * Turns any failure into words a business owner can act on. The backend writes
 * its 4xx messages for customers (including "This feature is available on
 * Growth. Upgrade to unlock it."); anything else becomes the fallback, so raw
 * status text, provider errors and stack traces never reach the screen.
 */
export function describeError(
  err: unknown,
  fallback = "Something went wrong. Please try again.",
): string {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      return "Your session has expired. Please sign in again.";
    }

    if (err.status === 403) {
      return "You don't have permission to do that. Ask the account owner.";
    }

    if ([400, 402, 409, 429].includes(err.status) && err.message) {
      return err.message;
    }
  }

  return fallback;
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });

  if (!res.ok) {
    let detail = res.statusText;

    try {
      const body = await res.json();

      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body.detail)) {
        detail =
          body.detail
            .map((e: { msg?: string }) => e.msg)
            .filter(Boolean)
            .join("; ") || detail;
      } else if (body.detail) {
        detail = JSON.stringify(body.detail);
      }
    } catch {
      // response wasn't JSON — fall back to statusText
    }

    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return res.json();
}

export interface Organization {
  id: string;
  name: string;
  role: string;
}

export interface StructuredBusinessInfo {
  business_name: string | null;
  description: string | null;
  industry: string | null;
  services: string[];
  address: string | null;
  phone: string | null;
  hours: Record<string, string> | null;
  faqs: { question: string; answer: string }[];
  policies: string[];
}

export interface Voice {
  id: string;
  retell_voice_id: string;
  name: string;
  provider: string;
  gender: string | null;
  accent: string | null;
  age_style: string | null;
  preview_url: string | null;
}

export interface PhoneNumberOut {
  id: string;
  number: string;
  area_code: string | null;
  country: string;
  status: string;
}

export interface AgentOut {
  id: string;
  name: string;
  status: string;
  retell_agent_id: string | null;
  synced: boolean;
}

export interface PlanFeature {
  id: string;
  label: string;
}

export interface PlanPrice {
  amount_minor: number; // lowest currency denomination, e.g. cents
  currency: string;
  interval: string; // "month" | "year" ...
  interval_count: number;
}

export interface PlanOut {
  id: string;
  name: string;
  features: PlanFeature[];
  price: PlanPrice | null; // null when billing can't be read — show no number, never a wrong one
  included_minutes: number; // monthly receptionist minutes included (the same number the credit ledger grants)
  baseline: string[]; // what every plan includes besides `features`
  when_credits_run_out: string;
}

export interface SubscriptionOut {
  plan_id: string;
  plan_name: string | null; // null when the plan isn't recognised
  status: string;

  // The server's verdict on whether paid features are unlocked right now.
  entitled: boolean;

  features: PlanFeature[]; // what the plan includes right now (empty when not entitled)

  current_period_start: string | null;
  current_period_end: string | null;

  // set when the customer cancelled but service continues until this date
  cancel_effective_at: string | null;

  // past_due only: when the receptionist stops answering if payment isn't fixed
  service_ends_at: string | null;
}

export interface OnboardingState {
  step: "website" | "review" | "voice" | "behavior" | "test" | "done";
  business_id: string | null;
  business_name: string | null;
  structured_info: StructuredBusinessInfo | null;
  agent: { id: string; synced: boolean; voice_id: string | null } | null;
  tested: boolean;
}

export interface UsageOut {
  period_start: string | null;
  period_end: string | null;
  calls_count: number;
  billable_seconds: number;
}

export interface CreditsOut {
  entitled: boolean;
  plan_name: string | null;
  period_start: string | null;
  period_end: string | null;
  included_granted_seconds: number;
  included_used_seconds: number;
  included_remaining_seconds: number;
  purchased_balance_seconds: number; // negative = owed after a refund
  available_seconds: number;
  status: "ok" | "low" | "empty";
  calls_handled: number;
  topup_min_minor: number;
  topup_max_minor: number;
  topup_cents_per_minute: number;
  free_test_calls_remaining: number;
  when_credits_run_out: string;
}

export interface DailyPoint {
  date: string; // YYYY-MM-DD in the viewer's timezone
  calls: number;
  credit_seconds: number;
}

export interface CreditEvent {
  id: string;
  kind: "plan_grant" | "topup" | "refund" | "chargeback" | "adjustment";
  seconds: number;
  note: string | null;
  occurred_at: string;
}

export interface TopupQuote {
  amount_minor: number;
  currency: string;
  credit_minutes: number;
  cents_per_minute: number;
}

export interface TopupPurchase {
  id: string;
  amount_minor: number;
  currency: string;
  credit_minutes: number;
  status: string;
  created_at: string;
}

export interface CheckoutState {
  state: "none" | "open" | "processing" | "expired" | "unconfirmed";
  plan: string | null;
  checkout_url: string | null;
  message: string;
  expires_in_seconds: number | null;
}

export interface TranscriptSegment {
  speaker: string; // "Receptionist" | "Caller"
  role: string;
  text: string;
  start_seconds: number | null;
  end_seconds: number | null;
}

export interface CallSearchHit {
  call_id: string;
  direction: string;
  caller_number: string | null;
  started_at: string | null;
  summary: string | null;
  matches: { speaker: string; snippet: string; start_seconds: number | null }[];
  match_count: number;
}

export interface AnalyticsDay {
  date: string;
  calls: number;
  credit_seconds: number;
  leads: number;
}

export interface AnalyticsOut {
  days: number;
  series: AnalyticsDay[];
  total_calls: number;
  total_credit_seconds: number;
  total_leads: number;
  leads_by_status: Record<string, number>;
  leads_awaiting_follow_up: number;
  oldest_unattended_lead_at: string | null;
}
export interface DashboardBucket {
  start: string; // local time of the bucket: "2026-10-12T14:00:00" (hourly) or "2026-10-12" (daily)
  calls: number;
  call_seconds: number;
  leads: number;
}

export interface DashboardTotals {
  calls: number;
  call_seconds: number;
  leads: number;
}

export interface AwaitingLead {
  id: string;
  name: string | null;
  phone: string | null;
  reason: string | null;
  created_at: string;
  call_id: string | null;
}

export interface DashboardSummary {
  range: "today" | "7d" | "30d";
  granularity: "hour" | "day";
  series: DashboardBucket[];
  totals: DashboardTotals;
  previous: DashboardTotals;
  leads_awaiting_follow_up: number;
  oldest_unattended_lead_at: string | null;
  awaiting_leads: AwaitingLead[];
}

export interface CallOut {
  id: string;
  direction: string;
  caller_number: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  status: string | null;
  disconnect_reason: string | null;
  summary: string | null;
  sentiment: string | null;
}

export interface CallRow extends CallOut {
  lead_id: string | null;
  lead_name: string | null;
  lead_status: string | null;
}

export interface CallPage {
  items: CallRow[];
  total: number; // calls matching the filters
  total_seconds: number; // time on those calls
  leads_captured: number; // of those calls, how many produced a lead
  limit: number;
  offset: number;
}

export interface LeadOut {
  id: string;
  name: string | null;
  phone: string | null;
  email: string | null;
  reason: string | null;
  summary: string | null;
  status: string;
  source: string;
  created_at: string;
}

export const api = {
  signup: (
    data: {
      email: string;
      password: string;
      organization_name: string;
    },
  ) =>
    request<{ id: string; email: string }>("/api/v1/auth/signup", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  login: (
    data: {
      email: string;
      password: string;
    },
  ) =>
    request<{ id: string; email: string }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  logout: () =>
    request<void>("/api/v1/auth/logout", {
      method: "POST",
    }),

  me: () =>
    request<{ id: string; email: string }>("/api/v1/auth/me"),

  myOrganizations: () =>
    request<Organization[]>("/api/v1/orgs/me"),

  listPlans: () =>
    request<PlanOut[]>("/api/v1/plans"),

  importWebsite: (orgId: string, url: string) =>
    request<{
      business_id: string;
      pages_fetched: string[];
      structured_info: StructuredBusinessInfo;
    }>(`/api/v1/orgs/${orgId}/businesses/import-website`, {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  createManualBusiness: (orgId: string, name: string) =>
    request<{
      id: string;
      name: string;
      status: string;
    }>(`/api/v1/orgs/${orgId}/businesses/create-manual`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  getBusiness: (orgId: string, businessId: string) =>
    request<Record<string, unknown>>(
      `/api/v1/orgs/${orgId}/businesses/${businessId}`,
    ),

  approveBusiness: (
    orgId: string,
    businessId: string,
    data: {
      name: string;
      industry: string | null;
      address: string | null;
      description: string | null;
      hours: Record<string, string> | null;
      phone: string | null;
      services: string[];
      faqs: { question: string; answer: string }[];
      policies: string[];
    },
  ) =>
    request<Record<string, unknown>>(
      `/api/v1/orgs/${orgId}/businesses/${businessId}/approve`,
      {
        method: "POST",
        body: JSON.stringify(data),
      },
    ),

  listVoices: (
    params?: {
      gender?: string;
      accent?: string;
      search?: string;
    },
  ) => {
    const qs = new URLSearchParams(
      params as Record<string, string>,
    ).toString();

    return request<Voice[]>(
      `/api/v1/voices${qs ? `?${qs}` : ""}`,
    );
  },

  purchasePhoneNumber: (
    orgId: string,
    country: string,
    areaCode?: string,
  ) =>
    request<PhoneNumberOut>(
      `/api/v1/orgs/${orgId}/phone-numbers`,
      {
        method: "POST",
        body: JSON.stringify({
          country,
          area_code: areaCode || null,
        }),
      },
    ),

  getOnboarding: (orgId: string) =>
    request<OnboardingState>(
      `/api/v1/orgs/${orgId}/onboarding`,
    ),

  getUsage: (orgId: string) =>
    request<UsageOut>(
      `/api/v1/orgs/${orgId}/usage`,
    ),

  requestPasswordReset: (email: string) =>
    request<void>("/api/v1/auth/password-reset/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),

  confirmPasswordReset: (
    token: string,
    new_password: string,
  ) =>
    request<void>("/api/v1/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({
        token,
        new_password,
      }),
    }),

  changePassword: (
    current_password: string,
    new_password: string,
  ) =>
    request<void>("/api/v1/auth/password/change", {
      method: "POST",
      body: JSON.stringify({
        current_password,
        new_password,
      }),
    }),

  deleteAccount: (
    orgId: string,
    confirm_name: string,
    password: string,
  ) =>
    request<void>(
      `/api/v1/orgs/${orgId}/delete`,
      {
        method: "POST",
        body: JSON.stringify({
          confirm_name,
          password,
        }),
      },
    ),

  cancelSubscription: (orgId: string) =>
    request<{ status: string }>(
      `/api/v1/orgs/${orgId}/billing/cancel`,
      {
        method: "POST",
      },
    ),

  getBillingManage: (orgId: string) =>
    request<{
      update_payment_method_url: string | null;
    }>(
      `/api/v1/orgs/${orgId}/billing/manage`,
    ),

  getCredits: (orgId: string) => request<CreditsOut>(`/api/v1/orgs/${orgId}/usage/credits`),
  getDailyUsage: (orgId: string, days: number, tz: string) =>
    request<DailyPoint[]>(`/api/v1/orgs/${orgId}/usage/daily?${new URLSearchParams({ days: String(days), tz })}`),
  getCreditEvents: (orgId: string) => request<CreditEvent[]>(`/api/v1/orgs/${orgId}/usage/events?limit=10`),
  getTopupQuote: (orgId: string, amountMinor: number) =>
    request<TopupQuote>(`/api/v1/orgs/${orgId}/billing/topup-quote?${new URLSearchParams({ amount_minor: String(amountMinor) })}`),
  createTopup: (orgId: string, amountMinor: number) =>
    request<{ checkout_url: string; amount_minor: number; credit_minutes: number }>(`/api/v1/orgs/${orgId}/billing/topups`, {
      method: "POST",
      body: JSON.stringify({ amount_minor: amountMinor }),
    }),
  listTopups: (orgId: string) => request<TopupPurchase[]>(`/api/v1/orgs/${orgId}/billing/topups`),
  getCheckoutState: (orgId: string) => request<CheckoutState>(`/api/v1/orgs/${orgId}/billing/checkout`),
  getDashboardSummary: (orgId: string, range: "today" | "7d" | "30d", tz: string) =>
  request<DashboardSummary>(`/api/v1/orgs/${orgId}/dashboard/summary?${new URLSearchParams({ range, tz })}`),
  getSubscription: (orgId: string) =>
    request<SubscriptionOut | null>(
      `/api/v1/orgs/${orgId}/billing/subscription`,
    ),

  createCheckoutSession: (
    orgId: string,
    plan: "starter" | "growth",
  ) =>
    request<{ checkout_url: string }>(
      `/api/v1/orgs/${orgId}/billing/checkout-session`,
      {
        method: "POST",
        body: JSON.stringify({ plan }),
      },
    ),

  listPhoneNumbers: (orgId: string) =>
    request<PhoneNumberOut[]>(
      `/api/v1/orgs/${orgId}/phone-numbers`,
    ),

  upsertAgent: (
    orgId: string,
    businessId: string,
    data: {
      name: string;
      greeting: string | null;
      personality: string;
      language: string;
      voice_id: string;
      tasks: Record<string, boolean>;
      transfer_number: string | null;
      business_hours: Record<string, string> | null;
      after_hours_behavior: string | null;
    },
  ) =>
    request<AgentOut>(
      `/api/v1/orgs/${orgId}/agents/${businessId}`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      },
    ),

  listAgents: (orgId: string) =>
    request<AgentOut[]>(
      `/api/v1/orgs/${orgId}/agents`,
    ),

  startTestCall: (
    orgId: string,
    agentId: string,
  ) =>
    request<{
      call_id: string;
      access_token: string;
      max_seconds: number;
    }>(
      `/api/v1/orgs/${orgId}/agents/${agentId}/test-call`,
      {
        method: "POST",
      },
    ),

  activate: (
    orgId: string,
    agentId: string,
    phoneNumberId: string,
  ) =>
    request<{
      phone_number_id: string;
      agent_id: string;
      status: string;
    }>(
      `/api/v1/orgs/${orgId}/activate`,
      {
        method: "POST",
        body: JSON.stringify({
          agent_id: agentId,
          phone_number_id: phoneNumberId,
        }),
      },
    ),

  listCalls: (
    orgId: string,
    params?: {
      limit?: number;
      offset?: number;
    },
  ) => {
    const qs = new URLSearchParams(
      params as unknown as Record<string, string>,
    ).toString();

    return request<CallOut[]>(
      `/api/v1/orgs/${orgId}/calls${qs ? `?${qs}` : ""}`,
    );
  },

  browseCalls: (orgId: string, params: Record<string, string>) =>
    request<CallPage>(`/api/v1/orgs/${orgId}/calls/browse?${new URLSearchParams(params)}`),
  getCall: (
    orgId: string,
    callId: string,
  ) =>
    request<CallRow>(
      `/api/v1/orgs/${orgId}/calls/${callId}`,
    ),

  getCallTranscript: (orgId: string, callId: string) =>
    request<{ call_id: string; segments: TranscriptSegment[]; text: string | null }>(`/api/v1/orgs/${orgId}/calls/${callId}/transcript`),
  searchCalls: (orgId: string, q: string) =>
    request<CallSearchHit[]>(`/api/v1/orgs/${orgId}/calls/search?${new URLSearchParams({ q })}`),
  getAnalytics: (orgId: string, days: number, tz: string) =>
    request<AnalyticsOut>(`/api/v1/orgs/${orgId}/analytics/overview?${new URLSearchParams({ days: String(days), tz })}`),

  listLeads: (
    orgId: string,
    params?: {
      status?: string;
      limit?: number;
      offset?: number;
    },
  ) => {
    const qs = new URLSearchParams(
      params as unknown as Record<string, string>,
    ).toString();

    return request<LeadOut[]>(
      `/api/v1/orgs/${orgId}/leads${qs ? `?${qs}` : ""}`,
    );
  },

  updateLeadStatus: (
    orgId: string,
    leadId: string,
    status: string,
  ) =>
    request<LeadOut>(
      `/api/v1/orgs/${orgId}/leads/${leadId}`,
      {
        method: "PATCH",
        body: JSON.stringify({ status }),
      },
    ),
};