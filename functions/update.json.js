// Cloudflare Pages Function: the auto-update manifest at https://safecontext.me/update.json,
// read from the R2 bucket bound as DOWNLOADS. Uploaded with each release, after the DMG
// (see packaging/README.md → Auto-update). Logs nothing.
//
// Install counts: each GET writes one anonymous point to Analytics Engine (binding STATS,
// dataset safecontext_updates) with only the app version, from ?v=X.Y.Z if it looks like a
// version, else "unknown". Analytics Engine adds the timestamp. No index, IP, user agent or
// location. A no-op without the binding. Viewed with scripts/stats.py.
const KEY = "update.json";
const VERSION_RE = /^\d{1,3}\.\d{1,3}\.\d{1,4}$/;

function countCheck(request, env) {
  try {
    if (request.method !== "GET" || !env.STATS || typeof env.STATS.writeDataPoint !== "function") return;
    const v = new URL(request.url).searchParams.get("v") || "";
    env.STATS.writeDataPoint({ blobs: [VERSION_RE.test(v) ? v : "unknown"], doubles: [1] });
  } catch {
    // Best effort: never break the manifest.
  }
}

export async function onRequest({ request, env }) {
  const method = request.method;
  if (method !== "GET" && method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
  }
  countCheck(request, env);
  const object = await env.DOWNLOADS.get(KEY);
  if (!object) return new Response("Not found", { status: 404 });
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-cache",
    "ETag": object.httpEtag,
    "X-Content-Type-Options": "nosniff",
  });
  return new Response(method === "HEAD" ? null : object.body, { headers });
}
