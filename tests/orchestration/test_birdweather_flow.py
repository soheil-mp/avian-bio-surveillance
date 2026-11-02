"""Tests for BirdWeather orchestration flows."""

import logging
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from avian_biosurveillance.orchestration import birdweather_flow


def _fake_resolve_species(
    species_ids: list[str] | None,
    *,
    all_species: bool = False,
) -> list[str] | None:
    return None if all_species else species_ids or ["1"]


def test_fetch_birdweather_data_uses_collector_default_detection_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow should not inject a hidden max_detections cap."""
    calls: list[dict[str, object]] = []

    class FakeCollector:
        def collect(self, **kwargs: object) -> dict[str, int]:
            calls.append(dict(kwargs))
            return {"inserted": 1}

    monkeypatch.setattr(
        birdweather_flow,
        "BirdweatherCollector",
        FakeCollector,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )

    result = birdweather_flow.fetch_birdweather_data(target="database")

    assert result == {"inserted": 1}
    assert calls == [
        {
            "hours": 24,
            "country": "Netherlands",
            "species_ids": ["1"],
            "min_confidence": 0.1,
            "download_audio": False,
            "workers": 20,
            "target": "database",
        }
    ]


def test_backfill_birdweather_day_uses_collector_default_detection_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-day backfills should request the full day by default."""
    calls: list[dict[str, object]] = []

    class FakeCollector:
        def collect(self, **kwargs: object) -> dict[str, int]:
            calls.append(dict(kwargs))
            return {"inserted": 2}

    monkeypatch.setattr(
        birdweather_flow,
        "BirdweatherCollector",
        FakeCollector,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )

    result = birdweather_flow.backfill_birdweather_day(
        date(2024, 1, 2),
        target="database",
    )

    assert result == {"inserted": 2}
    assert calls == [
        {
            "start_time": datetime(2024, 1, 2, 0, 0, 0),
            "end_time": datetime(2024, 1, 2, 23, 59, 59),
            "country": "Netherlands",
            "species_ids": ["1"],
            "min_confidence": 0.1,
            "download_audio": False,
            "workers": 20,
            "target": "database",
        }
    ]


def test_backfill_birdweather_day_can_disable_species_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-species mode should pass no species filter to the collector."""
    calls: list[dict[str, object]] = []

    class FakeCollector:
        def collect(self, **kwargs: object) -> dict[str, int]:
            calls.append(dict(kwargs))
            return {"inserted": 2}

    monkeypatch.setattr(
        birdweather_flow,
        "BirdweatherCollector",
        FakeCollector,
    )

    birdweather_flow.backfill_birdweather_day(
        date(2024, 1, 2),
        all_species=True,
        target="database",
    )

    assert calls[0]["species_ids"] is None


def test_backfill_birdweather_gaps_raises_when_any_day_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap repairs should fail the run instead of silently skipping days."""
    days = [date(2024, 1, 1), date(2024, 1, 2)]

    monkeypatch.setattr(
        birdweather_flow,
        "_find_missing_birdweather_dates",
        lambda _start_date, _end_date: days,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_birdweather_date_has_detections",
        lambda _obs_date: False,
    )

    def fail_second_day(**kwargs: object) -> None:
        if kwargs["obs_date"] == date(2024, 1, 2):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_day",
        fail_second_day,
    )

    with pytest.raises(RuntimeError, match="2024-01-02"):
        birdweather_flow.backfill_birdweather_gaps(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 3),
        )


def test_backfill_birdweather_gaps_skips_days_already_filled_during_db_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database repairs should skip days that another run has already filled."""
    days = [date(2024, 1, 1), date(2024, 1, 2)]
    calls: list[date] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_find_missing_birdweather_dates",
        lambda _start_date, _end_date: days,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_birdweather_date_has_detections",
        lambda obs_date: obs_date == date(2024, 1, 2),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_day",
        lambda **kwargs: calls.append(kwargs["obs_date"]),
    )

    birdweather_flow.backfill_birdweather_gaps(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        target="database",
    )

    assert calls == [date(2024, 1, 1)]


def test_backfill_birdweather_gaps_does_not_skip_for_local_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local repairs should still run even if the database already has rows."""
    calls: list[date] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_find_missing_birdweather_dates",
        lambda _start_date, _end_date: [date(2024, 1, 1)],
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_birdweather_date_has_detections",
        lambda _obs_date: True,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_day",
        lambda **kwargs: calls.append(kwargs["obs_date"]),
    )

    birdweather_flow.backfill_birdweather_gaps(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        target="local",
    )

    assert calls == [date(2024, 1, 1)]


