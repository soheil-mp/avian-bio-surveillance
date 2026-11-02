#!/usr/bin/env bash
# =============================================================================
# code_quality.sh — Run the project's full code-quality stack.
# =============================================================================
# Wraps every quality tool the project is configured for and reports a
# single pass/fail summary. Phases match the tools declared in
# pyproject.toml and .sqlfluff:
#
#   format  — ruff format            (check mode, or writes with --fix)
#   lint    — ruff check             (check mode, or --fix with --fix)
#   type    — mypy                   (strict mode per pyproject.toml)
#   sql     — sqlfluff               (lint mode, or fix with --fix)
#   test    — pytest --cov           (enforces the 80% coverage floor
#                                    declared in [tool.coverage.report])
#
# Usage:
#   ./scripts/tooling/code_quality.sh                  # all phases, no writes
#   ./scripts/tooling/code_quality.sh --fix            # auto-fix where possible
#   ./scripts/tooling/code_quality.sh --check          # explicit no-write mode
#   ./scripts/tooling/code_quality.sh --only lint,type # run a subset
#   ./scripts/tooling/code_quality.sh --skip sql,test  # skip a subset
#   ./scripts/tooling/code_quality.sh --no-cov         # drop the coverage gate
#
# If Poetry is installed and a pyproject.toml is present, all tools are
# invoked through `poetry run` so the script works without a manually
# activated virtualenv. Otherwise the tools must be on PATH directly.
#
# Frontend quality (eslint + vitest + tsc) is intentionally out of scope —
# run `npm run lint` and `npm run test` inside `frontend/` separately.
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# -----------------------------------------------------------------------------
# TTY colors. Plain output when stdout is not a terminal (e.g. piped to a log).
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

step()    { printf "\n${BLUE}▸ %s${NC}\n" "$*"; }
ok()      { printf "${GREEN}  ✔ %s${NC}\n" "$*"; }
warn()    { printf "${YELLOW}  ⚠ %s${NC}\n" "$*"; }
fail()    { printf "${RED}  ✖ %s${NC}\n" "$*"; }
skipped() { printf "${YELLOW}  ↷ %s${NC}\n" "$*"; }

ERRORS=0
record_result() {
    if [ "$1" -eq 0 ]; then
        ok "$2 passed"
    else
        fail "$2 failed"
        ERRORS=$((ERRORS + 1))
    fi
}

# -----------------------------------------------------------------------------
# Argument parsing.
# -----------------------------------------------------------------------------
FIX=false
CHECK=false
NO_COV=false
ONLY=""
SKIP=""

print_help() {
    sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case $1 in
        --fix)     FIX=true; shift ;;
        --check)   CHECK=true; shift ;;
        --no-cov)  NO_COV=true; shift ;;
        --only)    ONLY="$2"; shift 2 ;;
        --skip)    SKIP="$2"; shift 2 ;;
        -h|--help) print_help; exit 0 ;;
        *)         echo "Unknown option: $1" >&2; print_help; exit 1 ;;
    esac
done

# `--only` wins over `--skip` when both are present.
phase_active() {
    local phase="$1"
    if [ -n "$ONLY" ]; then
        case ",$ONLY," in *",$phase,"*) return 0 ;; *) return 1 ;; esac
    fi
    if [ -n "$SKIP" ]; then
        case ",$SKIP," in *",$phase,"*) return 1 ;; *) return 0 ;; esac
    fi
    return 0
}

# -----------------------------------------------------------------------------
# Tool runner. Prefix every invocation with `poetry run` when Poetry is
# available and we're inside a Poetry-managed project, so the script does
# not require the venv to be active.
# -----------------------------------------------------------------------------
if command -v poetry >/dev/null 2>&1 && [ -f pyproject.toml ]; then
    RUN_PREFIX="poetry run"
else
    RUN_PREFIX=""
fi

run_tool() {
    if [ -n "$RUN_PREFIX" ]; then
        $RUN_PREFIX "$@"
    else
        "$@"
    fi
}

# -----------------------------------------------------------------------------
# Phases.
# -----------------------------------------------------------------------------
if phase_active format; then
    step "Ruff formatter"
    if $FIX; then
        run_tool ruff format src tests scripts; record_result $? "ruff format"
    else
        run_tool ruff format --check src tests scripts; record_result $? "ruff format"
    fi
fi

if phase_active lint; then
    step "Ruff linter"
    if $FIX; then
        run_tool ruff check --fix src tests scripts; record_result $? "ruff check"
    else
        run_tool ruff check src tests scripts; record_result $? "ruff check"
    fi
fi

if phase_active type; then
    step "Mypy type checker"
    run_tool mypy src; record_result $? "mypy"
fi

if phase_active sql; then
    if $FIX; then
        step "SQLFluff (fix)"
        run_tool sqlfluff fix --force db; record_result $? "sqlfluff fix"
    else
        step "SQLFluff (lint)"
        run_tool sqlfluff lint db; record_result $? "sqlfluff lint"
    fi
fi

if phase_active test; then
    if $NO_COV; then
        step "Pytest (no coverage)"
        run_tool pytest; record_result $? "pytest"
    else
        step "Pytest with coverage (fail_under from pyproject)"
        run_tool pytest --cov; record_result $? "pytest --cov"
    fi
fi

# -----------------------------------------------------------------------------
# Summary.
# -----------------------------------------------------------------------------
echo ""
if [ "$ERRORS" -eq 0 ]; then
    printf "${GREEN}All checks passed ✔${NC}\n"
else
    printf "${RED}%d check(s) failed ✖${NC}\n" "$ERRORS"
    exit 1
fi
