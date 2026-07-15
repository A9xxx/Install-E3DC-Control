#!/usr/bin/env python3
import os
import base64
import sys
import json

try:
    from Installer.config_secret_permissions import apply_config_secret_permissions
except Exception:
    try:
        from config_secret_permissions import apply_config_secret_permissions
    except Exception:
        apply_config_secret_permissions = None

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

if not HAS_CRYPTO and __name__ == "__main__":
    print("Fehler: python3-cryptography ist nicht installiert.")
    print("Bitte ausführen: sudo apt-get install python3-cryptography")
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

    config_path = "/var/www/html/data/e3dc_v4.json"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    elif os.path.exists("/app/data/e3dc_v4.json"):
        config_path = "/app/data/e3dc_v4.json"

    if os.path.exists(config_path):
        if config_path.endswith(".json"):
            with open(config_path, "r", encoding="utf-8") as f:
                content = json.load(f)
            if "push_vapid_public" not in content:
                content["push_vapid_public"] = keys["public"]
                content["push_vapid_private"] = keys["private"]
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                print(f"Erfolgreich in {config_path} gespeichert!")
            else:
                print("VAPID Keys existieren bereits in der V4-Konfiguration.")
        else:
            with open(config_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "push_vapid_public" not in content:
                with open(config_path, "a", encoding="utf-8") as f:
                    f.write("\n# Web-Push VAPID Keys\n")
                    f.write(f"push_vapid_public = {keys['public']}\n")
                    f.write(f"push_vapid_private = {keys['private']}\n")
                print(f"Erfolgreich an {config_path} angehaengt!")
            else:
                print("VAPID Keys existieren bereits in der Legacy-Konfiguration.")

        try:
            if os.path.basename(config_path) == "e3dc_v4.json" and apply_config_secret_permissions is not None:
                apply_config_secret_permissions(config_path, data=content if isinstance(content, dict) else None)
            else:
                os.chmod(config_path, 0o664)
        except Exception as e:
            print(f"Warning setting permissions: {e}")
    else:
        print(f"Hinweis: {config_path} nicht gefunden. Bitte zuerst die V4-Konfiguration anlegen und den Vorgang wiederholen.")
