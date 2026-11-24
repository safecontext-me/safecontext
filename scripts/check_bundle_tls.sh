#!/bin/sh
# Check that a built SafeContext.app can make TLS connections with its own bundled Python
# (see src/safecontext/tls.py). Run after `briefcase build macOS` and before shipping:
#   scripts/check_bundle_tls.sh [path/to/SafeContext.app]
# Reads no user data: it imports the bundled package and opens TLS to the relay and Google.
set -eu
APP="${1:-build/safecontext/macos/app/SafeContext.app}"
PY="$APP/Contents/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
RES="$APP/Contents/Resources"
[ -x "$PY" ] || { echo "No bundled Python at $PY" >&2; exit 1; }
env -u SSL_CERT_FILE -u SSL_CERT_DIR PYTHONPATH="$RES/app:$RES/app_packages" "$PY" -I -c '
import sys; sys.path[:0] = sys.argv[1:]
import socket, ssl, safecontext
for host in ("relay.safecontext.me", "oauth2.googleapis.com"):
    with socket.create_connection((host, 443), timeout=15) as s:
        with ssl.create_default_context().wrap_socket(s, server_hostname=host):
            print("TLS OK:", host)
print("SafeContext", safecontext.__version__)
' "$RES/app" "$RES/app_packages"
