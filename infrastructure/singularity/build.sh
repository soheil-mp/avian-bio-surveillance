#!/usr/bin/env bash
# =============================================================================
# build.sh — Convert Docker images → Singularity/Apptainer SIF files
# =============================================================================
# Workflow: publish custom OCI images once, then convert them to SIF for an
# on-prem server where Docker is unavailable (rootless Singularity/Apptainer).
#
# Preferred path:
#   Step 1 (laptop/CI): publish custom images to Docker Hub / OCI registry
#   Step 2 (server):    build.sh --from-registry  →  sif/*.sif
#   Step 3 (server):    bash start.sh etl
#
# Fallback path (offline / no registry access):
#   Step 1 (laptop):  docker compose build  →  build.sh --from-local  →  sif/*.sif
#   Step 2 (laptop):  build.sh --deploy     →  scp SIF files + scripts to server
#   Step 3 (server):  bash start.sh etl
#
# Usage:
#   cd <repo_root>
#   bash infrastructure/singularity/build.sh --from-registry
#   bash infrastructure/singularity/build.sh --from-registry --only orchestration dagster
#   bash infrastructure/singularity/build.sh --from-local
#   bash infrastructure/singularity/build.sh --deploy
#   bash infrastructure/singularity/build.sh --sync-env
#   bash infrastructure/singularity/build.sh --env-file /path/to/local.env --sync-env --remote-env-file /path/to/server.env
#   bash infrastructure/singularity/build.sh --push
#   bash infrastructure/singularity/build.sh --tag sha-abc123 --namespace soheilmp --from-registry
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SIF_DIR="${SCRIPT_DIR}/sif"
BUILD_CACHE_DIR="${SINGULARITY_CACHEDIR:-${APPTAINER_CACHEDIR:-${SCRIPT_DIR}/.cache}}"
BUILD_TMP_DIR="${SINGULARITY_TMPDIR:-${APPTAINER_TMPDIR:-${TMPDIR:-${SCRIPT_DIR}/.tmp}}}"

# ---------------------------------------------------------------------------
# Detect Apptainer / Singularity
# ---------------------------------------------------------------------------
if command -v apptainer &>/dev/null; then
    RUNTIME="apptainer"
elif command -v singularity &>/dev/null; then
    RUNTIME="singularity"
else
    echo "ERROR: Neither apptainer nor singularity found in PATH." >&2
    exit 1
fi
echo "Runtime: ${RUNTIME}"
mkdir -p "${SIF_DIR}"
mkdir -p "${BUILD_CACHE_DIR}" "${BUILD_TMP_DIR}"

# Keep Singularity / Apptainer build scratch + cache inside the project tree by
# default so large OCI → SIF conversions do not exhaust the system temp area on
# the on-prem server. Respect caller-provided overrides if they were already
# set.
export TMPDIR="${BUILD_TMP_DIR}"
export APPTAINER_CACHEDIR="${BUILD_CACHE_DIR}"
export APPTAINER_TMPDIR="${BUILD_TMP_DIR}"
export SINGULARITY_CACHEDIR="${BUILD_CACHE_DIR}"
export SINGULARITY_TMPDIR="${BUILD_TMP_DIR}"

echo "Build cache dir: ${BUILD_CACHE_DIR}"
echo "Build tmp dir:   ${BUILD_TMP_DIR}"

SOURCE_MODE="local"
REGISTRY_NAMESPACE="${DOCKERHUB_NAMESPACE:-}"
IMAGE_TAG="${SINGULARITY_IMAGE_TAG:-}"
ENV_FILE_OVERRIDE=""
REMOTE_ENV_FILE_OVERRIDE=""
ENV_FILE=""

# ---------------------------------------------------------------------------
# Image map — image reference → SIF filename
# ---------------------------------------------------------------------------
# Extract local Docker image tags from docker-compose.yml to avoid hardcoded
# version drift. Falls back to a provided default if parsing fails (e.g. the
# compose file changes structure unexpectedly).
COMPOSE_FILE="${REPO_ROOT}/infrastructure/docker/docker-compose.yml"

