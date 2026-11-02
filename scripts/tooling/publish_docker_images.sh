#!/usr/bin/env bash
# =============================================================================
# publish_docker_images.sh — Build, tag, and push selected Docker images.
# =============================================================================
# Manual helper for publishing the project's custom images to a Docker Hub
# namespace without relying on CI/CD. Builds the requested services with
# `docker compose build`, derives a tag set from Git branch + short SHA
# (or explicit --tag values), tags every source image as
# `<namespace>/avian-biosurveillance-<service>:<tag>`, and pushes.
#
# Run `docker login` before invoking; this script does not handle
# credentials. Pre-create the destination repositories on Docker Hub as
# private if you want private visibility.
#
# Default target repositories:
#   <namespace>/avian-biosurveillance-frontend
#   <namespace>/avian-biosurveillance-ingestion
#   <namespace>/avian-biosurveillance-model
#   <namespace>/avian-biosurveillance-dagster
#
# Usage:
#   ./scripts/tooling/publish_docker_images.sh --namespace soheilmp
#   ./scripts/tooling/publish_docker_images.sh -n soheilmp --release
#   ./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend,ingestion --also-latest
#   ./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend -t demo-2026-03-31 --skip-build
#   ./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend,ingestion,model,dagster -t 2026-03-31 --release
#   ./scripts/tooling/publish_docker_images.sh -n soheilmp --dry-run
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# -----------------------------------------------------------------------------
# TTY output helpers.
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    GREEN='' RED='' YELLOW='' BLUE='' NC=''
fi

step() { printf "\n${BLUE}▸ %s${NC}\n" "$*"; }
ok()   { printf "${GREEN}  ✔ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  ⚠ %s${NC}\n" "$*"; }
fail() { printf "${RED}  ✖ %s${NC}\n" "$*" >&2; }
die()  { fail "$*"; exit 1; }

print_help() {
cat <<'EOF'
publish_docker_images.sh — Build, tag, and push selected Docker images.

Usage:
  ./scripts/tooling/publish_docker_images.sh -n <namespace> [options]

Required:
  -n, --namespace <name>        Docker Hub namespace.

Selection:
  -s, --service <list>          Comma-separated services. Default:
                                  frontend,ingestion,model,dagster.
  -t, --tag <value>             Explicit image tag. May be passed multiple
                                  times or as a comma-separated list. When
                                  omitted, tags default to the current Git
                                  branch and a `sha-<short-sha>` derived tag.

Build:
      --compose-file <path>     Default: infrastructure/docker/docker-compose.yml
      --env-file <path>         Default: .env
      --runtime-config <path>   Default: config/runtime.env
      --skip-build              Reuse existing local images instead of
                                  building them first.
      --no-cache                Pass --no-cache to `docker compose build`.

Tagging:
      --also-latest             Also push the mutable `latest` tag.
      --release                 Alias for --also-latest (release build).

Execution:
      --dry-run                 Print the build/tag/push actions without
                                  invoking docker.
  -h, --help                    Show this help and exit.

Notes:
  - Run `docker login` first; credentials are not handled here.
  - The destination repositories should already exist on Docker Hub if
    you want private visibility.
EOF
}

# -----------------------------------------------------------------------------
# Defaults.
# -----------------------------------------------------------------------------
NAMESPACE=""
SERVICES_INPUT="frontend,ingestion,model,dagster"
TAGS_INPUT=""
COMPOSE_FILE="infrastructure/docker/docker-compose.yml"
ENV_FILE=".env"
RUNTIME_CONFIG="config/runtime.env"
SKIP_BUILD=false
NO_CACHE=false
ALSO_LATEST=false
RELEASE=false
DRY_RUN=false

