"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, BusinessProfile, describeError } from "@/lib/api";
import {
  buildProfileUpdate, classifySaveError, draftFromProfile, reconcileProfileSave, removeItem, SAVED, validateProfile,
  type AgentSyncStatus, type ProfileDraft, type SaveOutcome,
} from "@/lib/settingsLogic";
import { BTN, BTN_GHOST, BTN_LINK, Collapsible, Field, Group, INPUT, SaveBar, SaveStatus, SettingsSection } from "./ui";

export function BusinessProfileSection({
  orgId, businessId,
}: { orgId: string; businessId: string }) {
  const [profile, setProfile] = useState<BusinessProfile | null>(null);
  const [draft, setDraft] = useState<ProfileDraft | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<SaveOutcome>({ state: "idle", message: null });

  const load = useCallback(async () => {
    const p = await api.getBusinessProfile(orgId, businessId);
    setProfile(p);
    setDraft(draftFromProfile(p));
    return p;
  }, [orgId, businessId]);

  useEffect(() => {
    let cancelled = false;
    api
      .getBusinessProfile(orgId, businessId)
      .then((p) => {
        if (cancelled) return;
        setProfile(p);
        setDraft(draftFromProfile(p));
      })
      .catch((e) => !cancelled && setLoadError(describeError(e, "We couldn't load your business profile.")));
    return () => {
      cancelled = true;
    };
  }, [orgId, businessId]);

  if (loadError) return <SettingsSection id="business" title="Business profile"><p className="text-[14px] text-[#7a2416]">{loadError}</p></SettingsSection>;
  if (!profile || !draft) return <SettingsSection id="business" title="Business profile"><p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p></SettingsSection>;

  const set = <K extends keyof ProfileDraft>(k: K, v: ProfileDraft[K]) => setDraft({ ...draft, [k]: v });

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!profile || !draft) return;
    const problem = validateProfile(draft);
    if (problem) return setOutcome({ state: "error", message: `Not saved. ${problem}` });
    setOutcome({ state: "saving", message: null });
    let raw: SaveOutcome = SAVED;
    try {
      await api.updateBusinessProfile(orgId, businessId, buildProfileUpdate(profile, draft));
    } catch (err) {
      raw = classifySaveError(err);
    }
    if (raw.state === "error") {
      // Not persisted (validation / permission / network): keep exactly what the user typed.
      setOutcome(raw);
      return;
    }
    // Persisted (the backend commits before it contacts the provider). Ask what the receptionist looks like now.
    let agent: AgentSyncStatus = "unknown";
    try {
      agent = (await api.getAgentConfig(orgId, businessId)).synced ? "synced" : "unsynced";
    } catch (err) {
      agent = err instanceof ApiError && err.status === 404 ? "none" : "unknown";
    }
    const result = reconcileProfileSave(raw, agent);
    if (result.reloadDraft) {
      try {
        await load(); // server copy (gives new items their ids); on failure keep the draft
      } catch {
        /* keep draft */
      }
    }
    setOutcome(result.outcome);
  }

  const rowCls = "flex flex-col gap-2 sm:flex-row sm:items-start";
  return (
    <SettingsSection id="business" title="Business profile" description="What your receptionist knows about your business. Saving updates it for callers.">
      <form onSubmit={save} className="space-y-5">
        <Group title="Basics">
          <Field label="Business name"><input className={INPUT} value={draft.name} maxLength={255} onChange={(e) => set("name", e.target.value)} /></Field>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Industry"><input className={INPUT} value={draft.industry} maxLength={128} onChange={(e) => set("industry", e.target.value)} /></Field>
            <Field label="Phone"><input className={INPUT} value={draft.phone} inputMode="tel" onChange={(e) => set("phone", e.target.value)} /></Field>
            <Field label="Website"><input className={INPUT} value={draft.website_url} placeholder="https://" inputMode="url" onChange={(e) => set("website_url", e.target.value)} /></Field>
            <Field label="Address"><input className={INPUT} value={draft.address} maxLength={512} onChange={(e) => set("address", e.target.value)} /></Field>
          </div>
          <Field label="Description"><textarea className={INPUT} rows={3} value={draft.description} maxLength={4000} onChange={(e) => set("description", e.target.value)} /></Field>
        </Group>

        <Group title="Hours, services &amp; policies" hint="Groups can be collapsed; nothing is removed unless you press Remove.">
          <Collapsible title="Hours" count={draft.hours.length}>
            {draft.hours.map((h, i) => (
              <div key={i} className={rowCls}>
                <div className="grid min-w-0 flex-1 grid-cols-1 gap-2 sm:grid-cols-[10rem_minmax(0,1fr)]">
                  <input aria-label="Days" className={INPUT} placeholder="Mon-Fri" value={h.label} maxLength={40} onChange={(e) => set("hours", draft.hours.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)))} />
                  <input aria-label="Hours" className={INPUT} placeholder="9am-5pm" value={h.value} maxLength={100} onChange={(e) => set("hours", draft.hours.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
                </div>
                <button type="button" className={BTN_GHOST} onClick={() => set("hours", draft.hours.filter((_, j) => j !== i))}>Remove</button>
              </div>
            ))}
            {draft.hours.length < 14 && <button type="button" className={BTN_GHOST} onClick={() => set("hours", [...draft.hours, { label: "", value: "" }])}>+ Add hours</button>}
          </Collapsible>

          <Collapsible title="Services" count={draft.services.length}>
            {draft.services.map((s, i) => (
              <div key={s.id ?? `n${i}`} className={rowCls}>
                <input aria-label={`Service ${i + 1}`} className={`${INPUT} sm:flex-1`} value={s.text} maxLength={1000} onChange={(e) => set("services", draft.services.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))} />
                <button type="button" className={BTN_GHOST} aria-label={`Remove service ${i + 1}`} onClick={() => setDraft(removeItem(draft, "services", i))}>Remove</button>
              </div>
            ))}
            {draft.services.length < 50 && <button type="button" className={BTN_GHOST} onClick={() => set("services", [...draft.services, { text: "" }])}>+ Add service</button>}
          </Collapsible>

          <Collapsible title="FAQs" count={draft.faqs.length}>
            {draft.faqs.map((f, i) => (
              <div key={f.id ?? `n${i}`} className="space-y-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-paper)] p-3">
                <input aria-label={`FAQ ${i + 1} question`} className={INPUT} placeholder="Question" value={f.question} maxLength={255} onChange={(e) => set("faqs", draft.faqs.map((x, j) => (j === i ? { ...x, question: e.target.value } : x)))} />
                <textarea aria-label={`FAQ ${i + 1} answer`} className={INPUT} rows={2} placeholder="Answer" value={f.answer} maxLength={1500} onChange={(e) => set("faqs", draft.faqs.map((x, j) => (j === i ? { ...x, answer: e.target.value } : x)))} />
                <button type="button" className={BTN_LINK} onClick={() => setDraft(removeItem(draft, "faqs", i))}>Remove FAQ</button>
              </div>
            ))}
            {draft.faqs.length < 50 && <button type="button" className={BTN_GHOST} onClick={() => set("faqs", [...draft.faqs, { question: "", answer: "" }])}>+ Add FAQ</button>}
          </Collapsible>

          <Collapsible title="Policies" count={draft.policies.length}>
            {draft.policies.map((s, i) => (
              <div key={s.id ?? `n${i}`} className={rowCls}>
                <textarea aria-label={`Policy ${i + 1}`} className={`${INPUT} sm:flex-1`} rows={2} value={s.text} maxLength={1000} onChange={(e) => set("policies", draft.policies.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))} />
                <button type="button" className={BTN_GHOST} aria-label={`Remove policy ${i + 1}`} onClick={() => setDraft(removeItem(draft, "policies", i))}>Remove</button>
              </div>
            ))}
            {draft.policies.length < 30 && <button type="button" className={BTN_GHOST} onClick={() => set("policies", [...draft.policies, { text: "" }])}>+ Add policy</button>}
          </Collapsible>

          {profile.other_knowledge_count > 0 && (
            <p className="text-[13px] text-[var(--color-ink-soft)]">{profile.other_knowledge_count} additional knowledge note{profile.other_knowledge_count === 1 ? "" : "s"} are kept as they are.</p>
          )}
        </Group>

        <SaveBar>
          <button type="submit" className={BTN} disabled={outcome.state === "saving"}>{outcome.state === "saving" ? "Saving…" : "Save business profile"}</button>
          <SaveStatus outcome={outcome} />
        </SaveBar>
      </form>
    </SettingsSection>
  );
}
