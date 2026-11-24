// Run with `npm test` (Miniflare via @cloudflare/vitest-pool-workers). Nothing is deployed.
import { SELF, env, runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { OFFLINE_MESSAGE, callbackAllowed, cleanName, hostAllowed, route, s256 } from "../src/relay.js";

const ORIGIN = "https://relay.safecontext.me";
const REDIRECT = "https://grok.com/connectors/oauth/callback";
const CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback";

function rand() {
  const b = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// A stand-in for the SafeContext app: connects, answers MCP requests, approves sign-ins.
async function connectMac(link, secret = rand(), { answer = true } = {}) {
  const res = await SELF.fetch(`${ORIGIN}/g/${link}/connect`, {
    headers: { Upgrade: "websocket", Authorization: `Bearer ${secret}` },
  });
  if (res.status !== 101) return { status: res.status };
  const ws = res.webSocket;
  ws.accept();
  const inbox = [];
  const waiters = [];
  ws.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    if (msg.t === "http" && answer) {
      const req = JSON.parse(msg.body);
      const result = req.method === "tools/call"
        ? { content: [{ type: "text", text: `ran ${req.params.name}` }], isError: false }
        : { ok: true };
      ws.send(JSON.stringify({ t: "http_result", id: msg.id, status: 200, headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: req.id, result }) }));
    }
    inbox.push(msg);
    for (const w of waiters.splice(0)) w();
  });
  const next = async (t) => {
    for (let i = 0; i < 100; i++) {
      const m = inbox.find((x) => x.t === t);
      if (m) { inbox.splice(inbox.indexOf(m), 1); return m; }
      await new Promise((r) => { waiters.push(r); setTimeout(r, 50); });
    }
    throw new Error(`mac never got ${t}`);
  };
  return { status: 101, ws, inbox, next, secret, send: (m) => ws.send(JSON.stringify(m)) };
}

async function signIn(link, mac, { name = "Grok", redirect: REDIRECT_ = REDIRECT, clientId = null } = {}) {
  const REDIRECT = REDIRECT_;
  let client_id = clientId;
  if (!client_id) {
    const reg = await SELF.fetch(`${ORIGIN}/g/${link}/register`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ client_name: name, redirect_uris: [REDIRECT], token_endpoint_auth_method: "none" }),
    });
    expect(reg.status).toBe(201);
    ({ client_id } = await reg.json());
  }
  const verifier = rand() + rand();
  const q = new URLSearchParams({ response_type: "code", client_id, redirect_uri: REDIRECT, state: "xyz",
    code_challenge: await s256(verifier), code_challenge_method: "S256", resource: `${ORIGIN}/g/${link}/mcp` });
  const page = await SELF.fetch(`${ORIGIN}/g/${link}/authorize?${q}`);
  expect(page.status).toBe(200);
  const html = await page.text();
  const ask = await mac.next("approve");
  expect(html).toContain(ask.code);  // the browser and the Mac show the same number
  mac.lastAsk = ask;
  mac.lastHtml = html;
  const reqId = html.match(/wait\?req=([A-Za-z0-9_-]+)/)[1];
  let w = await (await SELF.fetch(`${ORIGIN}/g/${link}/authorize/wait?req=${reqId}`)).json();
  expect(w.status).toBe("pending");
  mac.send({ t: "approve_result", id: ask.id, ok: true });
  for (let i = 0; i < 20 && w.status === "pending"; i++) {
    await new Promise((r) => setTimeout(r, 20));
    w = await (await SELF.fetch(`${ORIGIN}/g/${link}/authorize/wait?req=${reqId}`)).json();
  }
  expect(w.status).toBe("approved");
  const back = new URL(w.redirect);
  expect(back.origin + back.pathname).toBe(REDIRECT);
  expect(back.searchParams.get("state")).toBe("xyz");
  const tok = await SELF.fetch(`${ORIGIN}/g/${link}/token`, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", code: back.searchParams.get("code"), client_id,
      redirect_uri: REDIRECT, code_verifier: verifier }),
  });
  expect(tok.status).toBe(200);
  const body = await tok.json();
  return { ...body, client_id, verifier, code: back.searchParams.get("code") };
}

const call = (link, token, msg, extra = {}) => SELF.fetch(`${ORIGIN}/g/${link}/mcp`, {
  method: "POST",
  headers: { "content-type": "application/json", accept: "application/json, text/event-stream",
    ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra },
  body: typeof msg === "string" ? msg : JSON.stringify(msg),
});
const toolCall = (name, args = {}, id = 1) => ({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });

