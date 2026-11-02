#!/usr/bin/env bash
# =============================================================================
# start.sh — Start Avian Biosurveillance services via Singularity/Apptainer
# =============================================================================
# Starts all platform services as Singularity instances on localhost.
# Equivalent to `docker compose up -d`.
#
# Secrets are read from .env; non-secret service topology (ports, bind hosts,
# URLs, runtime mode) is read from config/runtime.env. Run
# `bash start.sh --check-ports` to verify no conflicts before starting.
#
# Instance names are prefixed with "ab-" to prevent collisions with other
# Singularity instances on the host (e.g. ab-timescaledb, ab-dagster-webserver).
# CLI accepts short names.
#
# Singularity CE 3.11 `instance start` only spawns sinit — it does NOT
# execute the Docker CMD/ENTRYPOINT. Each service function therefore calls
# `exec instance://ab-<name>` to launch the actual process, with output
# redirected to a project-local log directory under
# `infrastructure/singularity/volumes/logs/` for debugging.
#
# Workflow:
#   1. Build SIF images on laptop:  bash build.sh
#   2. On server:                   bash start.sh  (repo is on network mount)
#
# Usage:
#   cd <repo_root>
#   bash infrastructure/singularity/start.sh etl                              # recommended ETL stack
#   bash infrastructure/singularity/start.sh all                              # full stack (optional app)
#   bash infrastructure/singularity/start.sh timescaledb                        # start one service
#   bash infrastructure/singularity/start.sh --check-ports etl
#   bash infrastructure/singularity/start.sh --env-file /path/to/server.env etl
#
# Prerequisites:
#   1. SIF images in sif/ directory (built via build.sh)
#   2. Secret environment file with required passwords (repo-root .env by
#      default, or --env-file / AVIAN_ENV_FILE)
#   3. config/runtime.env with non-secret service topology
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SIF_DIR="${SCRIPT_DIR}/sif"
VOL_DIR="${SCRIPT_DIR}/volumes"
LOG_DIR="${VOL_DIR}/logs"
CONTAINER_LOG_DIR="/var/log/avian-biosurveillance"

# Instance name prefix — avoids collisions with other instances on the host
PREFIX="ab"

ENV_FILE_OVERRIDE=""
CHECK_PORTS=false
CHECK_PORTS_PROFILE=""
POSITIONAL_ARGS=()

usage() {
    echo "Usage: $0 [--env-file /path/to/server.env] [--check-ports [etl|all|full|timescaledb|minio|dagster|frontend]] [etl|all|full|timescaledb|minio|dagster|orchestration|frontend|init|status]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            if [ $# -lt 2 ]; then
                echo "ERROR: --env-file requires a path." >&2
                usage
                exit 1
            fi
            ENV_FILE_OVERRIDE="$2"
            shift 2
            ;;
        --check-ports)
            CHECK_PORTS=true
            if [ $# -gt 1 ] && [[ "$2" != --* ]]; then
                CHECK_PORTS_PROFILE="$2"
                shift 2
            else
                shift
            fi
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
    esac
done

if [ -n "${POSITIONAL_ARGS[0]+x}" ]; then
    set -- "${POSITIONAL_ARGS[@]}"
else
    set --
fi

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
        echo "Provide --env-file /path/to/server.env, export AVIAN_ENV_FILE, or create ${REPO_ROOT}/.env." >&2
        echo "If you have .env.enc, decrypt it first:  python scripts/encryption/env_crypto.py decrypt" >&2
        exit 1
    fi
}

# Load environment variables
# Load KEY=VALUE files safely (strips BOM + \r, exports line-by-line to avoid
# bash interpretation of unquoted JSON values like GEE_SERVICE_ACCOUNT_CREDENTIALS).
ENV_FILE=""
resolve_env_file

load_key_value_file() {
    local file_path="$1"
    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        # Skip lines that aren't valid KEY=VALUE assignments (allowing spaces around '=').
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
        key="${BASH_REMATCH[1]}"
        val="${BASH_REMATCH[2]}"
        # Trim surrounding whitespace before stripping optional quotes.
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        # Strip surrounding single or double quotes
        val="${val#\'}" ; val="${val%\'}"
        val="${val#\"}" ; val="${val%\"}"
        export "$key=$val"
    done < <(sed '1s/^\xEF\xBB\xBF//' "${file_path}" | tr -d '\r')
}

load_key_value_file "${ENV_FILE}"

RUNTIME_CONFIG_FILE="${AVIAN_RUNTIME_CONFIG:-${REPO_ROOT}/config/runtime.env}"
if [ ! -f "${RUNTIME_CONFIG_FILE}" ]; then
    echo "ERROR: Runtime config file not found: ${RUNTIME_CONFIG_FILE}" >&2
    echo "Create config/runtime.env or export AVIAN_RUNTIME_CONFIG=/path/to/runtime.env." >&2
    exit 1
fi
load_key_value_file "${RUNTIME_CONFIG_FILE}"

