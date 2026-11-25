# SafeContext

A private context layer for AI. SafeContext syncs your Gmail, Google Calendar, and Dropbox to your
Mac, scrubs them locally, and only then lets an AI read them. iMessage is included. WhatsApp and call
recordings are next.

It ships as a command-line tool (`safecontext`), a Mac app (pywebview), a local MCP server
for AI apps on your Mac, and **Use in Grok**: a one-switch connector that lets Grok (grok.com
and the Grok apps) search your scrubbed data through a live relay while the app is open.

## Privacy model

Mail is fetched from Gmail, parsed and scrubbed **in memory**, and only the scrubbed
result is written to a local SQLite database. Unscrubbed mail is never stored or logged.
Everything up to that point runs on your Mac; Gmail is read with the read-only
`gmail.readonly` scope (or `gmail.metadata` with `connect --headers-only`, which
fetches no message bodies). Google Calendar, if you connect it, is read with the read-only
`calendar.readonly` scope, requested separately only when you connect Calendar. Calendar
events go through the same scrubber (dial-in PINs and meeting passcodes are never read);
email-only rules such as `List-Unsubscribe` don't apply to them.
Dropbox, if you connect it, is a separate sign-in with the read scopes `account_info.read`,
`files.metadata.read` and `files.content.read`. Text documents are copied and scrubbed here;
other files keep only a name, type, and size. SafeContext cannot upload, edit, delete, move,
or share anything in Dropbox. Set `SAFECONTEXT_DROPBOX_APP_KEY` to your Dropbox app key
(`SAFECONTEXT_DROPBOX_APP_SECRET` only if the app has a secret).

PDFs are separate from mail and calendar. On the Files screen you add folders and
individual PDFs. Those are the only PDFs whose text SafeContext may read. A folder
covers the PDFs inside it. A PDF you add on its own stays readable if you later
remove the folder. A shortcut that points out of an added folder is skipped.
Every other file stays unread.

