#!/usr/bin/env python3
"""Encrypt and decrypt .env files for safe version control.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) with
PBKDF2-derived keys (600 000 iterations, SHA-256).

Encrypted output (.env.enc) is safe to commit to Git.
Plaintext .env and password.txt stay in .gitignore.

Usage:
    python scripts/encryption/env_crypto.py encrypt   # .env  → .env.enc
    python scripts/encryption/env_crypto.py decrypt   # .env.enc → .env
    python scripts/encryption/env_crypto.py status    # show current state

The module can also be imported:
    from scripts.encryption.env_crypto import encrypt, decrypt, ensure_env
"""

from __future__ import annotations

import base64
import getpass
import os
import sys
from collections.abc import Callable
from datetime import UTC
from pathlib import Path


try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:
    print(
        "Error: 'cryptography' package is required.\n"
        "Install with:  pip install cryptography  OR  poetry install",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths — all resolved relative to the repository root
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
ENC_FILE = REPO_ROOT / ".env.enc"
PASSWORD_FILE = Path(__file__).resolve().parent / "password.txt"

# ---------------------------------------------------------------------------
# Crypto parameters
# ---------------------------------------------------------------------------
SALT_SIZE = 16  # bytes
ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-SHA256


# ── Key derivation ────────────────────────────────────────────────────────
def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible key from *password* + *salt* via PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


# ── Password management ──────────────────────────────────────────────────
def _get_password(*, confirm: bool = False) -> str:
    """Read password from file or prompt the user, saving for future use.

    Args:
        confirm: If ``True`` and prompting, ask the user to type the
                 password twice.
    """
    if PASSWORD_FILE.exists():
        pw = PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if pw:
            return pw

    print(f"No password found at {PASSWORD_FILE.relative_to(REPO_ROOT)}")
    pw = getpass.getpass("Enter encryption password: ")
    if not pw:
        print("Error: password cannot be empty.", file=sys.stderr)
        sys.exit(1)

    if confirm:
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Error: passwords do not match.", file=sys.stderr)
            sys.exit(1)

    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(pw + "\n", encoding="utf-8")
    # Restrict permissions (best-effort on Windows)
    try:
        PASSWORD_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"✓ Password saved to {PASSWORD_FILE.relative_to(REPO_ROOT)}")
    return pw


# ── Core operations ───────────────────────────────────────────────────────
def encrypt(*, env_path: Path | None = None, enc_path: Path | None = None) -> Path:
    """Encrypt *.env* → *.env.enc* and return the encrypted file path.

    Args:
        env_path: Plaintext file to encrypt (default ``REPO_ROOT/.env``).
        enc_path: Output path (default ``REPO_ROOT/.env.enc``).

    Returns:
        Path to the encrypted file.

    Raises:
        SystemExit: If the plaintext file is missing or password entry fails.
    """
    env_path = env_path or ENV_FILE
    enc_path = enc_path or ENC_FILE

    if not env_path.exists():
        print(f"Error: {env_path} not found. Nothing to encrypt.", file=sys.stderr)
        sys.exit(1)

    password = _get_password(confirm=not PASSWORD_FILE.exists())
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)

    plaintext = env_path.read_bytes()
    token = Fernet(key).encrypt(plaintext)

    # File format: 16-byte salt ‖ Fernet token
    enc_path.write_bytes(salt + token)

    size_kb = len(salt + token) / 1024
    print(f"✓ Encrypted {env_path.name} → {enc_path.name} ({size_kb:.1f} KB)")
    return enc_path


def decrypt(*, enc_path: Path | None = None, env_path: Path | None = None) -> Path:
    """Decrypt *.env.enc* → *.env* and return the plaintext file path.

    Args:
        enc_path: Encrypted file (default ``REPO_ROOT/.env.enc``).
        env_path: Output path (default ``REPO_ROOT/.env``).

    Returns:
        Path to the decrypted file.

    Raises:
        SystemExit: If the encrypted file is missing or the password is wrong.
    """
    enc_path = enc_path or ENC_FILE
    env_path = env_path or ENV_FILE

    if not enc_path.exists():
        print(f"Error: {enc_path} not found. Nothing to decrypt.", file=sys.stderr)
        sys.exit(1)

    password = _get_password(confirm=False)

    raw = enc_path.read_bytes()
    if len(raw) < SALT_SIZE + 1:
        print("Error: encrypted file is too small / corrupted.", file=sys.stderr)
        sys.exit(1)

    salt, token = raw[:SALT_SIZE], raw[SALT_SIZE:]
    key = _derive_key(password, salt)

    try:
        plaintext = Fernet(key).decrypt(token)
    except InvalidToken:
        print(
            "Error: wrong password or corrupted .env.enc file.\n"
            f"Delete {PASSWORD_FILE.relative_to(REPO_ROOT)} and try again "
            "if the password has changed.",
            file=sys.stderr,
        )
        sys.exit(1)

    env_path.write_bytes(plaintext)
    print(f"✓ Decrypted {enc_path.name} → {env_path.name}")
    return env_path


def status() -> None:
    """Print the current state of .env / .env.enc / password.txt."""

    def _info(p: Path) -> str:
        if not p.exists():
            return "MISSING"
        size = p.stat().st_size
        from datetime import datetime

        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
        return f"{size:,} bytes — modified {mtime:%Y-%m-%d %H:%M UTC}"

    print("╭─ Env Encryption Status ──────────────────────────────────╮")
    print(f"│  .env         {_info(ENV_FILE):>42} │")
    print(f"│  .env.enc     {_info(ENC_FILE):>42} │")
    print(f"│  password.txt {_info(PASSWORD_FILE):>42} │")
    print("╰──────────────────────────────────────────────────────────╯")

    if ENV_FILE.exists() and ENC_FILE.exists():
        env_mt = ENV_FILE.stat().st_mtime
        enc_mt = ENC_FILE.stat().st_mtime
        if env_mt > enc_mt:
            print("⚠  .env is NEWER than .env.enc — run 'encrypt' to sync.")
        elif enc_mt > env_mt:
            print("⚠  .env.enc is NEWER than .env — run 'decrypt' to sync.")
        else:
            print("✓  Files appear in sync.")


def ensure_env() -> None:
    """Ensure .env exists, decrypting from .env.enc if necessary.

    Call this at the start of any workflow that needs environment
    variables (docker compose, dagster, scripts, etc.).
    Does nothing if .env already exists.
    """
    if ENV_FILE.exists():
        return
    if ENC_FILE.exists():
        print(f".env not found — decrypting from {ENC_FILE.name} …")
        decrypt()
    else:
        print(
            "Error: neither .env nor .env.enc found.\n"
            "Create a .env file in the repository root, then run:\n"
            "  python scripts/encryption/env_crypto.py encrypt",
            file=sys.stderr,
        )
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────
def main() -> None:
    """CLI entry point."""
    usage = (
        "Usage: python scripts/encryption/env_crypto.py <command>\n"
        "\n"
        "Commands:\n"
        "  encrypt   Encrypt .env → .env.enc\n"
        "  decrypt   Decrypt .env.enc → .env\n"
        "  status    Show file states\n"
        "  ensure    Decrypt only if .env is missing\n"
    )
    if len(sys.argv) < 2:
        print(usage)
        sys.exit(0)

    cmd = sys.argv[1].lower().strip()
    commands: dict[str, Callable[..., object]] = {
        "encrypt": encrypt,
        "decrypt": decrypt,
        "status": status,
        "ensure": ensure_env,
    }
    if cmd in ("-h", "--help"):
        print(usage)
    elif cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        print(usage, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
