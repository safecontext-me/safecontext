__version__ = "0.1.7"

from .tls import use_bundled_ca_certs as _use_bundled_ca_certs

_use_bundled_ca_certs()  # before any HTTPS or WebSocket connection; see tls.py
