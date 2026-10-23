"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { LogOut, Menu, Mic, X } from "lucide-react";
import { api, Organization } from "@/lib/api";
import { clearShellStatus, useShellStatus, type PlanChip } from "@/lib/useShellStatus";
import type { ReceptionistStatus, StatusTone } from "@/lib/receptionistStatus";
import { NAV, isActive } from "./nav";
import { SETTINGS_BILLING_HREF } from "@/lib/settingsRoutes";

const DOT: Record<StatusTone, string> = {
  ok: "bg-[var(--color-ok)]",
  warn: "bg-amber-500",
  bad: "bg-red-500",
  neutral: "bg-[var(--color-ink-soft)]/50",
};

const TEXT: Record<StatusTone, string> = {
  ok: "text-[var(--color-ok)]",
  warn: "text-amber-700",
  bad: "text-red-600",
  neutral: "text-[var(--color-ink-soft)]",
};

const VISIBLE_NAV = NAV.filter((item) => item.enabled);

function StatusCard({
  status,
  loading,
  canTest,
  onNavigate,
}: {
  status: ReceptionistStatus | null;
  loading: boolean;
  canTest: boolean;
  onNavigate?: () => void;
}) {
  if (loading) {
    return (
      <div className="rounded-xl border border-[var(--color-line)] p-3.5" aria-busy="true">
        <div className="h-3 w-24 animate-pulse rounded bg-[var(--color-line)]" />
        <div className="mt-2.5 h-3 w-36 animate-pulse rounded bg-[var(--color-line)]" />
      </div>
    );
  }
  if (!status) return null;
  return (
    <div className="rounded-xl border border-[var(--color-line)] p-3.5">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 flex-shrink-0 rounded-full ${DOT[status.tone]}`} aria-hidden />
        <span className={`text-[13px] font-semibold ${TEXT[status.tone]}`}>{status.label}</span>
      </div>
      <p className="mt-1 break-words text-[12.5px] leading-snug text-[var(--color-ink-soft)]">{status.detail}</p>
      {status.action && (
        <Link
          href={status.action.href}
          onClick={onNavigate}
          className="mt-3 inline-flex w-full items-center justify-center rounded-lg bg-[var(--color-ink)] px-3 py-1.5 text-[13px] font-medium text-[var(--color-paper)] transition-opacity hover:opacity-90"
        >
          {status.action.label}
        </Link>
      )}
      {canTest && (
        <Link
          href="/dashboard/test"
          onClick={onNavigate}
          className="mt-2.5 inline-flex items-center gap-1.5 text-[12.5px] font-medium text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]"
        >
          <Mic className="h-3.5 w-3.5" strokeWidth={1.75} />
          Test your receptionist
        </Link>
      )}
    </div>
  );
}

function PlanLink({ plan, onNavigate }: { plan: PlanChip | null; onNavigate?: () => void }) {
  if (!plan) return null;
  return (
    <Link
      href={SETTINGS_BILLING_HREF}
      onClick={onNavigate}
      className="flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-[13px] hover:bg-[var(--color-line)]"
    >
      <span className="truncate font-medium">{plan.name}</span>
      <span className={`flex-shrink-0 text-[12px] ${TEXT[plan.tone]}`}>{plan.state}</span>
    </Link>
  );
}

function NavLinks({
  items,
  pathname,
  onNavigate,
}: {
  items: typeof NAV;
  pathname: string;
  onNavigate?: () => void;
}) {
  return (
    <>
      {items.map((item) => {
        const active = isActive(item, pathname);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-[14px] font-medium transition-colors ${
              active
                ? "bg-[var(--color-ink)] text-[var(--color-paper)]"
                : "text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]"
            }`}
          >
            <Icon className="h-4 w-4" strokeWidth={1.75} />
            {item.label}
          </Link>
        );
      })}
    </>
  );
}

