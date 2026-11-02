#!/usr/bin/env bash
# Migrate the full logical TimescaleDB/PostgreSQL cluster between Docker contexts.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

mode="check"
source_context="${TSDB_MIGRATE_SOURCE_CONTEXT:-desktop-linux}"
target_context="${TSDB_MIGRATE_TARGET_CONTEXT:-default}"
source_container="${TSDB_MIGRATE_SOURCE_CONTAINER:-ab-timescaledb}"
target_container="${TSDB_MIGRATE_TARGET_CONTAINER:-ab-timescaledb}"
include_postgres_db="false"
restart_target_stack="false"
selected_databases=()

source_app_containers=(
  ab-ingestion
  ab-model
  ab-dagster-webserver
  ab-dagster-daemon
  ab-frontend
  ab-autoheal
)

target_app_containers=(
  ab-ingestion
  ab-model
  ab-dagster-webserver
  ab-dagster-daemon
  ab-frontend
  ab-autoheal
)

stopped_source_containers=()
stopped_target_containers=()

usage() {
  cat <<'USAGE'
Usage: scripts/ops/migrate_timescaledb_cluster.sh [--check] [--apply] [options]

Logically migrate the application PostgreSQL/TimescaleDB databases from one
Docker context to another. The default flow migrates from Docker Desktop
(`desktop-linux`) into the native Docker Engine (`default`) so the stack can
keep the historical database while running GPU workloads on the native daemon.

Modes:
  --check                 Report source/target versions, database list, and app-container state (default).
  --apply                 Stop DB-writer app containers, stream pg_dump -> pg_restore for each database, and leave the source apps stopped.

Options:
  --source-context NAME   Source Docker context. Default: desktop-linux
  --target-context NAME   Target Docker context. Default: default
  --source-container NAME Source TimescaleDB container name. Default: ab-timescaledb
  --target-container NAME Target TimescaleDB container name. Default: ab-timescaledb
  --db NAME               Migrate only the named database. Repeat to select multiple.
  --include-postgres-db   Include the postgres maintenance DB in the default database list.
  --restart-target-stack  After restore, run `bash infrastructure/docker/compose.sh up -d` on the target stack.
  -h, --help              Show this help.

Notes:
  - By default the helper migrates all non-template application databases except `postgres`.
    In this repo that means `avian_biosurveillance` and `dagster_db`.
  - The migration is downtime-sensitive: source app containers are stopped before dump
    so the source data stays consistent while the dump streams.
  - Source DB containers are left running. Source app containers remain stopped on success
    to avoid split-brain writes after the target stack is promoted.
USAGE
}

while (($#)); do
  case "$1" in
    --check)
      mode="check"
      ;;
    --apply)
      mode="apply"
      ;;
    --source-context)
      shift
      source_context="${1:-}"
      if [[ -z "$source_context" ]]; then
        echo "--source-context requires a value." >&2
        exit 2
      fi
      ;;
    --target-context)
      shift
      target_context="${1:-}"
      if [[ -z "$target_context" ]]; then
        echo "--target-context requires a value." >&2
        exit 2
      fi
      ;;
    --source-container)
      shift
      source_container="${1:-}"
      if [[ -z "$source_container" ]]; then
        echo "--source-container requires a value." >&2
        exit 2
      fi
      ;;
    --target-container)
      shift
      target_container="${1:-}"
      if [[ -z "$target_container" ]]; then
        echo "--target-container requires a value." >&2
        exit 2
      fi
      ;;
    --db)
      shift
      selected_databases+=("${1:-}")
      if [[ -z "${selected_databases[-1]}" ]]; then
        echo "--db requires a value." >&2
        exit 2
      fi
      ;;
    --include-postgres-db)
      include_postgres_db="true"
      ;;
    --restart-target-stack)
      restart_target_stack="true"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

docker_ctx() {
  local context="$1"
  shift
  docker --context "$context" "$@"
}

container_exists() {
  local context="$1"
  local container="$2"
  docker_ctx "$context" container inspect "$container" >/dev/null 2>&1
}

container_running() {
  local context="$1"
  local container="$2"
  [[ "$(docker_ctx "$context" inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" == "true" ]]
}

ensure_context() {
  local context="$1"
  if ! docker context inspect "$context" >/dev/null 2>&1; then
    echo "Docker context not found: $context" >&2
    exit 1
  fi
}

ensure_container_running() {
  local context="$1"
  local container="$2"
  if ! container_exists "$context" "$container"; then
    echo "Container not found in context '$context': $container" >&2
    exit 1
  fi
  if ! container_running "$context" "$container"; then
    echo "Starting $container in context '$context'."
    docker_ctx "$context" start "$container" >/dev/null
  fi
}

container_env() {
  local context="$1"
  local container="$2"
  local name="$3"
  docker_ctx "$context" exec -e ENV_NAME="$name" "$container" sh -lc 'printenv "$ENV_NAME"'
}

