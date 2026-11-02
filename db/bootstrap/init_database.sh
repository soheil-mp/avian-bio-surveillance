#!/usr/bin/env bash
# =============================================================================
# init_database.sh -- Single-source idempotent bootstrap for TimescaleDB
# =============================================================================
# Sole entry point for applying the entire `db/` tree to a live database.
# Designed to run on every container boot (idempotent re-application — closes
# the gap that PostgreSQL's `/docker-entrypoint-initdb.d/` only runs on empty
# volumes).
#
# Run by:
#   * `infrastructure/docker/docker-compose.yml`   -> service `timescaledb-init`
#   * `infrastructure/singularity/start.sh`        -> equivalent init step
#
# Required env: PGHOST, PGPORT, PGUSER, PGPASSWORD, POSTGRES_DB.
#
# Folder ownership (executed in this order):
#   1. db/bootstrap/                       auxiliary databases + legacy-role cleanup
#   2. db/baseline/                        canonical idempotent DDL
#   3. db/functions/020_fn_point_to_cell.sql
#                                          table-dependent helper requiring grid_cells_tbl
#   4. db/migrations/                      versioned, recorded in schema_migrations_tbl
#   5. db/triggers/                        cross-table trigger fn + binding
#   6. db/routines/                        stateful refresh procedures
#   7. db/caggs/                           continuous aggregates
#   8. db/views/                           read-only views
#   9. db/grants/                          object-level grants
#  10. post-bootstrap                      first-deploy cagg materialisation
# =============================================================================

set -euo pipefail

SCHEMA_ROOT="${SCHEMA_ROOT:-/init/db}"
MAIN_DB="${POSTGRES_DB:-avian_biosurveillance}"
PSQL=(psql -v ON_ERROR_STOP=1 -U "${PGUSER}" -d "${MAIN_DB}")

echo "=== init_database: bootstrap starting (db=${MAIN_DB}, root=${SCHEMA_ROOT}) ==="

# TimescaleDB's built-in 000_install_timescaledb.sh restarts PostgreSQL after
# loading the extension. Wait up to 60s for the server to accept connections.
echo "--- waiting for PostgreSQL to accept connections ---"
for i in $(seq 1 30); do
    if psql -U "${PGUSER}" -d "${MAIN_DB}" -c "SELECT 1" > /dev/null 2>&1; then
        echo "  [ok] PostgreSQL ready (attempt ${i})"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "  [fail] PostgreSQL not ready after 60s — aborting"
        exit 1
    fi
    echo "  [wait] attempt ${i}/30, retrying in 2s..."
    sleep 2
done

schema_bootstrap_ready() {
    local ready
    ready=$(
        "${PSQL[@]}" -tAX <<'EOSQL'
SELECT
    to_regclass('public.schema_migrations_tbl') IS NOT NULL
    AND to_regclass('public.species_tracker_tbl') IS NOT NULL
    AND to_regclass('public.idx_spt_natural_key') IS NOT NULL
    AND to_regprocedure('refresh_species_tracker()') IS NOT NULL
    AND to_regclass('public.birdweather_daily_cagg_tbl') IS NOT NULL
    AND to_regclass('public.knmi_daily_cagg_tbl') IS NOT NULL;
EOSQL
    )
    [[ "${ready}" == "t" ]]
}

other_client_backends() {
    "${PSQL[@]}" -tAX <<'EOSQL'
SELECT count(*)
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND backend_type = 'client backend';
EOSQL
}

if schema_bootstrap_ready; then
    live_clients="$(other_client_backends)"
    if [[ "${live_clients}" != "0" ]]; then
        echo "--- bootstrap guard: schema already present; skipping replay because ${live_clients} other client backend(s) are connected ---"
        echo "  [skip] Re-run bootstrap after application traffic drains or after a TimescaleDB restart."
        exit 0
    fi
fi

apply_sql() {
    local label="$1"
    local path="$2"
    if [[ ! -f "${path}" ]]; then
        echo "  [skip] ${label} (missing: ${path})"
        return
    fi
    echo "  [run ] ${label}  <-  ${path#${SCHEMA_ROOT}/}"
    "${PSQL[@]}" -f "${path}"
}

