import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildCsp, generateNonce } from "../src/lib/csp.ts";

const directive = (csp: string, name: string) => csp.split("; ").find((d) => d.startsWith(name + " ")) ?? "";
const prod = (nonce = "NONCE123") => buildCsp({ nonce, apiOrigin: "https://api.atla.example", isDev: false });

test("nonces are random, base64 and unique per call", () => {
  const set = new Set(Array.from({ length: 200 }, generateNonce));
  assert.equal(set.size, 200);
  for (const n of set) assert.match(n, /^[A-Za-z0-9+/]{22}==$/);
});

test("production script-src: nonce + strict-dynamic, and NO unsafe-inline / unsafe-eval", () => {
  const s = directive(prod(), "script-src");
  assert.ok(s.includes("'nonce-NONCE123'") && s.includes("'strict-dynamic'"));
  assert.ok(!s.includes("'unsafe-inline'") && !s.includes("'unsafe-eval'"));
});

test("dev adds unsafe-eval only", () => {
  const s = directive(buildCsp({ nonce: "N", apiOrigin: "http://localhost:8000", isDev: true }), "script-src");
  assert.ok(s.includes("'unsafe-eval'") && !s.includes("'unsafe-inline'"));
});

test("Paddle stays allowed for script (fallback), frames and connections", () => {
  const csp = prod();
  assert.ok(directive(csp, "script-src").includes("https://cdn.paddle.com"));
  assert.ok(directive(csp, "frame-src").includes("https://*.paddle.com"));
  assert.ok(directive(csp, "connect-src").includes("https://*.paddle.com"));
});

test("API origin, Retell and LiveKit stay in a single connect-src", () => {
  const csp = prod();
  assert.equal(csp.split("; ").filter((d) => d.startsWith("connect-src ")).length, 1);
  const c = directive(csp, "connect-src");
  for (const o of ["https://api.atla.example", "https://api.retellai.com", "wss://*.livekit.cloud"]) assert.ok(c.includes(o));
});

test("clickjacking / base / form / object protections are present", () => {
  const csp = prod();
  for (const d of ["frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'", "object-src 'none'", "default-src 'self'"]) assert.ok(csp.includes(d), d);
});

test("next.config.ts no longer sets a static CSP (it would be combined with, and defeat, the nonce policy)", () => {
  assert.ok(!/Content-Security-Policy/i.test(readFileSync(new URL("../next.config.ts", import.meta.url), "utf8").replace(/\/\/.*$/gm, "")));
});

test("proxy sets the CSP on both request and response, and layout forces dynamic rendering", () => {
  const proxy = readFileSync(new URL("../src/proxy.ts", import.meta.url), "utf8");
  assert.ok(/requestHeaders\.set\("Content-Security-Policy"/.test(proxy) && /response\.headers\.set\("Content-Security-Policy"/.test(proxy));
  assert.ok(/await connection\(\)/.test(readFileSync(new URL("../src/app/layout.tsx", import.meta.url), "utf8")));
});