# -----------------------------------------------------------------------------
# Argument parsing.
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case $1 in
        -n|--namespace)    NAMESPACE="$2"; shift 2 ;;
        -s|--service)      SERVICES_INPUT="$2"; shift 2 ;;
        -t|--tag)
            if [ -z "$TAGS_INPUT" ]; then
                TAGS_INPUT="$2"
            else
                TAGS_INPUT="$TAGS_INPUT,$2"
            fi
            shift 2 ;;
        --compose-file)    COMPOSE_FILE="$2"; shift 2 ;;
        --env-file)        ENV_FILE="$2"; shift 2 ;;
        --runtime-config)  RUNTIME_CONFIG="$2"; shift 2 ;;
        --skip-build)      SKIP_BUILD=true; shift ;;
        --no-cache)        NO_CACHE=true; shift ;;
        --also-latest)     ALSO_LATEST=true; shift ;;
        --release)         RELEASE=true; ALSO_LATEST=true; shift ;;
        --dry-run)         DRY_RUN=true; shift ;;
        -h|--help)         print_help; exit 0 ;;
        *)                 die "Unknown option: $1 (use --help)" ;;
    esac
done

[ -n "$NAMESPACE" ] || die "Missing required --namespace (use --help for usage)"
[ -f "$COMPOSE_FILE" ] || die "Compose file not found: $COMPOSE_FILE"
[ -f "$ENV_FILE" ] || die "Env file not found: $ENV_FILE"
[ -f "$RUNTIME_CONFIG" ] || die "Runtime config file not found: $RUNTIME_CONFIG"

# -----------------------------------------------------------------------------
# Service catalogue. Bash 4+ associative arrays.
# -----------------------------------------------------------------------------
declare -A SOURCE_IMAGE=(
    [frontend]="avian-biosurveillance/frontend:0.1.0"
    [ingestion]="avian-biosurveillance/ingestion:0.1.0"
    [model]="avian-biosurveillance/model:0.1.0"
    [dagster]="avian-biosurveillance/dagster:1.9"
)
declare -A REPO_SUFFIX=(
    [frontend]="frontend"
    [ingestion]="ingestion"
    [model]="model"
    [dagster]="dagster"
)
SUPPORTED_SERVICES="frontend, ingestion, model, dagster"

IFS=',' read -ra REQUESTED_SERVICES <<< "$SERVICES_INPUT"
# Trim whitespace from each entry.
for i in "${!REQUESTED_SERVICES[@]}"; do
    REQUESTED_SERVICES[$i]="$(echo "${REQUESTED_SERVICES[$i]}" | tr -d '[:space:]')"
