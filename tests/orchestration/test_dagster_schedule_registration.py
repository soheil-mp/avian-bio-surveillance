"""Tests for Dagster schedule registration filtering."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest


dagster = pytest.importorskip("dagster")
SkipReason = dagster.SkipReason
from dagster._core.execution.api import create_execution_plan

from avian_biosurveillance.config.schedules import (
    DeploymentSchedule,
    ScheduleRegistry,
    schedule_registry,
)
from avian_biosurveillance.orchestration.dagster_defs import defs
from avian_biosurveillance.orchestration.dagster_defs import (
    schedules as dagster_schedules,
)
from avian_biosurveillance.orchestration.dagster_defs.code_locations import (
    CODE_LOCATION_ENV,
    select_flow_jobs,
)
from avian_biosurveillance.orchestration.dagster_defs.jobs import (
    FLOW_JOBS,
    run_config_for,
)
from avian_biosurveillance.orchestration.dagster_defs.naming import build_temporal_tags
from avian_biosurveillance.orchestration.dagster_defs.sensors import _resolve_parameters


def _deployment(
    name: str,
    *,
    execution_modes: list[str] | None = None,
    enabled: bool = True,
) -> DeploymentSchedule:
    return DeploymentSchedule(
        name=name,
        flow=f"{name}_flow",
        cron="0 0 * * *",
        tags=["test"],
        description=f"Test deployment {name}",
        enabled=enabled,
        execution_modes=execution_modes or ["cron"],
    )


def test_build_schedules_registers_only_cron_managed_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    def _fake_build(dep: DeploymentSchedule) -> Any:
        built.append(dep.name)
        return dep.name

    monkeypatch.setattr(
        dagster_schedules,
        "schedule_registry",
        ScheduleRegistry(
            [
                _deployment("daily-enabled", execution_modes=["cron"], enabled=True),
                _deployment("daily-stopped", execution_modes=["cron"], enabled=False),
                _deployment(
                    "manual-backfill", execution_modes=["manual"], enabled=False
                ),
                _deployment("sensor-owned", execution_modes=["sensor"], enabled=False),
                _deployment("auto-only", execution_modes=["auto"], enabled=False),
            ]
        ),
    )
    monkeypatch.setattr(dagster_schedules, "_build_schedule", _fake_build)

    schedules: Any = dagster_schedules.build_schedules()

    assert schedules == ["daily-enabled", "daily-stopped"]
    assert built == ["daily-enabled", "daily-stopped"]


def test_build_schedules_logs_other_code_location_flows_at_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        dagster_schedules,
        "schedule_registry",
        ScheduleRegistry(
            [
                _deployment("model-only"),
            ]
        ),
    )
    monkeypatch.setattr(dagster_schedules, "FLOW_JOBS", {})
    monkeypatch.setattr(
        dagster_schedules,
        "ALL_FLOW_JOBS",
        {"model-only_flow": object()},
    )
    monkeypatch.setattr(
        dagster_schedules,
        "active_code_location",
        lambda: "ingestion",
    )

    with caplog.at_level("INFO"):
        schedules = dagster_schedules.build_schedules()

    assert schedules == []
    assert "owned by another Dagster code location" in caplog.text
    assert "with no Dagster target" not in caplog.text


def test_cron_deployments_build_execution_plans_from_schedule_config() -> None:
    validated: list[str] = []

    for dep in schedule_registry:
        if not dep.has_execution_mode("cron"):
            continue

        unresolved_job = FLOW_JOBS.get(dep.flow)
        assert unresolved_job is not None, dep.name

        run_config = run_config_for(dep.flow, _resolve_parameters(dep)) or {}
        create_execution_plan(
            defs.resolve_job_def(unresolved_job.name),
            run_config=run_config,
        )
        validated.append(dep.name)

    assert validated


def test_startup_deployments_build_execution_plans_from_sensor_config() -> None:
    validated: list[str] = []
    expected: list[str] = []

    for dep in schedule_registry.startup_deployments:
        unresolved_job = FLOW_JOBS.get(dep.flow)
        if unresolved_job is None:
            continue
        expected.append(dep.name)

        run_config = run_config_for(dep.flow, _resolve_parameters(dep)) or {}
        create_execution_plan(
            defs.resolve_job_def(unresolved_job.name),
            run_config=run_config,
        )
        validated.append(dep.name)

    assert validated == expected


def test_code_location_selector_routes_modeling_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_jobs = {
        "birdweather_ingest": object(),
        "birdweather_audio_processing": object(),
        "birdweather_feature_engineering": object(),
        "birdweather_audio_feature_pipeline": object(),
        "model_anomaly_scoring": object(),
    }

    monkeypatch.setenv(CODE_LOCATION_ENV, "modeling")
    assert set(select_flow_jobs(flow_jobs)) == {
        "birdweather_feature_engineering",
        "model_anomaly_scoring",
    }

    monkeypatch.setenv(CODE_LOCATION_ENV, "ingestion")
    assert set(select_flow_jobs(flow_jobs)) == {
        "birdweather_ingest",
        "birdweather_audio_processing",
    }


def test_dependency_skip_reason_waits_for_missing_cron_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = DeploymentSchedule(
        name="birdweather-audio-processing",
        flow="birdweather_audio_processing",
        cron="15 7 * * *",
        tags=["post-processing"],
        description="upstream",
        execution_modes=["cron", "auto"],
    )
    downstream = DeploymentSchedule(
        name="birdweather-feature-engineering",
        flow="birdweather_feature_engineering",
        cron="30 7 * * *",
        tags=["post-processing"],
        description="downstream",
        execution_modes=["cron", "auto"],
        depends_on=[upstream.name],
    )

    monkeypatch.setattr(
        dagster_schedules,
        "schedule_registry",
        ScheduleRegistry([upstream, downstream]),
    )
    monkeypatch.setattr(
        dagster_schedules,
        "ALL_FLOW_JOBS",
        {
            upstream.flow: SimpleNamespace(name="birdweather_audio_processing_job"),
            downstream.flow: SimpleNamespace(
                name="birdweather_feature_engineering_job"
            ),
        },
    )
    monkeypatch.setattr(dagster_schedules, "_runs_for", lambda *args, **kwargs: [])
    helper = dagster_schedules._dependency_skip_reason
    context: Any = SimpleNamespace(instance=object())

    reason = helper(
        cast(Any, context),
        downstream,
        logical_date="2026-05-15",
    )

    assert isinstance(reason, SkipReason)
    assert "birdweather-audio-processing" in reason.skip_message
    assert "2026-05-15" in reason.skip_message


def test_dependency_skip_reason_ignores_non_cron_upstreams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = DeploymentSchedule(
        name="derived-aggregates-refresh",
        flow="derived_aggregates_refresh",
        cron="45 6 * * *",
        tags=["post-processing"],
        description="derived aggregates",
        execution_modes=["auto", "manual"],
    )
    features = DeploymentSchedule(
        name="birdweather-feature-engineering",
        flow="birdweather_feature_engineering",
        cron="30 7 * * *",
        tags=["post-processing"],
        description="features",
        execution_modes=["cron", "auto"],
    )
    model = DeploymentSchedule(
        name="model-anomaly-scoring",
        flow="model_anomaly_scoring",
        cron="15 8 * * *",
        tags=["modeling", "daily"],
        description="model scoring",
        execution_modes=["cron", "auto"],
        depends_on=[derived.name, features.name],
    )
    calls: list[str] = []

    monkeypatch.setattr(
        dagster_schedules,
        "schedule_registry",
        ScheduleRegistry([derived, features, model]),
    )
    monkeypatch.setattr(
        dagster_schedules,
        "ALL_FLOW_JOBS",
        {
            derived.flow: SimpleNamespace(name="derived_aggregates_refresh_job"),
            features.flow: SimpleNamespace(name="birdweather_feature_engineering_job"),
            model.flow: SimpleNamespace(name="model_anomaly_scoring_job"),
        },
    )

    def _fake_runs(*_args: Any, **kwargs: Any) -> list[object]:
        calls.append(str(kwargs["deployment"]))
        return [object()]

    monkeypatch.setattr(dagster_schedules, "_runs_for", _fake_runs)
    helper = dagster_schedules._dependency_skip_reason
    context: Any = SimpleNamespace(instance=object())

    reason = helper(
        cast(Any, context),
        model,
        logical_date="2026-05-15",
    )

    assert reason is None
    assert calls == ["birdweather-feature-engineering"]


def test_build_temporal_tags_adds_logical_date_for_scheduled_backstops() -> None:
    dep = DeploymentSchedule(
        name="birdweather-feature-engineering",
        flow="birdweather_feature_engineering",
        cron="30 7 * * *",
        tags=["post-processing", "audio"],
        description="features",
        execution_modes=["cron", "auto"],
    )

    tags = build_temporal_tags(
        dep,
        {},
        scheduled_execution_time=datetime(2026, 5, 15, 7, 30, tzinfo=UTC),
    )

    assert tags["avian/logical_date"] == "2026-05-15"
    assert tags["avian/cohort"] == "scheduled"


def test_dependency_skip_reason_pauses_birdweather_backstops_in_backlog_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dep = DeploymentSchedule(
        name="birdweather-feature-engineering",
        flow="birdweather_feature_engineering",
        cron="30 7 * * *",
        tags=["post-processing", "birdweather"],
        description="features",
        execution_modes=["cron", "auto"],
    )
    context: Any = SimpleNamespace(instance=object())

    monkeypatch.setattr(
        dagster_schedules,
        "_birdweather_backlog_pause_reason",
        lambda candidate: (
            "BirdWeather backlog mode is active; birdweather-feature-engineering stays paused while birdweather-daily is disabled and birdweather-catchup owns the 2024 backfill."
            if candidate.name == dep.name
            else None
        ),
    )

    reason = dagster_schedules._dependency_skip_reason(
        cast(Any, context),
        dep,
        logical_date="2026-05-15",
    )

    assert isinstance(reason, SkipReason)
    assert "BirdWeather backlog mode is active" in reason.skip_message
    assert "2026-05-15" in reason.skip_message
