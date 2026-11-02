"""Dagster sensors for the Avian Biosurveillance orchestration layer.

Six sensors cooperate to drive the platform end-to-end without relying on
ad-hoc cron entries for follow-up work:

``startup_sensor``
    Fires every Phase-1 deployment that opted into ``run_on_startup`` exactly
    once after the code-server boots, provided no historic run for that
    deployment exists.

``phase2_sensor``
    Watches the startup post-processing backstops and fires each one exactly
    once after one of its declared upstream dependencies has produced a
    successful run.

``daily_quality_sensor``
    Watches the quality-audit dependency cohort. As soon as every declared
    upstream daily ingestion has a SUCCESS run for the same
    ``avian/logical_date`` value, it fires the quality audit for that logical
    date and remembers it.

``birdweather_bundle_sync_sensor``
    Watches local BirdWeather date bundles under ``data/raw/birdweather`` and
    launches at most one sync run per logical date, only when no successful or
    active sync run already exists for that date.

``birdweather_catchup_sensor``
    Walks the configured BirdWeather date window from start to end, launching
    a single BirdWeather-only catchup run per logical date and only advancing
    its cursor once the previous date's run has reached SUCCESS.

``historical_catchup_sensor``
    Walks the configured shared non-BirdWeather historical date window from
    start to end, launching a single ``run_historical_catchup_flow`` run per
    logical date and only advancing its cursor once the previous date's run
    has reached SUCCESS.

All sensors use cursor JSON, are idempotent, and tag every emitted run with
``avian/deployment`` so the ``QueuedRunCoordinator`` concurrency limits in
``dagster.yaml`` apply uniformly to schedule, sensor, and AMP-launched runs.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from dagster import (
    DagsterRunStatus,
    DefaultSensorStatus,
    RunRequest,
    RunsFilter,
    SensorEvaluationContext,
    SkipReason,
    sensor,
)

from avian_biosurveillance.config.schedules import (
    DeploymentSchedule,
    schedule_registry,
)
from avian_biosurveillance.config.species import registry as _sp_registry
from avian_biosurveillance.ingestion.earth_engine_cadence import (
    anchor_dates_in_range,
)
from avian_biosurveillance.orchestration.dagster_defs.jobs import (
    ALL_FLOW_JOBS,
    FLOW_JOBS,
    run_config_for,
)
from avian_biosurveillance.orchestration.dagster_defs.naming import (
    BIRDWEATHER_BUNDLE_SYNC_AUTOMATION_KEY,
    BIRDWEATHER_BUNDLE_SYNC_SENSOR_NAME,
    BIRDWEATHER_CATCHUP_AUTOMATION_KEY,
    BIRDWEATHER_CATCHUP_SENSOR_NAME,
    DAILY_QUALITY_AUTOMATION_KEY,
    DAILY_QUALITY_SENSOR_NAME,
    HISTORICAL_CATCHUP_AUTOMATION_KEY,
    HISTORICAL_CATCHUP_SENSOR_NAME,
    STARTUP_INGESTION_AUTOMATION_KEY,
    STARTUP_INGESTION_SENSOR_NAME,
    STARTUP_POSTPROCESSING_AUTOMATION_KEY,
    STARTUP_POSTPROCESSING_SENSOR_NAME,
    build_automation_run_key,
    build_run_tags,
)


logger = logging.getLogger(__name__)


_SENSOR_DEFAULT_INTERVAL_SECONDS = 60
_BIRDWEATHER_CATCHUP_INTERVAL_SECONDS = 10
_HISTORICAL_CATCHUP_INTERVAL_SECONDS = 120
_HISTORICAL_MAX_ATTEMPTS = 3
_HISTORICAL_CATCHUP_CURSOR_DIRECTION = "asc"
_SENSOR_TARGET_JOBS = list(FLOW_JOBS.values())
_STARTUP_BOOT_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
_FINISHED_STATUSES = {DagsterRunStatus.SUCCESS, DagsterRunStatus.FAILURE}
_ACTIVE_STATUSES = {
    DagsterRunStatus.QUEUED,
    DagsterRunStatus.NOT_STARTED,
    DagsterRunStatus.STARTING,
    DagsterRunStatus.STARTED,
}
_BIRDWEATHER_BACKLOG_BACKSTOP_DEPLOYMENTS = frozenset(
    {
        "birdweather-raw-audio-ingestion",
        "birdweather-audio-processing",
        "birdweather-feature-engineering",
        "model-anomaly-scoring",
    }
)


# ---------------------------------------------------------------------------
# Helpers — kept as module-level functions so the test suite can monkeypatch
# them without recreating sensor definitions.
# ---------------------------------------------------------------------------

_SPECIES_INJECTIONS: dict[tuple[str, str], list[str]] = {
    ("birdweather-daily", "species_ids"): list(_sp_registry.birdweather_ids),
    ("birdweather-catchup", "species_ids"): list(_sp_registry.birdweather_ids),
    ("birdweather-repair", "species_ids"): list(_sp_registry.birdweather_ids),
}

_HISTORICAL_ENV_OVERRIDES = {
    "start_date": ("CATCHUP_START_DATE", "BACKFILL_START_DATE"),
    "end_date": ("CATCHUP_END_DATE", "BACKFILL_END_DATE"),
    "target": ("CATCHUP_TARGET", "BACKFILL_TARGET"),
    "sources": ("CATCHUP_SOURCES", "BACKFILL_SOURCES"),
}


def _first_env_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _env_flag(*names: str, default: bool = False) -> bool:
    value = _first_env_value(*names)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _historical_sensor_default_status() -> DefaultSensorStatus:
    return _catchup_sensor_default_status(None)


def _catchup_sensor_default_status(
    dep: DeploymentSchedule | None,
) -> DefaultSensorStatus:
    if dep is not None and not dep.enabled:
        return DefaultSensorStatus.STOPPED
    return (
        DefaultSensorStatus.RUNNING
        if _env_flag("CATCHUP_AUTOSTART", "BACKFILL_AUTOSTART", default=True)
        else DefaultSensorStatus.STOPPED
    )


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _resolve_parameters(dep: DeploymentSchedule) -> dict[str, Any]:
    """Return a deployment's parameters with runtime injections applied."""
    params = dict(dep.parameters)
    for (dep_name, key), value in _SPECIES_INJECTIONS.items():
        if (
            dep_name == dep.name
            and not params.get("all_species")
            and key not in params
        ):
            params[key] = value
    if dep.name in {"historical-catchup", "birdweather-catchup"}:
        for param_name in ("start_date", "end_date", "target"):
            env_value = _first_env_value(*_HISTORICAL_ENV_OVERRIDES[param_name])
            if env_value:
                params[param_name] = env_value
    if dep.name == "historical-catchup":
        env_value = _first_env_value(*_HISTORICAL_ENV_OVERRIDES["sources"])
        if env_value:
            params["sources"] = env_value
    return params


