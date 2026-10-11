"""Create development secrets once. Never use these files for a production deployment."""
import os
import secrets
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

def write_once(path, content):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)


def initialize(directory=None):
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent.parent / ".local"
    directory.mkdir(mode=0o700, exist_ok=True)
    write_once(directory / "django.key", secrets.token_urlsafe(64).encode())
    if not (directory / "oidc.key").exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        write_once(directory / "oidc.key", key.private_bytes(serialization.Encoding.PEM,
                   serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


if __name__ == "__main__":
    initialize()
    print("Local development secrets are available in .local/ (not printed or tracked).")
