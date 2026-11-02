"""Tests for Dagster startup sensors."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, TypedDict, cast

import pytest


dagster = pytest.importorskip("dagster")
RunRequest = dagster.RunRequest
SkipReason = dagster.SkipReason

from avian_biosurveillance.config.schedules import DeploymentSchedule  # noqa: E402
from avian_biosurveillance.orchestration.dagster_defs import sensors  # noqa: E402


class RunRecord(TypedDict):
    """Minimal run-storage payload used by the fake Dagster instance."""

    job_name: str
    status: object
    tags: dict[str, str]


class FakeDagsterInstance:
    """Minimal Dagster instance stub for sensor run-history lookups."""

    def __init__(self, existing_runs: list[RunRecord] | None = None):
        self._existing_runs = existing_runs or []

    def get_runs(self, *, limit: int | None = None, filters: Any) -> list[RunRecord]:
        job_name = getattr(filters, "job_name", None)
        tags = getattr(filters, "tags", {}) or {}
        statuses = getattr(filters, "statuses", None)
        matches = [
            run
            for run in self._existing_runs
            if run["job_name"] == job_name
            and (not statuses or run["status"] in set(statuses))
            and all(run["tags"].get(key) == value for key, value in tags.items())
        ]
        if limit is None:
            return matches
        return matches[:limit]


class FakeSensorContext:
    """Minimal sensor context stub with cursor persistence."""

    def __init__(
        self,
        *,
        cursor: str | None = None,
        existing_runs: list[RunRecord] | None = None,
    ):
        self.cursor = cursor
        self.instance = FakeDagsterInstance(existing_runs)
        self.log = logging.getLogger("tests.orchestration.test_dagster_sensors")
        self.updated_cursor: str | None = None

    def update_cursor(self, cursor: str) -> None:
        self.cursor = cursor
        self.updated_cursor = cursor


def _deployment(
    name: str,
    flow: str,
    *,
    tags: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> DeploymentSchedule:
    return DeploymentSchedule(
        name=name,
        flow=flow,
        cron="0 0 * * *",
        tags=tags or ["ingestion"],
        description=f"Test deployment {name}",
        run_on_startup=True,
        depends_on=depends_on or [],
    )


def _existing_run(job_name: str, deployment_name: str) -> RunRecord:
    return {
        "job_name": job_name,
        "status": dagster.DagsterRunStatus.SUCCESS,
        "tags": {"avian/deployment": deployment_name},
    }


def _patch_sensor_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sensors, "_STARTUP_BOOT_ID", "test-boot")
    flow_jobs = {
        "birdweather_bundle_sync": SimpleNamespace(name="birdweather_bundle_sync_job"),
        "birdweather_catchup": SimpleNamespace(name="birdweather_catchup"),
        "birdweather_ingest": SimpleNamespace(name="birdweather_ingest"),
        "birdweather_repair": SimpleNamespace(name="birdweather_repair"),
        "historical_catchup": SimpleNamespace(name="historical_catchup"),
        "knmi_ingest": SimpleNamespace(name="knmi_ingest"),
        "knmi_repair": SimpleNamespace(name="knmi_repair"),
        "station_summary_refresh": SimpleNamespace(name="station_summary_refresh"),
        "quality_audit": SimpleNamespace(name="quality_audit"),
    }
    monkeypatch.setattr(
        sensors,
        "FLOW_JOBS",
        flow_jobs,
    )
    monkeypatch.setattr(sensors, "ALL_FLOW_JOBS", dict(flow_jobs))
    monkeypatch.setattr(sensors, "_resolve_parameters", lambda dep: dep.parameters)
    monkeypatch.setattr(
        sensors,
        "_run_config_for",
        lambda flow, params: {"flow": flow, "params": params},
    )
    monkeypatch.setattr(
        sensors,
        "_validate_catchup_window_coverage",
        lambda **_kwargs: [],
    )


def _invoke_sensor(sensor_definition: Any, context: FakeSensorContext) -> list[object]:
    raw_fn = vars(sensor_definition)["_raw_fn"]
    return list(raw_fn(context))


def _json_cursor_value(cursor: str | None) -> Any:
    assert cursor is not None
    return json.loads(cursor)


def test_startup_sensor_runs_once_per_boot_and_skips_old_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup runs should not relaunch deployments with prior success."""
    _patch_sensor_runtime(monkeypatch)
    finished = _deployment("birdweather-repair", "birdweather_repair")
    pending = _deployment("knmi-repair", "knmi_repair")
    phase2 = _deployment(
        "station-summary-refresh",
        "station_summary_refresh",
        tags=["post-processing"],
    )
    monkeypatch.setattr(
        sensors,
        "_startup_eligible",
        lambda: [finished, pending, phase2],
    )

    first_context = FakeSensorContext(
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")]
    )

    first_result = _invoke_sensor(sensors.startup_sensor, first_context)

    assert [item.job_name for item in first_result if isinstance(item, RunRequest)] == [
        "knmi_repair",
    ]
    assert [
        item.tags["avian/deployment"]
        for item in first_result
        if isinstance(item, RunRequest)
    ] == ["knmi-repair"]
    assert [item.run_key for item in first_result if isinstance(item, RunRequest)] == [
        "automation/startup-ingestion/knmi-repair/test-boot"
    ]
    assert _json_cursor_value(first_context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "knmi-repair",
        "processed": ["birdweather-repair", "knmi-repair"],
    }

    second_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            _existing_run("birdweather_repair", "birdweather-repair"),
            _existing_run("knmi_repair", "knmi-repair"),
        ],
    )

    second_result = _invoke_sensor(sensors.startup_sensor, second_context)

    assert len(second_result) == 1
    assert isinstance(second_result[0], SkipReason)
    assert _json_cursor_value(second_context.updated_cursor) == {
        "boot_id": "test-boot",
        "processed": ["birdweather-repair", "knmi-repair"],
    }