# ---------------------------------------------------------------------------
# Port assignments — sourced from config/runtime.env.
# ---------------------------------------------------------------------------
PORT_TSDB="${TSDB_PORT:?TSDB_PORT must be set in config/runtime.env}"
PORT_MINIO_API="${MINIO_API_PORT:?MINIO_API_PORT must be set in config/runtime.env}"
PORT_MINIO_CONSOLE="${MINIO_CONSOLE_PORT:?MINIO_CONSOLE_PORT must be set in config/runtime.env}"
PORT_DAGSTER="${DAGSTER_PORT:?DAGSTER_PORT must be set in config/runtime.env}"
PORT_APP="${APP_PORT:?APP_PORT must be set in config/runtime.env}"
DAGSTER_CODE_SERVER_PORT="${DAGSTER_CODE_SERVER_PORT:?DAGSTER_CODE_SERVER_PORT must be set in config/runtime.env}"
POSTGRES_READY_TIMEOUT="${POSTGRES_READY_TIMEOUT:-120}"
DAGSTER_HEALTH_TIMEOUT="${DAGSTER_HEALTH_TIMEOUT:-120}"
DISABLE_TIMESCALEDB_TUNE="${DISABLE_TIMESCALEDB_TUNE:-0}"

# ---------------------------------------------------------------------------
# Detect runtime
# ---------------------------------------------------------------------------
if command -v apptainer &>/dev/null; then
    RUNTIME="apptainer"
elif command -v singularity &>/dev/null; then
    RUNTIME="singularity"
else
    echo "ERROR: Neither apptainer nor singularity found." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Pre-flight: check that none of our ports are already in use
# ---------------------------------------------------------------------------
check_ports() {
    local profile="${1:-all}"
    local ports=()
    local labels=()
    local conflicts=0

    case "${profile}" in
        etl)
            ports=("${PORT_TSDB}" "${PORT_MINIO_API}" "${PORT_MINIO_CONSOLE}" "${PORT_DAGSTER}")
            labels=("TimescaleDB" "MinIO API" "MinIO Console" "Dagster")
            ;;
        all|full)
            ports=("${PORT_TSDB}" "${PORT_MINIO_API}" "${PORT_MINIO_CONSOLE}" "${PORT_DAGSTER}" "${PORT_APP}")
            labels=("TimescaleDB" "MinIO API" "MinIO Console" "Dagster" "Frontend")
            ;;
        timescaledb)
            ports=("${PORT_TSDB}")
            labels=("TimescaleDB")
            ;;
        minio)
            ports=("${PORT_MINIO_API}" "${PORT_MINIO_CONSOLE}")
            labels=("MinIO API" "MinIO Console")
            ;;
        dagster)
            ports=("${PORT_DAGSTER}")
            labels=("Dagster")
            ;;
        frontend)
            ports=("${PORT_APP}")
            labels=("Frontend")
            ;;
        *)
            echo "ERROR: Unknown port-check profile: ${profile}" >&2
            return 1
            ;;
    esac

    echo "=== Port availability check (${profile}) ==="
    for i in "${!ports[@]}"; do
        local p="${ports[$i]}"
        local l="${labels[$i]}"
        if ss -tlnp 2>/dev/null | grep -qE ":${p}\b"; then
            echo "  CONFLICT  ${l} → port ${p} is ALREADY IN USE"
            conflicts=$((conflicts + 1))
        else
            echo "  OK        ${l} → port ${p} is free"
        fi
    done

    if [ "${conflicts}" -gt 0 ]; then
        echo ""
        echo "ERROR: ${conflicts} port conflict(s) found."
        echo "Edit TSDB_PORT / MINIO_API_PORT / DAGSTER_PORT / etc. in config/runtime.env to use free ports."
        return 1
    fi
    echo ""
    echo "All ports are free — safe to start."
    return 0
}

# Handle --check-ports flag early
if $CHECK_PORTS; then
    check_ports "${CHECK_PORTS_PROFILE:-${1:-all}}"
    exit $?
fi

SERVICE="${1:-all}"

case "${SERVICE}" in
    all|full)
        check_ports full || exit 1
        ;;
    etl)
        check_ports etl || exit 1
        ;;
    timescaledb|minio|dagster|frontend)
        check_ports "${SERVICE}" || exit 1
        ;;
esac

# Create persistent volume directories
mkdir -p "${VOL_DIR}/timescaledb"
mkdir -p "${VOL_DIR}/minio"
mkdir -p "${VOL_DIR}/timescaledb-run"
mkdir -p "${VOL_DIR}/timescaledb-tmp"
mkdir -p "${VOL_DIR}/timescaledb-home"
mkdir -p "${VOL_DIR}/minio-home"
mkdir -p "${VOL_DIR}/dagster-home"
mkdir -p "${VOL_DIR}/orchestration-home"
mkdir -p "${VOL_DIR}/app-home"
mkdir -p "${VOL_DIR}/orchestration-tmp"
mkdir -p "${LOG_DIR}"
# Sweep stale env-files left behind by interrupted prior runs.
rm -rf "${VOL_DIR}/.envtmp"
mkdir -p "${VOL_DIR}/.envtmp"

# Create host-side data directories that the read-only orchestration SIF must
# write through explicit bind mounts.
mkdir -p "${REPO_ROOT}/data/raw"
mkdir -p "${REPO_ROOT}/data/processed"
mkdir -p "${REPO_ROOT}/data/quality"

