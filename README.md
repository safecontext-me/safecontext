# SafeContext

A private context layer for AI. SafeContext syncs your Gmail and Google Calendar to your
Mac, scrubs them locally, and only then lets an AI read them. iMessage, WhatsApp and call
recordings are next.

It ships as a command-line tool (`safecontext`), a Mac app (pywebview), an MCP server
for other AI clients, and an `ask`/`chat` command that answers questions about your
mail with an xAI Grok model.

## Privacy model

Mail is fetched from Gmail, parsed and scrubbed **in memory**, and only the scrubbed
result is written to a local SQLite database. Unscrubbed mail is never stored or logged.
Everything up to that point runs on your Mac; Gmail is read with the read-only
`gmail.readonly` scope (or `gmail.metadata` with `connect --headers-only`, which
fetches no message bodies). Google Calendar, if you connect it, is read with the read-only
`calendar.readonly` scope, requested separately only when you connect Calendar. Calendar
events go through the same scrubber (dial-in PINs and meeting passcodes are never read);
email-only rules such as `List-Unsubscribe` don't apply to them.

**Dropped entirely** (only the message id and a reason are kept, so sync doesn't refetch it):

- Spam, trash, drafts, chats, and Promotions/Social/Forums categories
- Bulk mail: `List-Unsubscribe` or `Precedence: bulk/junk` headers, and no-reply senders
- Security mail: verification/login codes, password resets, sign-in alerts, 2FA, magic links
- Sensitive senders: a built-in list of banks, brokerages, payment apps, tax, health,
  insurance and pharmacy domains, plus domains containing words like `bank`, `clinic`,
  `insurance`, `mortgage`
- Sensitive subjects: statements, tax forms (W-2, 1099…), direct deposit, pay stubs,
  lab results, prescriptions, explanation of benefits, appointment reminders, and similar

**Redacted inline** as `[REDACTED:TYPE]` in subjects and bodies: credit cards, IBANs,
SSNs, bank account and routing numbers, CVVs, dates of birth, phone numbers, street
addresses, API keys, JWTs, private keys, passwords, one-time codes, TOTP secrets and
backup codes. Quoted replies and signatures are stripped. Attachments are stored as
filename, type and size only; there is no field that could hold their contents.

The scrubber leans toward dropping: a wrongly dropped newsletter costs little, a wrongly
kept bank statement is the failure this project exists to prevent. It is pattern-based,
not a guarantee, so review the output (see the demo section) before trusting it with real mail.

**What leaves the Mac:**

- Gmail and Google Calendar API traffic to sync, and Google's OAuth sign-in.
- With `ask`/`chat` or the app's Ask view: your question, the conversation so far, and the
  results of the search/read tools Grok calls (scrubbed text only), sent to xAI's API with
  `store=False`.
- The MCP server sends nothing itself; the client you connect decides what it does with the results.

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
| `connect [--source gmail\|gcal] [--headers-only] [--email ADDR]` | Google sign-in for Gmail (default) or Google Calendar. `--headers-only` uses Gmail's metadata scope (no bodies); `--email` pre-fills the account chooser. |
| `sync [--source gmail\|gcal]` | Sync every connected source (or one): backfill, then incremental updates. Resumable. |
| `sources` | List sources, their status, item counts and the access each needs. |
| `status` | Print account, sync mode, auth state, counts and whether a sync is running (JSON). |
| `search QUERY [--source ID] [--from SENDER] [--after DATE] [--before DATE] [--limit N]` | Full-text search of the scrubbed store (default limit 10). |
| `disconnect [--source gmail\|gcal] [--wipe]` | Forget the source's token (revoking the Google grant once no other Google source uses it); `--wipe` also deletes that source's local data. |
| `set-xai-key` | Prompt for an xAI API key and save it to the Keychain. |
| `ask QUESTION [--model ID]` | Ask Grok a question about your mail. |
| `chat [--model ID]` | Interactive version of `ask`. |
| `mcp` | Run the MCP server on stdio. |
| `app [--demo] [--debug]` | Open the Mac app window. |

Running `safecontext` with no arguments opens the app (this is how the packaged `.app` launches).
`ask`/`chat` use model `grok-4.7` by default, overridable with `--model` or `SAFECONTEXT_GROK_MODEL`.
The xAI key comes from the `XAI_API_KEY` environment variable or the Keychain.

Typical first run:

```bash
safecontext connect
safecontext sync
safecontext search "dinner" --after 2025-01-01
```

A data folder belongs to one Google account; run `disconnect --wipe` before connecting a different one.
Only one sync runs at a time (the app and CLI share a lock).

## Mac app

```bash
safecontext app            # your real mailbox
safecontext app --debug    # with the web inspector
```

The window lets you connect Gmail, watch sync progress (including syncs started from the CLI),
search and read mail, see a privacy report of what was kept, dropped and redacted, ask Grok,
set the xAI key, and copy the MCP config. It auto-syncs periodically while open.
For a signed, double-clickable build, see [packaging/README.md](packaging/README.md).

## Demo / synthetic mode

```bash
safecontext app --demo
```

Opens the app on a synthetic mailbox served by an in-memory fake Gmail, with a local stand-in
for Grok. It needs no Google client, Gmail account or xAI key, makes no network calls, and uses
a temporary data directory unless you pass `--data-dir`. All values are fake (test card numbers,
documented example keys, `example.*` domains). Demo "Ask" answers are generated locally, not by Grok.

To see exactly what the scrubber does to that synthetic mailbox:

```bash
.venv/bin/python scripts/scrub_report.py   # writes scrub_report.md (git-ignored)
```

## MCP server

`safecontext mcp` serves read-only tools over stdio (newline-delimited JSON-RPC, no extra
dependencies): `search`, `read`, `list_items` and `list_sources`, across every connected source with an
optional `source` filter. The v1 Gmail tools `search_mail`, `read_thread` and `list_messages` still work.
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
| xAI API key | macOS Keychain, service `SafeContext xAI`, or the `XAI_API_KEY` environment variable. |
| Transcription models | `~/Library/Application Support/SafeContext/models/whisper`. |
| OAuth client file | `src/safecontext/oauth_client.json` or `$SAFECONTEXT_OAUTH_CLIENT`. Not committed. |

`disconnect` removes the token and revokes the grant at Google; `disconnect --wipe` also deletes the local mail.

These are the packaged app's locations. A development checkout uses its own; see below.

## Development and packaged app side by side

Running from the repo (`.venv/bin/safecontext …`, `python -m safecontext …`) and running the
installed `SafeContext.app` never share state, so both can be open at once:

| | Packaged app | Development checkout |
| --- | --- | --- |
| Data folder (mail, sync locks, models) | `…/Application Support/SafeContext` | `…/Application Support/SafeContext-Dev` |
| Keychain services | `SafeContext Gmail`, `SafeContext Google Calendar`, `SafeContext xAI` | the same with ` (Dev)` appended |
| MCP server name in Settings | `safecontext` | `safecontext-dev` |
| Window title | `SafeContext` | `SafeContext (Dev)` |

Which one is running is decided by where the code lives: inside a `.app` bundle (or started by the
bundle's launcher) is the packaged app; anything else is development. It doesn't depend on an
environment variable, because MCP clients and Finder start SafeContext without your shell's
variables. To force one or the other, set `SAFECONTEXT_ENV=dev` or `SAFECONTEXT_ENV=app`.
`--data-dir` still overrides the data folder in both. A checkout has to connect its sources and save
an xAI key separately from the app.

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
- The Gmail token and xAI key are copied from the `MyContext Gmail` / `MyContext xAI` Keychain items
  to `SafeContext Gmail (Dev)` / `SafeContext xAI (Dev)` the first time they're needed. The old items stay until you `disconnect` or clear the key, which
  removes both.
- `MYCONTEXT_OAUTH_CLIENT`, `MYCONTEXT_GROK_MODEL` and `MYCONTEXT_GMAIL_UNITS_PER_SEC` still work
  if the `SAFECONTEXT_` name isn't set.
- `mycontext` and `python -m mycontext` still work but are deprecated: they print a notice to
  stderr and run `safecontext`. Update MCP client configs to `-m safecontext` (or copy the new
  config from Settings); the old name will be removed in a later version.

## Tests

```bash
.venv/bin/pytest
```

The suite uses fakes for Gmail and xAI, so it needs no credentials or network. It includes
leak tests that check synthetic secrets never reach the store.

## Packaging

Signed, notarized Mac builds use Briefcase: see [packaging/README.md](packaging/README.md).

## License

Proprietary, all rights reserved. See [LICENSE](LICENSE).
