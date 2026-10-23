"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, AgentConfig, describeError, Voice } from "@/lib/api";
import { saveAgentSettings } from "@/lib/agentConfigSave";
import { loadedState, type SaveOutcome } from "@/lib/settingsLogic";
import { BTN, SaveBar, SaveStatus, SettingsSection } from "./ui";
import { VoicePicker } from "./VoicePicker";

/** Voice view: the selected voice, the searchable catalogue and previews. Saves only the voice id. */
export function VoiceSection({ orgId, businessId }: { orgId: string; businessId: string }) {
  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<SaveOutcome>({ state: "idle", message: null });

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getAgentConfig(orgId, businessId).catch((e) => (e?.status === 404 ? null : Promise.reject(e))), api.listVoices()])
      .then(([c, v]) => {
        if (cancelled) return;
        setVoices(v);
        if (c === null) return setMissing(true);
        setCfg(c);
        setVoiceId(c.voice_id ?? "");
        setOutcome(loadedState(c.synced));
      })
      .catch((e) => !cancelled && setLoadError(describeError(e, "We couldn't load your voice settings.")));
    return () => { cancelled = true; };
  }, [orgId, businessId]);

  if (loadError) return <SettingsSection id="voice" title="Voice"><p className="text-[14px] text-[#7a2416]">{loadError}</p></SettingsSection>;
  if (missing) return <SettingsSection id="voice" title="Voice"><p className="text-[14px] text-[var(--color-ink-soft)]">Your receptionist isn&apos;t set up yet. Finish setup first.</p></SettingsSection>;
  if (!cfg) return <SettingsSection id="voice" title="Voice"><p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p></SettingsSection>;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!voiceId) return setOutcome({ state: "error", message: "Not saved. Choose a voice." });
    setOutcome({ state: "saving", message: null });
    const { outcome: result, fresh } = await saveAgentSettings(orgId, businessId, { voice_id: voiceId });
    if (result.state !== "error" && fresh) {
      setCfg(fresh);
      setVoiceId(fresh.voice_id ?? voiceId);
    }
    setOutcome(result);
  }

  return (
    <SettingsSection id="voice" title="Voice" description="Preview a few and pick the one that fits your business. Previews are generic sample clips.">
      <form onSubmit={save} className="space-y-4">
        <VoicePicker voices={voices} value={voiceId} onChange={setVoiceId} />
        <p className="text-[13px] text-[var(--color-ink-soft)]">
          Name, greeting and behaviour are in <Link href="/dashboard/settings/receptionist" className="font-medium underline underline-offset-2">Receptionist</Link>.
        </p>
        <SaveBar>
          <button type="submit" className={BTN} disabled={outcome.state === "saving"}>{outcome.state === "saving" ? "Saving…" : "Save voice"}</button>
          <SaveStatus outcome={outcome} />
        </SaveBar>
      </form>
    </SettingsSection>
  );
}
