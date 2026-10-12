"use client";

import type { DailyPoint } from "@/lib/api";
import { chartBars, dayLabel } from "@/lib/credits";

/**
 * Bars for real daily values. Heights scale to the true maximum; a day with nothing is a real zero bar.
 * The SVG is decorative-plus-labelled; the same numbers are available as a table for screen readers.
 */
export function UsageChart({
  title,
  points,
  pick,
  format,
  emptyText,
}: {
  title: string;
  points: DailyPoint[];
  pick: (p: DailyPoint) => number;
  format: (value: number) => string;
  emptyText: string;
}) {
  const { bars, max, empty } = chartBars(points, pick);
  const total = bars.reduce((sum, b) => sum + b.value, 0);
  const W = 300;
  const H = 72;
  const gap = bars.length > 20 ? 2 : 4;
  const bw = Math.max(2, (W - gap * (bars.length - 1)) / bars.length);

  return (
    <figure className="min-w-0">
      <figcaption className="flex items-baseline justify-between gap-3">
        <span className="text-[13.5px] font-medium">{title}</span>
        <span className="text-[12.5px] text-[var(--color-ink-soft)]">{format(total)} total</span>
      </figcaption>
      {empty ? (
        <div className="mt-2 flex h-[72px] items-center justify-center rounded-lg border border-dashed border-[var(--color-line)] px-3 text-center text-[12.5px] text-[var(--color-ink-soft)]">
          {emptyText}
        </div>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            className="mt-2 h-[72px] w-full"
            role="img"
            aria-label={`${title}: ${format(total)} across ${bars.length} days. Busiest day ${format(max)}.`}
            preserveAspectRatio="none"
          >
            <line x1="0" y1={H - 0.5} x2={W} y2={H - 0.5} stroke="var(--color-line)" strokeWidth="1" />
            {bars.map((b, i) => {
              const h = Math.max(b.value > 0 ? 2 : 0, b.height * (H - 4));
              return (
                <rect key={b.label} x={i * (bw + gap)} y={H - h} width={bw} height={h} rx="1.5" fill="var(--color-ink)" opacity={b.value > 0 ? 0.85 : 0}>
                  <title>{`${dayLabel(b.label)}: ${format(b.value)}`}</title>
                </rect>
              );
            })}
          </svg>
          <div className="mt-1 flex justify-between text-[11.5px] text-[var(--color-ink-soft)]">
            <span>{dayLabel(bars[0].label)}</span>
            <span>{dayLabel(bars[bars.length - 1].label)}</span>
          </div>
          <table className="sr-only">
            <caption>{title}</caption>
            <tbody>
              {bars.map((b) => (
                <tr key={b.label}>
                  <th scope="row">{dayLabel(b.label)}</th>
                  <td>{format(b.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </figure>
  );
}
