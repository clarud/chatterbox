"""Use default trust; handle an MSYS Python build with an empty CA store."""
import os
from pathlib import Path
import ssl
import sys


def create_context():
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] == 0:
        # MSYS UCRT Python here points at an empty UCRT store, but its parent
        # distribution already ships a public CA bundle. Do not override an
        # explicitly configured SSL_CERT_FILE or search the working directory.
        bundle = Path(sys.prefix).parent / "usr" / "ssl" / "cert.pem"
        if not os.environ.get("SSL_CERT_FILE") and bundle.is_file():
            context.load_verify_locations(cafile=str(bundle))
        if context.cert_store_stats()["x509_ca"] == 0:
            raise RuntimeError("Python has no trusted CA certificates. Install a current standard Python, or set SSL_CERT_FILE to your trusted CA bundle. TLS verification will not be disabled.")
    return context
