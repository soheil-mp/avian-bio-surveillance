"""Tests for the manual daily pipeline orchestration."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


_STUB_MODULES: dict[str, dict[str, object]] = {
    "avian_biosurveillance.config.species": {
        "registry": SimpleNamespace(birdweather_ids=["sp-1"]),
    },
    "avian_biosurveillance.orchestration.birdweather_flow": {
        "deduplicate_birdweather_detections": lambda: None,
        "fetch_birdweather_data": SimpleNamespace(submit=lambda **kwargs: None),
    },
    "avian_biosurveillance.orchestration.data_quality_flow": {
        "run_quality_audit_task": lambda: None,
    },
    "avian_biosurveillance.orchestration.earth_engine_flow": {
        "fetch_earth_engine_grid_profiles": lambda **kwargs: None,
    },
    "avian_biosurveillance.orchestration.ebird_flow": {
        "fetch_ebird_data": SimpleNamespace(submit=lambda **kwargs: None),
    },
    "avian_biosurveillance.orchestration.knmi_flow": {
        "fetch_knmi_data": SimpleNamespace(submit=lambda **kwargs: None),
        "fetch_seismic_events": SimpleNamespace(submit=lambda **kwargs: None),
    },
    "avian_biosurveillance.orchestration.sovon_flow": {
        "download_sovon_charts": SimpleNamespace(submit=lambda **kwargs: None),
        "extract_sovon_data": lambda **kwargs: None,
    },
    "avian_biosurveillance.orchestration.station_summary_flow": {
        "refresh_station_summary_task": lambda: None,
    },
    "avian_biosurveillance.orchestration.derived_aggregates_flow": {
        "refresh_species_tracker_task": lambda: None,
    },
}


@pytest.fixture
def daily_pipeline_flow(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reload :mod:`daily_pipeline_flow` against lightweight stubs."""
    # Install stubs into ``sys.modules`` for the duration of the test only.
    for name, attrs in _STUB_MODULES.items():
        stub = ModuleType(name)
        for attr_name, value in attrs.items():
            setattr(stub, attr_name, value)
        monkeypatch.setitem(sys.modules, name, stub)

    # Drop any cached daily_pipeline_flow so it re-binds against the stubs.
    monkeypatch.delitem(
        sys.modules,
        "avian_biosurveillance.orchestration.daily_pipeline_flow",
        raising=False,
    )
    module = importlib.import_module(
        "avian_biosurveillance.orchestration.daily_pipeline_flow"
    )
    return module


class _Future:
    def __init__(self, source: str, events: list[str]) -> None:
        self.source = source
        self.events = events

    def result(self) -> None:
        self.events.append(f"result:{self.source}")


class _TaskSubmitter:
    def __init__(self, source: str, events: list[str]) -> None:
        self.source = source
        self.events = events

    def submit(self, **_kwargs):
        self.events.append(f"submit:{self.source}")
        return _Future(self.source, self.events)


def test_run_daily_pipeline_serializes_earth_engine_after_other_sources(
    daily_pipeline_flow: ModuleType,
) -> None:
    events: list[str] = []

    daily_pipeline_flow.fetch_birdweather_data = _TaskSubmitter("birdweather", events)
    daily_pipeline_flow.fetch_knmi_data = _TaskSubmitter("knmi", events)
    daily_pipeline_flow.fetch_seismic_events = _TaskSubmitter("seismic", events)
    daily_pipeline_flow.fetch_ebird_data = _TaskSubmitter("ebird", events)
    daily_pipeline_flow.download_sovon_charts = _TaskSubmitter("sovon_download", events)
    daily_pipeline_flow.fetch_earth_engine_grid_profiles = lambda **kwargs: (
        events.append("run:earth_engine_grid")
    )
    daily_pipeline_flow.extract_sovon_data = lambda **kwargs: events.append(
        "run:sovon_extract"
    )
    daily_pipeline_flow.deduplicate_birdweather_detections = lambda: events.append(
        "run:deduplicate"
    )
    daily_pipeline_flow.refresh_station_summary_task = lambda: events.append(
        "run:station_refresh"
    )
    daily_pipeline_flow.refresh_species_tracker_task = lambda: events.append(
        "run:species_tracker"
    )
    daily_pipeline_flow.run_quality_audit_task = lambda: events.append("run:audit")
    daily_pipeline_flow.process_birdweather_audio_clips = lambda: events.append(
        "run:audio_processing"
    )
    daily_pipeline_flow.engineer_birdweather_audio_features = lambda: events.append(
        "run:feature_engineering"
    )

    daily_pipeline_flow.run_daily_pipeline(target="database")

    assert "run:earth_engine_grid" in events

    non_ee_results = [event for event in events if event.startswith("result:")]
    assert non_ee_results
    assert max(events.index(event) for event in non_ee_results) < events.index(
        "run:earth_engine_grid"
    )
    assert "run:species_tracker" in events