_image_for() {
    # Usage: _image_for <service-name> <fallback-tag>
    local svc="$1" fallback="$2"
    local tag
    tag=$(awk '/^[[:space:]]+'"${svc}"':/{found=1} found && /^[[:space:]]+image:/{print $2; exit}' "${COMPOSE_FILE}" 2>/dev/null | tr -d '\r')
    # For commented-out services, try matching with #
    if [ -z "${tag}" ]; then
        tag=$(awk '/^[[:space:]]*#?[[:space:]]*'"${svc}"':/{found=1} found && /^[[:space:]]*#?[[:space:]]*image:/{gsub(/^[ #]+/,"",$0); print $2; exit}' "${COMPOSE_FILE}" 2>/dev/null | tr -d '\r')
    fi
    if [ -z "${tag}" ]; then
        echo "  WARN: Could not extract image for '${svc}' from compose file, using fallback: ${fallback}" >&2
        echo "${fallback}"
    else
        echo "${tag}"
    fi
}

# Custom images (built from our Dockerfiles)
# ---------------------------------------------------------------------------
# Image manifest — single source of truth.
# ---------------------------------------------------------------------------
# Each record: "<sif-name>|<compose-service>|<local-fallback-image>|<registry-repo>|<kind>"
#   sif-name           — basename of the produced .sif (and the --only filter token)
#   compose-service    — service key in docker-compose.yml; image: tag is read from here
#   local-fallback     — image:tag used if the compose lookup returns nothing
#   registry-repo      — Docker Hub repo name (custom only). Empty for upstream.
#   kind               — "custom" (built from our Dockerfile) or "upstream" (pulled as-is)
#
# Add a new service by adding ONE line. The conversion order follows array order:
# upstream first (DBs), then custom (so a Dagster/orchestration rebuild does not
# block the DB SIFs that rarely change).
IMAGE_SPECS=(
    "timescaledb|timescaledb|timescale/timescaledb:2.17.2-pg16||upstream"
    "minio|minio|minio/minio:RELEASE.2025-09-07T16-13-09Z||upstream"
    "dagster|dagster-webserver|avian-biosurveillance/dagster:1.9|avian-biosurveillance-dagster|custom"
    "orchestration|ingestion|avian-biosurveillance/ingestion:0.1.0|avian-biosurveillance-ingestion|custom"
    "frontend|frontend|avian-biosurveillance/frontend:0.1.0|avian-biosurveillance-frontend|custom"
)

# Default rootless ETL stack: TimescaleDB + MinIO + Dagster + orchestration.
DEFAULT_STACK=(timescaledb minio dagster orchestration)

# Field accessors. Usage: _spec_field <sif-name> <field>
#   field ∈ service | local | repo | kind | image
# "image" returns the resolved local image tag (compose lookup, falling back to local).
_spec_field() {
    local needle="$1" field="$2" rec name service local_fb repo kind
    for rec in "${IMAGE_SPECS[@]}"; do
        IFS='|' read -r name service local_fb repo kind <<<"${rec}"
        [ "${name}" = "${needle}" ] || continue
        case "${field}" in
            service) printf '%s' "${service}" ;;
            local)   printf '%s' "${local_fb}" ;;
            repo)    printf '%s' "${repo}" ;;
            kind)    printf '%s' "${kind}" ;;
            image)   _image_for "${service}" "${local_fb}" ;;
            *) echo "ERROR: unknown spec field '${field}'" >&2; return 1 ;;
        esac
        return 0
    done
    echo "ERROR: no image spec named '${needle}'" >&2
    return 1
}

# All sif-names in declared order, optionally filtered by kind.
_spec_names() {
    local want_kind="${1:-}" rec name _service _local _repo kind
    for rec in "${IMAGE_SPECS[@]}"; do
        IFS='|' read -r name _service _local _repo kind <<<"${rec}"
        if [ -z "${want_kind}" ] || [ "${kind}" = "${want_kind}" ]; then
            printf '%s\n' "${name}"
        fi
    done
}

