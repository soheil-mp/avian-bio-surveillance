#!/usr/bin/env bash
# Recover the local MinIO service when Docker marks it unhealthy or exec wedges.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
compose_cmd=(bash "${repo_root}/infrastructure/docker/compose.sh")

http_timeout_seconds="${MINIO_RECOVERY_HTTP_TIMEOUT_SECONDS:-10}"
exec_timeout_seconds="${MINIO_RECOVERY_EXEC_TIMEOUT_SECONDS:-10}"
docker_timeout_seconds="${MINIO_RECOVERY_DOCKER_TIMEOUT_SECONDS:-60}"
compose_timeout_seconds="${MINIO_RECOVERY_COMPOSE_TIMEOUT_SECONDS:-180}"
wait_seconds="${MINIO_RECOVERY_WAIT_SECONDS:-90}"
mode="recover"
force_recreate="false"

usage() {
  cat <<'USAGE'
Usage: scripts/ops/recover_minio.sh [--check] [--force]

Checks the Compose-managed MinIO service using Docker health, the published
HTTP readiness endpoint, and `mc ready local` via docker exec.

Options:
  --check   Only report health; do not recreate the service.
  --force   Recreate MinIO even if the checks are currently healthy.

The recovery path removes/recreates only the MinIO container. The MinIO data
path is preserved.
USAGE
}

while (($#)); do
  case "$1" in
    --check)
      mode="check"
      ;;
    --force)
      force_recreate="true"
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

current_container_id() {
  timeout "$docker_timeout_seconds" "${compose_cmd[@]}" ps -q minio 2>/dev/null | head -n 1 || true
}

docker_health_state() {
  local container_id="$1"
  timeout "$docker_timeout_seconds" docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true
}

container_name() {
  local container_id="$1"
  timeout "$docker_timeout_seconds" docker inspect -f '{{.Name}}' "$container_id" 2>/dev/null | sed 's#^/##' || true
}

readiness_url() {
  local container_id="$1"
  local published_api
  published_api="$(timeout "$docker_timeout_seconds" docker port "$container_id" 9000/tcp 2>/dev/null | head -n 1 || true)"
  if [[ -n "$published_api" ]]; then
    echo "http://127.0.0.1:${published_api##*:}/minio/health/ready"
  else
    echo "http://127.0.0.1:${MINIO_API_PORT:-19000}/minio/health/ready"
  fi
}

http_ready() {
  local url="$1"
  curl -fsS -m "$http_timeout_seconds" "$url" >/dev/null
}

exec_ready() {
  local container_id="$1"
  timeout "$exec_timeout_seconds" docker exec "$container_id" sh -lc 'mc ready local >/dev/null 2>&1'
}

print_state() {
  local container_id="$1"
  local health_state="$2"
  local url="$3"
  echo "container=$(container_name "$container_id") id=${container_id:0:12} docker_health=$health_state url=$url"
}

checks_pass() {
  local container_id="$1"
  local health_state="$2"
  local url="$3"

  [[ "$health_state" == "healthy" ]] || return 1
  http_ready "$url" || return 1
  exec_ready "$container_id" || return 1
}

report_checks() {
  local container_id="$1"
  local health_state="$2"
  local url="$3"
  local http_result="not-ready"
  local exec_result="not-ready"

  if http_ready "$url"; then
    http_result="ready"
  fi
  if exec_ready "$container_id"; then
    exec_result="ready"
  fi
  print_state "$container_id" "$health_state" "$url"
  echo "http=$http_result exec=$exec_result"
}

recover_minio() {
  local container_id="$1"
  echo "Recreating MinIO service container; the MinIO data path is preserved."
  if [[ -n "$container_id" ]]; then
    if ! timeout "$docker_timeout_seconds" docker rm -f "$container_id" >/dev/null; then
      echo "Docker did not remove MinIO within ${docker_timeout_seconds}s." >&2
      echo "Restart Docker Desktop or the Docker daemon, then rerun this script." >&2
      return 1
    fi
  fi
  if ! timeout "$compose_timeout_seconds" "${compose_cmd[@]}" up -d --no-build --no-deps minio; then
    echo "Compose did not recreate MinIO within ${compose_timeout_seconds}s." >&2
    echo "Restart Docker Desktop or the Docker daemon, then rerun this script." >&2
    return 1
  fi
}

wait_for_recovery() {
  local deadline=$((SECONDS + wait_seconds))
  local container_id
  local health_state
  local url

  while ((SECONDS < deadline)); do
    container_id="$(current_container_id)"
    if [[ -n "$container_id" ]]; then
      health_state="$(docker_health_state "$container_id")"
      url="$(readiness_url "$container_id")"
      if checks_pass "$container_id" "$health_state" "$url"; then
        report_checks "$container_id" "$health_state" "$url"
        return 0
      fi
    fi
    sleep 5
  done

  container_id="$(current_container_id)"
  if [[ -n "$container_id" ]]; then
    health_state="$(docker_health_state "$container_id")"
    url="$(readiness_url "$container_id")"
    report_checks "$container_id" "$health_state" "$url" || true
  else
    echo "MinIO container was not created." >&2
  fi
  return 1
}

main() {
  local container_id
  local health_state
  local url

  cd "$repo_root"
  container_id="$(current_container_id)"
  if [[ -z "$container_id" ]]; then
    echo "MinIO service container is not present."
    if [[ "$mode" == "check" ]]; then
      return 1
    fi
    recover_minio ""
    wait_for_recovery
    return $?
  fi

  health_state="$(docker_health_state "$container_id")"
  url="$(readiness_url "$container_id")"
  report_checks "$container_id" "$health_state" "$url"

  if [[ "$mode" == "check" ]]; then
    checks_pass "$container_id" "$health_state" "$url"
    return $?
  fi

  if [[ "$force_recreate" != "true" ]] && checks_pass "$container_id" "$health_state" "$url"; then
    echo "MinIO is healthy; no recovery needed."
    return 0
  fi

  recover_minio "$container_id"
  wait_for_recovery
}

main "$@"
