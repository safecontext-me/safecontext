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
# pywebview needs proxy_tools, which has no wheel; universal builds accept wheels only.
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

### Intel Macs

`universal_build = true` makes an arm64 + x86_64 app. cryptography 49+ publishes no
Intel Mac wheels, so the macOS `requires` pins `cryptography<49` (48.x is the last
universal2 build). To drop Intel support, remove that pin and set
`universal_build = false`.

## Before you ship

- Run the full test suite under 3.12: `.venv312/bin/pip install -e '.[dev]' && .venv312/bin/pytest`.
- Do one real sync with your own Gmail account, plus one `ask`, to confirm
  the live Gmail and xAI APIs behave the way the fakes do.
- The MCP config shown in Settings points at the Python that runs the app.
  In the bundled app that's the bundled runtime, so check the path shown there.
