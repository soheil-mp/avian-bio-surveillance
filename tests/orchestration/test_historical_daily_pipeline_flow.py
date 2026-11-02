"""Tests for the historical catchup pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

import avian_biosurveillance.orchestration.historical_daily_pipeline_flow as historical_pipeline


def test_historical_catchup_flow_runs_followup_per_day() -> None:
    """Database-target catchups should batch post-processing and quality work."""
    events: list[str] = []

    historical_pipeline.backfill_birdweather_day = lambda **kwargs: events.append(
        f"birdweather:{kwargs['obs_date'].isoformat()}"
    )
    historical_pipeline.backfill_knmi_day = lambda **kwargs: events.append(
        f"knmi:{kwargs['date'].isoformat()}"
    )
    historical_pipeline.backfill_ebird_day = lambda **kwargs: events.append(
        f"ebird:{kwargs['obs_date'].isoformat()}"
    )
    historical_pipeline.backfill_earth_engine_grid_day = lambda **kwargs: events.append(
        f"earth-engine:{kwargs['obs_date'].isoformat()}"
    )
    historical_pipeline.deduplicate_birdweather_detections = lambda: events.append(
        "dedup"
    )
    historical_pipeline.refresh_station_summary_task = lambda: events.append(
        "station-summary"
    )
    historical_pipeline.refresh_species_dim_task = lambda: events.append("species-dim")
    historical_pipeline.refresh_species_tracker_task = lambda **_kwargs: events.append(
        "species-tracker"
    )
    historical_pipeline.run_quality_audit_task = lambda: events.append("quality")
    historical_pipeline.process_birdweather_audio_clips = lambda **_kwargs: (
        events.append("audio-processing")
    )
    historical_pipeline.engineer_birdweather_audio_features = lambda **_kwargs: (
        events.append("feature-engineering")
    )

    summary = historical_pipeline.run_historical_catchup_flow(
        start_date="2024-01-01",
        end_date="2024-01-02",
        target="database",
    )

    assert events == [
        "knmi:2024-01-01",
        "ebird:2024-01-01",
        "earth-engine:2024-01-01",
        "knmi:2024-01-02",
        "ebird:2024-01-02",
        "earth-engine:2024-01-02",
        "species-dim",
        "quality",
        "species-tracker",
    ]
    assert summary["days_total"] == 2
    assert summary["days_completed"] == 2
    assert summary["days_with_failures"] == 0


def test_historical_catchup_flow_skips_followup_for_local_target() -> None:
    """Local-only catchups should skip DB maintenance and quality."""
    events: list[str] = []

    historical_pipeline.backfill_birdweather_day = lambda **_kwargs: events.append(
        "birdweather"
    )
    historical_pipeline.backfill_knmi_day = lambda **_kwargs: events.append("knmi")
    historical_pipeline.backfill_ebird_day = lambda **_kwargs: events.append("ebird")
    historical_pipeline.backfill_earth_engine_grid_day = lambda **_kwargs: (
        events.append("earth-engine")
    )
    historical_pipeline.deduplicate_birdweather_detections = lambda: events.append(
        "dedup"
    )
    historical_pipeline.refresh_station_summary_task = lambda: events.append(
        "station-summary"
    )
    historical_pipeline.refresh_species_dim_task = lambda: events.append("species-dim")
    historical_pipeline.refresh_species_tracker_task = lambda **_kwargs: events.append(
        "species-tracker"
    )
    historical_pipeline.run_quality_audit_task = lambda: events.append("quality")

    historical_pipeline.run_historical_catchup_flow(
        start_date="2024-01-01",
        target="local",
    )

    assert events == [
        "knmi",
        "ebird",
        "earth-engine",
    ]


def test_historical_catchup_flow_defaults_exclude_birdweather(
    monkeypatch: MonkeyPatch,
) -> None:
    """Shared historical catchup should not duplicate BirdWeather backlog work."""
    events: list[str] = []

    monkeypatch.setattr(
        historical_pipeline,
        "backfill_birdweather_day",
        lambda **_kwargs: events.append("birdweather"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_knmi_day",
        lambda **_kwargs: events.append("knmi"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_ebird_day",
        lambda **_kwargs: events.append("ebird"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_earth_engine_grid_day",
        lambda **_kwargs: events.append("earth-engine"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "run_quality_audit_task",
        lambda: events.append("quality"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_tracker_task",
        lambda **_kwargs: events.append("species-tracker"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )

    summary = historical_pipeline.run_historical_catchup_flow(
        start_date="2024-01-01",
        target="database",
    )

    assert "birdweather" not in events
    per_day = cast(list[dict[str, Any]], summary["per_day"])
    assert per_day[0]["sources"] == ["earth_engine_grid", "ebird", "knmi"]


def test_historical_catchup_flow_scopes_followup_to_sources(
    monkeypatch: MonkeyPatch,
) -> None:
    """Source-filtered catchups should avoid unrelated follow-up work."""
    events: list[str] = []

    monkeypatch.setattr(
        historical_pipeline,
        "backfill_birdweather_day",
        lambda **_kwargs: events.append("birdweather"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_knmi_day",
        lambda **_kwargs: events.append("knmi"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_ebird_day",
        lambda **_kwargs: events.append("ebird"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_earth_engine_grid_day",
        lambda **_kwargs: events.append("earth-engine"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_tracker_task",
        lambda **_kwargs: events.append("species-tracker"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "run_quality_audit_task",
        lambda: events.append("quality"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "process_birdweather_audio_clips",
        lambda **_kwargs: events.append("audio-processing"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "engineer_birdweather_audio_features",
        lambda **_kwargs: events.append("feature-engineering"),
    )

    summary = historical_pipeline.run_historical_catchup_flow(
        start_date="2024-01-01",
        target="database",
        sources="earth_engine_grid",
    )

    assert events == [
        "earth-engine",
        "quality",
        "species-tracker",
    ]
    per_day = cast(list[dict[str, Any]], summary["per_day"])
    assert per_day[0]["sources"] == ["earth_engine_grid"]


def test_historical_catchup_flow_raises_on_source_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """A failed source/day should fail the Dagster run instead of returning success."""
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_knmi_day",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("knmi down")),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_ebird_day",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "backfill_earth_engine_grid_day",
        lambda **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="knmi"):
        historical_pipeline.run_historical_catchup_flow(
            start_date="2024-01-01",
            target="database",
        )


def test_historical_catchup_flow_can_request_all_species_birdweather(
    monkeypatch: MonkeyPatch,
) -> None:
    """Explicit BirdWeather source runs can disable the species filter."""
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        historical_pipeline,
        "backfill_birdweather_day",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "deduplicate_birdweather_detections",
        lambda: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_station_summary_task",
        lambda: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_dim_task",
        lambda: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_tracker_task",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "run_quality_audit_task",
        lambda: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "process_birdweather_audio_clips",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        historical_pipeline,
        "engineer_birdweather_audio_features",
        lambda **_kwargs: None,
    )

    historical_pipeline.run_historical_catchup_flow(
        start_date="2024-01-01",
        target="database",
        sources="birdweather",
        bw_all_species=True,
        bw_download_audio=False,
    )

    assert calls[0]["species_ids"] is None
    assert calls[0]["all_species"] is True


def test_historical_catchup_flow_rejects_movebank_source() -> None:
    """Shared catchup no longer supports Movebank replay."""
    try:
        historical_pipeline.run_historical_catchup_flow(
            start_date="2024-01-01",
            target="database",
            sources="movebank",
        )
    except ValueError as exc:
        assert "Unsupported historical catchup sources" in str(exc)
    else:  # pragma: no cover - defensive branch
        raise AssertionError("Expected Movebank source to be rejected")


def test_historical_catchup_flow_ignores_birdweather_audio_decode_failures(
    monkeypatch: MonkeyPatch,
) -> None:
    """Historical BirdWeather follow-up should keep going on corrupt clips."""
    events: list[str] = []

    monkeypatch.setattr(
        historical_pipeline,
        "backfill_birdweather_day",
        lambda **kwargs: events.append(f"birdweather:{kwargs['obs_date'].isoformat()}"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "refresh_species_tracker_task",
        lambda **_kwargs: events.append("species-tracker"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "run_quality_audit_task",
        lambda: events.append("quality"),
    )
    monkeypatch.setattr(
        historical_pipeline,
        "process_birdweather_audio_clips",
        lambda **kwargs: (
            events.append(f"audio-processing:{kwargs['start_date'].isoformat()}"),
            SimpleNamespace(invalid_window=0, decode_failed=1, upload_failed=0),
        )[1],
    )
    monkeypatch.setattr(
        historical_pipeline,
        "engineer_birdweather_audio_features",
        lambda **kwargs: (
            events.append(f"feature-engineering:{kwargs['start_date'].isoformat()}"),
            SimpleNamespace(
                missing_clip=0,
                storage_unavailable=0,
                decode_failed=0,
                artifact_upload_failed=0,
                background_failed=0,
            ),
        )[1],
    )

    summary = historical_pipeline.run_historical_catchup_flow(
        start_date="2024-01-01",
        end_date="2024-01-01",
        target="database",
        sources="birdweather",
        bw_download_audio=False,
    )

    assert events == [
        "birdweather:2024-01-01",
        "dedup",
        "station-summary",
        "species-dim",
        "audio-processing:2024-01-01",
        "feature-engineering:2024-01-01",
        "quality",
        "species-tracker",
    ]
    assert summary["days_with_failures"] == 0