def test_birdweather_bundle_sync_sensor_emits_oldest_pending_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Pending local BirdWeather bundles should launch one sync run per date."""
    _patch_sensor_runtime(monkeypatch)
    dep = DeploymentSchedule(
        name="birdweather-bundle-sync",
        flow="birdweather_bundle_sync",
        cron="*/10 * * * *",
        tags=["post-processing", "ingestion", "birdweather", "daily"],
        description="Sync BirdWeather local bundles",
        enabled=True,
        run_on_startup=False,
        execution_modes=["sensor", "manual"],
        parameters={
            "limit": 3,
            "delete_on_success": True,
            "output_dir": str(tmp_path),
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_bundle_sync_deployment", lambda: dep)

    import avian_biosurveillance.ingestion.birdweather as birdweather_ingestion

    monkeypatch.setattr(
        birdweather_ingestion,
        "find_pending_birdweather_bundle_dirs",
        lambda *_args, **_kwargs: [
            tmp_path / "2026-05-05",
            tmp_path / "2026-05-06",
        ],
    )

    result = _invoke_sensor(sensors.birdweather_bundle_sync_sensor, FakeSensorContext())

    requests = [item for item in result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["birdweather_bundle_sync_job"]
    assert requests[0].tags["avian/logical_date"] == "2026-05-05"
    assert requests[0].run_config["flow"] == "birdweather_bundle_sync"
    assert requests[0].run_config["params"]["bundle_dates"] == ["2026-05-05"]
    assert requests[0].run_config["params"]["limit"] == 1


def test_birdweather_bundle_sync_sensor_skips_dates_with_successful_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Completed bundle dates should not be re-emitted by the sync sensor."""
    _patch_sensor_runtime(monkeypatch)
    dep = DeploymentSchedule(
        name="birdweather-bundle-sync",
        flow="birdweather_bundle_sync",
        cron="*/10 * * * *",
        tags=["post-processing", "ingestion", "birdweather", "daily"],
        description="Sync BirdWeather local bundles",
        enabled=True,
        run_on_startup=False,
        execution_modes=["sensor", "manual"],
        parameters={
            "limit": 2,
            "delete_on_success": True,
            "output_dir": str(tmp_path),
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_bundle_sync_deployment", lambda: dep)

    import avian_biosurveillance.ingestion.birdweather as birdweather_ingestion

    monkeypatch.setattr(
        birdweather_ingestion,
        "find_pending_birdweather_bundle_dirs",
        lambda *_args, **_kwargs: [tmp_path / "2026-05-05"],
    )

    result = _invoke_sensor(
        sensors.birdweather_bundle_sync_sensor,
        FakeSensorContext(
            existing_runs=[
                {
                    "job_name": "birdweather_bundle_sync_job",
                    "status": dagster.DagsterRunStatus.SUCCESS,
                    "tags": {
                        "avian/deployment": "birdweather-bundle-sync",
                        "avian/logical_date": "2026-05-05",
                    },
                }
            ]
        ),
    )

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)


def test_startup_sensor_skips_active_run_from_prior_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup runs should wait for an active deployment before launching more."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = _deployment("birdweather-repair", "birdweather_repair")
    knmi = _deployment("knmi-repair", "knmi_repair")
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [birdweather, knmi])

    active_context = FakeSensorContext(
        existing_runs=[
            {
                "job_name": "birdweather_repair",
                "status": dagster.DagsterRunStatus.STARTED,
                "tags": {"avian/deployment": "birdweather-repair"},
            }
        ]
    )

    result = _invoke_sensor(sensors.startup_sensor, active_context)

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert _json_cursor_value(active_context.updated_cursor) == {
        "boot_id": "test-boot",
        "processed": ["birdweather-repair"],
    }

    finished_context = FakeSensorContext(
        cursor=active_context.updated_cursor,
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")],
    )

    finished_result = _invoke_sensor(sensors.startup_sensor, finished_context)

    requests = [item for item in finished_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["knmi_repair"]
    assert _json_cursor_value(finished_context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "knmi-repair",
        "processed": ["birdweather-repair", "knmi-repair"],
    }


def test_startup_sensor_serializes_pending_deployments_across_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup work should launch one pending deployment at a time per boot."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = _deployment("birdweather-repair", "birdweather_repair")
    knmi = _deployment("knmi-repair", "knmi_repair")
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [birdweather, knmi])

    first_context = FakeSensorContext()

    first_result = _invoke_sensor(sensors.startup_sensor, first_context)

    first_requests = [item for item in first_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in first_requests] == ["birdweather_repair"]
    assert _json_cursor_value(first_context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "birdweather-repair",
        "processed": ["birdweather-repair"],
    }

    materializing_context = FakeSensorContext(cursor=first_context.updated_cursor)

    materializing_result = _invoke_sensor(sensors.startup_sensor, materializing_context)

    assert len(materializing_result) == 1
    assert isinstance(materializing_result[0], SkipReason)
    assert _json_cursor_value(materializing_context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "birdweather-repair",
        "processed": ["birdweather-repair"],
    }

    active_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            {
                "job_name": "birdweather_repair",
                "status": dagster.DagsterRunStatus.STARTED,
                "tags": {"avian/deployment": "birdweather-repair"},
            }
        ],
    )

    active_result = _invoke_sensor(sensors.startup_sensor, active_context)

    assert len(active_result) == 1
    assert isinstance(active_result[0], SkipReason)
    assert _json_cursor_value(active_context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "birdweather-repair",
        "processed": ["birdweather-repair"],
    }

    finished_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")],
    )

    finished_result = _invoke_sensor(sensors.startup_sensor, finished_context)

    finished_requests = [
        item for item in finished_result if isinstance(item, RunRequest)
    ]
    assert [item.job_name for item in finished_requests] == ["knmi_repair"]
    assert _json_cursor_value(finished_context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "knmi-repair",
        "processed": ["birdweather-repair", "knmi-repair"],
    }