run_psql_raw() {
  local context="$1"
  local container="$2"
  local database="$3"
  local sql="$4"
  docker_ctx "$context" exec \
    -e DB_NAME="$database" \
    -e SQL_QUERY="$sql" \
    "$container" \
    sh -lc 'psql -X -U "$POSTGRES_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -Atqc "$SQL_QUERY"'
}

sql_literal() {
  printf "%s" "${1//\'/\'\'}"
}

sql_identifier() {
  printf '"%s"' "${1//\"/\"\"}"
}

list_databases() {
  local context="$1"
  local container="$2"
  local bootstrap_db="$3"
  local sql="select datname from pg_database where datallowconn and not datistemplate"
  if [[ "$include_postgres_db" != "true" && ${#selected_databases[@]} -eq 0 ]]; then
    sql+=" and datname <> 'postgres'"
  fi
  sql+=" order by 1"
  run_psql_raw "$context" "$container" "$bootstrap_db" "$sql"
}

list_database_sizes() {
  local context="$1"
  local container="$2"
  local bootstrap_db="$3"
  local sql="select datname || ' (' || pg_size_pretty(pg_database_size(datname)) || ')' from pg_database where datallowconn and not datistemplate"
  if [[ "$include_postgres_db" != "true" && ${#selected_databases[@]} -eq 0 ]]; then
    sql+=" and datname <> 'postgres'"
  fi
  sql+=" order by 1"
  run_psql_raw "$context" "$container" "$bootstrap_db" "$sql"
}

list_database_extensions() {
  local context="$1"
  local container="$2"
  local database="$3"
  run_psql_raw \
    "$context" \
    "$container" \
    "$database" \
    "select extname from pg_extension where extname <> 'plpgsql' order by case when extname = 'timescaledb' then 0 else 1 end, extname"
}

target_extension_available() {
  local extension="$1"
  local escaped_extension
  escaped_extension="$(sql_literal "$extension")"
  run_psql_raw \
    "$target_context" \
    "$target_container" \
    postgres \
    "select count(*) from pg_available_extensions where name = '${escaped_extension}';"
}

create_target_extension() {
  local database="$1"
  local extension="$2"
  local extension_identifier
  extension_identifier="$(sql_identifier "$extension")"
  run_psql_raw \
    "$target_context" \
    "$target_container" \
    "$database" \
    "create extension if not exists ${extension_identifier};"
}

run_timescaledb_restore_hook() {
  local database="$1"
  local hook="$2"
  run_psql_raw \
    "$target_context" \
    "$target_container" \
    "$database" \
    "select timescaledb_${hook}_restore();"
}

selected_or_default_databases() {
  if ((${#selected_databases[@]})); then
    printf '%s\n' "${selected_databases[@]}"
  else
    list_databases "$source_context" "$source_container" "$source_bootstrap_db"
  fi
}

print_running_containers() {
  local context="$1"
  shift
  local container
  for container in "$@"; do
    if container_running "$context" "$container"; then
      echo "  $container"
    fi
  done
}

stop_named_containers() {
  local context="$1"
  local list_name="$2"
  local out_name="$3"
  local -n container_list_ref="$list_name"
  local -n stopped_ref="$out_name"
  local container
  for container in "${container_list_ref[@]}"; do
    if container_running "$context" "$container"; then
      echo "Stopping $container in context '$context'."
      docker_ctx "$context" stop "$container" >/dev/null
      stopped_ref+=("$container")
    fi
  done
}

terminate_target_connections() {
  local database="$1"
  local escaped_database
  escaped_database="$(sql_literal "$database")"
  run_psql_raw \
    "$target_context" \
    "$target_container" \
    postgres \
    "select pg_terminate_backend(pid) from pg_stat_activity where datname = '${escaped_database}' and pid <> pg_backend_pid();"
}

prepare_target_database() {
  local database="$1"
  local database_identifier
  database_identifier="$(sql_identifier "$database")"

  terminate_target_connections "$database" >/dev/null || true

  if [[ "$database" == "postgres" ]]; then
    return 0
  fi

  run_psql_raw \
    "$target_context" \
    "$target_container" \
    postgres \
    "drop database if exists ${database_identifier} with (force);"
  run_psql_raw \
    "$target_context" \
    "$target_container" \
    postgres \
    "create database ${database_identifier};"
}

migrate_database() {
  local database="$1"
  local extension
  local has_timescaledb="false"
  local -a source_extensions=()

  echo "Migrating database: $database"
  mapfile -t source_extensions < <(list_database_extensions "$source_context" "$source_container" "$database")

  for extension in "${source_extensions[@]}"; do
    if [[ "$(target_extension_available "$extension")" != "1" ]]; then
      echo "Required extension '$extension' is not available in target context '$target_context'." >&2
      return 1
    fi
  done

  prepare_target_database "$database"

  for extension in "${source_extensions[@]}"; do
    create_target_extension "$database" "$extension"
    if [[ "$extension" == "timescaledb" ]]; then
      has_timescaledb="true"
    fi
  done

  if [[ "$has_timescaledb" == "true" ]]; then
    run_timescaledb_restore_hook "$database" pre
  fi

  if ! docker_ctx "$source_context" exec \
      -e DB_NAME="$database" \
      "$source_container" \
      sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$DB_NAME" --format=custom --no-owner --no-privileges' \
      | docker_ctx "$target_context" exec -i \
          -e DB_NAME="$database" \
          "$target_container" \
          sh -lc 'pg_restore -U "$POSTGRES_USER" -d "$DB_NAME" --exit-on-error --no-owner --no-privileges'; then
    if [[ "$has_timescaledb" == "true" ]]; then
      run_timescaledb_restore_hook "$database" post >/dev/null 2>&1 || true
    fi
    return 1
  fi

  if [[ "$has_timescaledb" == "true" ]]; then
    run_timescaledb_restore_hook "$database" post
  fi
}

check_database_exists_on_target() {
  local database="$1"
  local escaped_database
  escaped_database="$(sql_literal "$database")"
  run_psql_raw \
    "$target_context" \
    "$target_container" \
    postgres \
    "select count(*) from pg_database where datname = '${escaped_database}';"
}

report_failure() {
  local exit_code="$?"
  if ((exit_code == 0)); then
    return 0
  fi

  echo >&2
  echo "Migration aborted." >&2
  if ((${#stopped_source_containers[@]})); then
    echo "Source app containers left stopped: ${stopped_source_containers[*]}" >&2
  fi
  if ((${#stopped_target_containers[@]})); then
    echo "Target app containers left stopped: ${stopped_target_containers[*]}" >&2
  fi
  echo "Source data remains intact in Docker context '$source_context'." >&2
}

trap report_failure EXIT

cd "$repo_root"

ensure_context "$source_context"
ensure_context "$target_context"

if [[ "$source_context" == "$target_context" ]]; then
  echo "Source and target Docker contexts must differ." >&2
  exit 1
fi

ensure_container_running "$source_context" "$source_container"
ensure_container_running "$target_context" "$target_container"

source_bootstrap_db="$(container_env "$source_context" "$source_container" POSTGRES_DB)"
target_bootstrap_db="$(container_env "$target_context" "$target_container" POSTGRES_DB)"

if [[ -z "$source_bootstrap_db" || -z "$target_bootstrap_db" ]]; then
  echo "Unable to read POSTGRES_DB from the source or target container." >&2
  exit 1
fi

mapfile -t databases < <(selected_or_default_databases)

if ((${#databases[@]} == 0)); then
  echo "No databases selected for migration." >&2
  exit 1
fi

case "$mode" in
  check)
    echo "Source context: $source_context ($source_container)"
    docker_ctx "$source_context" exec "$source_container" sh -lc 'psql --version'
    echo
    echo "Target context: $target_context ($target_container)"
    docker_ctx "$target_context" exec "$target_container" sh -lc 'psql --version'
    echo
    echo "Databases selected for migration:"
    printf '  %s\n' "${databases[@]}"
    echo
    echo "Source database sizes:"
    list_database_sizes "$source_context" "$source_container" "$source_bootstrap_db" | sed 's/^/  /'
    echo
    echo "Source app containers that will be stopped during --apply:"
    print_running_containers "$source_context" "${source_app_containers[@]}"
    echo
    echo "Target app containers that will be stopped during --apply:"
    print_running_containers "$target_context" "${target_app_containers[@]}"
    ;;
  apply)
    echo "Stopping source app containers to freeze writes."
    stop_named_containers "$source_context" source_app_containers stopped_source_containers
    echo "Stopping target app containers before restore."
    stop_named_containers "$target_context" target_app_containers stopped_target_containers

    ensure_container_running "$source_context" "$source_container"
    ensure_container_running "$target_context" "$target_container"

    local_database_count=0
    for database in "${databases[@]}"; do
      migrate_database "$database"
      local_database_count=$((local_database_count + 1))
    done

    echo
    echo "Migrated $local_database_count database(s) into context '$target_context'."
    echo "Target databases now present:"
    for database in "${databases[@]}"; do
      if [[ "$(check_database_exists_on_target "$database")" == "1" ]]; then
        echo "  $database"
      fi
    done

    if printf '%s\n' "${databases[@]}" | grep -qx 'avian_biosurveillance'; then
      echo
      echo "BirdWeather hypertable metadata on target:"
      docker_ctx "$target_context" exec "$target_container" sh -lc \
        'psql -U "$POSTGRES_USER" -d avian_biosurveillance -P pager=off -c "select hypertable_schema, hypertable_name, num_chunks from timescaledb_information.hypertables where hypertable_name = '\''birdweather_tbl'\'';" && psql -U "$POSTGRES_USER" -d avian_biosurveillance -P pager=off -c "select * from hypertable_detailed_size('\''public.birdweather_tbl'\'');"'
    fi

    echo
    echo "Source app containers were left stopped on context '$source_context' to avoid split-brain writes."
    if [[ "$restart_target_stack" == "true" ]]; then
      echo "Starting target stack."
      bash infrastructure/docker/compose.sh up -d
    else
      echo "Bring the target stack up when ready:"
      echo "  bash infrastructure/docker/compose.sh up -d"
    fi
    ;;
esac