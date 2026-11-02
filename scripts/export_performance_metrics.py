#!/usr/bin/env python3
"""Run the website performance audit and verify the exported metrics file.

This script auto-discovers current Next.js App Router pages and internal API
routes, exercises them against a running local frontend, triggers a synthetic
client-side metric ingestion, flushes pending writes, and verifies that the
exported CSV exists and is sorted by ``duration_ms`` in descending order.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_APP_ROOT = REPO_ROOT / "frontend" / "src" / "app"
DEFAULT_BASE_URL = os.environ.get(
    "PERFORMANCE_METRICS_BASE_URL", "http://localhost:13000"
)
DEFAULT_EXPORT_PATH = Path(
    os.environ.get(
        "PERFORMANCE_METRICS_EXPORT_PATH",
        str(REPO_ROOT / "performance_metrics_export.csv"),
    )
)
DEFAULT_FRONTEND_CONTAINER_NAME = os.environ.get(
    "PERFORMANCE_METRICS_CONTAINER_NAME",
    "ab-frontend",
)
LEGACY_EXPORT_PATH = REPO_ROOT / "performance_audit.csv"
CLIENT_EVENT_ROUTE = "/api/performance/client"
FLUSH_ROUTE = "/api/performance/flush"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS = 30
CONTROL_ROUTES = {CLIENT_EVENT_ROUTE, FLUSH_ROUTE}
SKIPPED_AUTO_ROUTES = {"/api/audio-clip"}


@dataclass(frozen=True)
class Probe:
    path: str
    kind: str
    params: dict[str, str] | None = None


@dataclass(frozen=True)
class ProbeResult:
    path: str
    kind: str
    status_code: int | None
    ok: bool
    detail: str
    skipped: bool = False


class AuditFailure(RuntimeError):
    """Raised when the audit runner cannot complete successfully."""


def normalize_route_segments(parts: tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for part in parts:
        if part.startswith("(") and part.endswith(")"):
            continue
        if part.startswith("@"):
            continue
        normalized.append(part)
    return normalized


def has_dynamic_segment(parts: tuple[str, ...]) -> bool:
    return any("[" in part and "]" in part for part in parts)


def route_path_from_parts(parts: tuple[str, ...]) -> str:
    segments = normalize_route_segments(parts)
    if not segments:
        return "/"
    return "/" + "/".join(segments)


def discover_page_paths() -> list[str]:
    page_paths: set[str] = set()
    for page_file in FRONTEND_APP_ROOT.rglob("page.tsx"):
        relative = page_file.relative_to(FRONTEND_APP_ROOT)
        parts = relative.parts[:-1]
        if has_dynamic_segment(parts):
            continue
        page_paths.add(route_path_from_parts(parts))
    return sorted(page_paths)


def discover_api_paths() -> list[str]:
    api_paths: set[str] = set()
    for route_file in FRONTEND_APP_ROOT.rglob("route.ts"):
        relative = route_file.relative_to(FRONTEND_APP_ROOT)
        parts = relative.parts[:-1]
        if not parts or parts[0] != "api":
            continue
        if has_dynamic_segment(parts):
            continue
        api_paths.add(route_path_from_parts(parts))
    return sorted(api_paths)


def build_url(base_url: str, path: str, params: dict[str, str] | None = None) -> str:
    cleaned_base = base_url.rstrip("/")
    cleaned_path = path if path.startswith("/") else f"/{path}"
    if not params:
        return f"{cleaned_base}{cleaned_path}"
    return f"{cleaned_base}{cleaned_path}?{urlencode(params)}"


def reset_export_files(export_path: Path) -> None:
    for path in {export_path, LEGACY_EXPORT_PATH}:
        if path.exists():
            path.unlink()


def read_container_environment(frontend_container_name: str) -> dict[str, str]:
    result = subprocess.run(
        ["docker", "exec", frontend_container_name, "env"],
        check=True,
        capture_output=True,
        text=True,
        timeout=DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
    )

    env_values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            env_values[key] = value
    return env_values


def resolve_container_export_path(frontend_container_name: str) -> Path | None:
    try:
        env_values = read_container_environment(frontend_container_name)
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    ):
        return None

    configured_path = (
        env_values.get("PERFORMANCE_METRICS_EXPORT_PATH", "").strip()
        or env_values.get("PERFORMANCE_AUDIT_CSV_PATH", "").strip()
    )
    if configured_path:
        return Path(configured_path)

    app_ready_file = env_values.get("APP_READY_FILE", "").strip()
    if app_ready_file:
        return Path(app_ready_file).parent / "performance_metrics_export.csv"

    return Path("/app/.runtime/performance_metrics_export.csv")


def reset_container_export_file(
    frontend_container_name: str, export_path: Path
) -> None:
    try:
        subprocess.run(
            ["docker", "exec", frontend_container_name, "rm", "-f", str(export_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
        )
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    ):
        return


def reset_export_files_from_source(
    export_path: Path,
    frontend_container_name: str | None,
) -> None:
    reset_export_files(export_path)

    if not frontend_container_name:
        return

    container_export_path = resolve_container_export_path(frontend_container_name)
    if container_export_path is None:
        return

    reset_container_export_file(frontend_container_name, container_export_path)


def load_grid_cell_id(
    session: requests.Session,
    base_url: str,
    request_timeout_seconds: float,
) -> str | None:
    response = session.get(
        build_url(base_url, "/api/grid-cells"),
        timeout=request_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    cells = payload.get("cells") if isinstance(payload, dict) else None
    if not isinstance(cells, list) or not cells:
        return None

    first_cell = cells[0]
    if not isinstance(first_cell, dict):
        return None
    grid_cell_id = first_cell.get("grid_cell_id")
    return str(grid_cell_id) if isinstance(grid_cell_id, str) and grid_cell_id else None


def api_params_for_path(path: str, grid_cell_id: str | None) -> dict[str, str] | None:
    if path in CONTROL_ROUTES or path in SKIPPED_AUTO_ROUTES:
        return None
    if path == "/api/boundaries":
        return {"type": "country"}
    if path == "/api/phylopic":
        return {"name": "turdus merula"}
    if path == "/api/movebank-overlays":
        return {"overlay": "recent_fixes"}
    if path == "/api/grid-cell-profile":
        return {"grid_cell_id": grid_cell_id} if grid_cell_id else None
    return {}


def page_params_for_path(path: str) -> dict[str, str]:
    if path == "/explore":
        return {"page": "1"}
    return {}


def run_get_probe(
    session: requests.Session,
    base_url: str,
    probe: Probe,
    request_timeout_seconds: float,
) -> ProbeResult:
    url = build_url(base_url, probe.path, probe.params)
    try:
        response = session.get(url, timeout=request_timeout_seconds)
        ok = response.ok
        detail = f"GET {url} -> {response.status_code}"
        return ProbeResult(
            path=probe.path,
            kind=probe.kind,
            status_code=response.status_code,
            ok=ok,
            detail=detail,
        )
    except requests.RequestException as error:
        return ProbeResult(
            path=probe.path,
            kind=probe.kind,
            status_code=None,
            ok=False,
            detail=f"GET {url} failed: {error}",
        )


def run_client_metric_probe(
    session: requests.Session,
    base_url: str,
    request_timeout_seconds: float,
) -> ProbeResult:
    url = build_url(base_url, CLIENT_EVENT_ROUTE)
    payload = {
        "events": [
            {
                "eventKind": "client_fetch",
                "surface": "browser",
                "operation": "browser_fetch",
                "routePath": "/alerts",
                "target": "/api/grid-cells",
                "method": "GET",
                "durationMs": 123.4,
                "statusCode": 200,
                "success": True,
                "responseBytes": 512,
            }
        ]
    }
    try:
        response = session.post(url, json=payload, timeout=request_timeout_seconds)
        return ProbeResult(
            path=CLIENT_EVENT_ROUTE,
            kind="client",
            status_code=response.status_code,
            ok=response.ok,
            detail=f"POST {url} -> {response.status_code}",
        )
    except requests.RequestException as error:
        return ProbeResult(
            path=CLIENT_EVENT_ROUTE,
            kind="client",
            status_code=None,
            ok=False,
            detail=f"POST {url} failed: {error}",
        )


def flush_export(
    session: requests.Session,
    base_url: str,
    request_timeout_seconds: float,
) -> dict[str, Any]:
    url = build_url(base_url, FLUSH_ROUTE)
    response = session.get(url, timeout=request_timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise AuditFailure(f"Unexpected flush response: {payload!r}")
    return payload


def verify_export_sorted(export_path: Path) -> tuple[int, list[dict[str, str]]]:
    return verify_export_sorted_from_source(export_path, None)


def read_export_csv_text(
    export_path: Path,
    frontend_container_name: str | None,
) -> str:
    if export_path.exists():
        return export_path.read_text(encoding="utf8")

    if not frontend_container_name:
        raise AuditFailure(f"Export file was not created: {export_path}")

    try:
        result = subprocess.run(
            ["docker", "exec", frontend_container_name, "cat", str(export_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise AuditFailure(
            "Export file was not created locally and Docker is unavailable for "
            f"container fallback: {export_path}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise AuditFailure(
            f"Timed out reading export file from container {frontend_container_name}: {export_path}"
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        detail = f" ({stderr})" if stderr else ""
        raise AuditFailure(
            "Export file was not created locally and could not be read from "
            f"container {frontend_container_name}: {export_path}{detail}"
        ) from error

    # Save the file in the current repo's root dir
    local_save_path = REPO_ROOT / export_path.name
    local_save_path.write_text(result.stdout, encoding="utf8")

    return result.stdout


def verify_export_sorted_from_source(
    export_path: Path,
    frontend_container_name: str | None,
) -> tuple[int, list[dict[str, str]]]:
    content = read_export_csv_text(export_path, frontend_container_name)
    rows = list(csv.DictReader(StringIO(content)))

    durations: list[float] = []
    for row in rows:
        raw_duration = (row.get("duration_ms") or "").strip()
        if not raw_duration:
            continue
        try:
            durations.append(float(raw_duration))
        except ValueError as error:
            raise AuditFailure(
                f"Invalid duration_ms value {raw_duration!r}: {error}"
            ) from error

    for current, next_value in zip(durations, durations[1:]):
        if current < next_value:
            raise AuditFailure(
                "Export file is not sorted by duration_ms descending: "
                f"{current} < {next_value}"
            )

    return len(rows), rows[:10]


def ensure_frontend_is_reachable(
    session: requests.Session,
    base_url: str,
    request_timeout_seconds: float,
) -> None:
    response = session.get(
        build_url(base_url, "/api/health"),
        timeout=request_timeout_seconds,
    )
    response.raise_for_status()


def print_results(title: str, results: list[ProbeResult]) -> None:
    print(f"\n{title}")
    for result in results:
        prefix = "SKIP" if result.skipped else ("OK" if result.ok else "FAIL")
        print(f"  [{prefix}] {result.detail}")


def run_probe_batch(
    session: requests.Session,
    base_url: str,
    probes: list[Probe],
    request_timeout_seconds: float,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    total = len(probes)
    for index, probe in enumerate(probes, start=1):
        url = build_url(base_url, probe.path, probe.params)
        print(f"[{probe.kind} {index}/{total}] {url}", flush=True)
        result = run_get_probe(session, base_url, probe, request_timeout_seconds)
        prefix = "OK" if result.ok else "FAIL"
        print(f"  -> [{prefix}] {result.detail}", flush=True)
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help="Frontend base URL."
    )
    parser.add_argument(
        "--export-path",
        default=str(DEFAULT_EXPORT_PATH),
        help="Path to the performance metrics export CSV.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Keep existing export files instead of resetting them before the run.",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Per-request timeout in seconds for probes and flush operations.",
    )
    parser.add_argument(
        "--frontend-container-name",
        default=DEFAULT_FRONTEND_CONTAINER_NAME,
        help=(
            "Frontend container name for reading the exported CSV when the "
            "reported export path is only accessible inside Docker."
        ),
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    export_path = Path(args.export_path).resolve()
    request_timeout_seconds = args.request_timeout_seconds
    frontend_container_name = args.frontend_container_name.strip() or None

    session = requests.Session()
    session.headers.update({"User-Agent": "avian-performance-audit/1.0"})

    try:
        ensure_frontend_is_reachable(session, base_url, request_timeout_seconds)
        if not args.keep_existing:
            reset_export_files_from_source(export_path, frontend_container_name)

        page_probes = [
            Probe(path=path, kind="page", params=page_params_for_path(path))
            for path in discover_page_paths()
        ]
        api_paths = discover_api_paths()

        grid_cell_id: str | None = None
        if "/api/grid-cell-profile" in api_paths:
            try:
                grid_cell_id = load_grid_cell_id(
                    session, base_url, request_timeout_seconds
                )
            except requests.RequestException:
                grid_cell_id = None

        api_probes: list[Probe] = []
        skipped_results: list[ProbeResult] = []
        for path in api_paths:
            params = api_params_for_path(path, grid_cell_id)
            if path in CONTROL_ROUTES:
                continue
            if path in SKIPPED_AUTO_ROUTES:
                skipped_results.append(
                    ProbeResult(
                        path=path,
                        kind="api",
                        status_code=None,
                        ok=True,
                        detail=f"Skipped {path}: requires a concrete bucket/path payload.",
                        skipped=True,
                    )
                )
                continue
            if path == "/api/grid-cell-profile" and params is None:
                skipped_results.append(
                    ProbeResult(
                        path=path,
                        kind="api",
                        status_code=None,
                        ok=True,
                        detail=f"Skipped {path}: no grid_cell_id could be derived from /api/grid-cells.",
                        skipped=True,
                    )
                )
                continue
            api_probes.append(Probe(path=path, kind="api", params=params))

        page_results = run_probe_batch(
            session, base_url, page_probes, request_timeout_seconds
        )
        api_results = run_probe_batch(
            session, base_url, api_probes, request_timeout_seconds
        )
        print(f"[client] {build_url(base_url, CLIENT_EVENT_ROUTE)}", flush=True)
        client_result = run_client_metric_probe(
            session, base_url, request_timeout_seconds
        )
        print(
            f"  -> [{'OK' if client_result.ok else 'FAIL'}] {client_result.detail}",
            flush=True,
        )
        print(f"[flush] {build_url(base_url, FLUSH_ROUTE)}", flush=True)
        flush_payload = flush_export(session, base_url, request_timeout_seconds)
        print(
            f"  -> [OK] flushed {flush_payload.get('rowCount', 'unknown')} rows",
            flush=True,
        )
        reported_export_path = Path(
            str(flush_payload.get("exportPath", export_path))
        ).resolve()
        row_count, top_rows = verify_export_sorted_from_source(
            reported_export_path,
            frontend_container_name,
        )

        print_results("Page probes", page_results)
        print_results("API probes", api_results)
        if skipped_results:
            print_results("Skipped probes", skipped_results)
        print_results("Client ingest", [client_result])

        print("\nExport verification")
        print(f"  Export path: {reported_export_path}")
        print(f"  Flush response: {json.dumps(flush_payload)}")
        print(f"  CSV rows: {row_count}")
        print("  Top rows by duration_ms:")
        for row in top_rows[:5]:
            print(
                "   - "
                f"duration_ms={row.get('duration_ms', '')} "
                f"event_kind={row.get('event_kind', '')} "
                f"route_path={row.get('route_path', '')} "
                f"operation={row.get('operation', '')}"
            )

        failed = [
            result
            for result in [*page_results, *api_results, client_result]
            if not result.ok
        ]
        if failed:
            print("\nAudit completed with failures.", file=sys.stderr)
            return 1

        return 0
    except (AuditFailure, requests.RequestException) as error:
        print(f"Performance audit failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