describe("routing", () => {
  it("knows only link paths", async () => {
    expect((await SELF.fetch(`${ORIGIN}/`)).status).toBe(200);
    expect((await SELF.fetch(`${ORIGIN}/nope`)).status).toBe(404);
    expect((await SELF.fetch(`${ORIGIN}/g/short/mcp`)).status).toBe(404);
    const l = rand();
    expect(route(`/.well-known/oauth-protected-resource/g/${l}/mcp`)).toEqual({ link: l, sub: "wk:resource" });
    expect(route(`/g/${l}/mcp/.well-known/oauth-protected-resource`)).toEqual({ link: l, sub: "wk:resource" });
    expect(route(`/.well-known/oauth-authorization-server/g/${l}`)).toEqual({ link: l, sub: "wk:as" });
  });

  it("serves OAuth metadata", async () => {
    const l = rand();
    const pr = await (await SELF.fetch(`${ORIGIN}/.well-known/oauth-protected-resource/g/${l}/mcp`)).json();
    expect(pr.resource).toBe(`${ORIGIN}/g/${l}/mcp`);
    expect(pr.authorization_servers).toEqual([`${ORIGIN}/g/${l}`]);
    const as = await (await SELF.fetch(`${ORIGIN}/.well-known/oauth-authorization-server/g/${l}`)).json();
    expect(as.issuer).toBe(`${ORIGIN}/g/${l}`);
    expect(as.code_challenge_methods_supported).toEqual(["S256"]);
    expect(as.registration_endpoint).toBe(`${ORIGIN}/g/${l}/register`);
  });
});

describe("the Mac's socket", () => {
  it("first secret owns the link; others are refused", async () => {
    const l = rand();
    const mac = await connectMac(l);
    expect(mac.status).toBe(101);
    expect((await mac.next("hello")).grants).toBe(0);
    expect((await connectMac(l, rand())).status).toBe(401);
    expect((await connectMac(l, "short")).status).toBe(401);
    expect((await connectMac(l, mac.secret)).status).toBe(101);  // the same Mac reconnecting
  });

  it("needs a WebSocket upgrade", async () => {
    expect((await SELF.fetch(`${ORIGIN}/g/${rand()}/connect`)).status).toBe(426);
  });
});

