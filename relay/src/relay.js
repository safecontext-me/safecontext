// SafeContext relay (src/index.js re-exports the entry points): lets cloud AI apps' custom MCP connectors
// (Grok, Claude, ...) reach the SafeContext app on a user's Mac.
//
//   AI app (cloud) --HTTPS, MCP Streamable HTTP--> this Worker --> LinkRelay Durable Object
//                                                                   (one per link) --WebSocket--> the Mac
//
// The Mac holds an outbound WebSocket to its link's Durable Object, authenticated with a per-device
// secret. The AI app signs in with OAuth 2.1 (authorization code + PKCE, dynamic client registration):
// the sign-in page asks the user to click Approve in SafeContext on their Mac. Each MCP request is
// checked (token, size, rate, method and tool allowlist) and passed down the socket; the Mac's answer
// is passed back.
//
// Hard rules, kept by this file:
//   * No request or response body is ever stored or logged. Storage holds auth metadata only:
//     a hash of the Mac's secret, OAuth clients, hashes of tokens/codes, a pending approval, one setting.
//   * No console logging at all (and Workers observability is off in wrangler.toml).
//   * Read-only tools only, by allowlist; everything else is refused here and again on the Mac.
//   * Per-link rate limits, size limits and timeouts.
import { DurableObject } from "cloudflare:workers";

const LINK_RE = /^[A-Za-z0-9_-]{43}$/;          // 256 random bits, base64url
const SECRET_RE = /^[A-Za-z0-9_-]{43}$/;
export const MAX_REQUEST_BYTES = 64 * 1024;
export const MAX_RESPONSE_BYTES = 1024 * 1024;  // a Workers WebSocket message is at most 1 MiB
const MAC_TIMEOUT_MS = 25_000;
const APPROVAL_TTL_MS = 5 * 60_000;
const CODE_TTL_MS = 5 * 60_000;
const ACCESS_TTL_S = 30 * 86_400;
const REFRESH_TTL_S = 180 * 86_400;
const MAX_CLIENTS = 20;
// [capacity, refill seconds for a full bucket] per link
const LIMITS = { mcp: [120, 60], auth: [20, 60], connect: [10, 60], denied: [60, 60] };
export const TOOLS = new Set(["search", "read", "list_items", "list_sources"]);
export const METHODS = new Set(["initialize", "ping", "tools/list", "tools/call",
  "notifications/initialized", "notifications/cancelled"]);
export const OFFLINE_MESSAGE = "SafeContext isn't running on your Mac. Open SafeContext on your Mac " +
  "(it works while your Mac is awake and SafeContext is open), then try again.";
// Public client IDs for connector forms that ask for a Client ID instead of registering. Both accept
// any allowed callback. "grok" is kept so existing Grok connectors and their tokens keep working.
const STATIC_CLIENTS = { grok: "Grok", safecontext: "" };
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);
const DEFAULT_REDIRECT_HOSTS ="grok.com,*.grok.com,x.ai,*.x.ai,claude.ai";
const MAX_NAME = 40;
const SCOPE = "safecontext.read";

