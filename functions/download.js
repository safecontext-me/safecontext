// Cloudflare Pages Function: serves the Mac app at https://safecontext.me/download,
// streamed from the R2 bucket bound as DOWNLOADS (see wrangler.toml).
// Upload each release to the bucket under this fixed key, so the URL never changes.
const KEY = "SafeContext.dmg";

export async function onRequest({ request, env }) {
  const method = request.method;
  if (method !== "GET" && method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
  }

  const wantsRange = method === "GET" && request.headers.has("range");
  let object;
  try {
    object = method === "HEAD"
      ? await env.DOWNLOADS.head(KEY)
      : await env.DOWNLOADS.get(KEY, wantsRange ? { range: request.headers } : undefined);
  } catch {
    // R2 rejects a Range header it can't satisfy.
    return new Response("Range not satisfiable", { status: 416 });
  }
  if (!object) return new Response("Not found", { status: 404 });

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Content-Type", "application/x-apple-diskimage");
  headers.set("Content-Disposition", `attachment; filename="${KEY}"`);
  headers.set("ETag", object.httpEtag);
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", "no-cache"); // always revalidate, so a new release shows up at once
  headers.set("X-Content-Type-Options", "nosniff");

  if (method === "HEAD") {
    headers.set("Content-Length", String(object.size));
    return new Response(null, { headers });
  }

  const r = object.range;
  if (wantsRange && r) {
    const start = "suffix" in r ? object.size - r.suffix : (r.offset ?? 0);
    const end = "length" in r && r.length !== undefined ? start + r.length - 1 : object.size - 1;
    headers.set("Content-Range", `bytes ${start}-${end}/${object.size}`);
    headers.set("Content-Length", String(end - start + 1));
    return new Response(object.body, { status: 206, headers });
  }

  headers.set("Content-Length", String(object.size));
  return new Response(object.body, { headers });
}
