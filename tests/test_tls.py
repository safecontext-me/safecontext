"""The bundled app's Python has no CA list; tls.py must give it certifi's."""
import os
import ssl
import subprocess
import sys
from pathlib import Path

import certifi

from safecontext import tls

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_sets_ssl_cert_file_to_certifi(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    tls.use_bundled_ca_certs()
    assert os.environ["SSL_CERT_FILE"] == certifi.where()


def test_keeps_a_users_own_ssl_cert_file(monkeypatch, tmp_path):
    own = tmp_path / "corp.pem"
    own.write_text("")
    monkeypatch.setenv("SSL_CERT_FILE", str(own))
    tls.use_bundled_ca_certs()
    assert os.environ["SSL_CERT_FILE"] == str(own)


def test_importing_the_package_loads_certifi_into_default_contexts():
    """Like the bundled app: a Python whose OpenSSL default CA paths don't exist.
    After `import safecontext`, a default SSL context must still trust public CAs."""
    code = (
        "import ssl, safecontext, certifi, os;"
        "assert os.environ['SSL_CERT_FILE'] == certifi.where();"
        "n = ssl.create_default_context().cert_store_stats()['x509_ca'];"
        "assert n > 50, n; print(n)"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
    env["PYTHONPATH"] = SRC
    # Point OpenSSL's default directory somewhere empty, as in the bundled runtime.
    env["SSL_CERT_DIR"] = "/nonexistent"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
