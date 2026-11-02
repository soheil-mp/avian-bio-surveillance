"""Dagster code-location for the Avian Biosurveillance platform.

Exports the singleton :data:`defs` (:class:`dagster.Definitions`) consumed by
``dagster-webserver`` and ``dagster-daemon``.

Layout
------
``ingestion_assets``
    Data ingestion processes translated into Software-Defined Assets (SDAs).

``assets``
    Postgres-backed maintenance assets for station summaries, derived
    aggregates, dashboard caches, audio processing, audio features, and
    persisted quality-audit output.

``jobs``
    Centralised flow-key → :class:`~dagster.JobDefinition` registry shared by
    both schedules and sensors.

``schedules``
    One :class:`~dagster.ScheduleDefinition` per scheduled deployment in
    ``config/schedules.json``.

``sensors``
    Startup, post-processing, daily-quality, and historical-backfill sensors.
"""

from __future__ import annotations

from dagster import Definitions

from avian_biosurveillance.orchestration.dagster_defs.assets import (
    MODELING_ASSETS,
    OPERATIONAL_POSTPROCESSING_ASSETS,
    POSTPROCESSING_ASSETS,
)
from avian_biosurveillance.orchestration.dagster_defs.code_locations import (
    CODE_LOCATION_ALL,
    CODE_LOCATION_INGESTION,
    CODE_LOCATION_MODEL,
    active_code_location,
)
from avian_biosurveillance.orchestration.dagster_defs.ingestion_assets import (
    INGESTION_ASSETS,
)
from avian_biosurveillance.orchestration.dagster_defs.jobs import FLOW_JOBS
from avian_biosurveillance.orchestration.dagster_defs.schedules import (
    build_schedules,
)
from avian_biosurveillance.orchestration.dagster_defs.sensors import SENSORS


_schedules = build_schedules()
_code_location = active_code_location()

# Deduplicate jobs by name; FLOW_JOBS is already filtered for this code location.
_jobs = list({job.name: job for job in FLOW_JOBS.values()}.values())


def _assets_for_code_location() -> list[object]:
    if _code_location == CODE_LOCATION_MODEL:
        return [*MODELING_ASSETS]
    if _code_location == CODE_LOCATION_INGESTION:
        return [*INGESTION_ASSETS, *OPERATIONAL_POSTPROCESSING_ASSETS]
    if _code_location == CODE_LOCATION_ALL:
        return [*INGESTION_ASSETS, *POSTPROCESSING_ASSETS]
    raise AssertionError(f"Unhandled code location: {_code_location}")


defs = Definitions(
    assets=_assets_for_code_location(),
    jobs=_jobs,
    schedules=_schedules,
    sensors=SENSORS,
)

__all__ = ["defs"]