def test_startup_sensor_updates_cursor_only_after_generator_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup sensor should not advance its cursor before yielding work."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = _deployment("birdweather-repair", "birdweather_repair")
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [birdweather])

    context = FakeSensorContext()
    raw_fn = vars(sensors.startup_sensor)["_raw_fn"]
    generator = raw_fn(context)

    first_item = next(generator)

    assert isinstance(first_item, RunRequest)
    assert context.updated_cursor is None

    with pytest.raises(StopIteration):
        next(generator)

    assert _json_cursor_value(context.updated_cursor) == {
        "boot_id": "test-boot",
        "inflight": "birdweather-repair",
        "processed": ["birdweather-repair"],
    }


def test_phase2_sensor_waits_for_phase1_and_fires_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2 runs should wait for Phase-1 history, then stay skipped."""
    _patch_sensor_runtime(monkeypatch)
    phase1 = _deployment("birdweather-repair", "birdweather_repair")
    phase2 = _deployment(
        "station-summary-refresh",
        "station_summary_refresh",
        tags=["post-processing"],
    )
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [phase1, phase2])

    waiting_context = FakeSensorContext()

    waiting_result = _invoke_sensor(sensors.phase2_sensor, waiting_context)

    assert len(waiting_result) == 1
    assert isinstance(waiting_result[0], SkipReason)
    assert waiting_context.updated_cursor is None

    ready_context = FakeSensorContext(
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")]
    )

    ready_result = _invoke_sensor(sensors.phase2_sensor, ready_context)

    assert [item.job_name for item in ready_result if isinstance(item, RunRequest)] == [
        "station_summary_refresh"
    ]
    assert [
        item.tags["avian/deployment"]
        for item in ready_result
        if isinstance(item, RunRequest)
    ] == ["station-summary-refresh"]
    assert _json_cursor_value(ready_context.updated_cursor) == {
        "boot_id": "test-boot",
        "fired": ["station-summary-refresh"],
    }

    rerun_context = FakeSensorContext(cursor=ready_context.updated_cursor)

    rerun_result = _invoke_sensor(sensors.phase2_sensor, rerun_context)

    assert len(rerun_result) == 1
    assert isinstance(rerun_result[0], SkipReason)
    assert _json_cursor_value(rerun_context.updated_cursor) == {
        "boot_id": "test-boot",
        "fired": ["station-summary-refresh"],
    }


def test_phase2_sensor_requires_success_not_just_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2 should wait while Phase-1 has only failed or active runs."""
    _patch_sensor_runtime(monkeypatch)
    phase1 = _deployment("birdweather-repair", "birdweather_repair")
    phase2 = _deployment(
        "station-summary-refresh",
        "station_summary_refresh",
        tags=["post-processing"],
    )
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [phase1, phase2])

    failed_context = FakeSensorContext(
        existing_runs=[
            {
                "job_name": "birdweather_repair",
                "status": dagster.DagsterRunStatus.FAILURE,
                "tags": {"avian/deployment": "birdweather-repair"},
            }
        ]
    )

    failed_result = _invoke_sensor(sensors.phase2_sensor, failed_context)

    assert len(failed_result) == 1
    assert isinstance(failed_result[0], SkipReason)
    assert failed_context.updated_cursor is None


