import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from chatterbox.tls import create_context


class TlsTests(unittest.TestCase):
    def test_real_context_verifies_certificates_and_hostname(self):
        context = create_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_msys_fallback_only_for_empty_store(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "usr" / "ssl" / "cert.pem"
            bundle.parent.mkdir(parents=True)
            bundle.write_text("synthetic-test-bundle")
            context = Mock()
            context.cert_store_stats.side_effect = [{"x509_ca": 0}, {"x509_ca": 2}]
            with patch("chatterbox.tls.sys.prefix", str(Path(directory) / "ucrt64")), patch("chatterbox.tls.ssl.create_default_context", return_value=context), patch.dict(os.environ, {}, clear=True):
                self.assertIs(create_context(), context)
            context.load_verify_locations.assert_called_once_with(cafile=str(bundle))

    def test_default_store_is_not_replaced(self):
        context = Mock()
        context.cert_store_stats.return_value = {"x509_ca": 5}
        with patch("chatterbox.tls.ssl.create_default_context", return_value=context):
            self.assertIs(create_context(), context)
        context.load_verify_locations.assert_not_called()

    def test_explicit_ca_setting_is_not_overridden(self):
        context = Mock()
        context.cert_store_stats.return_value = {"x509_ca": 0}
        with patch("chatterbox.tls.ssl.create_default_context", return_value=context), patch.dict(os.environ, {"SSL_CERT_FILE": "explicit-user-bundle.pem"}):
            with self.assertRaisesRegex(RuntimeError, "no trusted CA"):
                create_context()
        context.load_verify_locations.assert_not_called()


if __name__ == "__main__":
    unittest.main()