# ---------------------------------------------------------------------------
# Resource caps (CPU / memory) per service. Apptainer & SingularityCE ≥3.11
# accept --cpus / --memory on `instance start`, applied via cgroups v2.
# Override any of these in config/runtime.env to tune for the host. Set to empty string
# to disable a particular cap.
# ---------------------------------------------------------------------------
RES_TSDB_CPUS="${RES_TSDB_CPUS:-4}"
RES_TSDB_MEM="${RES_TSDB_MEM:-4G}"
RES_AUDIODB_CPUS="${RES_AUDIODB_CPUS:-2}"
RES_AUDIODB_MEM="${RES_AUDIODB_MEM:-2G}"
RES_DAGSTER_CPUS="${RES_DAGSTER_CPUS:-2}"
RES_DAGSTER_MEM="${RES_DAGSTER_MEM:-2G}"
RES_ORCHESTRATION_CPUS="${RES_ORCHESTRATION_CPUS:-4}"
RES_ORCHESTRATION_MEM="${RES_ORCHESTRATION_MEM:-8G}"
RES_APP_CPUS="${RES_APP_CPUS:-2}"
RES_APP_MEM="${RES_APP_MEM:-2G}"

_resource_args() {
    # Echo --cpus / --memory flags for the named service profile.
    local cpus="$1"
    local mem="$2"
    [ -n "${cpus}" ] && printf -- '--cpus %s ' "${cpus}"
    [ -n "${mem}" ] && printf -- '--memory %s ' "${mem}"
}

# ---------------------------------------------------------------------------
# Deferred env-file cleanup. Singularity reads --env-file lazily during
# `instance start` and during the subsequent `exec`; deleting it inline
# races against those reads (Failed to open env file). Files are queued
# here and removed at script exit instead.
# ---------------------------------------------------------------------------
_ENV_FILES_TO_CLEAN=()
_cleanup_env_files() {
    local f
    for f in "${_ENV_FILES_TO_CLEAN[@]:-}"; do
        [ -n "${f}" ] && rm -f "${f}"
    done
}
trap _cleanup_env_files EXIT

defer_env_cleanup() {
    _ENV_FILES_TO_CLEAN+=("$1")
}

# ---------------------------------------------------------------------------
# Helper: start a Singularity instance (sinit only — no CMD execution)
# ---------------------------------------------------------------------------
start_instance() {
    local name="$1"
    local sif="$2"
    shift 2
    local extra_args=("$@")

    if ! [ -f "${sif}" ]; then
        echo "  ERROR: SIF not found: ${sif}" >&2
        return 1
    fi

    if ${RUNTIME} instance list | grep -q "^${name} "; then
        echo "  [skip] ${name} already running"
        return 0
    fi

    echo "  [start] ${name}"
    ${RUNTIME} instance start \
        --bind "${LOG_DIR}:${CONTAINER_LOG_DIR}" \
        "${extra_args[@]}" \
        "${sif}" \
        "${name}"
}

# ---------------------------------------------------------------------------
# Helper: create a temporary env file inside the allowed workspace path.
# Singularity CE 3.11 parses `--env` values as comma-separated maps, which
# breaks on passwords / credentials containing commas or similar punctuation.
# ---------------------------------------------------------------------------
make_env_file() {
    local env_dir="${VOL_DIR}/.envtmp"
    local env_file
    local kv
    local key
    local val
    local escaped_val

    mkdir -p "${env_dir}"
    env_file=$(mktemp "${env_dir}/env.XXXXXX")

    for kv in "$@"; do
        key="${kv%%=*}"
        val="${kv#*=}"
        escaped_val=${val//\'/\'\\\'\'}
        printf "%s='%s'\n" "$key" "$escaped_val" >> "${env_file}"
    done

    echo "${env_file}"
}

write_dagster_workspace() {
    local workspace_path="$1"
    local grpc_host="${DAGSTER_CODE_SERVER_LOCAL_HOST:-127.0.0.1}"

    {
        printf '%s\n' '# Generated from config/runtime.env by infrastructure/singularity/start.sh.'
        printf '%s\n' 'load_from:'
        printf '%s\n' '  - grpc_server:'
        printf '%s\n' "      host: ${grpc_host}"
        printf '%s\n' "      port: ${DAGSTER_CODE_SERVER_PORT}"
        printf '%s\n' "      location_name: ${DAGSTER_LOCATION_NAME:-pipelines}"
    } > "${workspace_path}"
}

urlencode() {
    local raw="${1}"
    local length="${#raw}"
    local encoded=""
    local pos
    local char
    local hex

    for ((pos = 0; pos < length; pos++)); do
        char="${raw:pos:1}"
        case "${char}" in
            [a-zA-Z0-9.~_-])
                encoded+="${char}"
                ;;
            *)
                printf -v hex '%%%02X' "'${char}"
                encoded+="${hex}"
                ;;
        esac
    done

    printf '%s' "${encoded}"
}