def test_phase2_sensor_skips_existing_active_or_success_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2 should not relaunch work that is already active or finished."""
    _patch_sensor_runtime(monkeypatch)
    phase1 = _deployment("birdweather-repair", "birdweather_repair")
    phase2 = _deployment(
        "station-summary-refresh",
        "station_summary_refresh",
        tags=["post-processing"],
    )
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [phase1, phase2])

    existing_context = FakeSensorContext(
        existing_runs=[
            _existing_run("birdweather_repair", "birdweather-repair"),
            {
                "job_name": "station_summary_refresh",
                "status": dagster.DagsterRunStatus.STARTED,
                "tags": {"avian/deployment": "station-summary-refresh"},
            },
        ]
    )

    result = _invoke_sensor(sensors.phase2_sensor, existing_context)

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert _json_cursor_value(existing_context.updated_cursor) == {
        "boot_id": "test-boot",
        "fired": ["station-summary-refresh"],
    }


def test_phase2_sensor_uses_declared_dependencies_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2 should wait for one of the configured upstream dependencies."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = _deployment("birdweather-repair", "birdweather_repair")
    knmi = _deployment("knmi-repair", "knmi_repair")
    phase2 = _deployment(
        "station-summary-refresh",
        "station_summary_refresh",
        tags=["post-processing"],
        depends_on=["knmi-repair"],
    )
    monkeypatch.setattr(
        sensors, "_startup_eligible", lambda: [birdweather, knmi, phase2]
    )

    wrong_upstream_context = FakeSensorContext(
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")]
    )

    wrong_upstream_result = _invoke_sensor(
        sensors.phase2_sensor, wrong_upstream_context
    )

    assert len(wrong_upstream_result) == 1
    assert isinstance(wrong_upstream_result[0], SkipReason)

    correct_upstream_context = FakeSensorContext(
        existing_runs=[_existing_run("knmi_repair", "knmi-repair")]
    )

    correct_upstream_result = _invoke_sensor(
        sensors.phase2_sensor, correct_upstream_context
    )

    requests = [
        item for item in correct_upstream_result if isinstance(item, RunRequest)
    ]
    assert [item.job_name for item in requests] == ["station_summary_refresh"]


def test_phase2_sensor_updates_cursor_only_after_generator_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2 sensor should not record fired work before yielding it."""
    _patch_sensor_runtime(monkeypatch)
    phase1 = _deployment("birdweather-repair", "birdweather_repair")
    phase2 = _deployment(
        "station-summary-refresh",
        "station_summary_refresh",
        tags=["post-processing"],
    )
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [phase1, phase2])

    context = FakeSensorContext(
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")]
    )
    raw_fn = vars(sensors.phase2_sensor)["_raw_fn"]
    generator = raw_fn(context)

    first_item = next(generator)

    assert isinstance(first_item, RunRequest)
    assert context.updated_cursor is None

    with pytest.raises(StopIteration):
        next(generator)

    assert _json_cursor_value(context.updated_cursor) == {
        "boot_id": "test-boot",
        "fired": ["station-summary-refresh"],
    }