apply_dir() {
    local label="$1"
    local dir="$2"
    if [[ ! -d "${dir}" ]]; then
        echo "  [skip] ${label} (missing dir: ${dir})"
        return
    fi
    shopt -s nullglob
    local files=("${dir}"/*.sql)
    shopt -u nullglob
    if [[ ${#files[@]} -eq 0 ]]; then
        echo "  [skip] ${label} (empty dir: ${dir})"
        return
    fi
    # Lexicographic order matches the numeric prefixes used in each folder.
    IFS=$'\n' files=($(sort <<<"${files[*]}")); unset IFS
    for f in "${files[@]}"; do
        apply_sql "${label}: $(basename "${f}")" "${f}"
    done
}

# -- 1. Auxiliary databases + legacy-role cleanup ------------------------------
echo "--- step 1/10: auxiliary databases & legacy-role cleanup ---"
bash "${SCHEMA_ROOT}/bootstrap/ensure_aux_roles.sh"

# -- 2. Baseline canonical DDL ------------------------------------------------
echo "--- step 2/10: baseline DDL ---"
apply_dir "baseline" "${SCHEMA_ROOT}/baseline"

# -- 3. Post-baseline helper functions ----------------------------------------
# fn_point_to_cell() binds to grid_cells_tbl, so it must be created only after
# the baseline schema has established that table.
echo "--- step 3/10: post-baseline helper functions ---"
apply_sql \
    "functions: 020_fn_point_to_cell.sql" \
    "${SCHEMA_ROOT}/functions/020_fn_point_to_cell.sql"

# -- 4. Migrations (recorded in schema_migrations_tbl) ------------------------
echo "--- step 4/10: migrations ---"
"${PSQL[@]}" <<'EOSQL'
CREATE TABLE IF NOT EXISTS schema_migrations_tbl (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filename    TEXT        NOT NULL
);
COMMENT ON TABLE schema_migrations_tbl IS
    'Records which db/migrations/V*.sql files have been applied. Each row is one one-way migration.';
EOSQL

if [[ -d "${SCHEMA_ROOT}/migrations" ]]; then
    shopt -s nullglob
    migration_files=("${SCHEMA_ROOT}/migrations"/V*.sql)
    shopt -u nullglob
    IFS=$'\n' migration_files=($(sort <<<"${migration_files[*]}")); unset IFS

    for f in "${migration_files[@]}"; do
        version="$(basename "${f}" .sql)"
        filename="$(basename "${f}")"
        if [[ ! "${version}" =~ ^V[0-9]{3,}__[a-z0-9_]+$ ]]; then
            echo "  [fail] invalid migration name: ${filename}"
            exit 1
        fi
        applied=$("${PSQL[@]}" -tAX -c "SELECT 1 FROM schema_migrations_tbl WHERE version = '${version}' LIMIT 1;")
        if [[ "${applied}" == "1" ]]; then
            echo "  [skip] migration ${version} (already applied)"
            continue
        fi
        echo "  [run ] migration ${version}  <-  migrations/${filename}"
        "${PSQL[@]}" --single-transaction \
            -f "${f}" \
            -c "INSERT INTO schema_migrations_tbl (version, filename) VALUES ('${version}', '${filename}');"
    done
fi

# -- 5. Triggers (function + binding co-located) ------------------------------
echo "--- step 5/10: triggers ---"
apply_dir "triggers" "${SCHEMA_ROOT}/triggers"

# -- 6. Stateful refresh routines (called by Dagster) -------------------------
echo "--- step 6/10: routines ---"
apply_dir "routines" "${SCHEMA_ROOT}/routines"

# -- 7. Continuous aggregates --------------------------------------------------
echo "--- step 7/10: continuous aggregates ---"
apply_dir "caggs" "${SCHEMA_ROOT}/caggs"

# -- 8. Views -----------------------------------------------------------------
echo "--- step 8/10: views ---"
apply_dir "views"  "${SCHEMA_ROOT}/views"

# -- 9. Grants ----------------------------------------------------------------
echo "--- step 9/10: grants ---"
apply_dir "grants" "${SCHEMA_ROOT}/grants"

# -- 10. First-deploy cagg materialisation ------------------------------------
# Continuous aggregates are created WITH NO DATA so the schema bootstrap stays
# fast on empty volumes. The hourly refresh policy only covers the last 3 days,
# so historical buckets would stay blank until manually backfilled. To close
# that gap we materialise any cagg that is currently empty *and* whose source
# hypertable already has rows.
echo "--- step 10/10: materialising empty continuous aggregates ---"
materialise_cagg() {
    local cagg="$1"
    local source_tbl="$2"
    local has_rows
    has_rows=$("${PSQL[@]}" -tAX -c "
        SELECT EXISTS (SELECT 1 FROM ${source_tbl} LIMIT 1)
           AND NOT EXISTS (SELECT 1 FROM ${cagg}      LIMIT 1);
    ")
    if [[ "${has_rows}" == "t" ]]; then
        echo "  [run ] refresh_continuous_aggregate(${cagg})  (source=${source_tbl})"
        "${PSQL[@]}" -c "CALL refresh_continuous_aggregate('${cagg}', NULL, NOW());"
    else
        echo "  [skip] ${cagg} (already materialised or source ${source_tbl} empty)"
    fi
}
materialise_cagg "birdweather_daily_cagg_tbl" "birdweather_tbl"
materialise_cagg "knmi_daily_cagg_tbl"        "knmi_tbl"

echo "=== init_database: done ==="
