"""Trusted certificates for the app's own HTTPS and WebSocket connections.

The Python that Briefcase bundles into SafeContext.app has no CA list of its
own: OpenSSL looks for one under /Library/Frameworks/Python.framework, which
exists only on Macs with python.org's Python installed. Without a CA list every
TLS connection made with the standard `ssl` module fails with
CERTIFICATE_VERIFY_FAILED: the Grok relay socket, Google token revocation and
the recordings model download. (Google's API client brings its own CA list, so
Gmail sync worked, which hid the problem.)

`use_bundled_ca_certs` points OpenSSL at certifi's CA list through
SSL_CERT_FILE, which `ssl.create_default_context()` reads on every call. It
runs when the package is imported, before any connection is made.
"""
from __future__ import annotations

import os


def use_bundled_ca_certs() -> None:
    """Use certifi's CA list unless the user set SSL_CERT_FILE themselves."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:  # certifi is a dependency; without it keep OpenSSL's defaults
        return
    path = certifi.where()
    if os.path.isfile(path):
        os.environ["SSL_CERT_FILE"] = path
