"use client";

import { createContext, useContext } from "react";
import type { Organization } from "@/lib/api";

export type BusinessState = "loading" | "none" | "ready" | "error";

export interface SettingsCtx {
  org: Organization;
  businessId: string | null;
  businessState: BusinessState;
}

export const SettingsContext = createContext<SettingsCtx | null>(null);

export function useSettings(): SettingsCtx {
  const v = useContext(SettingsContext);
  if (!v) throw new Error("useSettings must be used inside the settings layout");
  return v;
}
