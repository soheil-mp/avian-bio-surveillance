"""Tests for avian_biosurveillance.db.models.birdweather."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime

import pytest

from avian_biosurveillance.db.models.birdweather import BirdWeatherDetection


def _minimal_raw(**overrides) -> dict:
    base = {
        "id": "det-001",
        "timestamp": "2026-01-15T10:30:00+00:00",
        "confidence": 0.9,
        # Only species_id is carried per-detection; names/codes/colours/URLs
        # live on species_tbl and are seeded by upsert_birdweather_species.
        "species": {"id": "35"},
        # Only station_id is carried per-detection; the station's coords,
        # name, type, etc. live on birdweather_stations_tbl. The per-event
        # ``coords`` block from BirdWeather is intentionally ignored.
        "station": {"id": "100", "name": "Test Station"},
    }
    base.update(overrides)
    return base


class TestFromJsonl:
    def test_minimal_raw(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        assert det.detection_id == "det-001"
        assert det.confidence == 0.9
        assert det.species_id == "35"
        assert det.station_id == "100"
        # Per-detection model carries only the FKs; reference attributes
        # (species names + station names/coords) live on species_tbl and
        # birdweather_stations_tbl respectively.
        assert not hasattr(det, "common_name")
        assert not hasattr(det, "scientific_name")
        assert not hasattr(det, "species_thumbnail_url")
        assert not hasattr(det, "station_name")
        assert not hasattr(det, "station_type")
        assert not hasattr(det, "station_country")
        assert not hasattr(det, "station_state")
        assert not hasattr(det, "station_lat")
        assert not hasattr(det, "station_lon")
        # Per-event coordinates were dropped — a detection lives at its
        # station, so its coords come from the joined station row.
        assert not hasattr(det, "detection_lat")
        assert not hasattr(det, "detection_lon")
        # The soundscape-level duration column was dropped — per-clip
        # duration is derived as ``audio_end_time_s - audio_start_time_s``.
        assert not hasattr(det, "audio_duration_s")
        # BirdWeather web-app interaction URLs are not carried.
        assert not hasattr(det, "favorite_url")
        assert not hasattr(det, "flag_url")
        assert not hasattr(det, "vote_url")
        # The split bg_classification_status / bg_classification_tags pair
        # was collapsed into a single JSONB envelope.
        assert not hasattr(det, "bg_classification_status")
        assert not hasattr(det, "bg_classification_tags")

    def test_observation_date_matches_timestamp(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        assert det.observation_date == date(2026, 1, 15)
        assert det.detected_at == datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_score_clamped_to_one(self):
        raw = _minimal_raw(score=1.5)
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.score == 1.0

    def test_score_below_one_not_clamped(self):
        raw = _minimal_raw(score=0.75)
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.score == 0.75

    def test_optional_fields_default_none(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        assert det.soundscape_id is None
        assert det.audio_url is None
        assert det.background_sound_clf is None

    def test_soundscape_fields(self):
        # ``duration`` is intentionally still present in the source payload
        # but the dataclass no longer carries it — the per-clip duration is
        # ``endTime - startTime`` and is computed by consumers as needed.
        raw = _minimal_raw(
            soundscape={
                "id": "ss-1",
                "url": "https://example.com/audio.mp3",
                "downloadFilename": "audio.mp3",
                "duration": 3.0,
                "filesize": 50000,
                "startTime": 0.0,
                "endTime": 3.0,
                "timestamp": "2026-01-15T10:30:00+00:00",
            }
        )
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.soundscape_id == "ss-1"
        assert det.audio_url == "https://example.com/audio.mp3"
        assert det.audio_filesize_bytes == 50000
        assert det.audio_start_time_s == 0.0
        assert det.audio_end_time_s == 3.0
        assert det.soundscape_at == datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_background_sound_clf_envelope(self):
        """Status + tags are merged into a single JSONB envelope."""
        import json

        raw = _minimal_raw(
            background_sound_classification_status="done",
            background_sound_classification_tags=[{"label": "rain", "score": 0.8}],
        )
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.background_sound_clf is not None
        payload = json.loads(det.background_sound_clf)
        assert payload["status"] == "done"
        assert payload["tags"] == [{"label": "rain", "score": 0.8}]

    def test_background_sound_clf_status_only(self):
        """Status without tags still produces a populated envelope."""
        import json

        raw = _minimal_raw(
            background_sound_classification_status="audio_not_downloaded",
        )
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.background_sound_clf is not None
        payload = json.loads(det.background_sound_clf)
        assert payload == {"status": "audio_not_downloaded", "tags": []}

    def test_background_sound_clf_absent(self):
        """No status + no tags ⇒ field stays None (no JSONB row stored)."""
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        assert det.background_sound_clf is None

    def test_station_weather_serialized(self):
        raw = _minimal_raw(station_weather={"temp": 15.0})
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.station_weather is not None
        assert "15.0" in det.station_weather

    def test_eclipse_false(self):
        raw = _minimal_raw(eclipse=False)
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.eclipse is False

    def test_eclipse_none_when_absent(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        assert det.eclipse is None

    def test_species_from_top_level_species_id(self):
        raw = {
            "id": "det-002",
            "timestamp": "2026-01-15T10:30:00+00:00",
            "speciesId": "61",
            "station": {},
        }
        det = BirdWeatherDetection.from_jsonl(raw)
        assert det.species_id == "61"


class TestHelpers:
    def test_to_dict_keys_match_fields(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        d = det.to_dict()
        field_names = {f.name for f in fields(det)}
        assert set(d.keys()) == field_names

    def test_column_names_matches_to_dict(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        assert set(BirdWeatherDetection.column_names()) == set(det.to_dict().keys())

    def test_pandas_dtypes_keys_match_column_names(self):
        dtype_keys = set(BirdWeatherDetection.pandas_dtypes().keys())
        col_names = set(BirdWeatherDetection.column_names())
        assert dtype_keys == col_names

    def test_frozen(self):
        det = BirdWeatherDetection.from_jsonl(_minimal_raw())
        with pytest.raises((AttributeError, TypeError)):
            det.confidence = 0.5  # type: ignore[misc]
