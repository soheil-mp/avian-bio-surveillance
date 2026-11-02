#!/usr/bin/env bash
# =============================================================================
# stop.sh — Stop Avian Biosurveillance Singularity/Apptainer instances
# =============================================================================
# Equivalent to `docker compose down`.
# Instances are stopped in reverse dependency order (app first, databases last).
#
# Usage:
#   bash infrastructure/singularity/stop.sh etl           # stop ETL stack only
#   bash infrastructure/singularity/stop.sh all           # stop full stack
#   bash infrastructure/singularity/stop.sh timescaledb  # stop one service (short name)
#   bash infrastructure/singularity/stop.sh ab-timescaledb  # or full instance name
# =============================================================================
set -euo pipefail

PREFIX="ab"

# Detect runtime
if command -v apptainer &>/dev/null; then
    RUNTIME="apptainer"
elif command -v singularity &>/dev/null; then
    RUNTIME="singularity"
else
    echo "ERROR: Neither apptainer nor singularity found." >&2
    exit 1
fi

# Reverse dependency order: consumers first, databases last
FULL_INSTANCES=("${PREFIX}-frontend" "${PREFIX}-dagster-daemon" "${PREFIX}-dagster-webserver" "${PREFIX}-orchestration" "${PREFIX}-minio" "${PREFIX}-timescaledb")
ETL_INSTANCES=("${PREFIX}-dagster-daemon" "${PREFIX}-dagster-webserver" "${PREFIX}-orchestration" "${PREFIX}-minio" "${PREFIX}-timescaledb")

stop_instance() {
    local name="$1"
    if ${RUNTIME} instance list | grep -q "^${name} "; then
        echo "  [stop] ${name}"
        ${RUNTIME} instance stop "${name}"
    else
        echo "  [skip] ${name} not running"
    fi
}

SERVICE="${1:-all}"

case "${SERVICE}" in
    all|full)
        echo "Stopping all instances (reverse dependency order)..."
        for inst in "${FULL_INSTANCES[@]}"; do
            stop_instance "${inst}"
        done
        echo "All instances stopped."
        ;;
    etl)
        echo "Stopping ETL stack (reverse dependency order)..."
        for inst in "${ETL_INSTANCES[@]}"; do
            stop_instance "${inst}"
        done
        echo "ETL stack stopped."
        ;;
    # Accept short names for convenience
    timescaledb)    stop_instance "${PREFIX}-timescaledb" ;;
    minio)          stop_instance "${PREFIX}-minio" ;;
    prefect|dagster) stop_instance "${PREFIX}-dagster-daemon"; stop_instance "${PREFIX}-dagster-webserver" ;;
    orchestration)  stop_instance "${PREFIX}-orchestration" ;;
    frontend)       stop_instance "${PREFIX}-frontend" ;;
    # Also accept full instance names directly
    *)
        stop_instance "${SERVICE}"
        ;;
esac

echo ""
echo "Running instances:"
${RUNTIME} instance list