is_selected() {
    local needle="$1"
    shift || true
    local selected
    for selected in "$@"; do
        if [ "${selected}" = "${needle}" ]; then
            return 0
        fi
    done
    return 1
}

registry_image_for() {
    local name="$1"
    local repo
    repo=$(_spec_field "${name}" repo) || return 1

    if [ -z "${repo}" ]; then
        echo "ERROR: '${name}' is an upstream image and has no registry repository." >&2
        return 1
    fi
    if [ -z "${REGISTRY_NAMESPACE}" ]; then
        echo "ERROR: --namespace or DOCKERHUB_NAMESPACE is required for --from-registry." >&2
        return 1
    fi
    if [ -z "${IMAGE_TAG}" ]; then
        echo "ERROR: --tag or SINGULARITY_IMAGE_TAG is required for --from-registry." >&2
        echo "       Use an immutable tag (for example sha-<gitsha> or a dated release tag)." >&2
        return 1
    fi

    echo "${REGISTRY_NAMESPACE}/${repo}:${IMAGE_TAG}"
}

# ---------------------------------------------------------------------------
# Build selected local Docker images first (idempotent — skips cached layers)
# ---------------------------------------------------------------------------
build_docker() {
    local filter=("$@")
    local has_filter=false
    local build_targets=()
    local name service

    if [ "$#" -gt 0 ]; then
        has_filter=true
    fi

    while IFS= read -r name; do
        if $has_filter && ! is_selected "${name}" "${filter[@]}"; then
            continue
        fi
        service=$(_spec_field "${name}" service)
        if [ -n "${service}" ]; then
            build_targets+=("${service}")
        fi
    done < <(_spec_names custom)

    if [ -z "${build_targets[0]+x}" ]; then
        echo "=== No local custom Docker images selected for build ==="
        return 0
    fi

    echo "=== Ensuring local Docker images are built ==="
    docker compose --env-file "${ENV_FILE}" \
        -f "${REPO_ROOT}/infrastructure/docker/docker-compose.yml" \
        build "${build_targets[@]}"
    echo ""
}

# ---------------------------------------------------------------------------
# Convert a single image to SIF
# ---------------------------------------------------------------------------
convert_one() {
    local name="$1"
    local source="$2"   # docker-daemon://… or docker://…
    echo "[build] ${name}.sif  ←  ${source}"
    ${RUNTIME} build --force "${SIF_DIR}/${name}.sif" "${source}"
}