def test_phase2_sensor_pauses_birdweather_backstops_in_backlog_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BirdWeather startup backstops should stay paused while catchup owns backlog work."""
    _patch_sensor_runtime(monkeypatch)
    phase1 = _deployment("birdweather-repair", "birdweather_repair")
    phase2 = _deployment(
        "birdweather-feature-engineering",
        "station_summary_refresh",
        tags=["post-processing", "birdweather", "audio"],
    )
    monkeypatch.setattr(sensors, "_startup_eligible", lambda: [phase1, phase2])
    monkeypatch.setattr(sensors, "_birdweather_backlog_mode_enabled", lambda: True)

    context = FakeSensorContext(
        existing_runs=[_existing_run("birdweather_repair", "birdweather-repair")]
    )

    result = _invoke_sensor(sensors.phase2_sensor, context)

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert "BirdWeather backlog mode keeps startup backstops paused" in result[0].skip_message
    assert context.updated_cursor is None


def test_daily_quality_sensor_waits_for_full_daily_cohort_and_fires_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daily quality should wait for all daily ingestions for a logical date."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = _deployment("birdweather-daily", "birdweather_ingest", tags=["daily"])
    knmi = _deployment("knmi-daily", "knmi_ingest", tags=["daily"])
    quality = DeploymentSchedule(
        name="quality-audit",
        flow="quality_audit",
        cron="0 7 * * *",
        tags=["audit"],
        description="Run quality after daily ingestion",
        enabled=False,
    )

    monkeypatch.setattr(
        sensors, "_daily_ingestion_deployments", lambda: [birdweather, knmi]
    )
    monkeypatch.setattr(sensors, "_quality_deployment", lambda: quality)

    waiting_context = FakeSensorContext(
        existing_runs=[
            {
                "job_name": "birdweather_ingest",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "birdweather-daily",
                    "avian/logical_date": "2026-05-05",
                },
            }
        ]
    )

    waiting_result = _invoke_sensor(sensors.daily_quality_sensor, waiting_context)

    assert len(waiting_result) == 1
    assert isinstance(waiting_result[0], SkipReason)
    assert waiting_context.updated_cursor is None

    ready_runs = cast(
        list[RunRecord],
        [
            {
                "job_name": "birdweather_ingest",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "birdweather-daily",
                    "avian/logical_date": "2026-05-05",
                },
            },
            {
                "job_name": "knmi_ingest",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "knmi-daily",
                    "avian/logical_date": "2026-05-05",
                },
            },
        ],
    )

    ready_context = FakeSensorContext(existing_runs=ready_runs)

    ready_result = _invoke_sensor(sensors.daily_quality_sensor, ready_context)

    requests = [item for item in ready_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["quality_audit"]
    assert [item.tags["avian/logical_date"] for item in requests] == ["2026-05-05"]
    assert _json_cursor_value(ready_context.updated_cursor) == {"fired": ["2026-05-05"]}

    rerun_context = FakeSensorContext(
        cursor=ready_context.updated_cursor,
        existing_runs=ready_runs,
    )

    rerun_result = _invoke_sensor(sensors.daily_quality_sensor, rerun_context)

    assert len(rerun_result) == 1
    assert isinstance(rerun_result[0], SkipReason)


def test_daily_quality_sensor_uses_quality_dependencies_when_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quality should honor its declared upstream cohort instead of all daily jobs."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = _deployment("birdweather-daily", "birdweather_ingest", tags=["daily"])
    knmi = _deployment("knmi-daily", "knmi_ingest", tags=["daily"])
    seismic = _deployment("seismic-daily", "knmi_ingest", tags=["daily"])
    quality = DeploymentSchedule(
        name="quality-audit",
        flow="quality_audit",
        cron="0 7 * * *",
        tags=["audit"],
        description="Run quality after daily ingestion",
        enabled=False,
        depends_on=["birdweather-daily", "knmi-daily"],
    )

    monkeypatch.setattr(
        sensors,
        "_daily_ingestion_deployments",
        lambda: [birdweather, knmi, seismic],
    )
    monkeypatch.setattr(sensors, "_quality_deployment", lambda: quality)

    ready_context = FakeSensorContext(
        existing_runs=[
            {
                "job_name": "birdweather_ingest",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "birdweather-daily",
                    "avian/logical_date": "2026-05-05",
                },
            },
            {
                "job_name": "knmi_ingest",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "knmi-daily",
                    "avian/logical_date": "2026-05-05",
                },
            },
        ]
    )

    ready_result = _invoke_sensor(sensors.daily_quality_sensor, ready_context)

    requests = [item for item in ready_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["quality_audit"]
    assert [item.tags["avian/logical_date"] for item in requests] == ["2026-05-05"]


def test_historical_catchup_sensor_launches_one_day_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical catchup should emit one logical day per run and advance on success."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)

    first_context = FakeSensorContext()

    first_result = _invoke_sensor(sensors.historical_catchup_sensor, first_context)

    requests = [item for item in first_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["historical_catchup"]
    assert requests[0].tags["avian/logical_date"] == "2024-01-01"
    assert requests[0].run_config["flow"] == "historical_catchup"
    assert requests[0].run_config["params"]["start_date"] == "2024-01-01"
    assert requests[0].run_config["params"]["end_date"] == "2024-01-01"
    assert _json_cursor_value(first_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
    }

    running_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            {
                "job_name": "historical_catchup",
                "status": dagster.DagsterRunStatus.STARTED,
                "tags": {
                    "avian/deployment": "historical-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    running_result = _invoke_sensor(sensors.historical_catchup_sensor, running_context)

    assert len(running_result) == 1
    assert isinstance(running_result[0], SkipReason)
    assert running_context.updated_cursor is None

    success_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            {
                "job_name": "historical_catchup",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "historical-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    success_result = _invoke_sensor(sensors.historical_catchup_sensor, success_context)

    assert len(success_result) == 1
    assert isinstance(success_result[0], SkipReason)
    assert _json_cursor_value(success_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-02",
        "attempt": 0,
    }


def test_historical_catchup_sensor_batches_window_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical catchup should launch multi-day windows when configured."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="Batched historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-10",
            "target": "database",
            "followup_batch_days": 7,
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)

    first_context = FakeSensorContext()

    first_result = _invoke_sensor(sensors.historical_catchup_sensor, first_context)

    requests = [item for item in first_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["historical_catchup"]
    assert requests[0].tags["avian/logical_date"] == "2024-01-01"
    assert requests[0].tags["avian/window_end_date"] == "2024-01-07"
    assert requests[0].run_config["params"]["start_date"] == "2024-01-01"
    assert requests[0].run_config["params"]["end_date"] == "2024-01-07"
    assert requests[0].run_config["params"]["run_tracker_refresh"] is False
    assert _json_cursor_value(first_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
    }

    success_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            {
                "job_name": "historical_catchup",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "historical-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    success_result = _invoke_sensor(sensors.historical_catchup_sensor, success_context)

    assert len(success_result) == 1
    assert isinstance(success_result[0], SkipReason)
    assert _json_cursor_value(success_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-08",
        "attempt": 0,
    }


def test_catchup_sensor_does_not_advance_after_success_with_missing_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful Dagster run must not advance the cursor if DB coverage is missing."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)
    monkeypatch.setattr(
        sensors,
        "_validate_catchup_window_coverage",
        lambda **_kwargs: ["knmi_tbl missing day(s): 2024-01-01"],
    )
    cursor = json.dumps(
        {
            "direction": "asc",
            "current_date": "2024-01-01",
            "attempt": 1,
        }
    )
    success_context = FakeSensorContext(
        cursor=cursor,
        existing_runs=[
            {
                "job_name": "historical_catchup",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "historical-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    result = _invoke_sensor(sensors.historical_catchup_sensor, success_context)

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert "coverage validation failed" in result[0].skip_message
    assert success_context.updated_cursor is None
    assert _json_cursor_value(success_context.cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
    }


def test_historical_catchup_sensor_waits_for_active_birdweather_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared historical catchup should not contend with BirdWeather catchup."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="One-day BirdWeather catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)
    monkeypatch.setattr(sensors, "_birdweather_catchup_deployment", lambda: birdweather)

    context = FakeSensorContext(
        existing_runs=[
            {
                "job_name": "birdweather_catchup",
                "status": dagster.DagsterRunStatus.STARTED,
                "tags": {
                    "avian/deployment": "birdweather-catchup",
                    "avian/logical_date": "2024-01-06",
                },
            }
        ]
    )

    result = _invoke_sensor(sensors.historical_catchup_sensor, context)

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert "higher-priority" in result[0].skip_message


def test_birdweather_catchup_sensor_launches_one_day_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BirdWeather catchup should advance independently of the shared backlog."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="One-day BirdWeather catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_catchup_deployment", lambda: birdweather)
    monkeypatch.setattr(sensors, "_birdweather_resume_date", lambda _start, _end: None)

    first_context = FakeSensorContext()

    first_result = _invoke_sensor(sensors.birdweather_catchup_sensor, first_context)

    requests = [item for item in first_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["birdweather_catchup"]
    assert requests[0].tags["avian/logical_date"] == "2024-01-01"
    assert requests[0].run_config["flow"] == "birdweather_catchup"
    assert requests[0].run_config["params"]["start_date"] == "2024-01-01"
    assert requests[0].run_config["params"]["end_date"] == "2024-01-01"
    assert _json_cursor_value(first_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
    }

    success_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            {
                "job_name": "birdweather_catchup",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "birdweather-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    success_result = _invoke_sensor(sensors.birdweather_catchup_sensor, success_context)

    assert len(success_result) == 1
    assert isinstance(success_result[0], SkipReason)
    assert _json_cursor_value(success_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-02",
        "attempt": 0,
    }


def test_birdweather_catchup_sensor_batches_window_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BirdWeather catchup should launch multi-day windows when configured."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="Batched BirdWeather catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-10",
            "target": "database",
            "followup_batch_days": 7,
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_catchup_deployment", lambda: birdweather)
    monkeypatch.setattr(sensors, "_birdweather_resume_date", lambda _start, _end: None)

    first_context = FakeSensorContext()

    first_result = _invoke_sensor(sensors.birdweather_catchup_sensor, first_context)

    requests = [item for item in first_result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["birdweather_catchup"]
    assert requests[0].tags["avian/logical_date"] == "2024-01-01"
    assert requests[0].tags["avian/window_end_date"] == "2024-01-07"
    assert requests[0].run_config["params"]["start_date"] == "2024-01-01"
    assert requests[0].run_config["params"]["end_date"] == "2024-01-07"
    assert requests[0].run_config["params"]["run_tracker_refresh"] is False

    success_context = FakeSensorContext(
        cursor=first_context.updated_cursor,
        existing_runs=[
            {
                "job_name": "birdweather_catchup",
                "status": dagster.DagsterRunStatus.SUCCESS,
                "tags": {
                    "avian/deployment": "birdweather-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    success_result = _invoke_sensor(sensors.birdweather_catchup_sensor, success_context)

    assert len(success_result) == 1
    assert isinstance(success_result[0], SkipReason)
    assert _json_cursor_value(success_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-08",
        "attempt": 0,
    }


def test_birdweather_catchup_sensor_fast_forwards_stale_cursor_from_db_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BirdWeather catchup should resume from persisted DB coverage after reset."""
    from avian_biosurveillance.db import database as db_database

    def _assert_query(query: str, params: tuple[date, date]) -> None:
        assert "generate_series" in query
        assert "birdweather_tbl" in query
        assert "observation_date" in query
        assert params[0] == date(2024, 1, 1)
        assert params[1] == date(2026, 5, 18)

    @contextmanager
    def _fake_cursor(rows: list[date]) -> Iterator[Any]:
        yield SimpleNamespace(
            execute=_assert_query,
            fetchall=lambda: [(row,) for row in rows],
        )

    _patch_sensor_runtime(monkeypatch)
    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="BirdWeather catchup resume",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2026-05-18",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_catchup_deployment", lambda: birdweather)
    contiguous_rows = [
        date(2024, 1, 1) + timedelta(days=offset)
        for offset in range((date(2024, 6, 1) - date(2024, 1, 1)).days)
    ]
    monkeypatch.setattr(
        db_database,
        "get_pool",
        lambda **_kwargs: SimpleNamespace(
            cursor=lambda: _fake_cursor([*contiguous_rows, date(2026, 5, 18)])
        ),
    )

    stale_context = FakeSensorContext(
        cursor=json.dumps(
            {
                "direction": "asc",
                "current_date": "2024-03-11",
                "attempt": 2,
            }
        )
    )

    result = _invoke_sensor(sensors.birdweather_catchup_sensor, stale_context)

    requests = [item for item in result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["birdweather_catchup"]
    assert requests[0].tags["avian/logical_date"] == "2024-06-01"
    assert requests[0].run_config["params"]["start_date"] == "2024-06-01"
    assert requests[0].run_config["params"]["end_date"] == "2024-06-01"
    assert _json_cursor_value(stale_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-06-01",
        "attempt": 1,
    }


def test_catchup_sensor_retries_without_advancing_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catchup should never skip a failed window after the retry budget."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)
    context = FakeSensorContext(
        cursor=json.dumps(
            {
                "direction": "asc",
                "current_date": "2024-01-01",
                "attempt": 3,
            }
        ),
        existing_runs=[
            {
                "job_name": "historical_catchup",
                "status": dagster.DagsterRunStatus.FAILURE,
                "tags": {
                    "avian/deployment": "historical-catchup",
                    "avian/logical_date": "2024-01-01",
                },
            }
        ],
    )

    result = _invoke_sensor(sensors.historical_catchup_sensor, context)

    requests = [item for item in result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["historical_catchup"]
    assert requests[0].tags["avian/attempt"] == "4"
    assert _json_cursor_value(context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 4,
    }


def test_birdweather_all_species_disables_species_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime species defaults should not override explicit all-species mode."""
    dep = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="BirdWeather all-species catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-02",
            "all_species": True,
            "target": "database",
        },
    )
    monkeypatch.delenv("CATCHUP_START_DATE", raising=False)
    monkeypatch.delenv("BACKFILL_START_DATE", raising=False)
    monkeypatch.delenv("CATCHUP_END_DATE", raising=False)
    monkeypatch.delenv("BACKFILL_END_DATE", raising=False)

    params = vars(sensors)["_resolve_parameters"](dep)

    assert params["all_species"] is True
    assert "species_ids" not in params


def test_birdweather_catchup_schedule_defaults_to_target_species() -> None:
    """The shipped BirdWeather catchup deployment should use target species by default."""
    dep = sensors._birdweather_catchup_deployment()

    assert dep is not None

    params = vars(sensors)["_resolve_parameters"](dep)

    assert not params.get("all_species", False)
    assert params["species_ids"] == list(sensors._sp_registry.birdweather_ids)


def test_historical_catchup_schedule_does_not_force_all_species_birdweather() -> None:
    """The shared historical schedule should leave BirdWeather on config defaults."""
    dep = sensors._historical_catchup_deployment()

    assert dep is not None

    params = vars(sensors)["_resolve_parameters"](dep)

    assert not params.get("bw_all_species", False)


def test_birdweather_all_species_resets_target_species_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-species catchup must not trust a cursor from target-species mode."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="BirdWeather all-species catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "all_species": True,
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_catchup_deployment", lambda: birdweather)

    context = FakeSensorContext(
        cursor=json.dumps(
            {
                "direction": "asc",
                "current_date": "2025-01-28",
                "attempt": 1,
            }
        )
    )

    result = _invoke_sensor(sensors.birdweather_catchup_sensor, context)

    requests = [item for item in result if isinstance(item, RunRequest)]
    assert [item.job_name for item in requests] == ["birdweather_catchup"]
    assert requests[0].tags["avian/logical_date"] == "2024-01-01"
    assert requests[0].run_config["params"]["all_species"] is True
    assert _json_cursor_value(context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
        "scope": "all_species",
    }


def test_historical_catchup_sensor_updates_cursor_only_after_generator_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical catchup should not advance attempts before yielding work."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)

    context = FakeSensorContext()
    raw_fn = vars(sensors.historical_catchup_sensor)["_raw_fn"]
    generator = raw_fn(context)

    first_item = next(generator)

    assert isinstance(first_item, RunRequest)
    assert context.updated_cursor is None

    with pytest.raises(StopIteration):
        next(generator)

    assert _json_cursor_value(context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
    }


def test_historical_catchup_sensor_uses_utc_today_for_default_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical catchup fallback boundaries should be derived from UTC."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)
    monkeypatch.setattr(sensors, "_utc_today", lambda: date(2024, 1, 4))

    result = _invoke_sensor(sensors.historical_catchup_sensor, FakeSensorContext())

    requests = [item for item in result if isinstance(item, RunRequest)]
    assert [item.tags["avian/logical_date"] for item in requests] == ["2024-01-01"]


def test_historical_catchup_sensor_resets_legacy_cursor_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy reverse-order cursors should not skip the configured start date."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=True,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)

    legacy_context = FakeSensorContext(
        cursor=json.dumps({"current_date": "2024-01-03", "attempt": 1})
    )

    result = _invoke_sensor(sensors.historical_catchup_sensor, legacy_context)

    requests = [item for item in result if isinstance(item, RunRequest)]
    assert [item.tags["avian/logical_date"] for item in requests] == ["2024-01-01"]
    assert _json_cursor_value(legacy_context.updated_cursor) == {
        "direction": "asc",
        "current_date": "2024-01-01",
        "attempt": 1,
    }


def test_catchup_sensor_default_status_stops_when_deployment_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled catchup deployments should never default to RUNNING."""
    monkeypatch.setenv("CATCHUP_AUTOSTART", "true")

    enabled_dep = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="Enabled historical catchup",
        enabled=True,
    )
    disabled_dep = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="Disabled historical catchup",
        enabled=False,
    )

    assert sensors._catchup_sensor_default_status(enabled_dep) is dagster.DefaultSensorStatus.RUNNING
    assert sensors._catchup_sensor_default_status(disabled_dep) is dagster.DefaultSensorStatus.STOPPED
    assert sensors._catchup_sensor_default_status(None) is dagster.DefaultSensorStatus.RUNNING


