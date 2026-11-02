#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Docker Compose Up — with automatic .env decryption
# ---------------------------------------------------------------------------
# Ensures .env exists (decrypting from .env.enc if needed), then starts
# the full stack with non-secret runtime topology from config/runtime.env.
#
# Usage:  bash scripts/ops/docker_up.sh            # up -d (all services)
#         bash scripts/ops/docker_up.sh app         # up -d app only
#         bash scripts/ops/docker_up.sh --build     # rebuild then up
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

echo "── Ensuring .env is available ──"
poetry run python scripts/encryption/env_crypto.py ensure

echo ""
echo "── Starting Docker Compose ──"
bash infrastructure/docker/compose.sh up -d "$@"