export function DashboardShell({
  org,
  children,
}: {
  org: Organization | null;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { loading, status, plan, canTest } = useShellStatus(org?.id);
  const [menuOpen, setMenuOpen] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  async function signOut() {
    try {
      await api.logout();
    } catch {
      // Leave anyway; the session cookie is cleared server-side when it can be.
    }
    clearShellStatus();
    router.replace("/get-started");
  }

  useEffect(() => {
    if (!menuOpen) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [menuOpen]);

  const tabs = VISIBLE_NAV.filter((item) => item.mobileTab);
  const moreItems = VISIBLE_NAV.filter((item) => !item.mobileTab);

  return (
    <div className="flex min-h-screen">
      {/* Desktop sidebar */}
      <aside className="sticky top-0 hidden h-screen w-64 flex-shrink-0 flex-col border-r border-[var(--color-line)] bg-[var(--color-paper-raised)] md:flex">
        <div className="border-b border-[var(--color-line)] px-5 py-5">
          <Link href="/dashboard" className="font-[family-name:var(--font-display)] text-lg font-bold">
            Atla
          </Link>
          {org && <div className="mt-1 truncate text-[13px] text-[var(--color-ink-soft)]">{org.name}</div>}
        </div>

        <div className="px-3 pt-4">
          <StatusCard status={status} loading={loading} canTest={canTest} />
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4" aria-label="Main">
          <NavLinks items={VISIBLE_NAV} pathname={pathname} />
        </nav>

        <div className="space-y-1 border-t border-[var(--color-line)] px-3 py-3">
          <PlanLink plan={plan} />
          <button
            type="button"
            onClick={signOut}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]"
          >
            <LogOut className="h-4 w-4" strokeWidth={1.75} />
            Sign out
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Mobile top bar */}
        <header className="sticky top-0 z-30 flex items-center justify-between gap-3 border-b border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3 md:hidden">
          <div className="min-w-0">
            <div className="font-[family-name:var(--font-display)] text-base font-bold leading-none">Atla</div>
            {org && <div className="mt-1 truncate text-[12px] text-[var(--color-ink-soft)]">{org.name}</div>}
          </div>
          <div className="flex flex-shrink-0 items-center gap-2">
            {status && (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--color-line)] px-2.5 py-1 text-[12px] font-medium">
                <span className={`h-1.5 w-1.5 rounded-full ${DOT[status.tone]}`} aria-hidden />
                {status.state === "live" ? "Live" : status.state === "billing" ? "Paused" : "Not live"}
              </span>
            )}
            <button
              type="button"
              onClick={() => setMenuOpen(true)}
              aria-label="Open menu"
              className="rounded-lg p-2 text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]"
            >
              <Menu className="h-5 w-5" strokeWidth={1.75} />
            </button>
          </div>
        </header>

        <main className="min-w-0 flex-1 px-4 py-6 pb-28 sm:px-6 md:px-10 md:py-8 md:pb-8">{children}</main>
      </div>

      {/* Mobile bottom tab bar */}
      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-30 border-t border-[var(--color-line)] bg-[var(--color-paper-raised)] pb-[env(safe-area-inset-bottom)] md:hidden"
      >
        <ul className="flex">
          {tabs.map((item) => {
            const active = isActive(item, pathname);
            const Icon = item.icon;
            return (
              <li key={item.href} className="min-w-0 flex-1">
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={`flex flex-col items-center gap-1 px-1 py-2.5 text-[11px] font-medium ${
                    active ? "text-[var(--color-ink)]" : "text-[var(--color-ink-soft)]"
                  }`}
                >
                  <Icon className="h-5 w-5" strokeWidth={active ? 2 : 1.75} />
                  <span className="max-w-full truncate">{item.label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Mobile menu sheet */}
      {menuOpen && (
        <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="Menu">
          <button
            type="button"
            aria-label="Close menu"
            tabIndex={-1}
            className="absolute inset-0 bg-black/40"
            onClick={() => setMenuOpen(false)}
          />
          <div className="absolute inset-x-0 bottom-0 max-h-[85vh] overflow-y-auto rounded-t-2xl bg-[var(--color-paper-raised)] p-4 pb-[calc(1rem+env(safe-area-inset-bottom))] shadow-xl">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-[14px] font-semibold">{org?.name ?? "Menu"}</span>
              <button
                ref={closeRef}
                type="button"
                onClick={() => setMenuOpen(false)}
                aria-label="Close menu"
                className="rounded-lg p-2 text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]"
              >
                <X className="h-5 w-5" strokeWidth={1.75} />
              </button>
            </div>
            <StatusCard status={status} loading={loading} canTest={canTest} onNavigate={() => setMenuOpen(false)} />
            {moreItems.length > 0 && (
              <div className="mt-4 space-y-1">
                <NavLinks items={moreItems} pathname={pathname} onNavigate={() => setMenuOpen(false)} />
              </div>
            )}
            <div className="mt-4 space-y-1 border-t border-[var(--color-line)] pt-3">
              <PlanLink plan={plan} onNavigate={() => setMenuOpen(false)} />
              <button
                type="button"
                onClick={signOut}
                className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]"
              >
                <LogOut className="h-4 w-4" strokeWidth={1.75} />
                Sign out
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export function LoadingScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p>
    </div>
  );
}

export function ErrorScreen({ message }: { message: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center px-6 text-center">
      <p className="text-[15px] text-[var(--color-ink-soft)]">{message}</p>
    </div>
  );
}