// ---------------------------------------------------------------------------------------------
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "") {
      return new Response("SafeContext relay. Nothing is stored here.\n",
        { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    const r = route(url.pathname);
    if (!r) return json({ error: "not_found" }, 404);
    const stub = env.LINKS.get(env.LINKS.idFromName(r.link));
    return stub.fetch(request);
  },
};

// Every path a link answers on. Well-known metadata is served both at the RFC 8414 / RFC 9728
// locations (/.well-known/<name>/g/<link>[/mcp]) and under the link (/g/<link>/.well-known/<name>).
export function route(pathname) {
  let m = pathname.match(/^\/\.well-known\/(oauth-protected-resource|oauth-authorization-server|openid-configuration)\/g\/([^/]+)(?:\/mcp)?\/?$/);
  if (m) return LINK_RE.test(m[2]) ? { link: m[2], sub: wellKnown(m[1]) } : null;
  m = pathname.match(/^\/g\/([^/]+)(\/.*)?$/);
  if (!m || !LINK_RE.test(m[1])) return null;
  let sub = (m[2] || "/").replace(/\/+$/, "") || "/";
  const wk = sub.match(/^(?:\/mcp)?\/\.well-known\/(oauth-protected-resource|oauth-authorization-server|openid-configuration)$/);
  if (wk) sub = wellKnown(wk[1]);
  return { link: m[1], sub };
}

function wellKnown(name) {
  return name === "oauth-protected-resource" ? "wk:resource" : "wk:as";
}

// ---------------------------------------------------------------------------------------------
export class LinkRelay extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.env = env;
    this.pending = new Map();   // request id -> {resolve, timer}; in memory only, never stored
    this.buckets = new Map();
  }

  async fetch(request) {
    const url = new URL(request.url);
    const r = route(url.pathname);
    if (!r) return json({ error: "not_found" }, 404);
    const ctx = { url, link: r.link, origin: url.origin };
    ctx.issuer = `${ctx.origin}/g/${r.link}`;
    ctx.resource = `${ctx.issuer}/mcp`;
    try {
      switch (r.sub) {
        case "/connect": return await this.connect(request);
        case "wk:resource": return json(this.resourceMetadata(ctx));
        case "wk:as": return json(this.serverMetadata(ctx));
        case "/register": return await this.register(request);
        case "/authorize": return await this.authorize(request, ctx);
        case "/authorize/wait": return await this.authorizeWait(ctx);
        case "/token": return await this.token(request, ctx);
        case "/mcp": return await this.mcp(request, ctx);
        default: return json({ error: "not_found" }, 404);
      }
    } catch {
      // Deliberately no logging: the error could carry request content.
      return json({ error: "server_error" }, 500);
    }
  }

  // --- the Mac's socket ---------------------------------------------------------------------
  async connect(request) {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return json({ error: "expected a WebSocket" }, 426);
    }
    if (!this.take("connect")) return json({ error: "rate_limited" }, 429);
    const secret = bearer(request);
    if (!secret || !SECRET_RE.test(secret)) return json({ error: "unauthorized" }, 401);
    const hash = await sha256(secret);
    const known = await this.ctx.storage.get("device");
    if (known === undefined) {
      await this.ctx.storage.put("device", hash);  // first Mac to use this (random, secret) link owns it
    } else if (!equal(known, hash)) {
      return json({ error: "unauthorized" }, 401);
    }
    for (const old of this.ctx.getWebSockets()) {
      try { old.close(4000, "replaced by a newer connection"); } catch {}
      this.failPending(old);
    }
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ since: Date.now() });
    server.send(JSON.stringify({ t: "hello", grants: await this.grantCount() }));
    return new Response(null, { status: 101, webSocket: client });
  }

  // The newest open socket (an older one may still be closing after a reconnect).
  mac() {
    let best = null, since = -1;
    for (const ws of this.ctx.getWebSockets()) {
      if (ws.readyState !== WebSocket.READY_STATE_OPEN) continue;
      const a = ws.deserializeAttachment() || {};
      if ((a.since || 0) > since) { best = ws; since = a.since || 0; }
    }
    return best;
  }

  async webSocketMessage(ws, data) {
    if (typeof data !== "string") return;
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    if (!msg || typeof msg !== "object") return;
    switch (msg.t) {
      case "http_result": {
        const p = this.pending.get(msg.id);
        if (p) { this.pending.delete(msg.id); clearTimeout(p.timer); p.resolve(msg); }
        return;
      }
      case "approve_result": {
        const key = `ap:${msg.id}`;
        const ap = await this.ctx.storage.get(key);
        if (ap && ap.status === "pending") {
          await this.ctx.storage.put(key, { ...ap, status: msg.ok === true ? "approved" : "denied" });
        }
        return;
      }
      case "settings":
        await this.ctx.storage.put("settings", { link_only: msg.link_only === true });
        return;
      case "revoke_all":
        // The Mac regenerated its link: forget everything this link ever held.
        await this.ctx.storage.deleteAll();
        try { ws.close(4001, "link revoked"); } catch {}
        return;
    }
  }

  async webSocketClose(ws, code, reason) {
    this.failPending(ws);
    try { ws.close(code === 1005 || code === 1006 ? 1000 : code, "closing"); } catch {}  // finish the handshake
  }

  async webSocketError(ws) {
    this.failPending(ws);
  }

  // Requests sent down a socket that has gone away get the "not running" answer at once.
  failPending(ws) {
    for (const [id, p] of this.pending) {
      if (p.ws !== ws) continue;
      clearTimeout(p.timer);
      this.pending.delete(id);
      p.resolve(null);
    }
  }

  askMac(payload) {
    const ws = this.mac();
    if (!ws) return Promise.resolve(null);
    const id = crypto.randomUUID();
    return new Promise((resolve) => {
      const timer = setTimeout(() => { this.pending.delete(id); resolve("timeout"); }, MAC_TIMEOUT_MS);
      this.pending.set(id, { resolve, timer, ws });
      try {
        ws.send(JSON.stringify({ ...payload, id }));
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve(null);
      }
    });
  }

  // --- MCP --------------------------------------------------------------------------------------
  async mcp(request, ctx) {
    if (!(await this.authorized(request))) {
      if (!this.take("denied")) return json({ error: "rate_limited" }, 429);
      const given = !!request.headers.get("Authorization");
      const challenge = `Bearer resource_metadata="${ctx.origin}/.well-known/oauth-protected-resource/g/${ctx.link}/mcp"` +
        (given ? ', error="invalid_token"' : "") + `, scope="${SCOPE}"`;
      return json({ error: "unauthorized" }, 401, { "WWW-Authenticate": challenge });
    }
    if (request.method !== "POST") {
      return json(rpcError(null, -32000, "Method not allowed: send JSON-RPC with POST."), 405, { Allow: "POST" });
    }
    if (!this.take("mcp")) {
      return json(rpcError(null, -32000, "Too many requests. Wait a minute and try again."), 429, { "Retry-After": "30" });
    }
    const len = Number(request.headers.get("content-length") || 0);
    if (len > MAX_REQUEST_BYTES) return json(rpcError(null, -32600, "Request too large."), 413);
    const body = await readCapped(request, MAX_REQUEST_BYTES);
    if (body === null) return json(rpcError(null, -32600, "Request too large."), 413);
    let payload;
    try { payload = JSON.parse(body); } catch { return json(rpcError(null, -32700, "Parse error"), 400); }
    const msgs = Array.isArray(payload) ? payload : [payload];
    if (!msgs.length) return json(rpcError(null, -32600, "Invalid Request"), 400);
    const refused = checkAllowlist(msgs);
    if (refused) return json(Array.isArray(payload) ? [refused] : refused, 200);

    const headers = {};
    for (const h of ["content-type", "accept", "mcp-protocol-version"]) {
      const v = request.headers.get(h);
      if (v) headers[h] = v.slice(0, 200);
    }
    const answer = await this.askMac({ t: "http", method: "POST", headers, body });
    if (answer === null || answer === "timeout") {
      const text = answer === null ? OFFLINE_MESSAGE
        : "SafeContext on your Mac didn't answer in time. Check that your Mac is awake, then try again.";
      return offlineReply(msgs, Array.isArray(payload), text);
    }
    const out = typeof answer.body === "string" ? answer.body : "";
    if (new TextEncoder().encode(out).length > MAX_RESPONSE_BYTES) {
      return json(rpcError(null, -32603, "The result was too large to send."), 200);
    }
    const status = Number.isInteger(answer.status) && answer.status >= 200 && answer.status < 600 ? answer.status : 502;
    const outHeaders = { "Cache-Control": "no-store" };
    const ct = answer.headers && answer.headers["content-type"];
    if (out) outHeaders["Content-Type"] = ct === "application/json" ? ct : "application/json";
    if (answer.headers && answer.headers.allow) outHeaders.Allow = "POST";
    return new Response([202, 204, 205, 304].includes(status) ? null : out, { status, headers: outHeaders });
  }

  async authorized(request) {
    const token = bearer(request);
    if (token) {
      const rec = await this.ctx.storage.get(`at:${await sha256(token)}`);
      return !!rec && rec.exp > now();
    }
    const settings = await this.ctx.storage.get("settings");
    return !!(settings && settings.link_only);  // the user chose "connect with just the link"
  }

  // --- OAuth 2.1 (MCP authorization) ---------------------------------------------------------
  resourceMetadata(ctx) {
    return { resource: ctx.resource, authorization_servers: [ctx.issuer], bearer_methods_supported: ["header"],
      scopes_supported: [SCOPE], resource_name: "SafeContext" };
  }

  serverMetadata(ctx) {
    return {
      issuer: ctx.issuer,
      authorization_endpoint: `${ctx.issuer}/authorize`,
      token_endpoint: `${ctx.issuer}/token`,
      registration_endpoint: `${ctx.issuer}/register`,
      response_types_supported: ["code"],
      grant_types_supported: ["authorization_code", "refresh_token"],
      code_challenge_methods_supported: ["S256"],
      token_endpoint_auth_methods_supported: ["none"],
      scopes_supported: [SCOPE],
      authorization_response_iss_parameter_supported: true,
    };
  }

  redirectAllowed(uri) {
    return callbackAllowed(uri, this.env.ALLOWED_REDIRECT_HOSTS || DEFAULT_REDIRECT_HOSTS);
  }

  async client(clientId) {
    if (typeof clientId === "string" && Object.hasOwn(STATIC_CLIENTS, clientId)) {
      return { client_id: clientId, client_name: STATIC_CLIENTS[clientId], redirect_uris: null };
    }
    if (typeof clientId !== "string" || !/^c_[A-Za-z0-9_-]{22}$/.test(clientId)) return null;
    return (await this.ctx.storage.get(`client:${clientId}`)) || null;
  }

  redirectOk(client, uri) {
    if (!this.redirectAllowed(uri)) return false;
    return client.redirect_uris === null || client.redirect_uris.includes(uri);
  }

  async register(request) {
    if (request.method !== "POST") return json({ error: "invalid_request" }, 405, { Allow: "POST" });
    if (!this.take("auth")) return json({ error: "rate_limited" }, 429);
    const text = await request.text();
    if (text.length > 8192) return json({ error: "invalid_client_metadata" }, 400);
    let meta;
    try { meta = JSON.parse(text); } catch { return json({ error: "invalid_client_metadata" }, 400); }
    const sent = meta && meta.redirect_uris;
    if (!Array.isArray(sent) || !sent.length || sent.length > 5 || !sent.every((u) => typeof u === "string")) {
      return json({ error: "invalid_redirect_uri",
        error_description: "Only https callbacks of supported AI apps are accepted." }, 400);
    }
    // Apps often send several callbacks (Grok sends grok.com plus Cursor and localhost ones). Keep
    // the allowed ones and drop the rest (RFC 7591 lets the server change the metadata it returns).
    // /authorize still requires an exact match with a kept callback.
    const uris = [...new Set(sent.filter((u) => this.redirectAllowed(u)))];
    if (!uris.length) {
      const refused = sent.map((u) => [...u.replace(/[\p{Cc}\p{Cf}\p{Z}]/gu, "")].slice(0, 120).join("")).join(", ");
      return json({ error: "invalid_redirect_uri",
        error_description: `Only https callbacks of supported AI apps are accepted. Refused: ${refused}` }, 400);
    }
    if (meta.token_endpoint_auth_method && meta.token_endpoint_auth_method !== "none") {
      return json({ error: "invalid_client_metadata", error_description: "Public clients only (PKCE)." }, 400);
    }
    const existing = await this.ctx.storage.list({ prefix: "client:" });
    if (existing.size >= MAX_CLIENTS) {
      // Never evict a working client. Regenerating the link in the app clears them all.
      return json({ error: "invalid_client_metadata",
        error_description: "Too many connectors for this link. Regenerate the link in SafeContext." }, 400);
    }
    const client = {
      client_id: `c_${randomId(16)}`,
      client_name: cleanName(meta.client_name),
      redirect_uris: uris,
      created: now(),
    };
    await this.ctx.storage.put(`client:${client.client_id}`, client);
    return json({ client_id: client.client_id, client_id_issued_at: client.created, client_name: client.client_name,
      redirect_uris: uris, token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"] }, 201);
  }

  async authorize(request, ctx) {
    if (request.method !== "GET") return json({ error: "invalid_request" }, 405, { Allow: "GET" });
    if (!this.take("auth")) return page("Too many attempts", "<p>Wait a minute, then try connecting again from your AI app.</p>", 429);
    const q = ctx.url.searchParams;
    const client = await this.client(q.get("client_id"));
    const redirect = q.get("redirect_uri") || "";
    if (!client || !this.redirectOk(client, redirect)) {
      return page("Can't connect", "<p>This sign-in request didn't come from a known connector. Start again from your AI app's connector settings.</p>", 400);
    }
    const name = cleanName(client.client_name);
    const app = name || "your AI app";
    const state = q.get("state");
    const fail = (error) => Response.redirect(withParams(redirect, { error, state, iss: ctx.issuer }), 302);
    if (q.get("response_type") !== "code") return fail("unsupported_response_type");
    const challenge = q.get("code_challenge") || "";
    if (q.get("code_challenge_method") !== "S256" || !/^[A-Za-z0-9_-]{43,128}$/.test(challenge)) return fail("invalid_request");
    const resource = q.get("resource");
    if (resource && resource !== ctx.resource) return fail("invalid_target");

    if (!this.mac()) {
      return page("Open SafeContext on your Mac",
        `<p>${esc(name || "An AI app")} wants to connect, but SafeContext isn't running on your Mac right now.</p>
         <p>Open SafeContext, make sure <b>MCP</b> is on, then reload this page.</p>
         <p><a class="btn" href="${esc(ctx.url.pathname + ctx.url.search)}">Reload</a></p>`, 503);
    }
    const reqId = randomId(16);
    const code = String(crypto.getRandomValues(new Uint32Array(1))[0] % 10000).padStart(4, "0");
    await this.ctx.storage.put(`ap:${reqId}`, { status: "pending", client_id: client.client_id, redirect, state,
      challenge, code, exp: Date.now() + APPROVAL_TTL_MS });
    const ws = this.mac();
    try {
      ws.send(JSON.stringify({ t: "approve", id: reqId, client: name, code }));
    } catch {}
    const nonce = randomId(12);
    const waitUrl = `${ctx.url.pathname}/wait?req=${reqId}`;
    return page("Approve on your Mac",
      `<p>Switch to <b>SafeContext</b> on your Mac and click <b>Approve</b>.</p>
       <p class="code" aria-label="Matching code">${esc(code)}</p>
       <p class="muted">Check that SafeContext shows the same number. ${esc(name || "The app")} gets read-only access to your scrubbed data,
       and only while SafeContext is open.</p>
       <p id="status" class="muted" role="status">Waiting for approval…</p>
       <script nonce="${nonce}">
       (async () => {
         const s = document.getElementById("status");
         for (let i = 0; i < 200; i++) {
           await new Promise((r) => setTimeout(r, 1500));
           let r;
           try { r = await (await fetch(${JSON.stringify(waitUrl)}, { cache: "no-store" })).json(); } catch { continue; }
           if (r.redirect) { s.textContent = r.status === "approved" ? ${jsStr(`Approved. Returning to ${app}…`)} : "Declined."; location.replace(r.redirect); return; }
           if (r.status === "expired") { s.textContent = ${jsStr(`This request expired. Start again from ${app}.`)}; return; }
         }
       })();
       </script>`, 200, nonce);
  }

  async authorizeWait(ctx) {
    const reqId = ctx.url.searchParams.get("req") || "";
    if (!/^[A-Za-z0-9_-]{22}$/.test(reqId)) return json({ status: "expired" }, 400);
    const key = `ap:${reqId}`;
    const ap = await this.ctx.storage.get(key);
    if (!ap || ap.exp < Date.now()) {
      if (ap) await this.ctx.storage.delete(key);
      return json({ status: "expired" });
    }
    if (ap.status === "pending") return json({ status: "pending" });
    await this.ctx.storage.delete(key);
    if (ap.status === "denied") {
      return json({ status: "denied", redirect: withParams(ap.redirect, { error: "access_denied", state: ap.state, iss: ctx.issuer }) });
    }
    const code = randomId(32);
    await this.ctx.storage.put(`code:${await sha256(code)}`, { client_id: ap.client_id, redirect: ap.redirect,
      challenge: ap.challenge, exp: Date.now() + CODE_TTL_MS });
    return json({ status: "approved", redirect: withParams(ap.redirect, { code, state: ap.state, iss: ctx.issuer }) });
  }

  async token(request, ctx) {
    if (request.method !== "POST") return json({ error: "invalid_request" }, 405, { Allow: "POST" });
    if (!this.take("auth")) return json({ error: "slow_down" }, 429);
    const text = await request.text();
    if (text.length > 8192) return tokenError("invalid_request");
    let p;
    if ((request.headers.get("content-type") || "").includes("application/json")) {
      try { p = JSON.parse(text); } catch { return tokenError("invalid_request"); }
    } else {
      p = Object.fromEntries(new URLSearchParams(text));
    }
    const client = await this.client(p.client_id);
    if (!client) return tokenError("invalid_client", 401);
    if (p.resource && p.resource !== ctx.resource) return tokenError("invalid_target");

    if (p.grant_type === "authorization_code") {
      if (typeof p.code !== "string" || typeof p.code_verifier !== "string") return tokenError("invalid_request");
      const key = `code:${await sha256(p.code)}`;
      const rec = await this.ctx.storage.get(key);
      if (rec) await this.ctx.storage.delete(key);  // one use, success or not
      if (!rec || rec.exp < Date.now() || rec.client_id !== client.client_id ||
          (p.redirect_uri && p.redirect_uri !== rec.redirect)) return tokenError("invalid_grant");
      if (!/^[A-Za-z0-9._~-]{43,128}$/.test(p.code_verifier) || !equal(await s256(p.code_verifier), rec.challenge)) {
        return tokenError("invalid_grant");
      }
      return json(await this.issue(client.client_id), 200, { "Cache-Control": "no-store" });
    }
    if (p.grant_type === "refresh_token") {
      if (typeof p.refresh_token !== "string") return tokenError("invalid_request");
      const key = `rt:${await sha256(p.refresh_token)}`;
      const rec = await this.ctx.storage.get(key);
      if (!rec || rec.exp < now() || rec.client_id !== client.client_id) return tokenError("invalid_grant");
      await this.ctx.storage.delete([key, `at:${rec.at}`]);  // rotate
      return json(await this.issue(client.client_id), 200, { "Cache-Control": "no-store" });
    }
    return tokenError("unsupported_grant_type");
  }

  async issue(clientId) {
    await this.purgeExpired();
    const access = randomId(32), refresh = randomId(32);
    const atHash = await sha256(access);
    await this.ctx.storage.put({
      [`at:${atHash}`]: { client_id: clientId, exp: now() + ACCESS_TTL_S },
      [`rt:${await sha256(refresh)}`]: { client_id: clientId, at: atHash, exp: now() + REFRESH_TTL_S },
    });
    const ws = this.mac();
    if (ws) { try { ws.send(JSON.stringify({ t: "grants", n: await this.grantCount() })); } catch {} }
    return { access_token: access, token_type: "Bearer", expires_in: ACCESS_TTL_S, refresh_token: refresh, scope: SCOPE };
  }

  async grantCount() {
    const t = now();
    let n = 0;
    for (const v of (await this.ctx.storage.list({ prefix: "rt:" })).values()) if (v.exp > t) n++;
    return n;
  }

  async purgeExpired() {
    const t = now(), ms = Date.now(), dead = [];
    for (const prefix of ["at:", "rt:", "code:", "ap:"]) {
      for (const [k, v] of await this.ctx.storage.list({ prefix })) {
        const exp = prefix === "code:" || prefix === "ap:" ? v.exp < ms : v.exp < t;
        if (exp) dead.push(k);
      }
    }
    for (let i = 0; i < dead.length; i += 128) await this.ctx.storage.delete(dead.slice(i, i + 128));
  }

  // --- limits -----------------------------------------------------------------------------------
  take(kind) {
    const [cap, per] = LIMITS[kind];
    const t = Date.now() / 1000;
    const b = this.buckets.get(kind) || { tokens: cap, at: t };
    b.tokens = Math.min(cap, b.tokens + ((t - b.at) * cap) / per);
    b.at = t;
    this.buckets.set(kind, b);
    if (b.tokens < 1) return false;
    b.tokens -= 1;
    return true;
  }
}

// ---------------------------------------------------------------------------------------------
export function checkAllowlist(msgs) {
  for (const m of msgs) {
    if (!m || typeof m !== "object" || m.jsonrpc !== "2.0") return rpcError(null, -32600, "Invalid Request");
    if (typeof m.method !== "string") return rpcError(m.id ?? null, -32600, "Invalid Request");
    if (!METHODS.has(m.method)) return rpcError(m.id ?? null, -32601, `Method not found: ${m.method}`);
    if (m.method === "tools/call") {
      const name = m.params && m.params.name;
      if (!TOOLS.has(name)) return rpcError(m.id ?? null, -32602, `Unknown tool: ${String(name).slice(0, 60)}`);
    }
  }
  return null;
}

// The body as text, or null if it is longer than `max` bytes (read in chunks, so a huge
// chunked upload is cut off instead of buffered whole).
async function readCapped(request, max) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) { try { await reader.cancel(); } catch {} return null; }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) { all.set(c, at); at += c.byteLength; }
  return new TextDecoder().decode(all);
}