# ---------------------------------------------------------------------------
# Helper: wait for a TCP port to accept connections (readiness check)
# ---------------------------------------------------------------------------
wait_for_port() {
    local port="$1"
    local label="$2"
    local timeout="${3:-60}"
    local elapsed=0

    echo -n "  Waiting for ${label} on port ${port}..."
    while ! ss -tlnp 2>/dev/null | grep -qE ":${port}\b"; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo " TIMEOUT after ${timeout}s"
            return 1
        fi
        echo -n "."
    done
    echo " ready (${elapsed}s)"
}

# ---------------------------------------------------------------------------
# Helper: wait until PostgreSQL accepts SQL connections, not just TCP sockets.
# ---------------------------------------------------------------------------
wait_for_postgres() {
    local timeout="${1:-120}"
    local elapsed=0
    local env_file

    env_file=$(make_env_file \
        "PGHOST=127.0.0.1" \
        "PGPORT=${PORT_TSDB}" \
        "PGUSER=${POSTGRES_USER:-root}" \
        "PGPASSWORD=${POSTGRES_PASSWORD}" \
        "PGDATABASE=${POSTGRES_DB:-avian_biosurveillance}" \
        "PGCONNECT_TIMEOUT=3" \
        "PGGSSENCMODE=disable")

    echo -n "  Waiting for TimescaleDB SQL readiness on port ${PORT_TSDB}..."
    while ! ${RUNTIME} exec \
        --env-file "${env_file}" \
        "${SIF_DIR}/timescaledb.sif" \
        pg_isready -h 127.0.0.1 -p "${PORT_TSDB}" -U "${POSTGRES_USER:-root}" -d "${POSTGRES_DB:-avian_biosurveillance}" >/dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "${elapsed}" -ge "${timeout}" ]; then
            rm -f "${env_file}"
            echo " TIMEOUT after ${timeout}s"
            return 1
        fi
        echo -n "."
    done

    rm -f "${env_file}"
    echo " ready (${elapsed}s)"
}

# ---------------------------------------------------------------------------
# Helper: wait for HTTP endpoint (e.g. Dagster server_info)
# ---------------------------------------------------------------------------
wait_for_http() {
    local url="$1"
    local label="$2"
    local timeout="${3:-60}"
    local elapsed=0

    echo -n "  Waiting for ${label} at ${url}..."
    while ! curl -sf "${url}" >/dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo " TIMEOUT after ${timeout}s"
            return 1
        fi
        echo -n "."
    done
    echo " ready (${elapsed}s)"
}

# ---------------------------------------------------------------------------
# Helper: show recent service log output after a readiness failure
# ---------------------------------------------------------------------------
show_recent_log() {
    local log_path="$1"
    local label="$2"

    if [ -f "${log_path}" ]; then
        echo ""
        echo "--- Last 80 lines from ${label} log: ${log_path} ---"
        tail -n 80 "${log_path}" || true
        echo "--- End ${label} log ---"
    else
        echo ""
        echo "NOTE: ${label} log not found yet at ${log_path}"
    fi
}

# ---------------------------------------------------------------------------
# Service: TimescaleDB
# ---------------------------------------------------------------------------
# Singularity `instance start` only runs sinit. We must explicitly exec the
# Docker entrypoint to start PostgreSQL. Output goes to the project-local
# bind mount under ${LOG_DIR}/ab-timescaledb.log.
start_timescaledb() {
    echo "Starting TimescaleDB on port ${PORT_TSDB}..."
    local env_file
    local log_file="${CONTAINER_LOG_DIR}/ab-timescaledb.log"
    local extra_binds=(
        --bind "${VOL_DIR}/timescaledb:/var/lib/postgresql/data"
        --bind "${VOL_DIR}/timescaledb-run:/var/run/postgresql"
        --bind "${VOL_DIR}/timescaledb-tmp:/tmp"
        # No /docker-entrypoint-initdb.d/ binds: the full schema is applied
        # post-start by db/bootstrap/init_database.sh below (idempotent,
        # single source of truth — matches the docker-compose layout).
    )

    case "${DISABLE_TIMESCALEDB_TUNE}" in
        1|true|TRUE|yes|YES)
            extra_binds+=(
                --bind "${SCRIPT_DIR}/disable_timescaledb_tune.sh:/docker-entrypoint-initdb.d/001_timescaledb_tune.sh:ro"
            )
            ;;
    esac

    env_file=$(make_env_file \
        "TMPDIR=/tmp" \
        "TEMP=/tmp" \
        "TMP=/tmp" \
        "POSTGRES_DB=${POSTGRES_DB:-avian_biosurveillance}" \
        "POSTGRES_USER=${POSTGRES_USER:-root}" \
        "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
        "TIMESCALEDB_TELEMETRY=off" \
        "PGPORT=${PORT_TSDB}" \
        "POSTGRES_INITDB_ARGS=--auth-host=md5")

    start_instance "${PREFIX}-timescaledb" "${SIF_DIR}/timescaledb.sif" \
        --home "${VOL_DIR}/timescaledb-home:/home/postgres" \
        $(_resource_args "${RES_TSDB_CPUS}" "${RES_TSDB_MEM}") \
        "${extra_binds[@]}" \
        --env-file "${env_file}"

    # Execute the Docker entrypoint to actually start PostgreSQL.
    # PGPORT is honoured by docker-entrypoint.sh to set the server
    # listening port — required for host-networking in Singularity.
    ${RUNTIME} exec \
        --env-file "${env_file}" \
        "instance://${PREFIX}-timescaledb" \
        bash -c 'nohup docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c port='"${PORT_TSDB}"' -c max_connections=100 -c shared_buffers=256MB > '"${log_file}"' 2>&1 &'

    defer_env_cleanup "${env_file}"
}

