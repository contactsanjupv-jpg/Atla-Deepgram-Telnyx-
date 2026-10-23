"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Voice } from "@/lib/api";
import { filterVoices, NO_VOICE_FILTERS, safePreviewUrl, voiceFilterOptions, voiceMeta, type VoiceFilters } from "@/lib/settingsLogic";
import { BTN_GHOST, BTN_LINK, INPUT } from "./ui";

function PreviewButton({ v, playing, onPreview }: { v: Voice; playing: string | null; onPreview: (v: Voice) => void }) {
  if (!safePreviewUrl(v.preview_url)) return null;
  return (
    <button type="button" className={BTN_GHOST} aria-label={`${playing === v.id ? "Stop" : "Play"} ${v.name} preview`} onClick={() => onPreview(v)}>
      {playing === v.id ? "Stop" : "Preview"}
    </button>
  );
}

/**
 * Compact voice selector: the chosen voice is always shown as a summary row; the full catalogue sits in a
 * searchable, filterable, scrollable panel. Every catalogue voice stays reachable (filters only narrow the view).
 * Value is our internal Voice.id — the same id the save endpoint already expects.
 */
export function VoicePicker({ voices, value, onChange }: { voices: Voice[]; value: string; onChange: (id: string) => void }) {
  const selected = voices.find((v) => v.id === value) ?? null;
  const [open, setOpen] = useState(!value);
  const [filters, setFilters] = useState<VoiceFilters>(NO_VOICE_FILTERS);
  const [playing, setPlaying] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const options = useMemo(() => voiceFilterOptions(voices), [voices]);
  const shown = useMemo(() => filterVoices(voices, filters), [voices, filters]);
  const filtering = !!(filters.search || filters.gender || filters.accent);

  useEffect(() => () => { audioRef.current?.pause(); audioRef.current = null; }, []);

  function stop() {
    const a = audioRef.current;
    if (a) { a.pause(); a.removeAttribute("src"); a.load(); }
    audioRef.current = null;
    setPlaying(null);
  }

  function preview(v: Voice) {
    const url = safePreviewUrl(v.preview_url);
    if (!url) return;
    if (playing === v.id) return stop();
    stop();
    const a = new Audio(url);
    audioRef.current = a;
    const done = () => { if (audioRef.current === a) { audioRef.current = null; setPlaying(null); } };
    a.onended = done; a.onerror = done;
    setPlaying(v.id);
    void a.play().catch(done);
  }

  if (voices.length === 0) return <p className="text-[13.5px] text-[var(--color-ink-soft)]">No voices available right now.</p>;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--color-ink)] px-3.5 py-3">
        <div className="min-w-0 flex-1 basis-40">
          <p className="text-[12px] font-medium uppercase tracking-wide text-[var(--color-ink-soft)]">Selected voice</p>
          {selected ? (
            <>
              <p className="truncate text-[15px] font-semibold">{selected.name}</p>
              <p className="truncate text-[12.5px] text-[var(--color-ink-soft)]">{voiceMeta(selected)}</p>
            </>
          ) : (
            <p className="text-[14px] text-[#7a2416]">No voice chosen yet</p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          {selected && <PreviewButton v={selected} playing={playing} onPreview={preview} />}
          <button type="button" className={BTN_GHOST} aria-expanded={open} aria-controls="voice-panel" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide voices" : "Change voice"}
          </button>
        </div>
      </div>

      {open && (
        <div id="voice-panel" className="space-y-3 rounded-xl border border-[var(--color-line)] p-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(0,1fr)_9rem_9rem]">
            <input
              type="search"
              aria-label="Search voices"
              placeholder="Search voices…"
              className={INPUT}
              value={filters.search}
              onChange={(e) => setFilters({ ...filters, search: e.target.value })}
            />
            <select aria-label="Filter by gender" className={INPUT} value={filters.gender} onChange={(e) => setFilters({ ...filters, gender: e.target.value })}>
              <option value="">Any gender</option>
              {options.genders.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
            <select aria-label="Filter by accent" className={INPUT} value={filters.accent} onChange={(e) => setFilters({ ...filters, accent: e.target.value })}>
              <option value="">Any accent</option>
              {options.accents.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          <div className="flex items-center justify-between gap-3 text-[12.5px] text-[var(--color-ink-soft)]" aria-live="polite">
            <span data-voice-count>{filtering ? `${shown.length} of ${voices.length} voices` : `${voices.length} voices`}</span>
            {filtering && <button type="button" className={BTN_LINK} onClick={() => setFilters(NO_VOICE_FILTERS)}>Clear filters</button>}
          </div>
          <div role="radiogroup" aria-label="Voices" className="max-h-72 space-y-1.5 overflow-y-auto overscroll-contain pr-0.5">
            {shown.length === 0 && <p className="px-1 py-4 text-center text-[13.5px] text-[var(--color-ink-soft)]">No voices match. Try clearing the filters.</p>}
            {shown.map((v) => (
              <div key={v.id} className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-1.5 ${value === v.id ? "border-[var(--color-ink)] bg-[var(--color-paper)]" : "border-[var(--color-line)]"}`}>
                <label className="flex min-h-11 min-w-0 flex-1 cursor-pointer items-center gap-3">
                  <input type="radio" name="voice" checked={value === v.id} onChange={() => onChange(v.id)} className="h-4 w-4 shrink-0" />
                  <span className="min-w-0">
                    <span className="block truncate text-[14.5px] font-medium">{v.name}</span>
                    <span className="block truncate text-[12.5px] text-[var(--color-ink-soft)]">{voiceMeta(v)}</span>
                  </span>
                </label>
                <PreviewButton v={v} playing={playing} onPreview={preview} />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
