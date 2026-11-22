# MyContext

A private context layer for AI. MyContext syncs your Gmail to your Mac, scrubs it
locally, and only then lets an AI read it. Gmail is the first source.

It ships as a command-line tool (`mycontext`), a Mac app (pywebview), an MCP server
for other AI clients, and an `ask`/`chat` command that answers questions about your
mail with an xAI Grok model.

## Privacy model

Mail is fetched from Gmail, parsed and scrubbed **in memory**, and only the scrubbed
result is written to a local SQLite database. Unscrubbed mail is never stored or logged.
Everything up to that point runs on your Mac; Gmail is read with the read-only
`gmail.readonly` scope (or `gmail.metadata` with `connect --headers-only`, which
fetches no message bodies).

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

- Gmail API traffic to sync, and Google's OAuth sign-in.
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
.venv/bin/mycontext --help
```

If `python3.12` isn't on your `PATH` (for example, a uv-managed install), use
`uv venv --python 3.12 .venv` instead, then run the `pip install` step above.

The app targets macOS:
tokens and keys are stored in the macOS Keychain and the default data directory is under
`~/Library/Application Support`.

## Google OAuth client

The OAuth client file is git-ignored (`src/mycontext/oauth_client.json`, `oauth_client*.json`,
`client_secret*.json`), so you create your own:

1. In Google Cloud Console, enable the Gmail API for a project and configure the OAuth consent screen.
2. Create an OAuth client of type **Desktop app** and download the JSON (it must contain an `installed` key).
3. Save it as `src/mycontext/oauth_client.json`, or point `MYCONTEXT_OAUTH_CLIENT` at it.

Sign-in uses the loopback redirect with PKCE. For signed Mac builds the file is bundled
into the app; see [packaging/README.md](packaging/README.md).

## CLI

Run as `mycontext …` or `python -m mycontext …`. The global option `--data-dir` goes before the
subcommand and defaults to `~/Library/Application Support/MyContext`.

| Command | What it does |
| --- | --- |
| `connect [--headers-only] [--email ADDR]` | Google sign-in. `--headers-only` uses the metadata scope (no bodies); `--email` pre-fills the account chooser. |
| `sync` | Backfill the mailbox, then incremental updates. Resumable. |
| `status` | Print account, sync mode, auth state, counts and whether a sync is running (JSON). |
| `search QUERY [--from SENDER] [--after DATE] [--before DATE] [--limit N]` | Full-text search of the scrubbed store (default limit 10). |
| `disconnect [--wipe]` | Revoke the Google grant and forget the token; `--wipe` also deletes all local mail. |
| `set-xai-key` | Prompt for an xAI API key and save it to the Keychain. |
| `ask QUESTION [--model ID]` | Ask Grok a question about your mail. |
| `chat [--model ID]` | Interactive version of `ask`. |
| `mcp` | Run the MCP server on stdio. |
| `app [--demo] [--debug]` | Open the Mac app window. |

Running `mycontext` with no arguments opens the app (this is how the packaged `.app` launches).
`ask`/`chat` use model `grok-4.7` by default, overridable with `--model` or `MYCONTEXT_GROK_MODEL`.
The xAI key comes from the `XAI_API_KEY` environment variable or the Keychain.

Typical first run:

```bash
mycontext connect
mycontext sync
mycontext search "dinner" --after 2025-01-01
```

A data folder belongs to one Google account; run `disconnect --wipe` before connecting a different one.
Only one sync runs at a time (the app and CLI share a lock).

## Mac app

```bash
mycontext app            # your real mailbox
mycontext app --debug    # with the web inspector
```

The window lets you connect Gmail, watch sync progress (including syncs started from the CLI),
search and read mail, see a privacy report of what was kept, dropped and redacted, ask Grok,
set the xAI key, and copy the MCP config. It auto-syncs periodically while open.
For a signed, double-clickable build, see [packaging/README.md](packaging/README.md).

## Demo / synthetic mode

```bash
mycontext app --demo
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

`mycontext mcp` serves three read-only tools over stdio (newline-delimited JSON-RPC, no extra
dependencies): `search_mail`, `read_thread` and `list_messages`. Results come only from the scrubbed
store and include date, sender and a Gmail link.

Add it to an MCP client's config, using absolute paths:

```json
{
  "mcpServers": {
    "mycontext": {
      "command": "/path/to/mycontext/.venv/bin/python",
      "args": ["-m", "mycontext", "mcp"]
    }
  }
}
```

The app's Settings view shows a ready-made config for your install, including the data directory
and Python path; prefer copying that. Run `connect` and `sync` first so there is mail to serve.

## Where data and credentials live

| What | Where |
| --- | --- |
| Scrubbed mail, sync state | `~/Library/Application Support/MyContext/mail.db` (SQLite + FTS5). Override with `--data-dir`. Directory is `0700`, files `0600`, and `secure_delete` is on. |
| Gmail OAuth token | macOS Keychain via `keyring`, service `MyContext Gmail`, one entry per account. Never a plain file. |
| xAI API key | macOS Keychain, service `MyContext xAI`, or the `XAI_API_KEY` environment variable. |
| OAuth client file | `src/mycontext/oauth_client.json` or `$MYCONTEXT_OAUTH_CLIENT`. Not committed. |

`disconnect` removes the token and revokes the grant at Google; `disconnect --wipe` also deletes the local mail.

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
