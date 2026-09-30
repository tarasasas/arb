import base64
import os
import tempfile
import unittest

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
except ImportError:  # signing is optional; skip if the package isn't installed
    serialization = None

from arb.kalshi_auth import load_signer


@unittest.skipIf(serialization is None, "cryptography not installed")
class SigningTests(unittest.TestCase):
    def _write(self, key, fmt):
        pem = key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
        f.write(pem)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_rsa_pss_signature_verifies(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sign = load_signer("key-id", self._write(key, serialization.PrivateFormat.TraditionalOpenSSL))
        h = sign("GET", "/trade-api/v2/markets")
        msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets".encode()
        key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())
        self.assertEqual(h["KALSHI-ACCESS-KEY"], "key-id")

    def test_key_given_as_text_verifies(self):
        # KALSHI_PRIVATE_KEY: the PEM itself, e.g. from a cloud environment's settings.
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        h = load_signer("key-id", key_pem=pem)("GET", "/trade-api/v2/markets")
        msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets".encode()
        key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg)

    def test_ed25519_signature_verifies(self):
        key = ed25519.Ed25519PrivateKey.generate()
        sign = load_signer("key-id", self._write(key, serialization.PrivateFormat.PKCS8))
        h = sign("GET", "/trade-api/v2/markets/orderbooks")
        msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets/orderbooks".encode()
        key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg)


if __name__ == "__main__":
    unittest.main()
