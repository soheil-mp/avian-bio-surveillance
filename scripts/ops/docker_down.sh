#!/usr/bin/env bash
# Docker Compose Down — convenience wrapper with the correct file paths.
# Usage:  bash scripts/ops/docker_down.sh
#         bash scripts/ops/docker_down.sh -v   # also remove volumes
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
bash infrastructure/docker/compose.sh down "$@"

# Surface env-encryption drift so devs notice before pushing.
if [ -f .env ]; then
    poetry run python scripts/encryption/env_crypto.py status || true
fi
