"use client";

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { api, describeError, TopupQuote } from "@/lib/api";
import { formatDollars, quickAmounts, validateTopup } from "@/lib/credits";
import { openCheckout, paymentsConfigured, transactionIdFrom } from "@/lib/paddle";

/**
 * Buy more credits: the customer picks any amount from the minimum up, sees what it buys BEFORE paying, and
 * pays through Paddle. Nothing is credited here — the server grants credits only after Paddle confirms payment.
 */
export function TopupDialog({
  orgId,
  minMinor,
  maxMinor,
  onClose,
  onPaid,
}: {
  orgId: string;
  minMinor: number;
  maxMinor: number;
  onClose: () => void;
  onPaid: () => void;
}) {
  const [text, setText] = useState(String(minMinor / 100));
  const [quote, setQuote] = useState<TopupQuote | null>(null);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const check = validateTopup(text, minMinor, maxMinor);
  const amount = check.ok ? check.amountMinor : null;

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [busy, onClose]);

  // What this amount buys, from the server (it owns the rate). Debounced so typing doesn't spam the API.
  useEffect(() => {
    if (amount === null) return;
    let live = true;
    const t = setTimeout(() => {
      api
        .getTopupQuote(orgId, amount)
        .then((q) => {
          if (!live) return;
          setQuote(q);
          setQuoteError(null);
        })
        .catch((err) => {
          if (!live) return;
          setQuote(null);
          setQuoteError(describeError(err, "We couldn't work out your credits right now."));
        });
    }, 300);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [amount, orgId]);

  const quoteMatches = quote !== null && amount !== null && quote.amount_minor === amount;

  async function pay() {
    if (amount === null || !quoteMatches) return;
    if (!paymentsConfigured) {
      setError("Payments aren't configured yet.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { checkout_url } = await api.createTopup(orgId, amount);
      const txn = transactionIdFrom(checkout_url);
      if (!txn) throw new Error("checkout unavailable");
      await openCheckout(txn, {
        onCompleted: () => {
          setBusy(false);
          onPaid();
        },
        onClosed: () => setBusy(false),
      });
    } catch (err) {
      setBusy(false);
      setError(describeError(err, "We couldn't start your purchase. You haven't been charged."));
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center" role="dialog" aria-modal="true" aria-label="Buy more credits">
      <button type="button" aria-label="Close" tabIndex={-1} className="absolute inset-0 bg-black/40" onClick={() => !busy && onClose()} />
      <div className="relative max-h-[90vh] w-full overflow-y-auto rounded-t-2xl bg-[var(--color-paper-raised)] p-6 shadow-xl sm:max-w-md sm:rounded-2xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-[18px] font-semibold">Buy more credits</h2>
            <p className="mt-1 text-[13.5px] text-[var(--color-ink-soft)]">Choose any amount from {formatDollars(minMinor)}. Credits never expire.</p>
          </div>
          <button onClick={onClose} disabled={busy} aria-label="Close" className="rounded-lg p-1.5 text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]">
            <X className="h-5 w-5" strokeWidth={1.75} />
          </button>
        </div>

        <label htmlFor="topup-amount" className="mt-5 block text-[13px] font-medium">
          Amount (USD)
        </label>
        <div className="mt-1.5 flex items-center rounded-xl border border-[var(--color-line)] px-3.5 focus-within:border-[var(--color-ink)]">
          <span className="text-[16px] text-[var(--color-ink-soft)]">$</span>
          <input
            id="topup-amount"
            ref={inputRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            inputMode="decimal"
            autoComplete="off"
            aria-invalid={!check.ok}
            aria-describedby="topup-help"
            className="w-full bg-transparent px-2 py-3 text-[18px] font-semibold outline-none"
          />
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          {quickAmounts(minMinor, maxMinor).map((a) => (
            <button
              key={a}
              type="button"
              onClick={() => setText(String(a / 100))}
              className="rounded-full border border-[var(--color-line)] px-3.5 py-1.5 text-[13px] font-medium hover:bg-[var(--color-line)]"
            >
              {formatDollars(a)}
            </button>
          ))}
        </div>

        <p id="topup-help" aria-live="polite" className={`mt-4 min-h-[44px] text-[14px] ${check.ok && !quoteError ? "" : "text-[#7a2416]"}`}>
          {!check.ok ? (
            check.message
          ) : quoteError ? (
            quoteError
          ) : quoteMatches ? (
            <>
              You&apos;ll get <strong>{quote!.credit_minutes.toLocaleString()} minutes</strong> of receptionist time for {formatDollars(quote!.amount_minor)}.
            </>
          ) : (
            <span className="text-[var(--color-ink-soft)]">Working out your credits…</span>
          )}
        </p>

        {error && <p className="mb-3 text-[13.5px] text-[#7a2416]">{error}</p>}

        <button
          onClick={pay}
          disabled={busy || !quoteMatches}
          className="w-full rounded-full bg-[var(--color-ink)] px-5 py-3 text-[14px] font-medium text-[var(--color-paper)] disabled:opacity-50"
        >
          {busy ? "Opening checkout…" : amount !== null ? `Pay ${formatDollars(amount)}` : "Pay"}
        </button>
        <p className="mt-3 text-[12px] leading-relaxed text-[var(--color-ink-soft)]">
          Taxes may be added at checkout. Credits are added after your payment is confirmed — usually within a minute.
        </p>
      </div>
    </div>
  );
}
