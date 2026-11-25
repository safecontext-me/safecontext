# SafeContext: working rules for Claude sessions

SafeContext is a local-first Mac app (Apple silicon only). It syncs the user's mail and calendar, scrubs them on the Mac, and serves the result to AI (MCP, and "Use in Grok" through a relay). Code lives in `src/safecontext/`, the website in `site/` + `functions/`, the relay in `relay/`, and packaging notes in `packaging/`.

## Who owns what

There are three coordinator sessions. Each one works only in its own area and hands everything else to the owning coordinator.

| Coordinator | Area | Outside accounts it owns |
|---|---|---|
| **Project & ops** | Cross-cutting work, launch plan, the Grok relay (`relay/`), CI (`.github/`) | Cloudflare DNS, the relay Worker and Email Routing; GitHub settings and secrets; Google Cloud (OAuth consent screen, verification) |
| **Marketing site** | `site/`, `functions/`, the root `wrangler.toml` | Cloudflare Pages project `safecontext` |
| **Desktop client** | The app, CLI and MCP server under `src/safecontext/`, plus `packaging/` | Apple signing and notarization; the release DMG in R2 (`safecontext-downloads/SafeContext.dmg`) |

- Only the owner walks the user through an account's setup. Anyone else who needs that account asks the owner.
- Only one session at a time gives the user manual steps. If two need the user, say so and let the user pick.

## How work lands

- Worker sessions do one task each in their own worktree and commit only on their branch.
- The area's coordinator reviews the work and merges it into `main`.
- Push to GitHub, deploy, or create or change outside resources only when the user says so in that session.
- Archive a session once its branch is merged and pushed.
- Human contributors (James, Pouyan) merge their own pull requests straight into `main` for now, with no coordinator review. Before starting work, and again before pushing, fetch and merge `origin/main`, then run the tests. If their changes break something or touch your area in a way that matters (privacy wording, a release), tell the owning coordinator.

## Manual steps for the user

- One step per message, with at most one command block.
- Interactive commands run in the user's terminal, not in the chat box.
- Never ask the user to paste passwords, tokens or keys into chat, and never write them to files.

## Privacy while developing

- Use test fixtures, demo data (`scripts/dev_preview.py`) or a temp `--data-dir` only.
- Never read the user's real SafeContext data folder or Keychain entries.
- Never read real Messages, Contacts, WhatsApp, Photos, Voice Memos or Documents/Desktop/Downloads contents.
- No whole-disk searches (`find /`), and nothing that triggers a macOS privacy prompt. If something would, stop and ask.

## Build facts

- Apple silicon only; don't add Intel or universal builds.
- Python 3.12 venv at `.venv`; run tests with `.venv/bin/pytest`.
- Node is in `~/.local/bin`; run wrangler as `PATH=~/.local/bin:$PATH npx wrangler …`.