# ---------------------------------------------------------------------------
# Service: MinIO
# ---------------------------------------------------------------------------
start_minio() {
    echo "Starting MinIO on ports ${PORT_MINIO_API}/${PORT_MINIO_CONSOLE}..."
    local env_file
    local log_file="${CONTAINER_LOG_DIR}/ab-minio.log"
    mkdir -p "${VOL_DIR}/minio/tmp"
    env_file=$(make_env_file \
        "TMPDIR=/data/tmp" \
        "TEMP=/data/tmp" \
        "TMP=/data/tmp" \
        "MINIO_ROOT_USER=${MINIO_ROOT_USER:-avian-minio}" \
        "MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}")

    start_instance "${PREFIX}-minio" "${SIF_DIR}/minio.sif" \
        --home "${VOL_DIR}/minio-home:/home/minio" \
        --bind "${VOL_DIR}/minio:/data" \
        $(_resource_args "${RES_AUDIODB_CPUS}" "${RES_AUDIODB_MEM}") \
        --env-file "${env_file}"

    # Execute MinIO server inside the instance
    ${RUNTIME} exec \
        --env-file "${env_file}" \
        "instance://${PREFIX}-minio" \
        bash -c 'nohup minio server /data --address 127.0.0.1:'"${PORT_MINIO_API}"' --console-address 127.0.0.1:'"${PORT_MINIO_CONSOLE}"' > '"${log_file}"' 2>&1 &'

    defer_env_cleanup "${env_file}"
}

# ---------------------------------------------------------------------------
# Service: Dagster Webserver (UI + GraphQL)
# ---------------------------------------------------------------------------
start_dagster_webserver() {
    echo "Starting Dagster Webserver on port ${PORT_DAGSTER}..."
    local env_file
    local log_file="${CONTAINER_LOG_DIR}/ab-dagster-webserver.log"
    local dagster_home_dir="${VOL_DIR}/dagster-home"
    local config_dir="${REPO_ROOT}/infrastructure/docker"
    local workspace_file="${dagster_home_dir}/workspace.yaml"

    mkdir -p "${dagster_home_dir}"
    write_dagster_workspace "${workspace_file}"

    env_file=$(make_env_file \
        "DAGSTER_HOME=/opt/dagster_home" \
        "DAGSTER_INTERNAL_PORT=${PORT_DAGSTER}" \
        "DAGSTER_PG_HOST=127.0.0.1" \
        "DAGSTER_PG_PORT=${PORT_TSDB}" \
        "DAGSTER_PG_USER=${POSTGRES_USER:-root}" \
        "DAGSTER_PG_PASSWORD=${POSTGRES_PASSWORD}" \
        "DAGSTER_PG_DB=dagster_db")

    start_instance "${PREFIX}-dagster-webserver" "${SIF_DIR}/dagster.sif" \
        --home "${dagster_home_dir}:/opt/dagster_home" \
        --bind "${config_dir}/dagster.yaml:/opt/dagster_home/dagster.yaml:ro" \
        $(_resource_args "${RES_DAGSTER_CPUS}" "${RES_DAGSTER_MEM}") \
        --env-file "${env_file}"

    ${RUNTIME} exec \
        --env-file "${env_file}" \
        "instance://${PREFIX}-dagster-webserver" \
        bash -c 'nohup dagster-webserver --host 127.0.0.1 --port '"${PORT_DAGSTER}"' --workspace /opt/dagster_home/workspace.yaml > '"${log_file}"' 2>&1 &'

    defer_env_cleanup "${env_file}"
}

# ---------------------------------------------------------------------------
# Service: Dagster Daemon (scheduler + sensors + run queue)
# ---------------------------------------------------------------------------
start_dagster_daemon() {
    echo "Starting Dagster Daemon..."
    local env_file
    local log_file="${CONTAINER_LOG_DIR}/ab-dagster-daemon.log"
    local dagster_home_dir="${VOL_DIR}/dagster-home"
    local config_dir="${REPO_ROOT}/infrastructure/docker"
    local workspace_file="${dagster_home_dir}/workspace.yaml"

    mkdir -p "${dagster_home_dir}"
    write_dagster_workspace "${workspace_file}"

    env_file=$(make_env_file \
        "DAGSTER_HOME=/opt/dagster_home" \
        "DAGSTER_PG_HOST=127.0.0.1" \
        "DAGSTER_PG_PORT=${PORT_TSDB}" \
        "DAGSTER_PG_USER=${POSTGRES_USER:-root}" \
        "DAGSTER_PG_PASSWORD=${POSTGRES_PASSWORD}" \
        "DAGSTER_PG_DB=dagster_db")

    start_instance "${PREFIX}-dagster-daemon" "${SIF_DIR}/dagster.sif" \
        --home "${dagster_home_dir}:/opt/dagster_home" \
        --bind "${config_dir}/dagster.yaml:/opt/dagster_home/dagster.yaml:ro" \
        $(_resource_args "${RES_DAGSTER_CPUS}" "${RES_DAGSTER_MEM}") \
        --env-file "${env_file}"

    ${RUNTIME} exec \
        --env-file "${env_file}" \
        "instance://${PREFIX}-dagster-daemon" \
        bash -c 'nohup dagster-daemon run --workspace /opt/dagster_home/workspace.yaml > '"${log_file}"' 2>&1 &'

    defer_env_cleanup "${env_file}"
}

