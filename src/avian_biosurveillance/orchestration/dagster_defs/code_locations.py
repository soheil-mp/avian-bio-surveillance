"""Dagster code-location selection helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar


CODE_LOCATION_ENV = "AVIAN_DAGSTER_CODE_LOCATION"
CODE_LOCATION_ALL = "all"
CODE_LOCATION_INGESTION = "ingestion"
CODE_LOCATION_MODEL = "model"

MODELING_FLOW_KEYS = {
    "birdweather_feature_engineering",
    "model_anomaly_scoring",
    "model_tuning_sweep",
}
CROSS_LOCATION_COMPOSITE_FLOW_KEYS = {"birdweather_audio_feature_pipeline"}

_LOCATION_ALIASES = {
    "default": CODE_LOCATION_ALL,
    "full": CODE_LOCATION_ALL,
    "modeling": CODE_LOCATION_MODEL,
    "model-pipeline": CODE_LOCATION_MODEL,
    "model_pipeline": CODE_LOCATION_MODEL,
}
_VALID_CODE_LOCATIONS = {
    CODE_LOCATION_ALL,
    CODE_LOCATION_INGESTION,
    CODE_LOCATION_MODEL,
}

_JobT = TypeVar("_JobT")


def active_code_location() -> str:
    """Return the active Dagster code-location role for this process."""
    configured = os.getenv(CODE_LOCATION_ENV, CODE_LOCATION_ALL).strip().lower()
    location = _LOCATION_ALIASES.get(configured, configured)
    if location not in _VALID_CODE_LOCATIONS:
        valid = ", ".join(sorted(_VALID_CODE_LOCATIONS | set(_LOCATION_ALIASES)))
        msg = f"{CODE_LOCATION_ENV} must be one of: {valid}"
        raise RuntimeError(msg)
    return location


def select_flow_jobs(flow_jobs: Mapping[str, _JobT]) -> dict[str, _JobT]:
    """Filter a full flow-key registry for the active code location."""
    location = active_code_location()
    if location == CODE_LOCATION_ALL:
        return dict(flow_jobs)
    if location == CODE_LOCATION_MODEL:
        return {key: job for key, job in flow_jobs.items() if key in MODELING_FLOW_KEYS}
    return {
        key: job
        for key, job in flow_jobs.items()
        if key not in MODELING_FLOW_KEYS | CROSS_LOCATION_COMPOSITE_FLOW_KEYS
    }
