"use client";

import { useEffect, useState } from "react";
import { describeError } from "./api";

/**
 * Load something for `key` (null = don't load yet). `data` is the result for THIS key only (never another
 * range's numbers); `last` is the most recent success for any key, for values that don't depend on the key.
 * Errors are per request and cleared by `retry`.
 */
export function useLoad<T>(key: string | null, load: () => Promise<T>, fallback: string) {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<{ key: string; nonce: number; data: T | null; error: string | null } | null>(null);
  const [last, setLast] = useState<T | null>(null);

  useEffect(() => {
    if (key === null) return;
    let live = true;
    load()
      .then((data) => {
        if (!live) return;
        setLast(data);
        setResult({ key, nonce, data, error: null });
      })
      .catch((err) => {
        if (live) setResult({ key, nonce, data: null, error: describeError(err, fallback) });
      });
    return () => {
      live = false;
    };
    // `load` is a fresh closure every render; the key is what identifies the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);

  const sameKey = result !== null && result.key === key;
  const current = sameKey && result.nonce === nonce;
  return {
    data: sameKey ? result.data : null,
    last,
    loading: key !== null && !current,
    error: current ? result.error : null,
    retry: () => setNonce((n) => n + 1),
  };
}