def _run_config_for(flow: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Thin indirection around :func:`run_config_for` for monkeypatching."""
    return run_config_for(flow, params)


def _is_postprocessing(dep: DeploymentSchedule) -> bool:
    return "post-processing" in dep.tags or "audit" in dep.tags


def _is_daily(dep: DeploymentSchedule) -> bool:
    return "daily" in dep.tags


def _startup_eligible() -> list[DeploymentSchedule]:
    """All deployments flagged ``run_on_startup`` that map to a known flow."""
    return [
        dep for dep in schedule_registry.startup_deployments if dep.flow in FLOW_JOBS
    ]


def _daily_ingestion_deployments() -> list[DeploymentSchedule]:
    """Daily-tagged ingestion deployments (excludes audits and post-processing)."""
    return [
        dep
        for dep in schedule_registry
        if _is_daily(dep) and not _is_postprocessing(dep) and dep.flow in FLOW_JOBS
    ]


def _quality_deployment() -> DeploymentSchedule | None:
    return schedule_registry.get("quality-audit")


def _birdweather_bundle_sync_deployment() -> DeploymentSchedule | None:
    return schedule_registry.get("birdweather-bundle-sync")


def _birdweather_catchup_deployment() -> DeploymentSchedule | None:
    return schedule_registry.get("birdweather-catchup")


def _historical_catchup_deployment() -> DeploymentSchedule | None:
    return schedule_registry.get("historical-catchup")


def _birdweather_backlog_mode_enabled() -> bool:
    birdweather_daily = schedule_registry.get("birdweather-daily")
    birdweather_catchup = _birdweather_catchup_deployment()
    return bool(
        birdweather_daily is not None
        and birdweather_catchup is not None
        and not birdweather_daily.enabled
        and birdweather_catchup.enabled
    )


def _birdweather_backlog_pause_reason(dep: DeploymentSchedule) -> str | None:
    if dep.name not in _BIRDWEATHER_BACKLOG_BACKSTOP_DEPLOYMENTS:
        return None
    if not _birdweather_backlog_mode_enabled():
        return None
    return (
        "BirdWeather backlog mode is active; "
        f"{dep.name} stays paused while birdweather-daily is disabled "
        "and birdweather-catchup owns the 2024 backfill."
    )


def _dependency_deployments(
    dep: DeploymentSchedule,
    *,
    daily_only: bool = False,
) -> list[DeploymentSchedule]:
    """Resolve ``depends_on`` names to known Dagster deployments."""
    resolved: list[DeploymentSchedule] = []
    seen: set[str] = set()

    for upstream_name in dep.depends_on:
        if upstream_name in seen:
            continue
        upstream = schedule_registry.get(upstream_name)
        if upstream is None or upstream.flow not in ALL_FLOW_JOBS:
            continue
        if daily_only and not _is_daily(upstream):
            continue
        resolved.append(upstream)
        seen.add(upstream_name)

    return resolved


def _deployment_has_success(
    instance: Any,
    dep: DeploymentSchedule,
    *,
    logical_date: str | None = None,
) -> bool:
    """Return ``True`` when *dep* has a successful Dagster run."""
    job = ALL_FLOW_JOBS.get(dep.flow)
    if job is None:
        return False
    return bool(
        _runs_for(
            instance,
            job_name=job.name,
            deployment=dep.name,
            logical_date=logical_date,
            statuses=[DagsterRunStatus.SUCCESS],
            limit=1,
        )
    )


def _phase2_dependencies(
    dep: DeploymentSchedule,
    fallback: list[DeploymentSchedule],
) -> list[DeploymentSchedule]:
    """Return the upstreams that make a Phase-2 startup backstop meaningful."""
    dependencies = _dependency_deployments(dep)
    return dependencies or fallback


def _quality_dependencies() -> list[DeploymentSchedule]:
    """Return the daily cohort that must succeed before quality runs."""
    quality_dep = _quality_deployment()
    if quality_dep is None:
        return []
    dependencies = _dependency_deployments(quality_dep, daily_only=True)
    return dependencies or _daily_ingestion_deployments()


# ---------------------------------------------------------------------------
# Run-history helpers
# ---------------------------------------------------------------------------


def _runs_for(
    instance: Any,
    *,
    job_name: str,
    deployment: str | None = None,
    logical_date: str | None = None,
    statuses: Iterable[DagsterRunStatus] | None = None,
    limit: int | None = None,
) -> list[Any]:
    tags: dict[str, str] = {}
    if deployment is not None:
        tags["avian/deployment"] = deployment
    if logical_date is not None:
        tags["avian/logical_date"] = logical_date
    filters = RunsFilter(
        job_name=job_name,
        tags=tags or None,
        statuses=list(statuses) if statuses else None,
    )
    return list(instance.get_runs(limit=limit, filters=filters))


def _run_status(run: Any) -> Any:
    return run.get("status") if isinstance(run, dict) else getattr(run, "status", None)


def _run_tag(run: Any, key: str) -> str | None:
    if isinstance(run, dict):
        value = run.get("tags", {}).get(key)
        return str(value) if value is not None else None
    tags = getattr(run, "tags", None)
    if not tags:
        return None
    value = tags.get(key)
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _load_cursor(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dump_cursor(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _next_missing_day(
    existing_dates: Iterable[date],
    *,
    start: date,
    end: date,
) -> date:
    """Return the first missing day in a contiguous date range.

    ``birdweather_tbl`` can contain later daily-ingestion rows even when the
    historical catchup window has a gap. This helper walks forward from
    ``start`` until it finds the first missing calendar day and ignores any
    non-contiguous rows after that gap.
    """
    expected = start
    for value in sorted(set(existing_dates)):
        if value < expected:
            continue
        if value != expected:
            break
        expected += timedelta(days=1)
        if expected > end:
            break
    return expected


def _birdweather_resume_date(start: date, end: date) -> date | None:
    """Return the next BirdWeather catchup day based on persisted coverage.

    The BirdWeather catchup sensor stores progress only in Dagster metadata.
    When that metadata is reset or partially lost, the sensor can replay months
    of already-ingested days unless it consults persisted BirdWeather coverage.
    Coverage must come from actual rows, not Timescale chunks, because empty
    chunks can exist for days whose ingestion failed before any rows landed.
    """
    try:
        from avian_biosurveillance.db.database import get_pool

        pool = get_pool(min_conn=1, max_conn=4)
        with pool.cursor() as cur:
            cur.execute(
                "SELECT observation_date::date "
                "FROM generate_series(%s::date, %s::date, interval '1 day') AS days(observation_date) "
                "WHERE EXISTS ("
                "    SELECT 1 "
                "    FROM birdweather_tbl bwt "
                "    WHERE bwt.observation_date = days.observation_date "
                "    LIMIT 1"
                ") "
                "ORDER BY observation_date ASC",
                (start, end),
            )
            existing_dates = [row[0] for row in cur.fetchall()]
    except Exception:  # pragma: no cover  # pylint: disable=broad-exception-caught
        logger.exception(
            "BirdWeather catchup could not inspect persisted date coverage."
        )
        return None

    return _next_missing_day(existing_dates, start=start, end=end)


def _source_list(value: object, *, default: Iterable[str]) -> set[str]:
    """Parse a schedule ``sources`` parameter into canonical source names."""
    if value is None:
        return set(default)
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, Iterable):
        return {str(part).strip() for part in value if str(part).strip()}
    return set(default)


_DAILY_COVERAGE_SQL: dict[tuple[str, str], str] = {
    (
        "birdweather_tbl",
        "observation_date",
    ): """
        SELECT days.observation_date::date
        FROM generate_series(%s::date, %s::date, interval '1 day') AS days(observation_date)
        WHERE NOT EXISTS (
            SELECT 1
            FROM birdweather_tbl source_rows
            WHERE source_rows.observation_date = days.observation_date::date
            LIMIT 1
        )
        ORDER BY days.observation_date
        """,
    (
        "knmi_tbl",
        "observation_date",
    ): """
        SELECT days.observation_date::date
        FROM generate_series(%s::date, %s::date, interval '1 day') AS days(observation_date)
        WHERE NOT EXISTS (
            SELECT 1
            FROM knmi_tbl source_rows
            WHERE source_rows.observation_date = days.observation_date::date
            LIMIT 1
        )
        ORDER BY days.observation_date
        """,
    (
        "ebird_tbl",
        "observation_date",
    ): """
        SELECT days.observation_date::date
        FROM generate_series(%s::date, %s::date, interval '1 day') AS days(observation_date)
        WHERE NOT EXISTS (
            SELECT 1
            FROM ebird_tbl source_rows
            WHERE source_rows.observation_date = days.observation_date::date
            LIMIT 1
        )
        ORDER BY days.observation_date
        """,
}


def _missing_row_coverage(
    cur: Any,
    *,
    table_name: str,
    date_column: str,
    start: date,
    end: date,
) -> list[date]:
    """Return catchup days with no persisted rows in ``table_name``."""
    sql = _DAILY_COVERAGE_SQL[(table_name, date_column)]
    cur.execute(sql, (start, end))
    return [row[0] for row in cur.fetchall()]


def _earth_engine_grid_system(params: Mapping[str, Any]) -> str:
    """Return the Earth Engine grid-system name for catchup coverage checks."""
    raw_size = params.get("ee_grid_size_km", params.get("grid_size_km", 5.0))
    try:
        grid_size = float(raw_size)
    except (TypeError, ValueError):
        grid_size = 5.0
    token = f"{grid_size:g}".replace(".", "p")
    return f"nl_geodesic_{token}km"


def _missing_earth_engine_grid_coverage(
    cur: Any,
    *,
    start: date,
    end: date,
    grid_system: str,
) -> list[tuple[date, int, int]]:
    """Return bi-weekly anchor days whose Earth Engine grid rows are below coverage."""
    anchors = anchor_dates_in_range(start, end)
    if not anchors:
        return []
    cur.execute(
        """
        WITH expected AS (
            SELECT COUNT(*)::int AS cells
            FROM grid_cells_tbl
            WHERE grid_system = %s
        ),
        actual AS (
            SELECT geeg.observation_date::date AS observation_date,
                   COUNT(*)::int AS rows
            FROM google_earth_engine_tbl geeg
            JOIN grid_cells_tbl cells
              ON cells.grid_cell_id = geeg.grid_cell_id
             AND cells.grid_system = %s
            WHERE geeg.observation_date = ANY(%s::date[])
            GROUP BY geeg.observation_date
        )
        SELECT days.observation_date::date,
               COALESCE(actual.rows, 0)::int AS rows,
               expected.cells::int AS expected_rows
        FROM unnest(%s::date[]) AS days(observation_date)
        CROSS JOIN expected
        LEFT JOIN actual
          ON actual.observation_date = days.observation_date::date
        WHERE expected.cells = 0
           OR COALESCE(actual.rows, 0) < expected.cells
        ORDER BY days.observation_date
        """,
        (grid_system, grid_system, anchors, anchors),
    )
    return [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in cur.fetchall()]


def _validate_catchup_window_coverage(
    *,
    dep: DeploymentSchedule,
    params: Mapping[str, Any],
    start: date,
    end: date,
) -> list[str]:
    """Return coverage errors that should block catchup cursor advancement."""
    if params.get("target", "database") != "database":
        return []

    try:
        from avian_biosurveillance.db.database import get_pool

        pool = get_pool(min_conn=1, max_conn=4)
        with pool.cursor() as cur:
            errors: list[str] = []
            if dep.name == "birdweather-catchup":
                missing = _missing_row_coverage(
                    cur,
                    table_name="birdweather_tbl",
                    date_column="observation_date",
                    start=start,
                    end=end,
                )
                if missing:
                    errors.append(
                        "birdweather_tbl missing day(s): "
                        + ", ".join(day.isoformat() for day in missing[:10])
                    )
                return errors

            if dep.name != "historical-catchup":
                return []

            active_sources = _source_list(
                params.get("sources"),
                default=("knmi", "ebird", "earth_engine_grid"),
            )
            daily_tables = {
                "birdweather": ("birdweather_tbl", "observation_date"),
                "knmi": ("knmi_tbl", "observation_date"),
                "ebird": ("ebird_tbl", "observation_date"),
            }
            for source, (table_name, date_column) in daily_tables.items():
                if source not in active_sources:
                    continue
                missing = _missing_row_coverage(
                    cur,
                    table_name=table_name,
                    date_column=date_column,
                    start=start,
                    end=end,
                )
                if missing:
                    errors.append(
                        f"{table_name} missing day(s): "
                        + ", ".join(day.isoformat() for day in missing[:10])
                    )

            if "earth_engine_grid" in active_sources:
                grid_system = _earth_engine_grid_system(params)
                missing_grid = _missing_earth_engine_grid_coverage(
                    cur,
                    start=start,
                    end=end,
                    grid_system=grid_system,
                )
                if missing_grid:
                    errors.append(
                        f"google_earth_engine_tbl incomplete for {grid_system}: "
                        + ", ".join(
                            f"{day.isoformat()} ({rows}/{expected})"
                            for day, rows, expected in missing_grid[:10]
                        )
                    )

            return errors
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("%s coverage validation failed.", dep.name)
        return [f"coverage validation failed: {exc}"]


def _boot_cursor(raw: str | None) -> dict[str, Any]:
    cursor = _load_cursor(raw, {})
    if not isinstance(cursor, dict) or cursor.get("boot_id") != _STARTUP_BOOT_ID:
        return {"boot_id": _STARTUP_BOOT_ID, "processed": [], "fired": []}
    return cursor


def _startup_run_key(automation_key: str, dep: DeploymentSchedule) -> str:
    # Scope startup run keys to the current boot so rebuilds can re-emit the
    # same startup deployment without Dagster deduping it against an older boot.
    return f"{build_automation_run_key(automation_key, dep)}/{_STARTUP_BOOT_ID}"


def _deployment_has_active_or_success(instance: Any, dep: DeploymentSchedule) -> bool:
    """Return ``True`` when *dep* already has active or successful work."""
    job = ALL_FLOW_JOBS.get(dep.flow)
    if job is None:
        return False
    statuses = list(_ACTIVE_STATUSES | {DagsterRunStatus.SUCCESS})
    return bool(
        _runs_for(
            instance,
            job_name=job.name,
            deployment=dep.name,
            statuses=statuses,
            limit=1,
        )
    )


def _deployment_has_active_run(instance: Any, dep: DeploymentSchedule) -> bool:
    """Return ``True`` when *dep* currently has active work."""
    job = ALL_FLOW_JOBS.get(dep.flow)
    if job is None:
        return False
    return bool(
        _runs_for(
            instance,
            job_name=job.name,
            deployment=dep.name,
            statuses=_ACTIVE_STATUSES,
            limit=1,
        )
    )


def _deployment_has_any_run(instance: Any, dep: DeploymentSchedule) -> bool:
    """Return ``True`` when *dep* already has any recorded Dagster run."""
    job = ALL_FLOW_JOBS.get(dep.flow)
    if job is None:
        return False
    return bool(
        _runs_for(
            instance,
            job_name=job.name,
            deployment=dep.name,
            limit=1,
        )
    )


def _priority_blockers(expected_name: str) -> list[DeploymentSchedule]:
    """Return higher-priority catchup deployments that must run first."""
    if expected_name == "historical-catchup":
        birdweather = _birdweather_catchup_deployment()
        return [birdweather] if birdweather is not None and birdweather.enabled else []
    return []


# ---------------------------------------------------------------------------
# Startup sensor (Phase 1)
# ---------------------------------------------------------------------------


@sensor(
    name=STARTUP_INGESTION_SENSOR_NAME,
    jobs=_SENSOR_TARGET_JOBS,
    minimum_interval_seconds=_SENSOR_DEFAULT_INTERVAL_SECONDS,
    default_status=DefaultSensorStatus.RUNNING,
    description=(
        "Fires Phase-1 (ingestion / automatic catchup) deployments flagged "
        "run_on_startup once per code-server boot."
    ),
)
def startup_sensor(
    context: SensorEvaluationContext,
) -> Iterator[RunRequest | SkipReason]:
    """Launch missing Phase-1 startup runs once per code-server boot."""
    eligible = [d for d in _startup_eligible() if not _is_postprocessing(d)]
    cursor = _boot_cursor(context.cursor)
    processed_list = list(cursor.get("processed", []))
    processed_set = set(processed_list)
    new_processed: list[str] = list(processed_list)
    inflight_name = str(cursor["inflight"]) if cursor.get("inflight") else None
    inflight_dep = next(
        (dep for dep in eligible if dep.name == inflight_name),
        None,
    )

    def _startup_cursor(*, inflight: str | None) -> str:
        payload: dict[str, Any] = {
            "boot_id": _STARTUP_BOOT_ID,
            "processed": new_processed,
        }
        if inflight:
            payload["inflight"] = inflight
        return _dump_cursor(payload)

    if inflight_dep is not None:
        if not _deployment_has_any_run(context.instance, inflight_dep):
            context.update_cursor(_startup_cursor(inflight=inflight_name))
            yield SkipReason(
                f"Waiting for startup deployment to materialize: {inflight_name}."
            )
            return
        if _deployment_has_active_run(context.instance, inflight_dep):
            context.update_cursor(_startup_cursor(inflight=inflight_name))
            yield SkipReason(
                "Waiting for active startup deployment(s): " + inflight_name
            )
            return
        inflight_name = None

    active_names: list[str] = []
    active_set: set[str] = set()
    for dep in eligible:
        if _deployment_has_active_run(context.instance, dep):
            active_names.append(dep.name)
            active_set.add(dep.name)

    pending: list[DeploymentSchedule] = []
    for dep in eligible:
        if dep.name in processed_set:
            continue
        if dep.name in active_set or _deployment_has_success(context.instance, dep):
            new_processed.append(dep.name)
            processed_set.add(dep.name)
            continue
        pending.append(dep)

    if active_names:
        context.update_cursor(_startup_cursor(inflight=inflight_name))
        yield SkipReason(
            "Waiting for active startup deployment(s): " + ", ".join(active_names)
        )
        return

    request: RunRequest | None = None
    for dep in pending:
        job = FLOW_JOBS.get(dep.flow)
        if job is None:
            continue
        params = _resolve_parameters(dep)
        run_config = _run_config_for(dep.flow, params)
        automation_name = f"sensor/{STARTUP_INGESTION_AUTOMATION_KEY}/{dep.name}"
        request = RunRequest(
            run_key=_startup_run_key(STARTUP_INGESTION_AUTOMATION_KEY, dep),
            run_config=run_config,
            job_name=job.name,
            tags=build_run_tags(
                dep,
                trigger="sensor",
                automation_name=automation_name,
                params=params,
            ),
        )
        new_processed.append(dep.name)
        processed_set.add(dep.name)
        break

    if request is None:
        context.update_cursor(_startup_cursor(inflight=None))
        yield SkipReason(
            "All eligible startup deployments already handled for this boot."
        )
        return

    yield request

    context.update_cursor(_startup_cursor(inflight=request.tags["avian/deployment"]))


# ---------------------------------------------------------------------------
# BirdWeather bundle sync sensor
# ---------------------------------------------------------------------------


@sensor(
    name=BIRDWEATHER_BUNDLE_SYNC_SENSOR_NAME,
    jobs=_SENSOR_TARGET_JOBS,
    minimum_interval_seconds=_SENSOR_DEFAULT_INTERVAL_SECONDS,
    default_status=(
        DefaultSensorStatus.RUNNING
        if (
            _birdweather_bundle_sync_deployment() is not None
            and _birdweather_bundle_sync_deployment().enabled
        )
        else DefaultSensorStatus.STOPPED
    ),
    description=(
        "Launches BirdWeather bundle-sync runs for pending local date bundles "
        "under data/raw/birdweather."
    ),
)
def birdweather_bundle_sync_sensor(
    context: SensorEvaluationContext,
) -> Iterator[RunRequest | SkipReason]:
    """Launch one BirdWeather bundle-sync run per pending local bundle date."""
    dep = _birdweather_bundle_sync_deployment()
    if dep is None or dep.flow not in FLOW_JOBS:
        yield SkipReason("birdweather-bundle-sync deployment is not configured.")
        return
    if not dep.enabled:
        yield SkipReason("birdweather-bundle-sync deployment is disabled.")
        return

    from avian_biosurveillance.ingestion.birdweather import (
        find_pending_birdweather_bundle_dirs,
    )

    params = _resolve_parameters(dep)
    pending = find_pending_birdweather_bundle_dirs(
        Path(str(params.get("output_dir", "data/raw/birdweather"))),
        limit=int(params.get("limit", 1) or 1),
    )
    if not pending:
        yield SkipReason("No pending BirdWeather local bundles found.")
        return

    job = FLOW_JOBS[dep.flow]
    automation_name = f"sensor/{BIRDWEATHER_BUNDLE_SYNC_AUTOMATION_KEY}"

    for bundle_dir in pending:
        logical_date = bundle_dir.name
        active_or_success = _runs_for(
            context.instance,
            job_name=job.name,
            deployment=dep.name,
            logical_date=logical_date,
            statuses=list(_ACTIVE_STATUSES | {DagsterRunStatus.SUCCESS}),
            limit=1,
        )
        if active_or_success:
            continue

        run_params = dict(params)
        run_params["bundle_dates"] = [logical_date]
        run_params["limit"] = 1
        run_config = _run_config_for(dep.flow, run_params)
        yield RunRequest(
            run_key=f"automation/{BIRDWEATHER_BUNDLE_SYNC_AUTOMATION_KEY}/{logical_date}",
            run_config=run_config,
            job_name=job.name,
            tags=build_run_tags(
                dep,
                trigger="sensor",
                automation_name=automation_name,
                params=run_params,
                extra_tags={
                    "avian/logical_date": logical_date,
                    "avian/cohort": "daily",
                },
            ),
        )
        return

    yield SkipReason(
        "All pending BirdWeather local bundles already have active or successful sync runs."
    )


# ---------------------------------------------------------------------------
# Phase-2 sensor (post-processing follow-up)
# ---------------------------------------------------------------------------


@sensor(
    name=STARTUP_POSTPROCESSING_SENSOR_NAME,
    jobs=_SENSOR_TARGET_JOBS,
    minimum_interval_seconds=_SENSOR_DEFAULT_INTERVAL_SECONDS,
    default_status=DefaultSensorStatus.RUNNING,
    description=(
        "Fires Phase-2 (post-processing) deployments once one of each "
        "deployment's declared upstream dependencies has produced a "
        "successful run."
    ),
)
def phase2_sensor(
    context: SensorEvaluationContext,
) -> Iterator[RunRequest | SkipReason]:
    """Launch startup post-processing backstops once their upstreams are ready."""
    eligible = _startup_eligible()
    phase1 = [d for d in eligible if not _is_postprocessing(d)]
    phase2 = [d for d in eligible if _is_postprocessing(d)]

    cursor = _boot_cursor(context.cursor)
    fired = list(cursor.get("fired", []))

    fired_set = set(fired)

    requests: list[RunRequest] = []
    waiting_on: list[str] = []
    paused_for_backlog: list[str] = []
    for dep in phase2:
        if dep.name in fired_set:
            continue
        backlog_pause_reason = _birdweather_backlog_pause_reason(dep)
        if backlog_pause_reason is not None:
            paused_for_backlog.append(dep.name)
            continue
        if _deployment_has_active_or_success(context.instance, dep):
            fired.append(dep.name)
            fired_set.add(dep.name)
            continue

        upstreams = _phase2_dependencies(dep, phase1)
        if not upstreams or not any(
            _deployment_has_success(context.instance, upstream)
            for upstream in upstreams
        ):
            waiting_on.append(dep.name)
            continue

        job = FLOW_JOBS.get(dep.flow)
        if job is None:
            continue
        params = _resolve_parameters(dep)
        run_config = _run_config_for(dep.flow, params)
        automation_name = f"sensor/{STARTUP_POSTPROCESSING_AUTOMATION_KEY}/{dep.name}"
        requests.append(
            RunRequest(
                run_key=_startup_run_key(STARTUP_POSTPROCESSING_AUTOMATION_KEY, dep),
                run_config=run_config,
                job_name=job.name,
                tags=build_run_tags(
                    dep,
                    trigger="sensor",
                    automation_name=automation_name,
                    params=params,
                ),
            )
        )
        fired.append(dep.name)
        fired_set.add(dep.name)

    if not requests:
        if requests or context.cursor or fired != list(cursor.get("fired", [])):
            context.update_cursor(
                _dump_cursor(
                    {
                        "boot_id": _STARTUP_BOOT_ID,
                        "fired": sorted(set(fired)),
                    }
                )
            )
        if waiting_on:
            yield SkipReason(
                "Waiting for successful upstream runs for: " + ", ".join(waiting_on)
            )
            return
        if paused_for_backlog:
            yield SkipReason(
                "BirdWeather backlog mode keeps startup backstops paused: "
                + ", ".join(paused_for_backlog)
            )
            return
        yield SkipReason("All Phase-2 deployments already fired for this boot.")
        return

    yield from requests

    context.update_cursor(
        _dump_cursor(
            {
                "boot_id": _STARTUP_BOOT_ID,
                "fired": sorted(set(fired)),
            }
        )
    )


# ---------------------------------------------------------------------------
# Daily quality follow-up sensor
# ---------------------------------------------------------------------------


@sensor(
    name=DAILY_QUALITY_SENSOR_NAME,
    jobs=_SENSOR_TARGET_JOBS,
    minimum_interval_seconds=_SENSOR_DEFAULT_INTERVAL_SECONDS,
    default_status=DefaultSensorStatus.RUNNING,
    description=(
        "Fires the quality audit once every declared daily dependency has "
        "produced a SUCCESS run for the same logical date."
    ),
)
def daily_quality_sensor(
    context: SensorEvaluationContext,
) -> Iterator[RunRequest | SkipReason]:
    """Launch a quality audit per fully-completed daily logical date."""
    quality_dep = _quality_deployment()
    if quality_dep is None or quality_dep.flow not in FLOW_JOBS:
        yield SkipReason("quality-audit deployment is not configured.")
        return

    daily_deps = _quality_dependencies()
    if not daily_deps:
        yield SkipReason("No daily ingestion deployments configured.")
        return

    quality_job = FLOW_JOBS[quality_dep.flow]

    # Find logical dates where every daily ingestion has at least one SUCCESS.
    success_dates_per_dep: list[set[str]] = []
    for dep in daily_deps:
        job = FLOW_JOBS.get(dep.flow)
        if job is None:
            success_dates_per_dep.append(set())
            continue
        runs = _runs_for(
            context.instance,
            job_name=job.name,
            deployment=dep.name,
            statuses=[DagsterRunStatus.SUCCESS],
        )
        dates = {d for d in (_run_tag(r, "avian/logical_date") for r in runs) if d}
        success_dates_per_dep.append(dates)

    if not success_dates_per_dep:
        yield SkipReason("No completed daily runs yet.")
        return

    completed_dates = set.intersection(*success_dates_per_dep)
    cursor = _load_cursor(context.cursor, {"fired": []})
    fired = set(cursor.get("fired", []))
    pending = sorted(completed_dates - fired)

    if not pending:
        yield SkipReason("All completed daily cohorts already audited.")
        return

    params = _resolve_parameters(quality_dep)
    run_config = _run_config_for(quality_dep.flow, params)
    automation_name = f"sensor/{DAILY_QUALITY_AUTOMATION_KEY}"
    new_fired = set(fired)
    for logical_date in pending:
        tags = build_run_tags(
            quality_dep,
            trigger="sensor",
            automation_name=automation_name,
            params=params,
            extra_tags={
                "avian/logical_date": logical_date,
                "avian/cohort": "daily",
            },
        )
        yield RunRequest(
            run_key=f"automation/{DAILY_QUALITY_AUTOMATION_KEY}/{logical_date}",
            run_config=run_config,
            job_name=quality_job.name,
            tags=tags,
        )
        new_fired.add(logical_date)

    context.update_cursor(_dump_cursor({"fired": sorted(new_fired)}))


# ---------------------------------------------------------------------------
# Historical catchup sensor
# ---------------------------------------------------------------------------


def _parse_date(value: str | None, fallback: date | None = None) -> date | None:
    if value is None:
        return fallback
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return fallback


def _catchup_batch_end(current: date, end: date, params: Mapping[str, Any]) -> date:
    """Return the inclusive end date for the next catchup run window."""
    raw_value = params.get("followup_batch_days", 1)
    try:
        batch_days = max(1, int(raw_value))
    except (TypeError, ValueError):
        batch_days = 1
    return min(current + timedelta(days=batch_days - 1), end)


def _catchup_cursor_payload(
    *,
    dep: DeploymentSchedule,
    params: Mapping[str, Any],
    current_date: date | str,
    attempt: int,
) -> dict[str, Any]:
    """Return a catchup cursor payload, including source-scope markers."""
    payload: dict[str, Any] = {
        "direction": _HISTORICAL_CATCHUP_CURSOR_DIRECTION,
        "current_date": str(current_date),
        "attempt": attempt,
    }
    if dep.name == "birdweather-catchup" and params.get("all_species"):
        payload["scope"] = "all_species"
    return payload


def _run_date_window_catchup_sensor(
    context: SensorEvaluationContext,
    *,
    expected_name: str,
    deployment_getter: Callable[[], DeploymentSchedule | None],
    automation_key: str,
) -> Iterator[RunRequest | SkipReason]:
    """Launch one chronological catchup window per tick, gated on prior success."""
    dep = deployment_getter()
    if dep is None or dep.flow not in FLOW_JOBS:
        yield SkipReason(f"{expected_name} deployment is not configured.")
        return
    if not dep.enabled:
        yield SkipReason(f"{dep.name} deployment is disabled.")
        return

    job = FLOW_JOBS[dep.flow]
    params = _resolve_parameters(dep)
    start = _parse_date(params.get("start_date"))
    end = _parse_date(params.get("end_date"), fallback=_utc_today() - timedelta(days=1))
    if start is None or end is None or end < start:
        yield SkipReason(
            f"Invalid catchup window: start={params.get('start_date')!r} "
            f"end={params.get('end_date')!r}."
        )
        return

    blockers = [
        blocker.name
        for blocker in _priority_blockers(expected_name)
        if blocker.flow in ALL_FLOW_JOBS
        and _deployment_has_active_run(context.instance, blocker)
    ]
    if blockers:
        yield SkipReason(
            f"{dep.name} waiting for higher-priority deployment(s): "
            f"{', '.join(blockers)}."
        )
        return

    cursor = _load_cursor(context.cursor, None)
    if not isinstance(cursor, dict):
        current = start
        attempt = 0
    else:
        # Legacy reverse-order cursors had no direction marker and started at
        # the window end. Reset them to the configured start so older cursor
        # state cannot skip the earlier days in the catchup window.
        if cursor.get("direction") != _HISTORICAL_CATCHUP_CURSOR_DIRECTION or (
            dep.name == "birdweather-catchup"
            and params.get("all_species")
            and cursor.get("scope") != "all_species"
        ):
            current = start
            attempt = 0
        else:
            current = _parse_date(cursor.get("current_date"), fallback=start) or start
            attempt = int(cursor.get("attempt", 0))

    if dep.name == "birdweather-catchup" and not params.get("all_species"):
        resume_from = _birdweather_resume_date(start, end)
        if resume_from is not None and resume_from > current:
            logger.warning(
                "%s cursor fast-forwarded from %s to %s based on "
                "contiguous birdweather_tbl coverage.",
                dep.name,
                current.isoformat(),
                resume_from.isoformat(),
            )
            current = resume_from
            attempt = 0

    if current > end:
        yield SkipReason(f"{dep.name} complete (last day: {end}).")
        return

    batch_end = _catchup_batch_end(current, end, params)
    logical_date = current.isoformat()
    runs = _runs_for(
        context.instance,
        job_name=job.name,
        deployment=dep.name,
        logical_date=logical_date,
    )

    if any(_run_status(r) in _ACTIVE_STATUSES for r in runs):
        yield SkipReason(
            f"{dep.name} window {logical_date}..{batch_end.isoformat()} is currently running."
        )
        return

    if any(_run_status(r) is DagsterRunStatus.SUCCESS for r in runs):
        coverage_errors = _validate_catchup_window_coverage(
            dep=dep,
            params=params,
            start=current,
            end=batch_end,
        )
        if coverage_errors:
            context.log.error(
                "%s window %s..%s succeeded but coverage validation failed; "
                "cursor remains parked at %s. Errors: %s",
                dep.name,
                logical_date,
                batch_end.isoformat(),
                logical_date,
                "; ".join(coverage_errors),
            )
            yield SkipReason(
                f"{dep.name} window {logical_date}..{batch_end.isoformat()} "
                "succeeded but coverage validation failed; cursor not advanced."
            )
            return

        new_date = batch_end + timedelta(days=1)
        context.update_cursor(
            _dump_cursor(
                _catchup_cursor_payload(
                    dep=dep,
                    params=params,
                    current_date=new_date.isoformat(),
                    attempt=0,
                )
            )
        )
        yield SkipReason(
            f"{dep.name} window {logical_date}..{batch_end.isoformat()} succeeded; advanced to {new_date}."
        )
        return

    failed_runs = [r for r in runs if _run_status(r) is DagsterRunStatus.FAILURE]
    if failed_runs and attempt >= _HISTORICAL_MAX_ATTEMPTS:
        context.log.error(
            "%s window %s..%s has failed %d time(s); retrying without advancing.",
            dep.name,
            logical_date,
            batch_end.isoformat(),
            attempt,
        )

    day_params = {
        **params,
        "start_date": logical_date,
        "end_date": batch_end.isoformat(),
        "run_tracker_refresh": batch_end == end,
    }
    run_config = _run_config_for(dep.flow, day_params)
    automation_name = f"sensor/{automation_key}"
    tags = build_run_tags(
        dep,
        trigger="sensor",
        automation_name=automation_name,
        params=day_params,
        extra_tags={
            "avian/logical_date": logical_date,
            "avian/window_end_date": batch_end.isoformat(),
            "avian/cohort": "historical-day",
            "avian/attempt": str(attempt + 1),
        },
    )

    yield RunRequest(
        run_key=(
            f"automation/{automation_key}/"
            f"{logical_date}..{batch_end.isoformat()}/attempt-{attempt + 1}"
        ),
        run_config=run_config,
        job_name=job.name,
        tags=tags,
    )

    context.update_cursor(
        _dump_cursor(
            _catchup_cursor_payload(
                dep=dep,
                params=params,
                current_date=logical_date,
                attempt=attempt + 1,
            )
        )
    )


@sensor(
    name=BIRDWEATHER_CATCHUP_SENSOR_NAME,
    jobs=_SENSOR_TARGET_JOBS,
    minimum_interval_seconds=_BIRDWEATHER_CATCHUP_INTERVAL_SECONDS,
    default_status=_catchup_sensor_default_status(_birdweather_catchup_deployment()),
    description=(
        "Walks the BirdWeather catchup date window from start to end, "
        "launching a single one-day BirdWeather catchup run per tick and "
        "only advancing once the previous date has reached SUCCESS."
    ),
)
def birdweather_catchup_sensor(
    context: SensorEvaluationContext,
) -> Iterator[RunRequest | SkipReason]:
    """Launch one BirdWeather catchup day per tick, gated on prior success."""
    yield from _run_date_window_catchup_sensor(
        context,
        expected_name="birdweather-catchup",
        deployment_getter=_birdweather_catchup_deployment,
        automation_key=BIRDWEATHER_CATCHUP_AUTOMATION_KEY,
    )


@sensor(
    name=HISTORICAL_CATCHUP_SENSOR_NAME,
    jobs=_SENSOR_TARGET_JOBS,
    minimum_interval_seconds=_HISTORICAL_CATCHUP_INTERVAL_SECONDS,
    default_status=_catchup_sensor_default_status(_historical_catchup_deployment()),
    description=(
        "Walks the historical-catchup date window from start to end, "
        "launching a single one-day pipeline run per tick and only "
        "advancing once the previous date has reached SUCCESS."
    ),
)
def historical_catchup_sensor(
    context: SensorEvaluationContext,
) -> Iterator[RunRequest | SkipReason]:
    """Launch one shared non-BirdWeather catchup day per tick."""
    yield from _run_date_window_catchup_sensor(
        context,
        expected_name="historical-catchup",
        deployment_getter=_historical_catchup_deployment,
        automation_key=HISTORICAL_CATCHUP_AUTOMATION_KEY,
    )


SENSORS = [
    startup_sensor,
    birdweather_bundle_sync_sensor,
    phase2_sensor,
    daily_quality_sensor,
    birdweather_catchup_sensor,
    historical_catchup_sensor,
]


__all__ = [
    "FLOW_JOBS",
    "SENSORS",
    "_birdweather_bundle_sync_deployment",
    "_birdweather_catchup_deployment",
    "_daily_ingestion_deployments",
    "_historical_catchup_deployment",
    "_quality_deployment",
    "_resolve_parameters",
    "_run_config_for",
    "_startup_eligible",
    "birdweather_bundle_sync_sensor",
    "birdweather_catchup_sensor",
    "daily_quality_sensor",
    "historical_catchup_sensor",
    "phase2_sensor",
    "startup_sensor",
]
