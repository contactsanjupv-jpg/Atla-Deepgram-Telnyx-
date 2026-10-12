"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, CheckoutState } from "./api";

const POLL_MS = 3000;
const MAX_POLLS = 40; // about two minutes of "payment is processing"

/**
 * What is happening with this organization's checkout, as the SERVER reads it from Paddle.
 * While a payment is processing it re-checks every few seconds; `onSettled` fires when the server says the
 * checkout is no longer in flight (the plan is active, or the checkout expired).
 */
export function useCheckoutState(orgId: string, enabled: boolean, onSettled?: () => void) {
  const [state, setState] = useState<CheckoutState | null>(null);
  const [failed, setFailed] = useState(false);
  const polls = useRef(0);
  const settled = useRef(onSettled);
  useEffect(() => {
    settled.current = onSettled;
  });

  const refresh = useCallback(async () => {
    try {
      const next = await api.getCheckoutState(orgId);
      setState(next);
      setFailed(false);
      return next;
    } catch {
      setFailed(true);
      return null;
    }
  }, [orgId]);

  useEffect(() => {
    if (!enabled) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    polls.current = 0;
    const tick = async () => {
      const next = await refresh();
      if (!live || !next) return;
      if (next.state === "processing" && polls.current++ < MAX_POLLS) timer = setTimeout(tick, POLL_MS);
      else if (next.state === "none" || next.state === "expired") settled.current?.();
    };
    void tick();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [enabled, refresh]);

  return { state, failed, refresh };
}
