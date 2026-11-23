# SafeContext relay

Lets Grok (grok.com and the Grok apps) use SafeContext on a user's Mac. Grok's custom
connectors call a **remote** MCP server from xAI's cloud and reject localhost and private
addresses, so the Mac can't be reached directly. The relay sits in between:

```
Grok (xAI cloud) --HTTPS, MCP Streamable HTTP--> Worker --> LinkRelay Durable Object --WebSocket--> SafeContext.app
                                                             (one per link)            (opened by the Mac)
```

- The connector link is `https://relay.safecontext.me/g/<link>/mcp`, where `<link>` is 256
  random bits made on the Mac. Each link has its own Durable Object.
- The Mac app opens an outbound WebSocket to `/g/<link>/connect` with
  `Authorization: Bearer <device secret>` (another 256 random bits, in the Mac's Keychain). The
  first secret a link sees owns it; the Durable Object keeps only its SHA-256.
- Grok signs in with OAuth 2.1, as the MCP authorization spec describes: protected-resource
  metadata (RFC 9728), authorization-server metadata (RFC 8414), dynamic client registration
  (RFC 7591), authorization code + PKCE S256 only, public clients, `resource` checked, refresh
  tokens rotated. The sign-in page shows a 4-digit code and asks the user to click **Approve** in
  SafeContext on their Mac, which shows the same code. For forms that ask for a Client ID instead
  of registering, the public client `grok` works with the same callback rules.
- Each MCP request is checked (token, size, rate, allowlist), sent down the socket, and the
  Mac's answer is returned. If the Mac isn't connected, Grok gets a normal MCP answer:
  "SafeContext isn't running on your Mac…".

Source: [src/index.js](src/index.js). Mac side: `src/safecontext/grok_link.py`.

## Security properties

| Rule | How |
| --- | --- |
| No storage of bodies | Requests and answers exist only in memory while in flight. Durable Object storage holds auth metadata only: the device-secret hash, OAuth client registrations, SHA-256 hashes of codes and tokens (with expiry), pending approvals (client, callback, PKCE challenge, 4-digit code) and one setting. A test dumps storage after a call and checks the query and result aren't there. |
| No body logging | The code never calls `console.*`. `wrangler.toml` turns off Workers observability and Logpush. Errors are caught and answered without being logged. |
| Read-only, allowlisted | Only `initialize`, `ping`, `tools/list`, `tools/call`, `notifications/initialized` and `notifications/cancelled`; `tools/call` only for `search`, `read`, `list_items`, `list_sources`. Anything else is answered by the relay and never reaches the Mac. The Mac checks the same allowlist again. |
| Per-user limits | Per link (token buckets): 120 MCP requests/min, 20 OAuth calls/min, 10 Mac connections/min, 60 refused requests/min. Requests ≤ 64 KiB, answers ≤ 1 MiB (the Mac caps itself at 900 KiB), OAuth bodies ≤ 8 KiB (MCP bodies are read in chunks and cut off at the limit), at most 20 registered clients (more are refused, never evicted). |
| Timeouts | 25 s for the Mac to answer, 5 min for an approval or an authorization code. Access tokens last 30 days, refresh tokens 180 days (rotated on use). |
| Grok's token stays at the relay | Only `content-type`, `accept` and `mcp-protocol-version` are passed to the Mac. |
| Callbacks | `https` only, on `grok.com`, `x.ai` or their subdomains (`ALLOWED_REDIRECT_HOSTS`). |
| Revocation | **Regenerate link** in the app sends `revoke_all` (the Durable Object deletes all its storage) and switches to a new link. Turning **Use in Grok** off closes the socket, so nothing more reaches the Mac. |
| Approval page | Strict CSP with a nonce, `frame-ancestors 'none'`, no referrer, no cache. |

What the relay can see: the scrubbed results it carries, in memory, because TLS ends at
Cloudflare. That is why it must stay a pipe, and why the privacy policy says so.

### Link-only fallback

If it turns out Grok's connector can't do OAuth with this server, the app's **Trouble
connecting?** section has "Let Grok connect with just the link". The Mac then tells its
Durable Object to accept requests without a token, so the 256-bit link itself is the credential.
It is off by default, and Regenerate link still revokes it.

## Test locally

These run on your Mac with Miniflare (through `@cloudflare/vitest-pool-workers`); nothing is
deployed and no Cloudflare resources are created. Run each command from this `relay/` folder.

1. Check that Node.js 20 or newer is installed (if not, install the LTS from nodejs.org):

   ```bash
   node --version
   ```

2. Install the dev tools (wrangler, vitest):

   ```bash
   npm install
   ```

3. Run the relay tests:

   ```bash
   npm test
   ```

4. Optional: run the relay locally on port 8787:

   ```bash
   npx wrangler dev
   ```

5. Optional: in another Terminal, from the repo root, open the app against that local relay.
   Grok itself can't reach your laptop, so this checks the Mac side only (the card should say
   "Waiting for Grok"):

   ```bash
   SAFECONTEXT_RELAY_URL=http://127.0.0.1:8787 .venv/bin/safecontext app
   ```

## Deploy (you do this; one step at a time)

The relay runs on the same Cloudflare account that hosts safecontext.me. SQLite-backed Durable
Objects are available on the Workers Free plan. Run each command from this `relay/` folder.

1. Sign in to Cloudflare. A browser window opens; choose the account that has the
   `safecontext.me` zone:

   ```bash
   npx wrangler login
   ```

2. Check that you're on the right account (it should list the account with `safecontext.me`):

   ```bash
   npx wrangler whoami
   ```

3. Build without deploying, to catch config errors:

   ```bash
   npx wrangler deploy --dry-run --outdir dist
   ```

4. Deploy. This creates the `safecontext-relay` Worker, its `LinkRelay` Durable Object class and
   the `relay.safecontext.me` custom domain (Cloudflare adds the DNS record and certificate):

   ```bash
   npx wrangler deploy
   ```

5. Check it answers (it may take a minute for the certificate):

   ```bash
   curl https://relay.safecontext.me/
   ```

   You should see `SafeContext relay. Nothing is stored here.`

6. In the Cloudflare dashboard, open **Workers & Pages → safecontext-relay → Settings →
   Observability** and confirm **Workers Logs** and **Logpush** are off. No command for this one.

7. Try it: open SafeContext, turn on **Use in Grok**, and follow the three steps on the card
   (grok.com/connectors → New connector → Custom → paste → Approve in SafeContext). Then ask Grok
   something like "what's on my calendar this week?" and check the card's **Activity** list.

To update later, repeat step 4. To take the relay down entirely:

```bash
npx wrangler delete
```

## Protocol between the relay and the Mac

JSON text frames on the WebSocket:

| Direction | Message |
| --- | --- |
| relay → Mac | `{"t":"hello","grants":n}` on connect; `{"t":"grants","n":n}` when Grok signs in |
| relay → Mac | `{"t":"http","id","method","headers","body"}`: one MCP request |
| relay → Mac | `{"t":"approve","id","client","code"}`: Grok is signing in; show Approve |
| Mac → relay | `{"t":"http_result","id","status","headers","body"}` |
| Mac → relay | `{"t":"approve_result","id","ok"}` |
| Mac → relay | `{"t":"settings","link_only":bool}` after each connect |
| Mac → relay | `{"t":"revoke_all"}`: forget every client, token and the device (Regenerate link) |