def test_historical_catchup_sensor_skips_when_deployment_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled historical catchup should skip even if the sensor is evaluated."""
    _patch_sensor_runtime(monkeypatch)
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="Disabled historical catchup",
        enabled=False,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_historical_catchup_deployment", lambda: historical)

    result = _invoke_sensor(sensors.historical_catchup_sensor, FakeSensorContext())

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert "disabled" in result[0].skip_message


def test_birdweather_catchup_sensor_skips_when_deployment_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled BirdWeather catchup should skip even if the sensor is evaluated."""
    _patch_sensor_runtime(monkeypatch)
    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="Disabled BirdWeather catchup",
        enabled=False,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )
    monkeypatch.setattr(sensors, "_birdweather_catchup_deployment", lambda: birdweather)

    result = _invoke_sensor(sensors.birdweather_catchup_sensor, FakeSensorContext())

    assert len(result) == 1
    assert isinstance(result[0], SkipReason)
    assert "disabled" in result[0].skip_message


def test_historical_catchup_parameters_can_be_overridden_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container startup can adjust the historical catchup window safely."""
    historical = DeploymentSchedule(
        name="historical-catchup",
        flow="historical_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical"],
        description="One-day historical catchup",
        enabled=False,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
            "sources": "birdweather,knmi",
        },
    )
    monkeypatch.setenv("CATCHUP_START_DATE", "2023-01-01")
    monkeypatch.setenv("CATCHUP_END_DATE", "2023-01-31")
    monkeypatch.setenv("CATCHUP_TARGET", "local")
    monkeypatch.setenv("CATCHUP_SOURCES", "knmi,earth_engine_grid")

    params = vars(sensors)["_resolve_parameters"](historical)

    assert params["start_date"] == "2023-01-01"
    assert params["end_date"] == "2023-01-31"
    assert params["target"] == "local"
    assert params["sources"] == "knmi,earth_engine_grid"

    birdweather = DeploymentSchedule(
        name="birdweather-catchup",
        flow="birdweather_catchup",
        cron="0 4 * * *",
        tags=["catchup", "historical", "birdweather"],
        description="One-day BirdWeather catchup",
        enabled=False,
        parameters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "target": "database",
        },
    )

    birdweather_params = vars(sensors)["_resolve_parameters"](birdweather)

    assert birdweather_params["start_date"] == "2023-01-01"
    assert birdweather_params["end_date"] == "2023-01-31"
    assert birdweather_params["target"] == "local"
