// Cloudflare Pages Function: the auto-update manifest at https://safecontext.me/update.json,
// read from the R2 bucket bound as DOWNLOADS. Uploaded with each release, after the DMG
// (see packaging/README.md → Auto-update). Logs nothing.
const KEY = "update.json";

export async function onRequest({ request, env }) {
  const method = request.method;
  if (method !== "GET" && method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
  }
  // onlyIf: R2 checks If-None-Match (and the other conditional headers) against the stored
  // object, and returns it without a body when the app's copy is still current.
  const object = await env.DOWNLOADS.get(KEY, { onlyIf: request.headers });
  if (!object) return new Response("Not found", { status: 404 });
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-cache",
    "ETag": object.httpEtag,
    "X-Content-Type-Options": "nosniff",
  });
  if (!("body" in object)) return new Response(null, { status: 304, headers }); // If-None-Match matched
  return new Response(method === "HEAD" ? null : object.body, { headers });
}
