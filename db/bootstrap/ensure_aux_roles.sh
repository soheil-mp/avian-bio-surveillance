#!/bin/bash
# =============================================================================
# ensure_aux_roles.sh -- Auxiliary DB provisioning + legacy role cleanup
# =============================================================================
# Thin orchestration wrapper around ``010_aux_databases_and_roles.sql``.
# PostgreSQL requires separate connections for per-database schema hardening,
# so shell remains responsible only for selecting target databases; role and
# grant logic lives in the SQL script.
# =============================================================================

set -euo pipefail

DB_USER="${PGUSER:-${POSTGRES_USER:-root}}"
MAIN_DB="${POSTGRES_DB:-avian_biosurveillance}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_FILE="${SCRIPT_DIR}/010_aux_databases_and_roles.sql"
PSQL=(psql -v ON_ERROR_STOP=1 -U "${DB_USER}")

echo "=== ensure_roles: starting (user=${DB_USER}, db=${MAIN_DB}) ==="

run_bootstrap_sql() {
    local db_name="$1"
    local drop_legacy_roles="$2"

    if [[ ! -f "${SQL_FILE}" ]]; then
        echo "  [fail] Missing SQL bootstrap file: ${SQL_FILE}"
        exit 1
    fi

    "${PSQL[@]}" \
        -d "${db_name}" \
        -v main_db="${MAIN_DB}" \
        -v drop_legacy_roles="${drop_legacy_roles}" \
        -f "${SQL_FILE}"
    echo "  [ok] Bootstrap SQL applied to '${db_name}'."
}

run_bootstrap_sql "${MAIN_DB}" false

# Legacy: clean up ownership in prefect_db if it still exists from the
# pre-Dagster era. Skipped silently on fresh installs.
if "${PSQL[@]}" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='prefect_db'" | grep -q 1; then
    run_bootstrap_sql "prefect_db" false
fi

run_bootstrap_sql "dagster_db" true

echo "=== ensure_roles: done ==="
