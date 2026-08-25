#!/usr/bin/env python3
import base64
import sys
import json

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

if not HAS_CRYPTO and __name__ == "__main__":
    print(json.dumps({
        "success": False,
        "error": "python3-cryptography ist nicht installiert.",
    }, ensure_ascii=False))
    sys.exit(1)

def base64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')

def generate_vapid_keys():
    if not HAS_CRYPTO:
        print("Fehler: python3-cryptography ist nicht installiert.")
        sys.exit(1)
    # Private Key (prime256v1 / SECP256R1)
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_numbers = private_key.private_numbers()

    # Rohe Bytes des Private Key (32 bytes)
    priv_bytes = private_numbers.private_value.to_bytes(32, byteorder='big')

    # Public Key
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()

    # Rohe Bytes des Public Key (Uncompressed format: 0x04 + X + Y)
    pub_bytes = b'\x04' + \
                public_numbers.x.to_bytes(32, byteorder='big') + \
                public_numbers.y.to_bytes(32, byteorder='big')

    return {
        "private": base64url_encode(priv_bytes),
        "public": base64url_encode(pub_bytes)
    }
if __name__ == "__main__":
    keys = generate_vapid_keys()
    # Dieser Helfer schreibt bewusst keine Konfiguration. Nur der gebundene
    # PHP-Transaktionspfad darf beide Schlüssel gemeinsam veröffentlichen.
    print(json.dumps({
        "success": True,
        "public": keys["public"],
        "private": keys["private"],
    }, ensure_ascii=False, separators=(",", ":")))
