"""Schedule definitions derived from ``config/schedules.json``.

For every enabled deployment, builds a :class:`~dagster.ScheduleDefinition`
that targets the corresponding ingestion asset job, postprocessing asset job,
or backfill op job from
:mod:`avian_biosurveillance.orchestration.dagster_defs.jobs`.
"""

from __future__ import annotations

import logging

from dagster import (
    DagsterRunStatus,
    DefaultScheduleStatus,
    RunRequest,
    ScheduleDefinition,
    ScheduleEvaluationContext,
    SkipReason,
    schedule,
)

from avian_biosurveillance.config.schedules import (
    DeploymentSchedule,
    schedule_registry,
)
from avian_biosurveillance.orchestration.dagster_defs.code_locations import (
    CODE_LOCATION_ALL,
    active_code_location,
)
from avian_biosurveillance.orchestration.dagster_defs.jobs import (
    ALL_FLOW_JOBS,
    FLOW_JOBS,
    run_config_for,
)
from avian_biosurveillance.orchestration.dagster_defs.naming import (
    build_run_tags,
    build_schedule_automation_name,
    build_schedule_run_key,
    build_temporal_tags,
    dagster_name,
)
from avian_biosurveillance.orchestration.dagster_defs.sensors import (
    _dependency_deployments,
    _resolve_parameters,
    _runs_for,
)


logger = logging.getLogger(__name__)


_BIRDWEATHER_BACKLOG_BACKSTOP_DEPLOYMENTS = frozenset(
    {
        "birdweather-raw-audio-ingestion",
        "birdweather-audio-processing",
        "birdweather-feature-engineering",
        "model-anomaly-scoring",
    }
)


def _is_cron_managed(dep: DeploymentSchedule) -> bool:
    """Return ``True`` when the deployment should create a Dagster schedule."""
    return dep.has_execution_mode("cron")


def _scheduled_dependency_deployments(
    dep: DeploymentSchedule,
) -> list[DeploymentSchedule]:
    return [
        upstream
        for upstream in _dependency_deployments(dep)
        if upstream.has_execution_mode("cron")
    ]


def _birdweather_backlog_pause_reason(dep: DeploymentSchedule) -> str | None:
    if dep.name not in _BIRDWEATHER_BACKLOG_BACKSTOP_DEPLOYMENTS:
        return None

    birdweather_daily = schedule_registry.get("birdweather-daily")
    birdweather_catchup = schedule_registry.get("birdweather-catchup")
    if (
        birdweather_daily is None
        or birdweather_catchup is None
        or birdweather_daily.enabled
        or not birdweather_catchup.enabled
    ):
        return None

    return (
        "BirdWeather backlog mode is active; "
        f"{dep.name} stays paused while birdweather-daily is disabled "
        "and birdweather-catchup owns the 2024 backfill."
    )


def _dependency_skip_reason(
    context: ScheduleEvaluationContext,
    dep: DeploymentSchedule,
    *,
    logical_date: str | None,
) -> SkipReason | None:
    backlog_pause_reason = _birdweather_backlog_pause_reason(dep)
    if backlog_pause_reason is not None:
        scope = f" for logical date {logical_date}" if logical_date else ""
        return SkipReason(backlog_pause_reason + scope)

    instance = getattr(context, "instance", None)
    if instance is None:
        return None

    upstreams = _scheduled_dependency_deployments(dep)
    if not upstreams:
        return None

    waiting_on: list[str] = []
    for upstream in upstreams:
        job = ALL_FLOW_JOBS.get(upstream.flow)
        if job is None:
            continue
        runs = _runs_for(
            instance,
            job_name=job.name,
            deployment=upstream.name,
            logical_date=logical_date,
            statuses=[DagsterRunStatus.SUCCESS],
            limit=1,
        )
        if not runs:
            waiting_on.append(upstream.name)

    if not waiting_on:
        return None

    scope = f" for logical date {logical_date}" if logical_date else ""
    return SkipReason(
        "Waiting for successful upstream cron dependencies for "
        f"{dep.name}: {', '.join(waiting_on)}{scope}."
    )


def _build_schedule(dep: DeploymentSchedule) -> ScheduleDefinition | None:
    job = FLOW_JOBS.get(dep.flow)
    if job is None:
        return None

    schedule_name = dagster_name(dep.name)
    automation_name = build_schedule_automation_name(dep)
    params = _resolve_parameters(dep)
    run_config = run_config_for(dep.flow, params) or {}

    @schedule(
        name=schedule_name,
        cron_schedule=dep.cron,
        job=job,
        execution_timezone="UTC",
        default_status=(
            DefaultScheduleStatus.RUNNING
            if dep.enabled
            else DefaultScheduleStatus.STOPPED
        ),
        description=dep.description or None,
    )
    def _scheduled_run(_ctx: ScheduleEvaluationContext) -> RunRequest | SkipReason:
        extra_tags = build_temporal_tags(
            dep,
            params,
            scheduled_execution_time=_ctx.scheduled_execution_time,
        )
        dependency_skip = _dependency_skip_reason(
            _ctx,
            dep,
            logical_date=extra_tags.get("avian/logical_date"),
        )
        if dependency_skip is not None:
            return dependency_skip
        return RunRequest(
            run_key=build_schedule_run_key(dep, _ctx.scheduled_execution_time),
            run_config=run_config,
            tags=build_run_tags(
                dep,
                trigger="schedule",
                automation_name=automation_name,
                params=params,
                extra_tags=extra_tags,
            ),
        )

    return _scheduled_run


def build_schedules() -> list[ScheduleDefinition]:
    schedules: list[ScheduleDefinition] = []
    registered: list[str] = []
    non_cron: list[str] = []
    other_location: list[DeploymentSchedule] = []
    unsupported: list[DeploymentSchedule] = []
    active_location = active_code_location()

    for dep in schedule_registry:
        if not _is_cron_managed(dep):
            non_cron.append(dep.name)
            continue
        sched = _build_schedule(dep)
        if sched is None:
            if dep.flow in ALL_FLOW_JOBS and active_location != CODE_LOCATION_ALL:
                other_location.append(dep)
                continue
            unsupported.append(dep)
            continue
        schedules.append(sched)
        registered.append(dep.name)

    if registered:
        running = sum(
            1 for dep in schedule_registry if dep.name in registered and dep.enabled
        )
        logger.info(
            "Registered %d Dagster schedule(s) (%d running, %d stopped).",
            len(registered),
            running,
            len(registered) - running,
        )
        logger.debug("Dagster schedules: %s", ", ".join(registered))

    if unsupported:
        logger.warning(
            "Skipped %d schedule entr%s with no Dagster target: %s",
            len(unsupported),
            "y" if len(unsupported) == 1 else "ies",
            ", ".join(f"{dep.name} -> {dep.flow}" for dep in unsupported),
        )

    if other_location:
        logger.info(
            "Skipped %d schedule entr%s owned by another Dagster code location: %s",
            len(other_location),
            "y" if len(other_location) == 1 else "ies",
            ", ".join(f"{dep.name} -> {dep.flow}" for dep in other_location),
        )

    if non_cron:
        logger.info(
            "Skipped %d non-cron deployment entr%s: %s",
            len(non_cron),
            "y" if len(non_cron) == 1 else "ies",
            ", ".join(non_cron),
        )

    return schedules


__all__ = ["build_schedules"]