**Dropped entirely** (only the message id and a reason are kept, so sync doesn't refetch it):

- Spam, trash, drafts, chats, and Promotions/Social/Forums categories
- Bulk mail: `List-Unsubscribe` or `Precedence: bulk/junk` headers, and bounce notices
  (mailer-daemon). Mail from no-reply addresses (receipts, bookings) is kept and scrubbed,
  unless another rule here drops it
- Security mail: verification/login codes, password resets, sign-in alerts, 2FA, magic links,
  and account-access notices: an app or OAuth application connected to your account, a new
  device signed in, a new SSH/GPG/API key or passkey added, a recovery email or phone
  changed, 2FA turned on or off
- Sensitive senders: a built-in list of banks, brokerages, payment apps, tax, health,
  insurance and pharmacy domains, plus domains containing words like `bank`, `clinic`,
  `insurance`, `mortgage`
- Dating apps: Hinge, Tinder, Bumble, Match, OkCupid, Grindr, Feeld and similar, plus
  domains containing `dating`
- Sensitive subjects: statements, tax forms (W-2, 1099…), direct deposit, pay stubs,
  lab results, prescriptions, explanation of benefits, appointment reminders, and similar

**Redacted inline** as `[REDACTED:TYPE]` in subjects and bodies: credit cards, IBANs,
SSNs, bank account and routing numbers, CVVs, dates of birth, phone numbers, street
addresses, API keys, JWTs, private keys, passwords, one-time codes, TOTP secrets,
backup codes, and booking codes (airline record locators and hotel, rail and car-rental
confirmation numbers, when they follow a word like "confirmation" or "reservation"). Quoted replies and signatures are stripped. Attachments are stored as
filename, type and size only; there is no field that could hold their contents.

The scrubber leans toward dropping: a wrongly dropped newsletter costs little, a wrongly
kept bank statement is the failure this project exists to prevent. It is pattern-based,
not a guarantee, so review the output (see the demo section) before trusting it with real mail.

**What leaves the Mac:**

- Gmail and Google Calendar API traffic to sync, and Google's OAuth sign-in.
- Dropbox API traffic to list files and download text, and Dropbox's OAuth sign-in.
- With **Use in Grok** on: the results of the search/read tools Grok calls (scrubbed text only),
  answered on your Mac and passed through SafeContext's relay (`relay.safecontext.me`) to Grok.
  The relay doesn't store or log them. From there xAI's own privacy policy applies. Turn the
  switch off and nothing more is sent; **Regenerate link** revokes Grok's access entirely.
- The local MCP server sends nothing itself; the client you connect decides what it does with the results.

Demo mode (below) makes no network calls.

## Install for development

Requires Python 3.11 or newer; 3.12 is what the Mac app is built with. macOS's built-in
`python3` is 3.9 and won't work. Install 3.12 from python.org or with `uv python install 3.12`.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/safecontext --help
```

If `python3.12` isn't on your `PATH` (for example, a uv-managed install), use
`uv venv --python 3.12 .venv` instead, then run the `pip install` step above.

The app targets macOS:
tokens and keys are stored in the macOS Keychain and the default data directory is under
`~/Library/Application Support`.

## Google OAuth client

The OAuth client file is git-ignored (`src/safecontext/oauth_client.json`, `oauth_client*.json`,
`client_secret*.json`), so you create your own:

1. In Google Cloud Console, enable the Gmail API and the Google Calendar API for a project and configure
   the OAuth consent screen with the `gmail.readonly`, `gmail.metadata` and `calendar.readonly` scopes.
2. Create an OAuth client of type **Desktop app** and download the JSON (it must contain an `installed` key).
3. Save it as `src/safecontext/oauth_client.json`, or point `SAFECONTEXT_OAUTH_CLIENT` at it.

Sign-in uses the loopback redirect with PKCE. For signed Mac builds the file is bundled
into the app; see [packaging/README.md](packaging/README.md).

## CLI

Run as `safecontext …` or `python -m safecontext …`. The global option `--data-dir` goes before the
subcommand and defaults to `~/Library/Application Support/SafeContext-Dev` when run from a checkout
(see [Development and packaged app side by side](#development-and-packaged-app-side-by-side)).

| Command | What it does |
| --- | --- |
| `connect [--source gmail\|gcal\|imessage\|dropbox] [--headers-only] [--email ADDR]` | Sign in to one source. `--headers-only` is Gmail's metadata scope (no bodies). `--email` pre-fills the chooser when that source supports it. |
| `sync [--source gmail\|gcal\|imessage\|dropbox]` | Sync every connected source (or one): backfill, then incremental updates. Resumable. |
| `sources` | List sources, their status, item counts and the access each needs. |
| `status` | Print account, sync mode, auth state, counts and whether a sync is running (JSON). |
| `search QUERY [--source ID] [--from SENDER] [--after DATE] [--before DATE] [--limit N]` | Full-text search of the scrubbed store (default limit 10). |
| `disconnect [--source gmail\|gcal\|imessage\|dropbox] [--wipe]` | Forget the source's token. A Google grant is revoked only when no other Google source still uses it. Dropbox's token is revoked on its own. `--wipe` also deletes that source's local data. |
| `mcp` | Run the MCP server on stdio (for local AI apps). |
| `app [--demo] [--debug]` | Open the Mac app window. |

Running `safecontext` with no arguments opens the app (this is how the packaged `.app` launches).

Typical first run:

```bash
safecontext connect
safecontext sync
safecontext search "dinner" --after 2025-01-01
```

A data folder belongs to one Google account; run `disconnect --wipe` before connecting a different one.
Only one sync runs at a time (the app and CLI share a lock).

## Mac app

Requires a Mac with Apple silicon (M1 or later). The bundled app is arm64 only and
needs macOS 11 or later.

```bash
safecontext app            # your real mailbox
safecontext app --debug    # with the web inspector
```

The window lets you connect Gmail, watch sync progress (including syncs started from the CLI),
search and read mail, see a privacy report of what was kept, dropped and redacted, turn on
Use in Grok, and copy the local MCP config. It auto-syncs periodically while open.
For a signed, double-clickable build, see [packaging/README.md](packaging/README.md).

## Demo / synthetic mode

```bash
safecontext app --demo
```

Opens the app on a synthetic mailbox served by an in-memory fake Gmail, with a local stand-in
for the Grok relay (turning on Use in Grok walks through a pretend approval and two searches).
It needs no Google client or Gmail account, makes no network calls, never opens a browser or
touches the clipboard or Keychain, and uses a temporary data directory unless you pass
`--data-dir`. All values are fake (test card numbers, documented example keys, `example.*` domains).

To see exactly what the scrubber does to that synthetic mailbox:

```bash
.venv/bin/python scripts/scrub_report.py   # writes scrub_report.md (git-ignored)
```

## Use in Grok

Grok's custom connectors call a remote MCP server from xAI's cloud, so they can't reach your Mac
directly. When you turn on **Use in Grok**, the app opens an outbound WebSocket to SafeContext's
relay ([relay/](relay/README.md), a Cloudflare Worker) and gives you a private connector link:

```
https://relay.safecontext.me/g/<256-bit random id>/mcp
```

1. The link is copied and grok.com/connectors opens. Click **New connector → Custom**, paste, add.
2. Grok signs in with OAuth (authorization code + PKCE). Its sign-in page shows a 4-digit code and
   SafeContext asks you to **Approve**; check the numbers match.
3. Done. Grok calls `search`, `read`, `list_items` and `list_sources`, plus `search_email` and
   `read_email_thread` while Gmail is connected. The relay passes each request
   down the socket, the app answers from the scrubbed store, and the answer goes back the same way.

It works only while your Mac is awake and SafeContext is open; otherwise Grok gets "SafeContext isn't
running on your Mac". The card shows live status (Off / Waiting for Grok / Grok connected · last used),
a list of what Grok asked for (time, request, number of results; never the results), an optional
**Keep Mac awake while plugged in** (`caffeinate -s`), and **Regenerate link**, which revokes every
sign-in Grok holds and makes a new link. If Grok's form asks for OAuth details or can't sign in,
**Trouble connecting?** has the Client ID and endpoints, and a link-only mode.

The relay is a pipe, not a copy: it stores no request or response content and logs nothing.
Set `SAFECONTEXT_RELAY_URL` to point the app at another relay (for example `wrangler dev`).

## Local MCP server

`safecontext mcp` serves read-only tools over stdio (newline-delimited JSON-RPC, no extra
dependencies): `search`, `read`, `list_items` and `list_sources`, across every connected source with an
optional `source` filter. Descriptions name the connected sources and how many items each holds.
`search_email` and `read_email_thread` are listed while Gmail is connected. The v1 Gmail tools
`search_mail`, `read_thread` and `list_messages` still work.
Results come only from the scrubbed store and include date, sender, source and a link to the original.

Add it to an MCP client's config, using absolute paths:

```json
{
  "mcpServers": {
    "safecontext": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": ["-m", "safecontext", "mcp"]
    }
  }
}
```

The app's Settings view shows a ready-made config for your install, including the data directory
and Python path; prefer copying that. Run `connect` and `sync` first so there is mail to serve.

## Where data and credentials live

| What | Where |
| --- | --- |
| Scrubbed mail, sync state, sync locks | `~/Library/Application Support/SafeContext/mail.db` (SQLite + FTS5). Override with `--data-dir`. Directory is `0700`, files `0600`, and `secure_delete` is on. |
| Gmail OAuth token | macOS Keychain via `keyring`, service `SafeContext Gmail`, one entry per account. Never a plain file. |
| Google Calendar token | macOS Keychain, service `SafeContext Google Calendar`, one entry per account. |
| Dropbox token | macOS Keychain, service `SafeContext Dropbox`, one entry per account. Never a plain file. |
| Grok link | macOS Keychain, service `SafeContext Grok`: the link id and this Mac's relay secret. Grok's activity list is `grok_activity.json` in the data folder (`0600`). |
| Transcription models | `~/Library/Application Support/SafeContext/models/whisper`. |
| OAuth client file | `src/safecontext/oauth_client.json` or `$SAFECONTEXT_OAUTH_CLIENT`. Not committed. |

`disconnect` removes that source's token and revokes its grant (Google or Dropbox); `disconnect --wipe` also deletes that source's local data.

These are the packaged app's locations. A development checkout uses its own; see below.

## Development and packaged app side by side

Running from the repo (`.venv/bin/safecontext …`, `python -m safecontext …`) and running the
installed `SafeContext.app` never share state, so both can be open at once:

| | Packaged app | Development checkout |
| --- | --- | --- |
| Data folder (mail, sync locks, models) | `…/Application Support/SafeContext` | `…/Application Support/SafeContext-Dev` |
| Keychain services | `SafeContext Gmail`, `SafeContext Google Calendar`, `SafeContext Dropbox`, `SafeContext Grok` | the same with ` (Dev)` appended |
| MCP server name in Settings | `safecontext` | `safecontext-dev` |
| Window title | `SafeContext` | `SafeContext (Dev)` |

Which one is running is decided by where the code lives: inside a `.app` bundle (or started by the
bundle's launcher) is the packaged app; anything else is development. It doesn't depend on an
environment variable, because MCP clients and Finder start SafeContext without your shell's
variables. To force one or the other, set `SAFECONTEXT_ENV=dev` or `SAFECONTEXT_ENV=app`.
`--data-dir` still overrides the data folder in both. A checkout has to connect its sources and turn
on Use in Grok separately from the app (each gets its own Grok link).

Keeping the Keychain items apart also matters because macOS ties each item to the program that
created it: the app reading an item the checkout's Python made (or the other way round) would ask
for your login keychain password.

## Coming from MyContext

This app used to be called MyContext, which only ever ran from a source checkout. So a checkout
carries that setup into its development names on first start; the packaged app leaves MyContext
state alone and starts fresh:

- `~/Library/Application Support/MyContext` is renamed to `…/SafeContext-Dev` (moved, not copied, so
  there is one copy of your mail) and a symlink is left at the old path, so configs that pass
  `--data-dir …/MyContext` keep working. If both folders already hold data, neither is touched and a
  warning is printed; merge them by hand.
- The Gmail token is copied from the `MyContext Gmail` Keychain item to `SafeContext Gmail (Dev)` the
  first time it's needed. The old item stays until you `disconnect`, which removes both.
- The app used to have an Ask view that sent questions to xAI's API with your own xAI key. It's
  gone (use Grok itself, through Use in Grok). On first start the app deletes any saved xAI key: its
  own `SafeContext xAI` item (`SafeContext xAI (Dev)` in a checkout, which also removes
  `MyContext xAI`).
- `MYCONTEXT_OAUTH_CLIENT` and `MYCONTEXT_GMAIL_UNITS_PER_SEC` still work
  if the `SAFECONTEXT_` name isn't set.
- `mycontext` and `python -m mycontext` still work but are deprecated: they print a notice to
  stderr and run `safecontext`. Update MCP client configs to `-m safecontext` (or copy the new
  config from Settings); the old name will be removed in a later version.

## Tests

```bash
.venv/bin/pytest
```

The suite uses fakes for Gmail, Google Calendar, and Dropbox and a local stand-in for the Grok relay, so it
needs no credentials or network. The relay has its own tests (`cd relay && npm test`). It includes
leak tests that check synthetic secrets never reach the store.

## Packaging

Signed, notarized Mac builds use Briefcase: see [packaging/README.md](packaging/README.md).

## License

Proprietary, all rights reserved. See [LICENSE](LICENSE).
