"""Kalshi API key request signing (RSA-PSS/SHA-256 or Ed25519, per the key type)."""

import base64
import time


def load_signer(key_id, key_path):
    """Return sign(method, path) -> auth headers. `path` is the full URL path without the
    query string, e.g. /trade-api/v2/markets."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as e:
        raise SystemExit("Kalshi API keys need the 'cryptography' package: pip install cryptography") from e

    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)

    def sign(method, path):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path}".encode()
        if isinstance(key, Ed25519PrivateKey):
            sig = key.sign(msg)
        else:
            sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                           hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    return sign
