#!/bin/sh
# Check that a built SafeContext.app carries the Google OAuth client, which is gitignored and so
# easy to forget. Without it Connect fails for every user (Gmail/Calendar). Run after
# `briefcase build macOS`; set_bundle_version.sh runs it too.
#   scripts/check_bundle_config.sh [path/to/SafeContext.app]
set -eu
APP="${1:-build/safecontext/macos/app/SafeContext.app}"
PKG="$APP/Contents/Resources/app/safecontext"
[ -d "$PKG" ] || { echo "No app package at $PKG" >&2; exit 1; }
if [ ! -f "$PKG/oauth_client.json" ]; then
  echo "MISSING: oauth_client.json (Google sign-in). Save it in src/safecontext/ and rebuild." >&2; exit 1
fi
echo "Sign-in config bundled: oauth_client.json"
