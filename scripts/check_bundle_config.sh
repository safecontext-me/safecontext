#!/bin/sh
# Check that a built SafeContext.app carries the sign-in config files that are gitignored and
# so easy to forget: the Google OAuth client and the Dropbox app key. Without them Connect
# fails for every user (Gmail/Calendar), or the Dropbox card says "Not available in this
# build". Run after `briefcase build macOS`; set_bundle_version.sh runs it too.
#   scripts/check_bundle_config.sh [path/to/SafeContext.app]
set -eu
APP="${1:-build/safecontext/macos/app/SafeContext.app}"
PKG="$APP/Contents/Resources/app/safecontext"
[ -d "$PKG" ] || { echo "No app package at $PKG" >&2; exit 1; }
fail=0
if [ ! -f "$PKG/oauth_client.json" ]; then
  echo "MISSING: oauth_client.json (Google sign-in). Save it in src/safecontext/ and rebuild." >&2; fail=1
fi
if [ ! -f "$PKG/dropbox_app.json" ]; then
  echo "MISSING: dropbox_app.json (Dropbox app key). Save it in src/safecontext/ and rebuild." >&2; fail=1
elif ! /usr/bin/python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert str(d.get("app_key","")).strip(); assert not any("secret" in k.lower() for k in d)' "$PKG/dropbox_app.json" 2>/dev/null; then
  echo "BAD: dropbox_app.json must be {\"app_key\": \"...\"} with no secret in it." >&2; fail=1
fi
[ "$fail" = 0 ] || exit 1
echo "Sign-in config bundled: oauth_client.json, dropbox_app.json"
