#!/bin/sh
set -eu

PORT="${PORT:-3000}"
BASE_URL="http://127.0.0.1:${PORT}"
READY_FILE="${APP_READY_FILE:-/app/.runtime/species-tracker-ready}"
DB_READY_SCRIPT='const { Client } = require("pg"); const parsePort = (value, fallback) => { const numeric = Number(value); return Number.isFinite(numeric) ? numeric : fallback; }; const host = process.env.PGHOST || process.env.POSTGRES_HOST; const user = process.env.PGUSER || process.env.POSTGRES_USER || "root"; const password = process.env.PGPASSWORD ?? process.env.POSTGRES_PASSWORD ?? ""; const database = process.env.PGDATABASE || process.env.POSTGRES_DB || "avian_biosurveillance"; const port = parsePort(process.env.PGPORT || process.env.TSDB_PORT, 5432); const connectionString = process.env.DATABASE_URL; const client = new Client(host ? { host, port, user, password, database } : connectionString ? { connectionString } : (() => { process.exit(1); })()); (async () => { try { await client.connect(); await client.query("SELECT 1"); await client.end(); process.exit(0); } catch (error) { try { await client.end(); } catch {} process.exit(1); } })();'

mkdir -p "$(dirname "$READY_FILE")"
rm -f "$READY_FILE"

node server.js &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}

trap cleanup INT TERM

wait_for_http() {
  endpoint="$1"
  attempts="${2:-60}"
  delay_seconds="${3:-1}"
  current_attempt=0

  until wget --quiet --output-document=/dev/null --tries=1 "$endpoint" 2>/dev/null; do
    current_attempt=$((current_attempt + 1))
    if [ "$current_attempt" -ge "$attempts" ]; then
      return 1
    fi
    sleep "$delay_seconds"
  done

  return 0
}

wait_for_postgres() {
  attempts="${1:-60}"
  delay_seconds="${2:-1}"
  current_attempt=0

  until node -e "$DB_READY_SCRIPT"; do
    current_attempt=$((current_attempt + 1))
    if [ "$current_attempt" -ge "$attempts" ]; then
      return 1
    fi
    sleep "$delay_seconds"
  done

  return 0
}

warm_endpoint() {
  endpoint="$1"
  attempts="${2:-20}"
  delay_seconds="${3:-2}"
  current_attempt=0

  until wget --quiet --output-document=/dev/null --tries=1 "$endpoint" 2>/dev/null; do
    current_attempt=$((current_attempt + 1))
    if [ "$current_attempt" -ge "$attempts" ]; then
      echo "warning: warmup failed for $endpoint after $attempts attempts" >&2
      return 1
    fi
    sleep "$delay_seconds"
  done

  return 0
}

wait_for_http "${BASE_URL}/api/health" 60 1 || true

# Wait for the database itself before hitting DB-backed warmup routes.
# This avoids noisy startup-time 500s when Postgres is still accepting
# startup packets but not yet ready to execute queries.
if wait_for_postgres 60 1; then
  # Warm the most expensive cache paths so the first dashboard visitor doesn't.
  warm_endpoint "${BASE_URL}/api/species-tracker" 20 2 || true
  warm_endpoint "${BASE_URL}/api/grid-cells" 20 2 || true
  warm_endpoint "${BASE_URL}/api/earth-engine-overlays" 20 2 || true
  warm_endpoint "${BASE_URL}/alerts" 20 2 || true
else
  echo "warning: database did not become ready in time; skipping DB-backed warmup" >&2
fi
touch "$READY_FILE"

wait "$SERVER_PID"