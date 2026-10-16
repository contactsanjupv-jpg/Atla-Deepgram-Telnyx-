"use client";

import type { CallRow } from "@/lib/api";
import { DIRECTION_LABEL, callerLabel, formatCallTime, formatDuration } from "@/lib/calls";
import { CallStatusBadge, LeadChip } from "./CallBadges";

/**
 * The calls as a table on wide screens and as cards on phones (so nothing scrolls sideways).
 * Every cell is something the server returned; a missing value is shown as an em dash, never guessed.
 */
export function CallList({ calls, tz, onOpen }: { calls: CallRow[]; tz: string; onOpen: (c: CallRow) => void }) {
  return (
    <>
      <ul className="space-y-2 md:hidden">
        {calls.map((c) => (
          <li key={c.id}>
            <button type="button" onClick={() => onOpen(c)} className="block w-full min-w-0 rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3 text-left">
              <div className="flex items-baseline justify-between gap-3">
                <span className="min-w-0 truncate text-[14px] font-medium">{callerLabel(c)}</span>
                <span className="flex-shrink-0 text-[13px] tabular-nums text-[var(--color-ink-soft)]">{formatDuration(c.duration_seconds)}</span>
              </div>
              <div className="mt-0.5 text-[12.5px] text-[var(--color-ink-soft)]">{formatCallTime(c.started_at, tz) ?? "Time not recorded"}</div>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <CallStatusBadge call={c} />
                <span className="text-[12.5px] text-[var(--color-ink-soft)]">{DIRECTION_LABEL[c.direction] ?? c.direction}</span>
                {c.lead_id && <LeadChip call={c} />}
              </div>
              {c.summary && <p className="mt-2 line-clamp-2 break-words text-[13px] text-[var(--color-ink-soft)]">{c.summary}</p>}
            </button>
          </li>
        ))}
      </ul>

      <div className="hidden overflow-hidden rounded-xl border border-[var(--color-line)] md:block">
        <table className="w-full table-fixed text-left text-[13.5px]">
          <thead className="border-b border-[var(--color-line)] bg-[var(--color-paper-raised)] text-[12px] uppercase tracking-wide text-[var(--color-ink-soft)]">
            <tr>
              <th className="w-[22%] px-4 py-3 font-medium">Caller</th>
              <th className="w-[17%] px-4 py-3 font-medium">When</th>
              <th className="w-[9%] px-4 py-3 font-medium">Length</th>
              <th className="w-[14%] px-4 py-3 font-medium">Status</th>
              <th className="w-[11%] px-4 py-3 font-medium">Lead</th>
              <th className="px-4 py-3 font-medium">Summary</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((c) => (
              <tr key={c.id} onClick={() => onOpen(c)} className="cursor-pointer border-b border-[var(--color-line)] last:border-0 hover:bg-[var(--color-line)]/30">
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpen(c);
                    }}
                    className="block max-w-full truncate text-left font-medium underline-offset-2 hover:underline"
                  >
                    {callerLabel(c)}
                  </button>
                  <div className="text-[12px] text-[var(--color-ink-soft)]">{DIRECTION_LABEL[c.direction] ?? c.direction}</div>
                </td>
                <td className="px-4 py-3 text-[var(--color-ink-soft)]">{formatCallTime(c.started_at, tz) ?? "—"}</td>
                <td className="px-4 py-3 tabular-nums">{formatDuration(c.duration_seconds)}</td>
                <td className="px-4 py-3">
                  <CallStatusBadge call={c} />
                </td>
                <td className="px-4 py-3">
                  <LeadChip call={c} />
                </td>
                <td className="px-4 py-3 text-[var(--color-ink-soft)]">
                  <span className="line-clamp-2 break-words">{c.summary || "—"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