# ---------------------------------------------------------------------------
# Convert all images
# ---------------------------------------------------------------------------
convert_all() {
    local mode="$1"
    shift
    local filter=("$@")
    local has_filter=false
    local name kind source

    if [ "$#" -gt 0 ]; then
        has_filter=true
    fi

    while IFS= read -r name; do
        if $has_filter && ! is_selected "${name}" "${filter[@]}"; then
            continue
        fi
        kind=$(_spec_field "${name}" kind)
        if [ "${kind}" = "upstream" ]; then
            # Upstream images always come from the registry tag declared in
            # IMAGE_SPECS — independent of --from-local / --from-registry.
            source="docker://$(_spec_field "${name}" image)"
        elif [ "${mode}" = "registry" ]; then
            source="docker://$(registry_image_for "${name}")"
        else
            source="docker-daemon://$(_spec_field "${name}" image)"
        fi
        convert_one "${name}" "${source}"
    done < <(_spec_names)

    echo ""
    echo "SIF images in ${SIF_DIR}/:"
    ls -lh "${SIF_DIR}"/*.sif 2>/dev/null || echo "  (none)"
}

# ---------------------------------------------------------------------------
# Push to Sylabs Cloud Library (optional)
# ---------------------------------------------------------------------------
push_to_library() {
    echo "=== Pushing SIF images to Sylabs Cloud Library ==="
    if [ -z "${SYLAB_TOKEN:-}" ]; then
        echo "ERROR: SYLAB_TOKEN not set. Export it or add to .env" >&2
        exit 1
    fi
    ${RUNTIME} remote login --tokenfile <(echo "${SYLAB_TOKEN}")

    for sif in "${SIF_DIR}"/*.sif; do
        local name
        name=$(basename "${sif}" .sif)
        echo "  Pushing ${name}..."
        ${RUNTIME} push "${sif}" "library://avian-biosurveillance/${name}:latest"
    done
    echo "All images pushed."
}

# ---------------------------------------------------------------------------
# Sync .env to server via scp (requires sshpass)
# ---------------------------------------------------------------------------
# Try to decrypt .env.enc → .env using the best available Python runtime.
_try_auto_decrypt() {
    local enc_file="${REPO_ROOT}/.env.enc"
    local crypto_script="${REPO_ROOT}/scripts/encryption/env_crypto.py"
    if [ ! -f "${enc_file}" ] || [ ! -f "${crypto_script}" ]; then
        return 1
    fi
    echo "  .env not found — attempting auto-decrypt from .env.enc …"
    if command -v poetry &>/dev/null; then
        poetry run python "${crypto_script}" ensure
    elif command -v python3 &>/dev/null; then
        python3 "${crypto_script}" ensure
    elif command -v python &>/dev/null; then
        python "${crypto_script}" ensure
    else
        echo "  WARN: .env.enc found but no Python runtime available to decrypt." >&2
        return 1
    fi
}

resolve_env_file() {
    if [ -n "${ENV_FILE_OVERRIDE}" ]; then
        ENV_FILE="${ENV_FILE_OVERRIDE}"
    elif [ -n "${AVIAN_ENV_FILE:-}" ]; then
        ENV_FILE="${AVIAN_ENV_FILE}"
    else
        ENV_FILE="${REPO_ROOT}/.env"
    fi

    if [ ! -f "${ENV_FILE}" ]; then
        _try_auto_decrypt || true
    fi

    if [ ! -f "${ENV_FILE}" ]; then
        echo "ERROR: Environment file not found: ${ENV_FILE}" >&2
        echo "Create ${REPO_ROOT}/.env or pass --env-file /path/to/local.env." >&2
        echo "If you have .env.enc, decrypt it first:  python scripts/encryption/env_crypto.py decrypt" >&2
        exit 1
    fi
}

# Helper: load .env safely (strips BOM + \r, exports line-by-line to avoid
# bash interpretation of unquoted JSON values like GEE_SERVICE_ACCOUNT_CREDENTIALS)
source_env() {
    resolve_env_file
    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        # Skip lines that aren't valid KEY=VALUE assignments (allowing spaces around '=').
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
        local key="${BASH_REMATCH[1]}"
        local val="${BASH_REMATCH[2]}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        # Strip surrounding single or double quotes
        val="${val#\'}" ; val="${val%\'}"
        val="${val#\"}" ; val="${val%\"}"
        export "$key=$val"
    done < <(sed '1s/^\xEF\xBB\xBF//' "${ENV_FILE}" | tr -d '\r')
}

# ---------------------------------------------------------------------------
# Helper: run scp/ssh with sshpass if available, else prompt interactively
# ---------------------------------------------------------------------------
_scp() {
    local ssh_pass="$1"; shift
    if command -v sshpass &>/dev/null; then
        sshpass -p "${ssh_pass}" scp -o StrictHostKeyChecking=accept-new "$@"
    else
        scp -o StrictHostKeyChecking=accept-new "$@"
    fi
}
_ssh() {
    local ssh_pass="$1"; shift
    if command -v sshpass &>/dev/null; then
        sshpass -p "${ssh_pass}" ssh -o StrictHostKeyChecking=accept-new "$@"
    else
        ssh -o StrictHostKeyChecking=accept-new "$@"
    fi
}

sync_env() {
    # Load .env to get server credentials
    source_env

    local ssh_target="${ONPREM_SSH_USER:?Set ONPREM_SSH_USER in .env}"
    local ssh_pass="${ONPREM_SSH_PASSWORD:-}"
    local workdir="${ONPREM_WORKDIR:?Set ONPREM_WORKDIR in .env}"
    local remote_env
    local remote_env_dir
    local remote_env_dir_q
    workdir="${workdir%/}"  # strip trailing slash
    remote_env="${REMOTE_ENV_FILE_OVERRIDE:-${ONPREM_ENV_FILE:-${workdir}/.env}}"
    remote_env_dir=$(dirname "${remote_env}")
    printf -v remote_env_dir_q "%q" "${remote_env_dir}"

    echo "=== Syncing env file (${ENV_FILE}) → ${ssh_target}:${remote_env} ==="
    _ssh "${ssh_pass}" "${ssh_target}" "mkdir -p ${remote_env_dir_q}"
    _scp "${ssh_pass}" "${ENV_FILE}" "${ssh_target}:${remote_env}"
    echo "Environment file transferred successfully."
}

# ---------------------------------------------------------------------------
# Deploy SIF images + scripts to server via scp (requires sshpass)
# ---------------------------------------------------------------------------
deploy_to_server() {
    source_env

    local ssh_target="${ONPREM_SSH_USER:?Set ONPREM_SSH_USER in .env}"
    local ssh_pass="${ONPREM_SSH_PASSWORD:-}"
    local workdir="${ONPREM_WORKDIR:?Set ONPREM_WORKDIR in .env}"
    workdir="${workdir%/}"

    local remote_sif="${workdir}/infrastructure/singularity/sif"

    # Create remote sif/ directory
    echo "=== Deploying to ${ssh_target}:${remote_sif}/ ==="
    _ssh "${ssh_pass}" "${ssh_target}" "mkdir -p ${remote_sif}"

    # Transfer each SIF file (skip if unchanged via size check)
    local count=0
    for sif in "${SIF_DIR}"/*.sif; do
        [ -f "$sif" ] || continue
        local name
        name=$(basename "$sif")
        local local_size
        local_size=$(stat -c%s "$sif" 2>/dev/null || stat -f%z "$sif")
        local remote_size
        remote_size=$(_ssh "${ssh_pass}" "${ssh_target}" \
            "stat -c%s ${remote_sif}/${name} 2>/dev/null || echo 0")

        if [ "$local_size" = "$remote_size" ]; then
            echo "  [skip] ${name} (${local_size} bytes, same size on server)"
        else
            echo "  [scp]  ${name} ($(numfmt --to=iec "$local_size"))..."
            _scp "${ssh_pass}" "$sif" "${ssh_target}:${remote_sif}/${name}"
            count=$((count + 1))
        fi
    done

    # Also transfer the shell scripts
    echo "  [scp]  start.sh, stop.sh"
    _scp "${ssh_pass}" \
        "${SCRIPT_DIR}/start.sh" "${SCRIPT_DIR}/stop.sh" \
        "${ssh_target}:${workdir}/infrastructure/singularity/"

    # Transfer encryption scripts so the server can auto-decrypt .env.enc
    local remote_enc="${workdir}/scripts/encryption"
    _ssh "${ssh_pass}" "${ssh_target}" "mkdir -p ${remote_enc}"
    echo "  [scp]  scripts/encryption/"
    _scp "${ssh_pass}" \
        "${REPO_ROOT}/scripts/encryption/env_crypto.py" \
        "${REPO_ROOT}/scripts/encryption/__init__.py" \
        "${ssh_target}:${remote_enc}/"

    echo "Deploy complete: ${count} SIF file(s) transferred."
}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
PUSH=false
SYNC_ENV=false
DEPLOY=false
ONLY=()
HAS_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-registry) SOURCE_MODE="registry"; shift ;;
        --from-local) SOURCE_MODE="local"; shift ;;
        --push)      PUSH=true; shift ;;
        --sync-env)  SYNC_ENV=true; shift ;;
        --deploy)    DEPLOY=true; shift ;;
        --env-file)  ENV_FILE_OVERRIDE="$2"; shift 2 ;;
        --remote-env-file) REMOTE_ENV_FILE_OVERRIDE="$2"; shift 2 ;;
        --namespace) REGISTRY_NAMESPACE="$2"; shift 2 ;;
        --tag)       IMAGE_TAG="$2"; shift 2 ;;
        --only)  shift; while [[ $# -gt 0 && "$1" != --* ]]; do ONLY+=("$1"); HAS_ONLY=true; shift; done ;;
        -h|--help)
            echo "Usage: $0 [--from-registry|--from-local] [--env-file path] [--sync-env] [--remote-env-file path] [--deploy] [--push] [--namespace ns] [--tag tag] [--only name1 name2 ...]"
            echo "  --from-registry  Pull OCI images from Docker Hub / registry and convert to SIF"
            echo "  --from-local     Build local Docker images first, then convert to SIF (default)"
            echo "  --env-file  Use an environment file outside the repo (or AVIAN_ENV_FILE)"
            echo "  --sync-env  Copy the selected environment file to the server via scp (requires sshpass)"
            echo "  --remote-env-file  Override the server-side destination path for --sync-env"
            echo "  --deploy    Transfer SIF images + scripts to server via scp"
            echo "  --push      Push SIF images to Sylabs Cloud Library"
            echo "  --namespace Docker Hub / OCI namespace for custom images (or DOCKERHUB_NAMESPACE)"
            echo "  --tag       Immutable image tag for custom registry images (or SINGULARITY_IMAGE_TAG)"
            echo "  --only      Build only the listed images"
            echo "  Names: $(_spec_names | tr '\n' ' ')"
            echo "  Default stack: ${DEFAULT_STACK[*]}"
            exit 0 ;;
        *)  echo "Unknown flag: $1"; exit 1 ;;
    esac
done

# Load the selected env file before any registry/local build step so the
# Singularity workflow can read DOCKERHUB_NAMESPACE / SINGULARITY_IMAGE_TAG
# without requiring the user to export them manually in the shell first.
if [ -n "${ENV_FILE_OVERRIDE}" ] || [ -n "${AVIAN_ENV_FILE:-}" ] || [ -f "${REPO_ROOT}/.env" ]; then
    source_env
    if [ -z "${REGISTRY_NAMESPACE}" ] && [ -n "${DOCKERHUB_NAMESPACE:-}" ]; then
        REGISTRY_NAMESPACE="${DOCKERHUB_NAMESPACE}"
    fi
    if [ -z "${IMAGE_TAG}" ] && [ -n "${SINGULARITY_IMAGE_TAG:-}" ]; then
        IMAGE_TAG="${SINGULARITY_IMAGE_TAG}"
    fi
fi

if $SYNC_ENV; then
    sync_env
fi

if $DEPLOY; then
    deploy_to_server
fi

# If only --sync-env / --deploy were requested, exit early (skip build)
if ! $PUSH && ! $HAS_ONLY && ($SYNC_ENV || $DEPLOY); then
    exit 0
fi

TARGET_IMAGES=()
if $HAS_ONLY; then
    TARGET_IMAGES=("${ONLY[@]}")
else
    TARGET_IMAGES=("${DEFAULT_STACK[@]}")
fi

if [ "${SOURCE_MODE}" = "local" ]; then
    build_docker "${TARGET_IMAGES[@]}"
else
    echo "=== Pulling ETL stack images from registry ==="
    echo "  Namespace: ${REGISTRY_NAMESPACE:-<unset>}"
    echo "  Tag:       ${IMAGE_TAG:-<unset>}"
fi

convert_all "${SOURCE_MODE}" "${TARGET_IMAGES[@]}"

if $PUSH; then
    push_to_library
fi
