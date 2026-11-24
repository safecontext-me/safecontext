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
scripts/check_bundle_tls.sh                 # the bundled Python can verify TLS (relay, Google)
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

## Cloudflare account and DNS

The site, the download and the relay all run on Cloudflare, so the `safecontext.me`
zone must use Cloudflare's nameservers (Pages can only serve an apex domain from a
Cloudflare zone). Add the site in the Cloudflare dashboard (Free plan), then at Porkbun
replace the nameservers with the two Cloudflare shows. Before switching, check that any
records you need (for example Porkbun email forwarding) were copied over; Cloudflare
Email Routing above replaces Porkbun's forwarding.

## Download (R2)

The home page's **Download for Mac** button links to `/download`, which
`functions/download.js` serves by streaming `SafeContext.dmg` from the R2 bucket
`safecontext-downloads` (bound as `DOWNLOADS` in the root `wrangler.toml`). The DMG is
too big for a Pages static file (25 MB limit), and the bucket is not public: the only way
in is through the function. Create the bucket once (R2 must be enabled on the account):

```bash
npx wrangler r2 bucket create safecontext-downloads
```

For each release, upload under the same key so the URL never changes, then update the
version and size shown on `site/index.html`:

```bash
npx wrangler r2 object put safecontext-downloads/SafeContext.dmg --remote --file dist/SafeContext-X.Y.Z.dmg --content-type application/x-apple-diskimage
```

## Deploy

Run from the repo root. `wrangler.toml` points Pages at `site/` for static files and
picks up `functions/` and the R2 binding:

```bash
npx wrangler pages deploy
```

The first run asks to create the `safecontext` project; answer with production branch `main`.

After that, `.github/workflows/deploy-site.yml` runs the same deploy on every push to `main`
that changes `site/`, `functions/` or `wrangler.toml`. It needs two repository secrets:
`CLOUDFLARE_API_TOKEN` (an API token for this account with **Cloudflare Pages: Edit** and
**Workers R2 Storage: Read**) and `CLOUDFLARE_ACCOUNT_ID`. Until both are set, the workflow
skips the deploy with a notice instead of failing. `relay/` is never deployed by CI.
(Connecting the repo with **Connect to Git** also works, with build output directory
`site` and no build command; Pages reads the same `wrangler.toml`.)

`site/_headers` sets security headers and `site/404.html` is served for unknown paths;
Pages picks both up automatically and does not publish `_headers` itself.
Pages serves `privacy.html` at `/privacy` (it redirects `.html` URLs to the clean form).

## Custom domain

In the Pages project → **Custom domains → Set up a custom domain** → `safecontext.me`
(the zone is on Cloudflare, so it adds the record itself). Optionally add `www.safecontext.me` too.

Check: `curl -sI https://safecontext.me/download` should show `200`,
`content-type: application/x-apple-diskimage` and the DMG's size as `content-length`.

## Google Search Console and the consent screen

1. [Search Console](https://search.google.com/search-console) → **Add property → Domain** → `safecontext.me`.
2. Copy the `google-site-verification=…` value and add it as a **TXT** record on `safecontext.me`
   (Cloudflare → DNS → Records → Add record, type TXT, name `@`). Click **Verify** (DNS can take a few minutes).
   Use the same Google account that owns the Cloud project.
3. Google Cloud Console → **Google Auth Platform → Branding**: app name **SafeContext**,
   home page `https://safecontext.me/`, privacy policy `https://safecontext.me/privacy`,
   and add `safecontext.me` under **Authorized domains**.
