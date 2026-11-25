#!/bin/sh
# Write the release version into a built SafeContext.app's Info.plist.
# Briefcase fills CFBundleShortVersionString/CFBundleVersion only on `briefcase create`, so a
# bundle that is only `update`d and rebuilt keeps the version it was created with (0.1.0),
# which is what Finder's Get Info shows. Run after `briefcase build macOS` and before
# `briefcase package macOS` (package signs the app, so the edit is sealed by the signature):
#   scripts/set_bundle_version.sh [path/to/SafeContext.app]
set -eu
APP="${1:-build/safecontext/macos/app/SafeContext.app}"
PLIST="$APP/Contents/Info.plist"
[ -f "$PLIST" ] || { echo "No Info.plist at $PLIST" >&2; exit 1; }
# A release without the Google client or the Dropbox app key must not get signed.
"$(dirname "$0")/check_bundle_config.sh" "$APP"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/safecontext/__init__.py)
PYPROJECT=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | sort -u)
[ -n "$VERSION" ] || { echo "No __version__ in src/safecontext/__init__.py" >&2; exit 1; }
[ "$VERSION" = "$PYPROJECT" ] || { echo "Version mismatch: __init__.py $VERSION, pyproject.toml $PYPROJECT" >&2; exit 1; }
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" -c "Set :CFBundleVersion $VERSION" "$PLIST"
echo "Info.plist version: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST")"
