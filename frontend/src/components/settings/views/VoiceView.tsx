"use client";

import { useSettings } from "../SettingsContext";
import { VoiceSection } from "../VoiceSection";
import { RequireBusiness } from "./RequireBusiness";

export function VoiceView() {
  const { org } = useSettings();
  return <RequireBusiness>{(businessId) => <VoiceSection orgId={org.id} businessId={businessId} />}</RequireBusiness>;
}