describe("OAuth", () => {
  it("challenges unauthenticated MCP requests", async () => {
    const l = rand();
    const res = await call(l, null, { jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
    expect(res.status).toBe(401);
    expect(res.headers.get("WWW-Authenticate")).toContain(`resource_metadata="${ORIGIN}/.well-known/oauth-protected-resource/g/${l}/mcp"`);
    expect((await call(l, "made-up", toolCall("search"))).headers.get("WWW-Authenticate")).toContain('error="invalid_token"');
  });

  it("signs an app in after Approve on the Mac, then forwards requests", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    expect(t.token_type).toBe("Bearer");
    expect((await mac.next("grants")).n).toBe(1);
    const res = await call(l, t.access_token, toolCall("search", { query: "dinner" }));
    expect(res.status).toBe(200);
    expect((await res.json()).result.content[0].text).toBe("ran search");
    const fwd = await mac.next("http");
    expect(fwd.headers["mcp-protocol-version"]).toBeUndefined();
    expect(fwd.headers.authorization).toBeUndefined();  // the app's token never reaches the Mac
  });

  it("codes work once and need the right verifier", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    const again = await SELF.fetch(`${ORIGIN}/g/${l}/token`, {
      method: "POST", body: new URLSearchParams({ grant_type: "authorization_code", code: t.code,
        client_id: t.client_id, code_verifier: t.verifier }),
    });
    expect((await again.json()).error).toBe("invalid_grant");
  });

  it("rotates refresh tokens", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    const r1 = await (await SELF.fetch(`${ORIGIN}/g/${l}/token`, { method: "POST",
      body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: t.refresh_token, client_id: t.client_id }) })).json();
    expect(r1.access_token).toBeTruthy();
    expect((await call(l, t.access_token, toolCall("search", { query: "x" }))).status).toBe(401);  // old one is gone
    const r2 = await (await SELF.fetch(`${ORIGIN}/g/${l}/token`, { method: "POST",
      body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: t.refresh_token, client_id: t.client_id }) })).json();
    expect(r2.error).toBe("invalid_grant");
  });

  it("denied approval goes back to the app as access_denied", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const verifier = rand() + rand();
    const q = new URLSearchParams({ response_type: "code", client_id: "grok", redirect_uri: REDIRECT, state: "s",
      code_challenge: await s256(verifier), code_challenge_method: "S256" });
    const html = await (await SELF.fetch(`${ORIGIN}/g/${l}/authorize?${q}`)).text();
    const ask = await mac.next("approve");
    mac.send({ t: "approve_result", id: ask.id, ok: false });
    const reqId = html.match(/wait\?req=([A-Za-z0-9_-]+)/)[1];
    let w;
    for (let i = 0; i < 20; i++) {
      w = await (await SELF.fetch(`${ORIGIN}/g/${l}/authorize/wait?req=${reqId}`)).json();
      if (w.status !== "pending") break;
      await new Promise((r) => setTimeout(r, 20));
    }
    expect(new URL(w.redirect).searchParams.get("error")).toBe("access_denied");
  });

  it("signs Claude in with its documented callback and names it on the page and the Mac", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac, { name: "Claude", redirect: CLAUDE_REDIRECT });
    expect(t.access_token).toBeTruthy();
    expect(mac.lastAsk.client).toBe("Claude");
    expect(mac.lastHtml).toContain("Approved. Returning to Claude…");
    expect(mac.lastHtml).not.toContain("Grok");
  });

  it("falls back to a neutral name when the app gives none", async () => {
    const l = rand();
    const mac = await connectMac(l);
    await signIn(l, mac, { name: null, redirect: CLAUDE_REDIRECT });
    expect(mac.lastAsk.client).toBe("");
    expect(mac.lastHtml).toContain("Returning to your AI app…");
  });

  it("cleans and shortens registered names before showing them", async () => {
    const l = rand();
    const mac = await connectMac(l);
    await signIn(l, mac, { name: "Evil\u202e</script><b>\n" + "x".repeat(80), redirect: CLAUDE_REDIRECT });
    expect(mac.lastAsk.client).toBe("Evil</script><b> " + "x".repeat(23));
    expect(mac.lastHtml).not.toContain("</script><b>");
    expect(cleanName("  Claude\u200b  Desktop\u0000 ")).toBe("Claude Desktop");
    expect(cleanName(42)).toBe("");
  });

  it("keeps the static client grok working and adds safecontext", async () => {
    for (const clientId of ["grok", "safecontext"]) {
      const l = rand();
      const mac = await connectMac(l);
      const t = await signIn(l, mac, { clientId, redirect: clientId === "grok" ? REDIRECT : CLAUDE_REDIRECT });
      expect((await call(l, t.access_token, toolCall("search", { query: "x" }))).status).toBe(200);
      expect(mac.lastAsk.client).toBe(clientId === "grok" ? "Grok" : "");
      const r = await (await SELF.fetch(`${ORIGIN}/g/${l}/token`, { method: "POST",
        body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: t.refresh_token, client_id: clientId }) })).json();
      expect(r.access_token).toBeTruthy();
    }
  });

  it("matches callback hosts exactly; wildcards only where listed", () => {
    const list = "grok.com,*.grok.com,x.ai,*.x.ai,claude.ai";
    const ok = (uri) => hostAllowed(new URL(uri), list);
    for (const uri of [CLAUDE_REDIRECT, REDIRECT, "https://x.ai/cb", "https://auth.x.ai/cb", "https://CLAUDE.AI/cb"]) {
      expect(ok(uri), uri).toBe(true);
    }
    for (const uri of ["https://claude.ai.evil.com/cb", "https://evilclaude.ai/cb", "https://www.claude.ai/cb",
      "https://claude.com/cb", "https://evilx.ai/cb", "https://claude.ai:8443/cb", "https://agent.meta.ai/cb"]) {
      expect(ok(uri), uri).toBe(false);
    }
  });

  it("keeps the allowed callbacks when Grok also sends Cursor ones", async () => {
    const l = rand();
    const sent = ["cursor://anysphere.cursor-mcp/oauth/callback", "https://www.cursor.com/agents/mcp/oauth/callback",
      REDIRECT, "http://localhost:8787/callback", "http://127.0.0.1:8787/callback"];
    const reg = await SELF.fetch(`${ORIGIN}/g/${l}/register`, { method: "POST",
      body: JSON.stringify({ client_name: "Grok", redirect_uris: sent, token_endpoint_auth_method: "none" }) });
    expect(reg.status).toBe(201);
    const { client_id, redirect_uris } = await reg.json();
    expect(redirect_uris).toEqual([REDIRECT, "http://localhost:8787/callback", "http://127.0.0.1:8787/callback"]);
    // A dropped callback can't be used to sign in.
    for (const redirect_uri of ["cursor://anysphere.cursor-mcp/oauth/callback", "https://www.cursor.com/agents/mcp/oauth/callback"]) {
      const q = new URLSearchParams({ response_type: "code", client_id, redirect_uri,
        code_challenge: "a".repeat(43), code_challenge_method: "S256" });
      expect((await SELF.fetch(`${ORIGIN}/g/${l}/authorize?${q}`)).status).toBe(400);
    }
    // The kept one signs in end to end.
    const mac = await connectMac(l);
    const t = await signIn(l, mac, { clientId: client_id });
    expect(t.access_token).toBeTruthy();
  });

  it("signs the Grok Mac app in through its localhost callback", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac, { name: "Grok", redirect: "http://localhost:8787/callback" });
    expect(t.access_token).toBeTruthy();
    const t2 = await signIn(l, mac, { name: "Grok", redirect: "http://[::1]:53682/cb" });
    expect(t2.access_token).toBeTruthy();
  });

  it("allows loopback http only, never other http hosts", () => {
    const ok = (uri) => callbackAllowed(uri, "grok.com,claude.ai");
    for (const uri of ["http://localhost:8787/callback", "http://127.0.0.1/cb", "http://[::1]:9/cb"]) {
      expect(ok(uri), uri).toBe(true);
    }
    for (const uri of ["http://localhost.evil.com/cb", "http://127.0.0.1.evil.com/cb", "http://evil.example/cb",
      "http://0.0.0.0:8787/cb", "http://192.168.1.2/cb", "http://user@localhost/cb", "http://localhost/cb#x",
      "ftp://localhost/cb", "cursor://anysphere.cursor-mcp/oauth/callback"]) {
      expect(ok(uri), uri).toBe(false);
    }
  });

  it("names the refused callbacks when none is allowed", async () => {
    const res = await SELF.fetch(`${ORIGIN}/g/${rand()}/register`, { method: "POST",
      body: JSON.stringify({ redirect_uris: ["http://evil.example/cb", "cursor://x/cb\u202e"] }) });
    expect(res.status).toBe(400);
    const { error_description } = await res.json();
    expect(error_description).toContain("Refused: http://evil.example/cb, cursor://x/cb");
    expect(error_description).not.toContain("\u202e");
  });

  it("refuses look-alike, unlisted and plain-http callbacks", async () => {
    for (const uri of ["https://evil.example/cb", "http://grok.com/cb", "https://grok.com.evil.example/cb",
      "https://claude.ai.evil.com/api/mcp/auth_callback", "https://evilclaude.ai/api/mcp/auth_callback",
      "http://claude.ai/api/mcp/auth_callback", "https://sub.claude.ai/api/mcp/auth_callback",
      "https://claude.ai:444/api/mcp/auth_callback", "https://user@claude.ai/api/mcp/auth_callback",
      "https://claude.ai/api/mcp/auth_callback#frag", "javascript:alert(1)"]) {
      const res = await SELF.fetch(`${ORIGIN}/g/${rand()}/register`, { method: "POST",
        body: JSON.stringify({ redirect_uris: [uri] }) });
      expect(res.status).toBe(400);
    }
    for (const client_id of ["grok", "safecontext"]) {
      for (const redirect_uri of ["https://evil.example/cb", "https://claude.ai.evil.com/cb", "http://claude.ai/cb"]) {
        const q = new URLSearchParams({ response_type: "code", client_id, redirect_uri,
          code_challenge: "a".repeat(43), code_challenge_method: "S256" });
        expect((await SELF.fetch(`${ORIGIN}/g/${rand()}/authorize?${q}`)).status).toBe(400);
      }
    }
  });

  it("asks the user to open the app when the Mac is offline", async () => {
    const q = new URLSearchParams({ response_type: "code", client_id: "grok", redirect_uri: REDIRECT,
      code_challenge: "a".repeat(43), code_challenge_method: "S256" });
    const res = await SELF.fetch(`${ORIGIN}/g/${rand()}/authorize?${q}`);
    expect(res.status).toBe(503);
    expect(await res.text()).toContain("Open SafeContext");
  });
});

