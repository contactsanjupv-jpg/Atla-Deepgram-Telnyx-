"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, AgentConfig, describeError } from "@/lib/api";
import { saveAgentSettings } from "@/lib/agentConfigSave";
import { LANGUAGE_OPTIONS, loadedState, TASK_LABELS, validateReceptionist, type SaveOutcome } from "@/lib/settingsLogic";
import { BTN, Field, Group, INPUT, SaveBar, SaveStatus, SettingsSection } from "./ui";

/** Receptionist view: name, greeting, personality, language and call behaviour. The voice has its own view. */
export function ReceptionistSection({ orgId, businessId }: { orgId: string; businessId: string }) {
  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<SaveOutcome>({ state: "idle", message: null });
  const [form, setForm] = useState({ name: "", greeting: "", personality: "professional", language: "en-US", transfer_number: "" });
  const [tasks, setTasks] = useState<Record<string, boolean>>({});
  const dirty = useRef(false);

  function apply(c: AgentConfig) {
    setCfg(c);
    setForm({ name: c.name, greeting: c.greeting ?? "", personality: c.personality, language: c.language, transfer_number: c.transfer_number ?? "" });
    setTasks(c.tasks);
  }

  useEffect(() => {
    let cancelled = false;
    api
      .getAgentConfig(orgId, businessId)
      .then((c) => {
        if (cancelled) return;
        apply(c);
        setOutcome(loadedState(c.synced));
      })
      .catch((e) => {
        if (cancelled) return;
        if (e?.status === 404) setMissing(true);
        else setLoadError(describeError(e, "We couldn't load your receptionist settings."));
      });
    return () => { cancelled = true; };
  }, [orgId, businessId]);

  const edit = (patch: Partial<typeof form>) => { dirty.current = true; setForm((f) => ({ ...f, ...patch })); };

  if (loadError) return <SettingsSection id="receptionist" title="Receptionist"><p className="text-[14px] text-[#7a2416]">{loadError}</p></SettingsSection>;
  if (missing) return <SettingsSection id="receptionist" title="Receptionist"><p className="text-[14px] text-[var(--color-ink-soft)]">Your receptionist isn&apos;t set up yet. Finish setup first.</p></SettingsSection>;
  if (!cfg) return <SettingsSection id="receptionist" title="Receptionist"><p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p></SettingsSection>;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!cfg) return;
    const problem = validateReceptionist({ ...form, voice_id: cfg.voice_id, tasks });
    if (problem) return setOutcome({ state: "error", message: `Not saved. ${problem}` });
    setOutcome({ state: "saving", message: null });
    const { outcome: result, fresh } = await saveAgentSettings(orgId, businessId, {
      name: form.name.trim(),
      greeting: form.greeting.trim() || null,
      personality: form.personality,
      language: form.language,
      tasks,
      transfer_number: tasks.transfer_calls ? form.transfer_number.replace(/[\s().-]/g, "") : null,
    });
    if (result.state !== "error") {
      dirty.current = false;
      if (fresh) apply(fresh);
    }
    setOutcome(result);
  }

  return (
    <SettingsSection id="receptionist" title="Receptionist" description="How your receptionist introduces itself and what it does on a call.">
      <form onSubmit={save} className="space-y-5">
        <Group title="Identity" hint="Who callers hear and how they are greeted.">
          <Field label="Receptionist name"><input className={INPUT} value={form.name} maxLength={255} onChange={(e) => edit({ name: e.target.value })} /></Field>
          <Field label="Greeting"><textarea className={INPUT} rows={2} maxLength={500} value={form.greeting} onChange={(e) => edit({ greeting: e.target.value })} /></Field>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Personality">
              <select className={INPUT} value={form.personality} onChange={(e) => edit({ personality: e.target.value })}>
                {cfg.personalities.map((p) => <option key={p} value={p}>{p[0].toUpperCase() + p.slice(1)}</option>)}
              </select>
            </Field>
            <Field label="Language">
              <select className={INPUT} value={form.language} onChange={(e) => edit({ language: e.target.value })}>
                {(LANGUAGE_OPTIONS.includes(form.language) ? LANGUAGE_OPTIONS : [form.language, ...LANGUAGE_OPTIONS]).map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
            </Field>
          </div>
          <p className="text-[13px] text-[var(--color-ink-soft)]">
            The voice is chosen separately in <Link href="/dashboard/settings/voice" className="font-medium underline underline-offset-2">Voice</Link>.
          </p>
        </Group>

        <Group title="Capabilities" hint="What your receptionist does on a call.">
          <fieldset className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            <legend className="sr-only">What your receptionist does</legend>
            {cfg.supported_tasks.map((t) => (
              <label key={t} className="flex min-h-11 items-center gap-3 rounded-lg border border-[var(--color-line)] px-3 py-2 text-[14.5px] sm:min-h-0">
                <input type="checkbox" className="h-4 w-4 shrink-0" checked={!!tasks[t]} onChange={() => { dirty.current = true; setTasks((x) => ({ ...x, [t]: !x[t] })); }} />
                {TASK_LABELS[t] ?? t}
              </label>
            ))}
          </fieldset>
          {tasks.transfer_calls && (
            <Field label="Transfer number" className="sm:max-w-xs"><input className={INPUT} inputMode="tel" placeholder="+14155551234" value={form.transfer_number} onChange={(e) => edit({ transfer_number: e.target.value })} /></Field>
          )}
        </Group>

        <SaveBar>
          <button type="submit" className={BTN} disabled={outcome.state === "saving"}>{outcome.state === "saving" ? "Saving…" : "Save receptionist"}</button>
          <SaveStatus outcome={outcome} />
        </SaveBar>
      </form>
    </SettingsSection>
  );
}
