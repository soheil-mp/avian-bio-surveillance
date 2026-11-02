"""Centralised Dagster job registry.

A single source of truth that maps every flow key in
``config/schedules.json`` to a concrete Dagster job. Both the schedules
and the sensors consume this registry so they cannot drift.

Job kinds
---------
* ingestion asset jobs — one per ingestion asset (single-asset selection).
* repair/catchup op jobs — wrap each ``run_*_repair_flow`` or
    ``run_*_catchup_flow`` function as an op.
* postprocessing asset jobs — imported from
  :mod:`avian_biosurveillance.orchestration.dagster_defs.assets`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from dagster import (
    Array,
    AssetSelection,
    Field,
    GraphDefinition,
    JobDefinition,
    Noneable,
    define_asset_job,
    in_process_executor,
    op,
)

from avian_biosurveillance.orchestration.birdweather_flow import (
    run_birdweather_catchup_flow,
    run_birdweather_repair_flow,
)
from avian_biosurveillance.orchestration.cds_drought_flow import (
    run_cds_drought_catchup_flow,
)
from avian_biosurveillance.orchestration.dagster_defs.assets import (
    POSTPROCESSING_ASSET_JOBS,
    build_asset_job_run_config,
)
from avian_biosurveillance.orchestration.dagster_defs.code_locations import (
    select_flow_jobs,
)
from avian_biosurveillance.orchestration.earth_engine_flow import (
    DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    run_earth_engine_grid_repair_flow,
)
from avian_biosurveillance.orchestration.ebird_flow import (
    run_ebird_repair_flow,
)
from avian_biosurveillance.orchestration.historical_daily_pipeline_flow import (
    run_historical_catchup_flow,
)
from avian_biosurveillance.orchestration.knmi_flow import (
    run_knmi_repair_flow,
    run_seismic_repair_flow,
)
from avian_biosurveillance.orchestration.movebank_flow import (
    run_movebank_repair_flow,
)


# ---------------------------------------------------------------------------
# Ingestion asset jobs
# ---------------------------------------------------------------------------

# Map flow keys to the ingestion asset they materialize.
FLOW_TO_ASSET: dict[str, str] = {
    "birdweather_ingest": "birdweather_raw_data",
    "knmi_ingest": "knmi_raw_data",
    "seismic_ingest": "seismic_raw_data",
    "ebird_ingest": "ebird_raw_data",
    "movebank_ingest": "movebank_raw_data",
    "earth_engine_ingest": "earth_engine_raw_data",
    "earth_engine_grid_ingest": "earth_engine_grid_raw_data",
    "cds_drought_ingest": "cds_drought_raw_data",
    "sovon_ingest": "sovon_raw_data",
}


def _build_ingestion_asset_jobs() -> dict[str, Any]:
    return {
        flow_key: define_asset_job(
            name=f"{flow_key}_job",
            selection=AssetSelection.assets(asset_name),
            description=f"Materialize the {asset_name} ingestion asset.",
            tags={
                "avian/flow_key": flow_key,
                "avian/orchestration_kind": "asset_job",
            },
        )
        for flow_key, asset_name in FLOW_TO_ASSET.items()
    }


# ---------------------------------------------------------------------------
# Repair/catchup op jobs
# ---------------------------------------------------------------------------


def _make_range_op_job(
    flow_key: str,
    runner: Callable[..., Any],
    config_schema: Mapping[str, Any],
    *,
    description: str,
) -> JobDefinition:
    """Wrap a range-style orchestration flow as a single-op Dagster job."""

    op_name = flow_key

    @op(
        name=op_name,
        config_schema=dict(config_schema),
        tags={
            "avian/flow_key": flow_key,
            "avian/orchestration_kind": "job",
        },
    )
    def _range_op(context) -> None:  # type: ignore[no-untyped-def]
        cfg = context.op_config
        runner(**{k: v for k, v in cfg.items() if v is not None})

    return GraphDefinition(
        name=f"{flow_key}_graph",
        node_defs=[_range_op],
    ).to_job(
        name=f"{flow_key}_job",
        description=description,
        executor_def=in_process_executor,
        tags={
            "avian/flow_key": flow_key,
            "avian/orchestration_kind": "job",
        },
    )


_DATE_RANGE_SCHEMA = {
    "start_date": Field(str, is_required=True),
    "end_date": Field(Noneable(str), is_required=False, default_value=None),
    "target": Field(str, is_required=False, default_value="database"),
}

_RANGE_JOBS: dict[str, JobDefinition] = {
    "birdweather_catchup": _make_range_op_job(
        "birdweather_catchup",
        run_birdweather_catchup_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "country": Field(str, is_required=False, default_value="Netherlands"),
            "min_confidence": Field(float, is_required=False, default_value=0.1),
            "species_ids": Field(
                Noneable(Array(str)), is_required=False, default_value=None
            ),
            "all_species": Field(bool, is_required=False, default_value=False),
            "workers": Field(int, is_required=False, default_value=20),
            "download_audio": Field(bool, is_required=False, default_value=True),
            "followup_batch_days": Field(
                int,
                is_required=False,
                default_value=7,
            ),
            "run_tracker_refresh": Field(
                bool,
                is_required=False,
                default_value=True,
            ),
        },
        description="BirdWeather one-day historical catchup.",
    ),
    "birdweather_repair": _make_range_op_job(
        "birdweather_repair",
        run_birdweather_repair_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "country": Field(str, is_required=False, default_value="Netherlands"),
            "min_confidence": Field(float, is_required=False, default_value=0.1),
            "species_ids": Field(
                Noneable(Array(str)), is_required=False, default_value=None
            ),
            "all_species": Field(bool, is_required=False, default_value=False),
            "workers": Field(int, is_required=False, default_value=20),
            "download_audio": Field(bool, is_required=False, default_value=True),
        },
        description="BirdWeather gap-detection repair.",
    ),
    "knmi_repair": _make_range_op_job(
        "knmi_repair",
        run_knmi_repair_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "force": Field(bool, is_required=False, default_value=False),
        },
        description="KNMI date-range repair.",
    ),
    "ebird_repair": _make_range_op_job(
        "ebird_repair",
        run_ebird_repair_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "region": Field(str, is_required=False, default_value="NL"),
            "target_species_only": Field(bool, is_required=False, default_value=True),
        },
        description="eBird historic observation repair.",
    ),
    "movebank_repair": _make_range_op_job(
        "movebank_repair",
        run_movebank_repair_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "all_sensors": Field(bool, is_required=False, default_value=False),
        },
        description="Movebank GPS tracking repair.",
    ),
    "seismic_repair": _make_range_op_job(
        "seismic_repair",
        run_seismic_repair_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "min_magnitude": Field(float, is_required=False, default_value=0.0),
        },
        description="KNMI seismic event date-range repair.",
    ),
    "earth_engine_grid_repair": _make_range_op_job(
        "earth_engine_grid_repair",
        run_earth_engine_grid_repair_flow,
        {
            **_DATE_RANGE_SCHEMA,
            "force": Field(bool, is_required=False, default_value=False),
            "grid_size_km": Field(float, is_required=False, default_value=5.0),
            "max_workers": Field(
                int,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
            ),
            "cell_workers": Field(
                int,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
            ),
            "db_batch_size": Field(
                int,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
            ),
            "high_volume": Field(
                bool,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
            ),
        },
        description="Earth Engine 5 km grid repair.",
    ),
    "cds_drought_catchup": _make_range_op_job(
        "cds_drought_catchup",
        run_cds_drought_catchup_flow,
        {
            "start_month": Field(str, is_required=True),
            "end_month": Field(Noneable(str), is_required=False, default_value=None),
            "force": Field(bool, is_required=False, default_value=False),
            "target": Field(str, is_required=False, default_value="database"),
            "include_ensemble_members": Field(
                bool, is_required=False, default_value=True
            ),
        },
        description="Copernicus CDS drought month-range catchup.",
    ),
    "historical_catchup": _make_range_op_job(
        "historical_catchup",
        run_historical_catchup_flow,
        {
            "start_date": Field(str, is_required=True),
            "end_date": Field(Noneable(str), is_required=False, default_value=None),
            "bw_country": Field(str, is_required=False, default_value="Netherlands"),
            "bw_all_species": Field(bool, is_required=False, default_value=False),
            "bw_min_confidence": Field(float, is_required=False, default_value=0.1),
            "bw_workers": Field(int, is_required=False, default_value=20),
            "bw_download_audio": Field(bool, is_required=False, default_value=True),
            "knmi_force": Field(bool, is_required=False, default_value=False),
            "ebird_region": Field(str, is_required=False, default_value="NL"),
            "ebird_target_species_only": Field(
                bool, is_required=False, default_value=True
            ),
            "ee_grid_size_km": Field(float, is_required=False, default_value=5.0),
            "ee_force": Field(bool, is_required=False, default_value=False),
            "ee_max_workers": Field(
                int,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
            ),
            "ee_cell_workers": Field(
                int,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
            ),
            "ee_db_batch_size": Field(
                int,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
            ),
            "ee_high_volume": Field(
                bool,
                is_required=False,
                default_value=DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
            ),
            "target": Field(str, is_required=False, default_value="database"),
            "followup_batch_days": Field(
                int,
                is_required=False,
                default_value=7,
            ),
            "run_tracker_refresh": Field(
                bool,
                is_required=False,
                default_value=True,
            ),
            "sources": Field(
                str,
                is_required=False,
                default_value="knmi,ebird,earth_engine_grid",
            ),
        },
        description="One-day-at-a-time historical catchup driver.",
    ),
}


# ---------------------------------------------------------------------------
# Combined registry
# ---------------------------------------------------------------------------

ALL_FLOW_JOBS: dict[str, Any] = {
    **_build_ingestion_asset_jobs(),
    **_RANGE_JOBS,
    **POSTPROCESSING_ASSET_JOBS,
}

FLOW_JOBS: dict[str, Any] = select_flow_jobs(ALL_FLOW_JOBS)


def run_config_for(flow_key: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Return the run config dict for a given flow + parameters."""
    if flow_key in POSTPROCESSING_ASSET_JOBS:
        return build_asset_job_run_config(flow_key, params) or {}

    if flow_key in _RANGE_JOBS:
        return {"ops": {flow_key: {"config": dict(params)}}}

    if flow_key in FLOW_TO_ASSET:
        asset_name = FLOW_TO_ASSET[flow_key]
        return {"ops": {asset_name: {"config": dict(params)}}} if params else {}

    return {}


__all__ = [
    "FLOW_JOBS",
    "FLOW_TO_ASSET",
    "run_config_for",
]
