#!/usr/bin/env bash
# Install versioned git hooks by pointing core.hooksPath at .githooks/.
#
# Run once after cloning the repo:
#   bash scripts/ops/install_git_hooks.sh
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

HOOKS_DIR=".githooks"
chmod +x "${HOOKS_DIR}"/*
git config core.hooksPath "${HOOKS_DIR}"
echo "✓ core.hooksPath set to ${HOOKS_DIR}"
echo "  Active hooks:"
ls -1 "${HOOKS_DIR}" | sed 's/^/    /'
