#!/usr/bin/env bash
#
# compose.sh — Docker Compose wrapper for the Avian Biosurveillance stack.
#
# Resolves the repository root, loads the two environment files the stack
# depends on (`.env` for secrets and `config/runtime.env` for runtime
# topology), and forwards all arguments verbatim to `docker compose`
# against the canonical Compose file. Any `docker compose` subcommand and
# flag combination is supported.
#
# GPU is automatic: when the host exposes an NVIDIA GPU AND Docker has the
# `nvidia` runtime (NVIDIA Container Toolkit installed), the GPU override
# `docker-compose.gpu.yml` is layered in transparently; otherwise the stack
# runs on CPU. Set AVIAN_DISABLE_GPU=1 to force CPU on a GPU-capable host.
#
# Usage:
#   bash infrastructure/docker/compose.sh up -d
#   bash infrastructure/docker/compose.sh logs -f ingestion model
#   bash infrastructure/docker/compose.sh down
#
# COMPOSE_BAKE is forced to "false" when unset. Docker Desktop 29.4.x can
# hang in `docker compose build` during the bake export/provenance phase
# for this stack even after the image layers are built. Callers may
# override this by exporting COMPOSE_BAKE before invoking the script.
#
# The wrapper also prefers the native Docker Engine via the `default`
# context. GPU passthrough and the canonical data-root both live there in
# this environment; Docker Desktop on Linux cannot provide the GPU and uses
# a separate volume store. Override with AVIAN_DOCKER_CONTEXT if needed.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

: "${COMPOSE_BAKE:=false}"
export COMPOSE_BAKE

docker_context="${AVIAN_DOCKER_CONTEXT:-default}"
if docker context inspect "$docker_context" >/dev/null 2>&1; then
  if [[ "${DOCKER_CONTEXT:-}" != "$docker_context" || -n "${DOCKER_HOST:-}" ]]; then
    echo "compose.sh: using Docker context '$docker_context'." >&2
  fi
  unset DOCKER_HOST DOCKER_TLS_VERIFY DOCKER_CERT_PATH
  export DOCKER_CONTEXT="$docker_context"
fi

compose_env_files=(
  --env-file .env
  --env-file config/runtime.env
)

if [[ -f config/docker.local.env ]]; then
  compose_env_files+=(--env-file config/docker.local.env)
fi

# ---------------------------------------------------------------------------
# GPU auto-detection
# ---------------------------------------------------------------------------
# Layer in docker-compose.gpu.yml only when this host can actually pass a GPU
# into a container, i.e. an NVIDIA device is present AND the Docker daemon has
# the `nvidia` runtime registered (the NVIDIA Container Toolkit is installed).
# Without both, attaching `gpus: all` would make `docker compose up` fail with
# `could not select device driver "nvidia"`, so we silently stay on CPU.
#
# Force CPU on a capable host with AVIAN_DISABLE_GPU=1.
gpu_compose_files=()
gpu_compose_file="infrastructure/docker/docker-compose.gpu.yml"
if [[ "${AVIAN_DISABLE_GPU:-0}" != "1" && -f "$gpu_compose_file" ]] \
  && command -v nvidia-smi >/dev/null 2>&1 \
  && nvidia-smi -L >/dev/null 2>&1 \
  && docker info --format '{{range $r, $_ := .Runtimes}}{{$r}} {{end}}' 2>/dev/null \
       | grep -qiw nvidia; then
  gpu_compose_files=(-f "$gpu_compose_file")
  echo "compose.sh: NVIDIA GPU detected and usable by Docker -> enabling GPU layer." >&2
fi

exec docker compose \
  "${compose_env_files[@]}" \
  -f infrastructure/docker/docker-compose.yml \
  "${gpu_compose_files[@]}" \
  "$@"
