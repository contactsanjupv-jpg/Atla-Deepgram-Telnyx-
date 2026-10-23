"use client";

import { useState } from "react";
import { api, describeError } from "@/lib/api";
import { BTN, INPUT, SettingsSection } from "../ui";

export function SecurityView() {
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwMsg(null);
    try {
      await api.changePassword(pwCurrent, pwNew);
      setPwCurrent("");
      setPwNew("");
      setPwMsg("Password changed. Other devices have been signed out.");
    } catch (err) {
      setPwMsg(describeError(err));
    }
  }

  return (
    <SettingsSection id="security" title="Change password" description="Other devices are signed out when you change it.">
      <form onSubmit={changePassword} className="max-w-md space-y-3">
        <input type="password" value={pwCurrent} onChange={(e) => setPwCurrent(e.target.value)} autoComplete="current-password" placeholder="Current password" aria-label="Current password" className={INPUT} />
        <input type="password" value={pwNew} onChange={(e) => setPwNew(e.target.value)} autoComplete="new-password" placeholder="New password (at least 10 characters)" aria-label="New password" className={INPUT} />
        <button type="submit" disabled={!pwCurrent || pwNew.length < 10} className={BTN}>
          Change password
        </button>
        {pwMsg && <p className="text-[13.5px] text-[var(--color-ink-soft)]">{pwMsg}</p>}
      </form>
    </SettingsSection>
  );
}