done
[ ${#REQUESTED_SERVICES[@]} -gt 0 ] || die "At least one service must be specified."

for svc in "${REQUESTED_SERVICES[@]}"; do
    [ -n "${SOURCE_IMAGE[$svc]:-}" ] || die "Unsupported service '$svc'. Supported: $SUPPORTED_SERVICES"
done

# -----------------------------------------------------------------------------
# Tag normalisation: lowercase, replace non-[a-z0-9._-] with -, collapse
# repeated dashes, trim leading/trailing dashes.
# -----------------------------------------------------------------------------
normalize_tag() {
    local raw="$1"
    local normalized
    normalized=$(echo "$raw" | tr '[:upper:]' '[:lower:]')
    normalized=$(echo "$normalized" | sed -E 's/[^a-z0-9._-]+/-/g; s/-{2,}/-/g; s/^-+//; s/-+$//')
    if [ -z "$normalized" ]; then
        die "Could not normalize tag value '$raw'"
    fi
    printf "%s" "$normalized"
}

# -----------------------------------------------------------------------------
# Build the tag list (immutable tags first, then `latest` if requested).
# -----------------------------------------------------------------------------
declare -a TAGS_TO_PUBLISH=()
if [ -n "$TAGS_INPUT" ]; then
    IFS=',' read -ra RAW_TAGS <<< "$TAGS_INPUT"
    for t in "${RAW_TAGS[@]}"; do
        t="$(echo "$t" | tr -d '[:space:]')"
        [ -n "$t" ] && TAGS_TO_PUBLISH+=("$(normalize_tag "$t")")
    done
else
    git_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")
    git_sha=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)
    TAGS_TO_PUBLISH+=("$(normalize_tag "$git_branch")")
    TAGS_TO_PUBLISH+=("$(normalize_tag "sha-$git_sha")")
fi

if $ALSO_LATEST; then
    TAGS_TO_PUBLISH+=("latest")
fi

# De-duplicate while preserving order.
declare -a UNIQUE_TAGS=()
declare -A SEEN_TAGS=()
for t in "${TAGS_TO_PUBLISH[@]}"; do
    if [ -z "${SEEN_TAGS[$t]:-}" ]; then
        UNIQUE_TAGS+=("$t")
        SEEN_TAGS[$t]=1
    fi
done
TAGS_TO_PUBLISH=("${UNIQUE_TAGS[@]}")

# -----------------------------------------------------------------------------
# Plan summary.
# -----------------------------------------------------------------------------
step "Publish plan"
printf "  Namespace : %s\n" "$NAMESPACE"
printf "  Services  : %s\n" "$(IFS=,; echo "${REQUESTED_SERVICES[*]}")"
printf "  Tags      : %s\n" "$(IFS=,; echo "${TAGS_TO_PUBLISH[*]}")"
if $RELEASE; then
    printf "  Mode      : release (includes latest)\n"
elif $ALSO_LATEST; then
    printf "  Mode      : manual latest alias enabled\n"
else
    printf "  Mode      : immutable tags only\n"
fi
if $SKIP_BUILD; then
    printf "  Build     : skip existing local images\n"
else
    printf "  Build     : build before publish\n"
fi
if $DRY_RUN; then
    printf "  Dry-run   : tag/push steps will be printed but NOT executed\n"
fi

# -----------------------------------------------------------------------------
# Build.
# -----------------------------------------------------------------------------
if ! $SKIP_BUILD; then
    step "Building selected services"
    build_args=(compose --env-file "$ENV_FILE" --env-file "$RUNTIME_CONFIG" -f "$COMPOSE_FILE" build)
    if $NO_CACHE; then
        build_args+=(--no-cache)
    fi
    build_args+=("${REQUESTED_SERVICES[@]}")
    if $DRY_RUN; then
        printf "  (dry-run) docker %s\n" "${build_args[*]}"
    else
        docker "${build_args[@]}"
        ok "Build complete"
    fi
fi

# -----------------------------------------------------------------------------
# Tag and push each service against each tag.
# -----------------------------------------------------------------------------
declare -a PUBLISHED=()
for svc in "${REQUESTED_SERVICES[@]}"; do
    source_image="${SOURCE_IMAGE[$svc]}"
    target_repo="$NAMESPACE/avian-biosurveillance-${REPO_SUFFIX[$svc]}"

    step "Preparing service '$svc'"

    if $DRY_RUN; then
        printf "  (dry-run) would verify local image: %s\n" "$source_image"
    else
        if ! docker image inspect "$source_image" >/dev/null 2>&1; then
            die "Required local image not found: $source_image"
        fi
        ok "Found local image $source_image"
    fi

    for tag in "${TAGS_TO_PUBLISH[@]}"; do
        target_ref="$target_repo:$tag"

        if $DRY_RUN; then
            printf "  (dry-run) docker tag %s %s\n" "$source_image" "$target_ref"
            printf "  (dry-run) docker push %s\n" "$target_ref"
        else
            docker tag "$source_image" "$target_ref"
            ok "Tagged $target_ref"
            docker push "$target_ref"
            ok "Pushed $target_ref"
        fi
        PUBLISHED+=("$target_ref")
    done
done

# -----------------------------------------------------------------------------
# Summary.
# -----------------------------------------------------------------------------
step "Publish summary"
declare -A SEEN_REFS=()
for ref in "${PUBLISHED[@]}"; do
    if [ -z "${SEEN_REFS[$ref]:-}" ]; then
        echo "  $ref"
        SEEN_REFS[$ref]=1
    fi
done

warn "Ensure the destination repositories already exist on Docker Hub and are marked private if you do not want public visibility."
