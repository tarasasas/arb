"""Polymarket US API key request signing (Ed25519 over timestamp + method + path)."""

import base64
import time


def load_signer(key_id, secret_b64):
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as e:
        raise SystemExit("Polymarket API keys need the 'cryptography' package: pip install cryptography") from e
    # The secret shown on polymarket.us/developer is base64; its first 32 bytes are the Ed25519 seed.
    key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_b64)[:32])

    def sign(method, path):
        ts = str(int(time.time() * 1000))
        sig = key.sign(f"{ts}{method}{path}".encode())
        return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(sig).decode()}

    return sign
