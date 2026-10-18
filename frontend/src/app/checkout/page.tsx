"use client";

import { useEffect } from "react";
import { initializePaddle } from "@paddle/paddle-js";
import { resolvePaddleEnvironment } from "@/lib/paddleEnv";

/**
 * Paddle's "default payment link" page. Paddle requires one, and checkout
 * URLs it generates point here with ?_ptxn=txn_... — initializing Paddle.js
 * on this page opens the checkout for that transaction automatically.
 * The normal flow opens checkout inside the onboarding wizard instead.
 */
export default function CheckoutPage() {
  useEffect(() => {
    const token = process.env.NEXT_PUBLIC_PADDLE_CLIENT_TOKEN;
    if (!token) return;
    const base = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
    void (async () => {
      try {
        const res = await fetch(`${base}/api/v1/public-config`, { credentials: "include" });
        const check = resolvePaddleEnvironment(res.ok ? (await res.json()).paddle_env : undefined, token);
        if (!check.ok) {
          console.error(check.reason);
          return; // fail closed: never open checkout in a guessed environment
        }
        initializePaddle({ environment: check.environment, token });
      } catch {
        /* backend unreachable: do not open checkout */
      }
    })();
  }, []);

  return (
    <div className="flex min-h-screen items-center justify-center px-6 text-center">
      <p className="text-[14px] text-[var(--color-ink-soft)]">Loading checkout…</p>
    </div>
  );
}