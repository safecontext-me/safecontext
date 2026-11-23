# Building the signed Mac app

The app is packaged with [Briefcase](https://briefcase.readthedocs.io), which bundles
its own Python runtime (use 3.12) so users don't need Python installed.

## You need

1. **Python 3.12** on the build Mac (python.org installer or `uv python install 3.12`).
2. **Apple Developer Program membership** ($99/year) and a
   **"Developer ID Application" certificate** in your login Keychain.
   Without it macOS blocks the app with an "unidentified developer" warning.
3. **The Google OAuth client file.** In Google Cloud Console, create an OAuth client
   of type *Desktop app*, download the JSON, and save it as
   `src/safecontext/oauth_client.json`. It is bundled into the app. Google treats
   Desktop client secrets as non-confidential, so shipping it is expected.
4. `bundle` in `pyproject.toml` is `me.safecontext` (from safecontext.me), so the bundle ID
   is `me.safecontext.safecontext`. Keep it stable once you ship: macOS ties permissions to it.

## Build

```bash
uv python install 3.12            # or the python.org installer
python3.12 -m venv .venv312 && .venv312/bin/pip install briefcase
# pywebview needs proxy_tools, which has no wheel; Briefcase installs macOS deps as wheels only.
.venv312/bin/pip wheel --no-deps -w packaging/wheels proxy_tools
.venv312/bin/briefcase create macOS
.venv312/bin/briefcase build macOS
.venv312/bin/briefcase run macOS            # smoke test
.venv312/bin/briefcase package macOS --identity "Developer ID Application: <Your Name> (<TEAMID>)"
```

`package` signs the app and submits it to Apple for notarization. The first time,
Briefcase asks for an app-specific password for `notarytool`. The result is a
`.dmg` in `dist/`.

The unsigned app built by `build` is at `build/safecontext/macos/app/SafeContext.app`
(ad-hoc signed, runs on this Mac only). After changing dependencies, run
`briefcase update macOS -r` before `build`.

### Apple silicon only

The app is built for arm64 only (`universal_build = false`) and needs macOS 11 or
later: Apple silicon (M1 or later) Macs. Intel Macs are not supported. To check a
build: `lipo -archs build/safecontext/macos/app/SafeContext.app/Contents/MacOS/SafeContext`
should print `arm64`.

## Before you ship

- Run the full test suite under 3.12: `.venv312/bin/pip install -e '.[dev]' && .venv312/bin/pytest`.
- Do one real sync with your own Gmail account, then turn on Use in Grok and run
  one search from grok.com, to confirm the live Gmail API and the relay behave the
  way the fakes do (deploy the relay first: see relay/README.md).
- The MCP config shown in Settings points at the Python that runs the app.
  In the bundled app that's the bundled runtime, so check the path shown there.

# Publishing the website (safecontext.me)

The home page and privacy policy that Google OAuth verification needs live in `site/`.
They are plain static files: no build step. Hosted on Cloudflare Pages.

Before going live, set up the contact address the pages list, `privacy@safecontext.me`:
Cloudflare dashboard → `safecontext.me` → **Email → Email Routing**, enable it (it adds the MX/TXT
records), add a custom address `privacy` that forwards to your own inbox, and confirm the destination.
Then send a test email to privacy@safecontext.me and check that it arrives.

## Deploy

Either connect the repo (redeploys on every push to `main`):

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git**, pick this GitHub repo.
2. Production branch `main`, framework preset **None**, build command **empty**,
   build output directory **`site`**. Save and deploy.

Or upload directly from this folder:

```bash
npx wrangler pages deploy site --project-name safecontext
```

`site/_headers` sets security headers and `site/404.html` is served for unknown paths;
Pages picks both up automatically and does not publish `_headers` itself.
Pages serves `privacy.html` at `/privacy` (it redirects `.html` URLs to the clean form).

## Custom domain

In the Pages project → **Custom domains → Set up a custom domain** → `safecontext.me`.
If the domain's DNS is on Cloudflare this adds the record for you; otherwise add the
CNAME it shows. Optionally add `www.safecontext.me` too.

## Google Search Console and the consent screen

1. [Search Console](https://search.google.com/search-console) → **Add property → Domain** → `safecontext.me`.
2. Copy the `google-site-verification=…` value and add it as a **TXT** record on `safecontext.me`
   (Cloudflare → DNS → Records → Add record, type TXT, name `@`). Click **Verify** (DNS can take a few minutes).
   Use the same Google account that owns the Cloud project.
3. Google Cloud Console → **Google Auth Platform → Branding**: app name **SafeContext**,
   home page `https://safecontext.me/`, privacy policy `https://safecontext.me/privacy`,
   and add `safecontext.me` under **Authorized domains**.