function offlineReply(msgs, batch, text) {
  const replies = [];
  for (const m of msgs) {
    if (!("id" in m)) continue;  // notifications need no answer
    replies.push(m.method === "tools/call"
      ? { jsonrpc: "2.0", id: m.id, result: { content: [{ type: "text", text }], isError: true } }
      : rpcError(m.id, -32000, text));
  }
  if (!replies.length) return new Response(null, { status: 202 });
  return json(batch ? replies : replies[0], 200);
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

function tokenError(error, status = 400) {
  return json({ error }, status, { "Cache-Control": "no-store" });
}

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function bearer(request) {
  const h = request.headers.get("Authorization") || "";
  const m = h.match(/^Bearer\s+([A-Za-z0-9._~+/=-]+)$/i);
  return m ? m[1] : null;
}

function withParams(uri, params) {
  const u = new URL(uri);
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) u.searchParams.set(k, v);
  return u.toString();
}

function now() {
  return Math.floor(Date.now() / 1000);
}

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomId(nBytes) {
  return b64url(crypto.getRandomValues(new Uint8Array(nBytes)));
}

async function sha256(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function s256(verifier) {
  return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
}

function equal(a, b) {
  const x = new TextEncoder().encode(String(a)), y = new TextEncoder().encode(String(b));
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

// An OAuth callback an app may register: https to a listed host, or (for desktop apps such as the
// Grok Mac app, which get the result on the user's own computer) plain http to a loopback address,
// any port (RFC 8252 section 7.3). The Mac still has to approve every sign-in.
export function callbackAllowed(uri, list) {
  let u;
  try { u = new URL(uri); } catch { return false; }
  if (u.username || u.password || u.hash) return false;
  if (u.protocol === "http:") return LOOPBACK_HOSTS.has(u.hostname);
  return u.protocol === "https:" && hostAllowed(u, list);
}

// Exact host match ("claude.ai"); a "*.example.com" entry allows subdomains of example.com only.
// https on the default port only (checked by the caller for the scheme).
export function hostAllowed(u, list) {
  if (u.port) return false;
  const host = u.hostname;
  return String(list).split(",").map((h) => h.trim().toLowerCase()).filter(Boolean).some((h) =>
    h.startsWith("*.") ? host.endsWith(h.slice(1)) && host.length > h.length - 1 : host === h);
}

// A registered app's name, safe to show: no control, format (bidi) or separator characters,
// whitespace collapsed, at most MAX_NAME characters. "" when there is no usable name.
export function cleanName(v) {
  if (typeof v !== "string") return "";
  const s = v.normalize("NFKC").replace(/\s+/gu, " ").replace(/[\p{Cc}\p{Cf}]/gu, "").replace(/ +/g, " ").trim();
  return [...s].slice(0, MAX_NAME).join("").trim();
}

// A string literal safe inside an inline <script>.
function jsStr(s) {
  return JSON.stringify(String(s)).replace(/[<>&\u2028\u2029]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function page(title, inner, status = 200, nonce = "") {
  const csp = `default-src 'none'; style-src 'unsafe-inline'; ${nonce ? `script-src 'nonce-${nonce}'; connect-src 'self';` : ""} ` +
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'";
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>${esc(title)} · SafeContext</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#14171c;--muted:#5d6673;--accent:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e8eaee;--muted:#9aa3af}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
main{background:var(--card);max-width:440px;width:100%;border-radius:16px;padding:28px;box-shadow:0 1px 3px #0002}
h1{font-size:22px;margin:0 0 12px}.muted{color:var(--muted);font-size:14px}
.code{font:600 40px/1 ui-monospace,Menlo,monospace;letter-spacing:.2em;text-align:center;margin:20px 0}
.btn{display:inline-block;background:var(--accent);color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none}
.brand{font-weight:600;color:var(--muted);font-size:14px;margin-bottom:16px}
</style></head><body><main><div class="brand">SafeContext</div><h1>${esc(title)}</h1>${inner}</main></body></html>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": csp,
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" } });
}