def test_run_birdweather_repair_flow_refreshes_station_summary_for_db_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database repairs should run the full DB post-processing chain."""
    events: list[str] = []
    backfill_calls: list[dict[str, object]] = []

    def record_backfill(**kwargs: object) -> None:
        events.append("repair")
        backfill_calls.append(dict(kwargs))

    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_gaps",
        record_backfill,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: events.append("species-tracker"),
    )

    birdweather_flow.run_birdweather_repair_flow(
        start_date="2024-01-01",
        end_date="2024-01-03",
        target="database",
    )

    assert events == [
        "repair",
        "dedup",
        "station-summary",
        "species-dim",
        "species-tracker",
    ]
    assert backfill_calls == [
        {
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 1, 3),
            "country": "Netherlands",
            "species_ids": ["1"],
            "all_species": False,
            "min_confidence": 0.1,
            "workers": 20,
            "download_audio": False,
            "target": "database",
            "parallel_days": 1,
        }
    ]


def test_run_birdweather_repair_flow_skips_station_summary_for_local_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-only repairs should not run DB post-processing."""
    events: list[str] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_gaps",
        lambda **_kwargs: events.append("repair"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: events.append("refresh"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: events.append("species-tracker"),
    )

    birdweather_flow.run_birdweather_repair_flow(
        start_date="2024-01-01",
        end_date="2024-01-03",
        target="local",
    )

    assert events == ["repair"]


def test_run_birdweather_repair_flow_can_enable_audio_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair audio should stay opt-in for historical windows."""
    events: list[dict[str, object]] = []
    audio_backfills: list[dict[str, object]] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_gaps",
        lambda **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_backfill_birdweather_raw_audio",
        lambda start_date, end_date, *, workers: audio_backfills.append(
            {
                "start_date": start_date,
                "end_date": end_date,
                "workers": workers,
            }
        ),
    )

    birdweather_flow.run_birdweather_repair_flow(
        start_date="2024-01-01",
        end_date="2024-01-03",
        target="database",
        download_audio=True,
    )

    assert events == [
        {
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 1, 3),
            "country": "Netherlands",
            "species_ids": ["1"],
            "all_species": False,
            "min_confidence": 0.1,
            "workers": 20,
            "download_audio": True,
            "target": "database",
            "parallel_days": 1,
        }
    ]
    assert audio_backfills == [
        {
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 1, 3),
            "workers": 20,
        }
    ]


def test_run_birdweather_repair_flow_can_skip_tracker_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database repairs can defer the tracker refresh for bulk backfills."""
    events: list[str] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_gaps",
        lambda **_kwargs: events.append("repair"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: events.append("species-tracker"),
    )

    birdweather_flow.run_birdweather_repair_flow(
        start_date="2024-01-01",
        end_date="2024-01-03",
        target="database",
        run_tracker_refresh=False,
    )

    assert events == [
        "repair",
        "dedup",
        "station-summary",
        "species-dim",
    ]


def test_run_birdweather_catchup_flow_runs_followup_per_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BirdWeather catchup should batch downstream work over completed ranges."""
    events: list[str] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_day",
        lambda **kwargs: events.append(
            f"backfill:{kwargs['obs_date'].isoformat()}:audio={kwargs['download_audio']}"
        ),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: events.append("species-tracker"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_backfill_birdweather_raw_audio",
        lambda start_date, end_date, *, workers: events.append(
            f"raw-audio:{start_date.isoformat()}..{end_date.isoformat()}"
        )
        if workers >= 0
        else None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "process_birdweather_audio_clips",
        lambda **kwargs: events.append(
            f"audio-processing:{kwargs['start_date'].isoformat()}..{kwargs['end_date'].isoformat()}"
        ),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "engineer_birdweather_audio_features",
        lambda **kwargs: events.append(
            f"feature-engineering:{kwargs['start_date'].isoformat()}..{kwargs['end_date'].isoformat()}:limit={kwargs['limit']}"
        ),
    )

    birdweather_flow.run_birdweather_catchup_flow(
        start_date="2024-01-01",
        end_date="2024-01-02",
        target="database",
        download_audio=True,
    )

    assert events == [
        "backfill:2024-01-01:audio=False",
        "backfill:2024-01-02:audio=False",
        "dedup",
        "station-summary",
        "species-dim",
        "raw-audio:2024-01-01..2024-01-03",
        "audio-processing:2024-01-01..2024-01-01",
        "feature-engineering:2024-01-01..2024-01-01:limit=None",
        "audio-processing:2024-01-02..2024-01-02",
        "feature-engineering:2024-01-02..2024-01-02:limit=None",
        "species-tracker",
    ]


def test_run_birdweather_catchup_flow_skips_followup_for_local_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-only BirdWeather catchup should not trigger DB follow-up work."""
    events: list[str] = []

    monkeypatch.setattr(
        birdweather_flow,
        "_resolve_species",
        _fake_resolve_species,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "backfill_birdweather_day",
        lambda **kwargs: events.append(
            f"backfill:{kwargs['obs_date'].isoformat()}:audio={kwargs['download_audio']}"
        ),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: events.append("species-tracker"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "_backfill_birdweather_raw_audio",
        lambda _start_date, _end_date, *, workers: events.append(
            f"raw-audio:{workers}"
        ),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "process_birdweather_audio_clips",
        lambda **_kwargs: events.append("audio-processing"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "engineer_birdweather_audio_features",
        lambda **_kwargs: events.append("feature-engineering"),
    )

    birdweather_flow.run_birdweather_catchup_flow(
        start_date="2024-01-01",
        target="local",
        download_audio=True,
    )

    assert events == ["backfill:2024-01-01:audio=True"]


def test_run_birdweather_catchup_followup_ignores_audio_decode_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Historical catchup should log corrupt clips and keep advancing."""
    events: list[str] = []

    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: events.append("dedup"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: events.append("station-summary"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: events.append("species-dim"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: events.append("species-tracker"),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "process_birdweather_audio_clips",
        lambda **kwargs: (
            events.append(f"audio-processing:{kwargs['start_date'].isoformat()}"),
            SimpleNamespace(invalid_window=0, decode_failed=2, upload_failed=0),
        )[1],
    )
    monkeypatch.setattr(
        birdweather_flow,
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

    caplog.set_level(logging.WARNING)

    birdweather_flow._run_birdweather_catchup_followup(
        date(2024, 1, 7),
        date(2024, 1, 7),
        workers=4,
        download_audio=False,
        run_tracker_refresh=False,
    )

    assert events == [
        "dedup",
        "station-summary",
        "species-dim",
        "audio-processing:2024-01-07",
        "feature-engineering:2024-01-07",
    ]
    assert "non-blocking failure counters" in caplog.text


def test_run_birdweather_catchup_followup_still_raises_audio_upload_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catchup should still fail when processed clips cannot be uploaded."""

    monkeypatch.setattr(
        birdweather_flow,
        "deduplicate_birdweather_detections",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_station_summary_task",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_dim_task",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "refresh_species_tracker_task",
        lambda: None,
    )
    monkeypatch.setattr(
        birdweather_flow,
        "process_birdweather_audio_clips",
        lambda **_kwargs: SimpleNamespace(
            invalid_window=1,
            decode_failed=2,
            upload_failed=1,
        ),
    )
    monkeypatch.setattr(
        birdweather_flow,
        "engineer_birdweather_audio_features",
        lambda **_kwargs: pytest.fail("feature engineering should not run"),
    )

    with pytest.raises(RuntimeError, match="upload_failed"):
        birdweather_flow._run_birdweather_catchup_followup(
            date(2024, 1, 7),
            date(2024, 1, 7),
            workers=4,
            download_audio=False,
            run_tracker_refresh=False,
        )


def test_backfill_birdweather_raw_audio_translates_exclusive_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw-audio repairs should scope the same date window as detection repairs."""
    calls: list[dict[str, object]] = []

    def record_download(**kwargs: object) -> dict[str, int]:
        calls.append(dict(kwargs))
        return {"uploaded": 0, "already_present": 0, "failed": 0}

    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_raw_audio.download_birdweather_raw_audio_objects",
        record_download,
    )

    helper = birdweather_flow._backfill_birdweather_raw_audio

    helper(
        date(2024, 1, 1),
        date(2024, 1, 3),
        workers=7,
    )

    assert calls == [
        {
            "lookback_days": None,
            "limit": None,
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 1, 1),
            "workers": 7,
        },
        {
            "lookback_days": None,
            "limit": None,
            "start_date": date(2024, 1, 2),
            "end_date": date(2024, 1, 2),
            "workers": 7,
        }
    ]


def test_backfill_birdweather_raw_audio_ignores_missing_file_only_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Historical catchup should not abort when only stale media files are missing."""

    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_raw_audio.download_birdweather_raw_audio_objects",
        lambda **_kwargs: SimpleNamespace(
            uploaded=0,
            already_present=0,
            archived=0,
            failed=1,
            missing_file=1,
            upload_failed=0,
        ),
    )

    caplog.set_level(logging.WARNING)

    birdweather_flow._backfill_birdweather_raw_audio(
        date(2024, 1, 1),
        date(2024, 1, 2),
        workers=4,
    )

    assert "continuing catchup" in caplog.text


def test_backfill_birdweather_raw_audio_still_raises_hard_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard raw-audio failures should still abort the catchup window."""

    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_raw_audio.download_birdweather_raw_audio_objects",
        lambda **_kwargs: SimpleNamespace(
            uploaded=0,
            already_present=0,
            archived=0,
            failed=2,
            missing_file=1,
            upload_failed=0,
        ),
    )

    with pytest.raises(RuntimeError, match="BirdWeather raw audio 2024-01-01"):
        birdweather_flow._backfill_birdweather_raw_audio(
            date(2024, 1, 1),
            date(2024, 1, 2),
            workers=4,
        )