# ---------------------------------------------------------------------------
# Service: DataOrchestration
# ---------------------------------------------------------------------------
start_orchestration() {
    echo "Starting DataOrchestration..."
    local env_file
    local log_file="${CONTAINER_LOG_DIR}/ab-orchestration.log"
    local orchestration_home_dir="${VOL_DIR}/orchestration-home"
    local raw_data_dir="${REPO_ROOT}/data/raw"
    local processed_data_dir="${REPO_ROOT}/data/processed"
    local quality_data_dir="${REPO_ROOT}/data/quality"
    local reference_data_dir="${REPO_ROOT}/data/reference"
    local config_dir="${REPO_ROOT}/config"
    local tmp_data_dir="${VOL_DIR}/orchestration-tmp"

    mkdir -p "${raw_data_dir}" "${processed_data_dir}" "${quality_data_dir}" "${tmp_data_dir}"
    mkdir -p "${orchestration_home_dir}/.cache" "${orchestration_home_dir}/.config" "${orchestration_home_dir}/.local/share"

    env_file=$(make_env_file \
        "PYTHONPATH=/app/src" \
        "DAGSTER_CODE_SERVER_HOST=127.0.0.1" \
        "DAGSTER_CODE_SERVER_PORT=${DAGSTER_CODE_SERVER_PORT}" \
        "DAGSTER_HOME=/home/appuser/.dagster_home" \
        "DAGSTER_PG_HOST=127.0.0.1" \
        "DAGSTER_PG_PORT=${PORT_TSDB}" \
        "DAGSTER_PG_USER=${POSTGRES_USER:-root}" \
        "DAGSTER_PG_PASSWORD=${POSTGRES_PASSWORD}" \
        "DAGSTER_PG_DB=dagster_db" \
        "DATABASE_URL=postgresql://${POSTGRES_USER:-root}:${POSTGRES_PASSWORD}@127.0.0.1:${PORT_TSDB}/${POSTGRES_DB:-avian_biosurveillance}" \
        "STATION_SUMMARY_DB_USER=${POSTGRES_USER:-root}" \
        "STATION_SUMMARY_DB_PASSWORD=${POSTGRES_PASSWORD}" \
        "STATION_SUMMARY_DB_HOST=127.0.0.1" \
        "STATION_SUMMARY_DB_PORT=${PORT_TSDB}" \
        "STATION_SUMMARY_DB_NAME=${POSTGRES_DB:-avian_biosurveillance}" \
        "MINIO_ENDPOINT=127.0.0.1:${PORT_MINIO_API}" \
        "MINIO_ACCESS_KEY=${MINIO_APP_USER:?MINIO_APP_USER must be set in .env}" \
        "MINIO_SECRET_KEY=${MINIO_APP_PASSWORD:?MINIO_APP_PASSWORD must be set in .env}" \
        "KNMI_OPEN_DATA_API=${KNMI_OPEN_DATA_API:-}" \
        "KNMI_EDR=${KNMI_EDR:-}" \
        "GEMINI_API_KEY=${GEMINI_API_KEY:-}" \
        "HF_TOKEN=${HF_TOKEN:-}" \
        "EBIRD_API_KEY=${EBIRD_API_KEY:-}" \
        "MOVEBANK_USERNAME=${MOVEBANK_USERNAME:-}" \
        "MOVEBANK_PASSWORD=${MOVEBANK_PASSWORD:-}" \
        "GEE_SERVICE_ACCOUNT_FILE=${GEE_SERVICE_ACCOUNT_FILE:-}" \
        "GEE_SERVICE_ACCOUNT_CREDENTIALS=${GEE_SERVICE_ACCOUNT_CREDENTIALS:-}" \
        "GEE_PROJECT=${GEE_PROJECT:-}" \
        "CDS_API_KEY=${CDS_API_KEY:-}" \
        "CDS_API_URL=${CDS_API_URL:-https://cds.climate.copernicus.eu/api}" \
        "XDG_CACHE_HOME=/home/appuser/.cache" \
        "XDG_CONFIG_HOME=/home/appuser/.config" \
        "XDG_DATA_HOME=/home/appuser/.local/share" \
        "HF_HOME=/home/appuser/.cache/huggingface" \
        "HF_HUB_CACHE=/home/appuser/.cache/huggingface/hub" \
        "TRANSFORMERS_CACHE=/home/appuser/.cache/huggingface/transformers" \
        "TORCH_HOME=/home/appuser/.cache/torch" \
        "MPLCONFIGDIR=/home/appuser/.config/matplotlib" \
        "CLOUDSDK_CONFIG=/home/appuser/.config/gcloud" \
        "PYTHONPYCACHEPREFIX=/home/appuser/.cache/pycache" \
        "TMPDIR=/app/data/tmp" \
        "TEMP=/app/data/tmp" \
        "TMP=/app/data/tmp")

    start_instance "${PREFIX}-orchestration" "${SIF_DIR}/orchestration.sif" \
        --home "${orchestration_home_dir}:/home/appuser" \
        --bind "${raw_data_dir}:/app/data/raw" \
        --bind "${processed_data_dir}:/app/data/processed" \
        --bind "${quality_data_dir}:/app/data/quality" \
        --bind "${tmp_data_dir}:/app/data/tmp" \
        --bind "${reference_data_dir}:/app/data/reference:ro" \
        --bind "${config_dir}:/app/config:ro" \
        $(_resource_args "${RES_ORCHESTRATION_CPUS}" "${RES_ORCHESTRATION_MEM}") \
        --env-file "${env_file}"

    # Execute the serve process (same as Docker CMD)
    ${RUNTIME} exec \
        --env-file "${env_file}" \
        --pwd /app \
        "instance://${PREFIX}-orchestration" \
        bash -c 'nohup python -m avian_biosurveillance.orchestration.serve > '"${log_file}"' 2>&1 &'

    defer_env_cleanup "${env_file}"
}

