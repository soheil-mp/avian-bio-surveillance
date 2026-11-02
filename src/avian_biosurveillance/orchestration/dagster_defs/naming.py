"""Shared Dagster naming and tagging helpers.

Keeps human-facing automation names, run keys, and run tags consistent
across schedules, sensors, classic jobs, and asset-backed jobs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from avian_biosurveillance.config.schedules import DeploymentSchedule


STARTUP_INGESTION_AUTOMATION_KEY = "startup-ingestion"
STARTUP_POSTPROCESSING_AUTOMATION_KEY = "startup-postprocessing"
DAILY_QUALITY_AUTOMATION_KEY = "daily-quality-followup"
BIRDWEATHER_BUNDLE_SYNC_AUTOMATION_KEY = "birdweather-bundle-sync"
BIRDWEATHER_CATCHUP_AUTOMATION_KEY = "birdweather-catchup"
HISTORICAL_CATCHUP_AUTOMATION_KEY = "historical-catchup"

STARTUP_INGESTION_SENSOR_NAME = "launch_startup_ingestion_runs"
STARTUP_POSTPROCESSING_SENSOR_NAME = "launch_startup_postprocessing_runs"
DAILY_QUALITY_SENSOR_NAME = "launch_daily_quality_runs"
BIRDWEATHER_BUNDLE_SYNC_SENSOR_NAME = "launch_birdweather_bundle_sync_runs"
BIRDWEATHER_CATCHUP_SENSOR_NAME = "launch_birdweather_catchup_runs"
HISTORICAL_CATCHUP_SENSOR_NAME = "launch_historical_catchup_runs"


def dagster_name(raw: str) -> str:
    """Convert a human-readable identifier into a Dagster-safe name."""
    return raw.replace("-", "_")


def build_schedule_automation_name(dep: DeploymentSchedule) -> str:
    """Return the stable automation name for a schedule tick."""
    return f"schedule/{dagster_name(dep.name)}"


def build_schedule_run_key(
    dep: DeploymentSchedule,
    scheduled_execution_time: datetime | None,
) -> str:
    """Return a readable, stable run key for scheduled runs."""
    return f"schedule/{dep.name}/{_timestamp_token(scheduled_execution_time)}"


def build_automation_run_key(
    automation_key: str,
    dep: DeploymentSchedule,
) -> str:
    """Return a readable, stable run key for sensor-triggered runs."""
    return f"automation/{automation_key}/{dep.name}"


def build_run_tags(
    dep: DeploymentSchedule,
    *,
    trigger: str,
    automation_name: str,
    params: Mapping[str, Any] | None = None,
    extra_tags: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return standard run tags for Dagster jobs and asset jobs."""
    resolved_params = dict(params or {})
    tags = {
        "avian/deployment": dep.name,
        "avian/flow_key": dep.flow,
        "avian/trigger": trigger,
        "avian/automation": automation_name,
        "avian/parameter_count": str(len(resolved_params)),
    }
    keys = parameter_keys(resolved_params)
    if keys:
        tags["avian/parameter_keys"] = keys
    for idx, dep_tag in enumerate(dep.tags):
        tags[f"avian/deployment_tag_{idx}"] = dep_tag
    if extra_tags:
        tags.update({key: str(value) for key, value in extra_tags.items()})
    return tags


def build_temporal_tags(
    dep: DeploymentSchedule,
    params: Mapping[str, Any] | None,
    *,
    scheduled_execution_time: datetime | None = None,
) -> dict[str, str]:
    """Return logical-date/month tags for a run when they can be inferred."""
    resolved_params = dict(params or {})
    tags: dict[str, str] = {}

    for key in ("date", "obs_date"):
        value = resolved_params.get(key)
        if value:
            tags["avian/logical_date"] = str(value)
            tags["avian/cohort"] = "historical-day"
            return tags

    start_date = resolved_params.get("start_date")
    end_date = resolved_params.get("end_date")
    if start_date and end_date and str(start_date) == str(end_date):
        tags["avian/logical_date"] = str(start_date)
        tags["avian/cohort"] = "historical-day"
        return tags

    if scheduled_execution_time is not None:
        timestamp = scheduled_execution_time
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        tags["avian/logical_date"] = timestamp.astimezone(UTC).date().isoformat()
        tags["avian/cohort"] = "daily" if "daily" in dep.tags else "scheduled"
        return tags

    start_month = resolved_params.get("start_month")
    end_month = resolved_params.get("end_month")
    if start_month and end_month and str(start_month) == str(end_month):
        tags["avian/logical_month"] = str(start_month)
        tags["avian/cohort"] = "historical-month"

    return tags


def parameter_keys(params: Mapping[str, Any]) -> str:
    """Return a stable comma-separated parameter-key list."""
    return ",".join(sorted(str(key) for key in params))


def format_parameter_summary(params: Mapping[str, Any] | None) -> str:
    """Return a compact log-friendly JSON summary of parameters."""
    resolved_params = dict(params or {})
    if not resolved_params:
        return "{}"
    summary = json.dumps(
        resolved_params,
        sort_keys=True,
        default=str,
        separators=(", ", ": "),
    )
    if len(summary) <= 240:
        return summary
    return f"{summary[:237]}..."


def _timestamp_token(value: datetime | None) -> str:
    if value is None:
        return "manual"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "BIRDWEATHER_BUNDLE_SYNC_AUTOMATION_KEY",
    "BIRDWEATHER_BUNDLE_SYNC_SENSOR_NAME",
    "BIRDWEATHER_CATCHUP_AUTOMATION_KEY",
    "BIRDWEATHER_CATCHUP_SENSOR_NAME",
    "DAILY_QUALITY_AUTOMATION_KEY",
    "DAILY_QUALITY_SENSOR_NAME",
    "HISTORICAL_CATCHUP_AUTOMATION_KEY",
    "HISTORICAL_CATCHUP_SENSOR_NAME",
    "STARTUP_INGESTION_AUTOMATION_KEY",
    "STARTUP_INGESTION_SENSOR_NAME",
    "STARTUP_POSTPROCESSING_AUTOMATION_KEY",
    "STARTUP_POSTPROCESSING_SENSOR_NAME",
    "build_automation_run_key",
    "build_run_tags",
    "build_schedule_automation_name",
    "build_schedule_run_key",
    "build_temporal_tags",
    "dagster_name",
    "format_parameter_summary",
    "parameter_keys",
]
