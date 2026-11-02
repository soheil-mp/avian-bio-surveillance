# .env Encryption / Decryption

Keep an encrypted backup of your local `.env` and restore it on another
machine (for example a deployment server) with a password.

## How it works

| File            | In Git? | Description                                  |
| --------------- | ------- | -------------------------------------------- |
| `.env`          | **No**  | Plaintext secrets — git-ignored              |
| `.env.enc`      | **No**  | Encrypted backup of `.env` — git-ignored     |
| `password.txt`  | **No**  | Encryption key — git-ignored                 |

**Crypto**: PBKDF2-SHA256 (600 000 iterations) → Fernet (AES-128-CBC + HMAC-SHA256).

## Quick start

### Create the encrypted backup

```bash
# 1. Make sure .env exists at the repo root
# 2. Encrypt it
python scripts/encryption/env_crypto.py encrypt
#    → You'll be prompted for a password (saved to password.txt)
#    → .env.enc is created next to .env (both stay out of Git)
```

### Restore on another machine

```bash
# 1. Copy .env.enc to the repo root on that machine (scp, USB, ...)
# 2. Create password.txt (or just run decrypt — you'll be prompted)
echo "your-password" > scripts/encryption/password.txt

# 3. Decrypt
python scripts/encryption/env_crypto.py decrypt
#    → .env is created from .env.enc
```

## Commands

| Command   | What it does                                           |
| --------- | ------------------------------------------------------ |
| `encrypt` | `.env` → `.env.enc` (creates/overwrites encrypted file)|
| `decrypt` | `.env.enc` → `.env` (creates/overwrites plaintext)     |
| `status`  | Show timestamps and sizes of all three files           |
| `ensure`  | Decrypt only if `.env` is missing (idempotent)         |

### Python

```bash
poetry run python scripts/encryption/env_crypto.py encrypt
poetry run python scripts/encryption/env_crypto.py decrypt
poetry run python scripts/encryption/env_crypto.py status
```

## Docker integration

Wrapper scripts automatically decrypt `.env` before running `docker compose`:

```bash
bash scripts/ops/docker_up.sh
```

These scripts call `ensure` (decrypt only if `.env` is missing) then
the repo-local compose wrapper:

```bash
bash infrastructure/docker/compose.sh up -d
```

The compose wrapper defaults `COMPOSE_BAKE=false` locally because plain
`docker compose build` can hang in this Docker Desktop environment during the
post-build export phase.

## Changing the password

1. Delete `scripts/encryption/password.txt`
2. Re-encrypt: `python scripts/encryption/env_crypto.py encrypt`
3. You'll be prompted for a new password
4. Copy the new `.env.enc` to any machine that needs it

## Security notes

- **Never commit** `.env`, `.env.enc`, or `password.txt` — all are in
  `.gitignore`, and the pre-commit hook refuses to commit env files.
- **Rotate the password** if it may have been exposed.
- The encrypted file uses a random salt per encryption, so re-encrypting the
  same `.env` with the same password produces a different `.env.enc`.