# ---------------------------------------------------------------------------
# Service: App (Next.js Dashboard)
# ---------------------------------------------------------------------------
start_app() {
    echo "Starting App (Next.js Dashboard) on port ${PORT_APP}..."
    local env_file
    local log_file="${CONTAINER_LOG_DIR}/ab-app.log"
    local app_home_dir="${VOL_DIR}/app-home"
    mkdir -p "${app_home_dir}/.cache" "${app_home_dir}/.config" "${app_home_dir}/tmp"
    env_file=$(make_env_file \
        "DATABASE_URL=postgresql://${POSTGRES_USER:-root}:${POSTGRES_PASSWORD}@127.0.0.1:${PORT_TSDB}/${POSTGRES_DB:-avian_biosurveillance}" \
        "DAGSTER_GRAPHQL_URL=http://127.0.0.1:${PORT_DAGSTER}/graphql" \
        "NEXT_PUBLIC_DAGSTER_GRAPHQL_URL=http://127.0.0.1:${PORT_DAGSTER}/graphql" \
        "MINIO_ENDPOINT=127.0.0.1:${PORT_MINIO_API}" \
        "MINIO_ACCESS_KEY=${MINIO_APP_USER:?MINIO_APP_USER must be set in .env}" \
        "MINIO_SECRET_KEY=${MINIO_APP_PASSWORD:?MINIO_APP_PASSWORD must be set in .env}" \
        "XDG_CACHE_HOME=/home/nextjs/.cache" \
        "XDG_CONFIG_HOME=/home/nextjs/.config" \
        "NPM_CONFIG_CACHE=/home/nextjs/.cache/npm" \
        "TMPDIR=/home/nextjs/tmp" \
        "TEMP=/home/nextjs/tmp" \
        "TMP=/home/nextjs/tmp" \
        "APP_READY_FILE=/home/nextjs/tmp/species-tracker-ready" \
        "NODE_ENV=production" \
        "HOSTNAME=127.0.0.1" \
        "PORT=${PORT_APP}")

    start_instance "${PREFIX}-app" "${SIF_DIR}/app.sif" \
        --home "${app_home_dir}:/home/nextjs" \
        --bind "${REPO_ROOT}/config:/app/config" \
        --bind "${REPO_ROOT}/data/reference:/app/data/reference:ro" \
        $(_resource_args "${RES_APP_CPUS}" "${RES_APP_MEM}") \
        --env-file "${env_file}"

    # Execute the same entrypoint as Docker so warmup/readiness behavior stays aligned.
    ${RUNTIME} exec \
        --env-file "${env_file}" \
        --pwd /app \
        "instance://${PREFIX}-frontend" \
        bash -c 'nohup ./docker-entrypoint.sh > '"${log_file}"' 2>&1 &'

    defer_env_cleanup "${env_file}"
}

start_etl_stack() {
    start_timescaledb
    start_minio
    if ! wait_for_postgres "${POSTGRES_READY_TIMEOUT}"; then
        show_recent_log "${LOG_DIR}/ab-timescaledb.log" "TimescaleDB"
        return 1
    fi
    if ! wait_for_port "${PORT_MINIO_API}" "MinIO" 30; then
        show_recent_log "${LOG_DIR}/ab-minio.log" "MinIO"
        return 1
    fi
    run_init
    # Orchestration first — it provides the gRPC code-server that the
    # Dagster webserver/daemon both connect to via generated workspace.yaml.
    start_orchestration
    if ! wait_for_port "${DAGSTER_CODE_SERVER_PORT}" "Dagster code-server" 60; then
        show_recent_log "${LOG_DIR}/ab-orchestration.log" "Orchestration"
        return 1
    fi
    start_dagster_webserver
    if ! wait_for_http "http://127.0.0.1:${PORT_DAGSTER}/server_info" "Dagster" "${DAGSTER_HEALTH_TIMEOUT}"; then
        show_recent_log "${LOG_DIR}/ab-dagster-webserver.log" "Dagster"
        return 1
    fi
    start_dagster_daemon
}

