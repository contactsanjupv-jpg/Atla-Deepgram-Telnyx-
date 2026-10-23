"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import { useSettings } from "../SettingsContext";
import { BTN_GHOST, INPUT, SettingsSection } from "../ui";

export function DeleteAccountView() {
  const router = useRouter();
  const { org } = useSettings();
  const [delName, setDelName] = useState("");
  const [delPw, setDelPw] = useState("");
  const [delMsg, setDelMsg] = useState<string | null>(null);

  async function deleteAccount(e: React.FormEvent) {
    e.preventDefault();
    setDelMsg(null);
    try {
      await api.deleteAccount(org.id, delName, delPw);
      router.replace("/");
    } catch (err) {
      setDelMsg(describeError(err));
    }
  }

  return (
    <SettingsSection id="danger" tone="danger" title="Delete account" description="This cancels your subscription, releases your phone number, and permanently deletes your receptionist, calls and leads.">
      <form onSubmit={deleteAccount} className="max-w-md space-y-3">
        <input value={delName} onChange={(e) => setDelName(e.target.value)} placeholder={`Type "${org.name}" to confirm`} aria-label="Confirm organization name" className={INPUT} />
        <input type="password" value={delPw} onChange={(e) => setDelPw(e.target.value)} autoComplete="current-password" placeholder="Your password" aria-label="Your password" className={INPUT} />
        <button type="submit" disabled={!delName || !delPw} className={`${BTN_GHOST} !border-[#7a2416] !px-5 !py-2.5 !text-[#7a2416]`}>
          Delete my account
        </button>
        {delMsg && <p className="text-[13.5px] text-[#7a2416]">{delMsg}</p>}
      </form>
    </SettingsSection>
  );
}
