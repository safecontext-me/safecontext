#!/bin/bash
# Rebuild the app icon files from safecontext.svg. Needs rsvg-convert (brew install librsvg)
# and macOS's iconutil. Writes:
#   packaging/icons/safecontext.icns   the packaged app's icon (Briefcase: icon = "packaging/icons/safecontext")
#   src/safecontext/app/icon.png        the Dock icon when running from a checkout (app/main.py)
set -euo pipefail
cd "$(dirname "$0")"
SET=$(mktemp -d)/safecontext.iconset
mkdir -p "$SET"
for s in 16 32 128 256 512; do
  rsvg-convert -w $s -h $s safecontext.svg -o "$SET/icon_${s}x${s}.png"
  rsvg-convert -w $((s*2)) -h $((s*2)) safecontext.svg -o "$SET/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$SET" -o safecontext.icns
rsvg-convert -w 512 -h 512 safecontext.svg -o ../../src/safecontext/app/icon.png
rm -rf "$(dirname "$SET")"
echo "wrote safecontext.icns and src/safecontext/app/icon.png"
