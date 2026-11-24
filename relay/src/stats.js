// Anonymous usage counts, written to Workers Analytics Engine (binding STATS).
//
// Each data point has exactly this shape, and nothing else:
//   indexes: [link token]  hex HMAC-SHA256(STATS_KEY, link ID). Stable per link, can't be reversed
//                          to the link (so it can't be used to reach anyone's Mac).
//   blobs:   [event, tool, app, outcome]  each from a fixed list below; anything else becomes "".
//   doubles: [count (always 1), latency_ms (Mac round trip, 0 if none), response size bucket in KiB]
// Never: IP, user agent, country, headers, queries, arguments, results, client names, link IDs.
//
// If STATS or STATS_KEY is missing, or anything throws, this does nothing: stats must never
// break the relay.
import { METHODS, TOOLS } from "./relay.js";

export const EVENTS = new Set(["mcp_request", "oauth_connect", "mac_offline", "rate_limited"]);
export const APPS = new Set(["grok", "claude", "other"]);
export const OUTCOMES = new Set([
  "ok",                 // the Mac answered 2xx (or the sign-in finished)
  "http_4xx", "http_5xx",   // the Mac answered with that status class
  "unauthorized",       // no or bad token
  "method_not_allowed", // not POST
  "too_large",          // request over the size limit
  "result_too_large",   // the Mac's answer over the size limit
  "parse_error",        // body isn't JSON
  "invalid_request",    // JSON, but not a JSON-RPC request
  "refused",            // method or tool not on the allowlist
  "offline",            // the Mac isn't connected
  "timeout",            // the Mac didn't answer in time
  "rate_limited",
]);
// A tool name, or for other MCP methods the method name. Both are allowlists in relay.js.
export function toolAllowed(v) {
  return v === "" || TOOLS.has(v) || METHODS.has(v);
}

// The app behind a callback URL: only the three classes, never the URL or a name.
export function appOf(redirect, name) {
  let host = "";
  try { host = new URL(redirect).hostname.toLowerCase(); } catch {}
  const under = (d) => host === d || host.endsWith(`.${d}`);
  if (under("grok.com") || under("x.ai")) return "grok";
  if (under("claude.ai") || under("anthropic.com")) return "claude";
  const n = typeof name === "string" ? name.toLowerCase() : "";
  if (n.includes("grok")) return "grok";
  if (n.includes("claude")) return "claude";
  return "other";
}

// The tool (or method) a request is about: the first allowlisted tools/call, else the first
// allowlisted method. Never anything a client typed that isn't on a list.
export function toolOf(msgs) {
  if (!Array.isArray(msgs)) return "";
  for (const m of msgs) {
    if (m && m.method === "tools/call" && m.params && TOOLS.has(m.params.name)) return m.params.name;
  }
  for (const m of msgs) {
    if (m && typeof m.method === "string" && METHODS.has(m.method)) return m.method;
  }
  return "";
}

// Response size, rounded up to a power of two KiB (0 = empty), so sizes say little about content.
export function sizeBucket(bytes) {
  if (!(bytes > 0)) return 0;
  return 2 ** Math.max(0, Math.ceil(Math.log2(bytes / 1024)));
}

export async function linkToken(key, link) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(link));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Builds the one allowed shape. Values outside the lists are replaced, never passed through.
export function dataPoint(token, { event, tool = "", app = "other", outcome = "ok", latencyMs = 0, bytes = 0 }) {
  const ms = Number.isFinite(latencyMs) && latencyMs > 0 ? Math.min(Math.round(latencyMs), 600_000) : 0;
  return {
    indexes: [token],
    blobs: [
      EVENTS.has(event) ? event : "",
      toolAllowed(tool) ? tool : "",
      APPS.has(app) ? app : "other",
      OUTCOMES.has(outcome) ? outcome : "",
    ],
    doubles: [1, ms, sizeBucket(bytes)],
  };
}

// Writes one data point. `cache` is per Durable Object (one link), so the HMAC is computed once.
export async function recordStat(env, cache, link, fields) {
  try {
    const ds = env && env.STATS, key = env && env.STATS_KEY;
    if (!ds || typeof ds.writeDataPoint !== "function" || typeof key !== "string" || !key) return;
    if (cache.key !== key || cache.link !== link) {
      cache.token = await linkToken(key, link);
      cache.key = key;
      cache.link = link;
    }
    ds.writeDataPoint(dataPoint(cache.token, fields));
  } catch {
    // Deliberately silent: stats are best effort.
  }
}