describe("MCP forwarding rules", () => {
  it("allows only the read-only tools and MCP basics", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    for (const name of ["search_mail", "delete_everything", "list_items_x"]) {
      expect((await (await call(l, t.access_token, toolCall(name))).json()).error.code).toBe(-32602);
    }
    const r = await (await call(l, t.access_token, { jsonrpc: "2.0", id: 2, method: "resources/read", params: {} })).json();
    expect(r.error.code).toBe(-32601);
    expect(mac.inbox.filter((m) => m.t === "http")).toEqual([]);  // none of these reached the Mac
  });

  it("returns a clean error when SafeContext isn't running", { timeout: 40_000 }, async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    mac.ws.close(1000, "bye");
    await new Promise((r) => setTimeout(r, 200));
    const r = await (await call(l, t.access_token, toolCall("search", { query: "x" }))).json();
    expect(r.result.isError).toBe(true);
    expect(r.result.content[0].text).toBe(OFFLINE_MESSAGE);
    const init = await (await call(l, t.access_token, { jsonrpc: "2.0", id: 3, method: "initialize", params: {} })).json();
    expect(init.error.message).toContain("SafeContext isn't running on your Mac");
    expect((await call(l, t.access_token, { jsonrpc: "2.0", method: "notifications/initialized" })).status).toBe(202);
  });

  it("limits size and rate", { timeout: 30_000 }, async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    const big = JSON.stringify(toolCall("search", { query: "x".repeat(70 * 1024) }));
    expect((await call(l, t.access_token, big)).status).toBe(413);
    expect((await SELF.fetch(`${ORIGIN}/g/${l}/mcp`, { headers: { Authorization: `Bearer ${t.access_token}` } })).status).toBe(405);
    let limited = false;
    for (let i = 0; i < 400 && !limited; i++) {
      limited = (await call(l, t.access_token, { jsonrpc: "2.0", id: i, method: "ping" })).status === 429;
    }
    expect(limited).toBe(true);
  });

  it("times out a Mac that never answers", { timeout: 40_000 }, async () => {
    const l = rand();
    const mac = await connectMac(l, rand(), { answer: false });
    mac.send({ t: "settings", link_only: true });
    await new Promise((r) => setTimeout(r, 50));
    const r = await (await call(l, null, toolCall("search", { query: "x" }))).json();
    expect(r.result.content[0].text).toContain("didn't answer in time");
  });

  it("link-only mode lets a URL-only connector in, and only when the Mac says so", async () => {
    const l = rand();
    const mac = await connectMac(l);
    expect((await call(l, null, toolCall("list_sources"))).status).toBe(401);
    mac.send({ t: "settings", link_only: true });
    await new Promise((r) => setTimeout(r, 50));
    expect((await (await call(l, null, toolCall("list_sources"))).json()).result.content[0].text).toBe("ran list_sources");
    mac.send({ t: "settings", link_only: false });
    await new Promise((r) => setTimeout(r, 50));
    expect((await call(l, null, toolCall("list_sources"))).status).toBe(401);
  });

  it("regenerating the link on the Mac revokes everything", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    mac.send({ t: "revoke_all" });
    await new Promise((r) => setTimeout(r, 50));
    expect((await call(l, t.access_token, toolCall("search", { query: "x" }))).status).toBe(401);
    const left = await runInDurableObject(env.LINKS.get(env.LINKS.idFromName(l)), async (_, state) =>
      [...(await state.storage.list()).keys()]);
    expect(left).toEqual([]);
  });

  it("stores no request or response content", async () => {
    const l = rand();
    const mac = await connectMac(l);
    const t = await signIn(l, mac);
    await call(l, t.access_token, toolCall("search", { query: "very-private-words" }));
    const dump = await runInDurableObject(env.LINKS.get(env.LINKS.idFromName(l)), async (_, state) =>
      JSON.stringify([...(await state.storage.list())]));
    expect(dump).not.toContain("very-private-words");
    expect(dump).not.toContain("ran search");
    expect(dump).not.toContain(t.access_token);   // tokens are stored hashed
    expect(dump).not.toContain(mac.secret);
  });
});