start_full_stack() {
    start_etl_stack
    start_app
}

# ---------------------------------------------------------------------------
# Init: Database provisioning (SQL schema) + MinIO buckets
# ---------------------------------------------------------------------------
run_init() {
    echo "Running database role provisioning..."
    local db_env_file
    local minio_env_file
    db_env_file=$(make_env_file \
        "PGHOST=127.0.0.1" \
        "PGPORT=${PORT_TSDB}" \
        "PGUSER=${POSTGRES_USER:-root}" \
        "PGPASSWORD=${POSTGRES_PASSWORD}" \
        "POSTGRES_DB=${POSTGRES_DB:-avian_biosurveillance}" \
        "PGGSSENCMODE=disable" \
        "POSTGRES_USER=${POSTGRES_USER:-root}" \
        "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}")
    minio_env_file=$(make_env_file \
        "MC_HOST_myminio=http://${MINIO_ROOT_USER:-avian-minio}:${MINIO_ROOT_PASSWORD}@127.0.0.1:${PORT_MINIO_API}" \
        "MINIO_APP_USER=${MINIO_APP_USER:?MINIO_APP_USER must be set in .env}" \
        "MINIO_APP_PASSWORD=${MINIO_APP_PASSWORD:?MINIO_APP_PASSWORD must be set in .env}")

    echo "Running full DB bootstrap via db/bootstrap/init_database.sh..."
    ${RUNTIME} exec \
        --bind "${REPO_ROOT}/db:/init/db:ro" \
        --env-file "${db_env_file}" \
        "${SIF_DIR}/timescaledb.sif" \
        bash /init/db/bootstrap/init_database.sh

    echo "Running MinIO bucket init + scoped app user..."
    ${RUNTIME} exec \
        --env-file "${minio_env_file}" \
        "${SIF_DIR}/minio.sif" \
        sh -c '
            set -e
            mc mb --ignore-existing myminio/raw-audio
            mc mb --ignore-existing myminio/processed-audio
            mc anonymous set none myminio/raw-audio
            mc anonymous set none myminio/processed-audio
            cat > /tmp/audio-rw.json <<POLICY
            {
              "Version": "2012-10-17",
              "Statement": [
                {
                  "Effect": "Allow",
                  "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                  "Resource": [
                    "arn:aws:s3:::raw-audio",
                    "arn:aws:s3:::raw-audio/*",
                    "arn:aws:s3:::processed-audio",
                    "arn:aws:s3:::processed-audio/*"
                  ]
                }
              ]
            }
            POLICY
            mc admin policy create myminio audio-rw /tmp/audio-rw.json 2>/dev/null \
              || mc admin policy update myminio audio-rw /tmp/audio-rw.json
            mc admin user add myminio "$MINIO_APP_USER" "$MINIO_APP_PASSWORD"
            mc admin policy attach myminio audio-rw --user="$MINIO_APP_USER" 2>/dev/null || true
            echo "MinIO buckets + scoped user provisioned."
        '

    rm -f "${db_env_file}" "${minio_env_file}"
}

# ---------------------------------------------------------------------------
# Status: show running instances and their ports
# ---------------------------------------------------------------------------
show_status() {
    echo ""
    echo "=== Service Ports ==="
    echo "  TimescaleDB :  ${PORT_TSDB}"
    echo "  MinIO API   :  ${PORT_MINIO_API}"
    echo "  MinIO Console: ${PORT_MINIO_CONSOLE}"
    echo "  Dagster     :  ${PORT_DAGSTER}"
    echo "  App         :  ${PORT_APP}"
    echo ""
    echo "=== Log Files ==="
    echo "  ${LOG_DIR}/ab-timescaledb.log    ${LOG_DIR}/ab-minio.log"
    echo "  ${LOG_DIR}/ab-dagster-webserver.log  ${LOG_DIR}/ab-dagster-daemon.log"
    echo "  ${LOG_DIR}/ab-orchestration.log  ${LOG_DIR}/ab-frontend.log"
    echo ""
    echo "=== Running Instances ==="
    ${RUNTIME} instance list
}

# ---------------------------------------------------------------------------
# Start all or a specific service
# ---------------------------------------------------------------------------
case "${SERVICE}" in
    timescaledb)    start_timescaledb ;;
    minio)          start_minio ;;
    dagster)        start_dagster_webserver; start_dagster_daemon ;;
    orchestration)  start_orchestration ;;
    frontend)       start_app ;;
    init)           run_init ;;
    status)         show_status ;;
    etl)
        start_etl_stack
        show_status
        ;;
    all|full)
        start_full_stack
        show_status
        ;;
    *)
        usage
        exit 1
        ;;
esac
