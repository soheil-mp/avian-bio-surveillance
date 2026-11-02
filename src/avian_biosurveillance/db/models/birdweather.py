"""
BirdWeather Data Model

This module contains the `BirdWeatherDetection` dataclass which maps to the
`birdweather_tbl` TimescaleDB hypertable. It encapsulates acoustic detections
including species, station, and soundscape information.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class BirdWeatherDetection:
    """One acoustic bird detection from BirdWeather.

    Maps 1-to-1 with ``birdweather_tbl``. The model is intentionally minimal:
    reference attributes are normalised onto dedicated tables and useless
    BirdWeather web-app links are dropped entirely.

      * Species reference attributes (common_name, scientific_name, codes,
        colour, image URLs) live on ``species_tbl`` (loader:
        ``upsert_birdweather_species``).
      * Station reference attributes (name, type, country, state, lat, lon)
        live on ``birdweather_stations_tbl`` (loader:
        ``upsert_birdweather_stations``). The detection's geographic
        coordinates are also derived by joining the station — per-event
        lat/lon would only differ for mobile/app stations which are
        excluded from the credible-tier analytical path.
      * BirdWeather interaction URLs (favorite/flag/vote) are not carried.
      * Per-clip duration is derived as
        ``audio_end_time_s - audio_start_time_s`` and is not stored.
      * The two-column AST background-sound classification was collapsed
        into a single JSONB envelope ``background_sound_clf``.
    """

    # Identity & Timestamp
    detection_id: str
    detected_at: datetime
    observation_date: date

    # Detection Metrics
    confidence: float = 0.0
    score: float | None = None
    probability: float | None = None
    certainty: str | None = None
    eclipse: bool | None = None

    # Species (FK → species_tbl.species_id)
    species_id: str = ""

    # Station (FK → birdweather_stations_tbl.station_id, also the source
    # of the detection's geographic coordinates).
    station_id: str = ""

    # Soundscape / Audio (flattened)
    soundscape_id: str | None = None
    audio_url: str | None = None
    audio_filename: str | None = None
    audio_filesize_bytes: int | None = None
    audio_start_time_s: float | None = None
    audio_end_time_s: float | None = None
    soundscape_at: datetime | None = None

    # Background Sound Classification (AST enrichment), JSONB envelope:
    #   {"status": "classified" | "audio_not_downloaded" | "decode_failed",
    #    "tags":   [{"label": "Rain", "score": 0.82}, ...]}
    # Stored as a JSON string for psycopg2 JSONB binding (consistent with
    # the other JSONB fields on this dataclass).
    background_sound_clf: str | None = None

    # Station Weather Enrichment (BirdWeather GraphQL → OpenWeatherMap).
    # Carried per-detection (not on the station table) because the values
    # are observed at detection time and change minute-to-minute.
    station_weather: str | None = None          # JSONB as JSON string
    station_air_pollution: str | None = None     # JSONB as JSON string
    station_sensors: str | None = None           # JSONB as JSON string

    # -- Factory ------------------------------------------------

    @classmethod
    def from_jsonl(
        cls, raw: dict[str, Any],
    ) -> BirdWeatherDetection:
        """Build from a ``detections.jsonl`` record.

        Args:
            raw: One parsed JSON line.

        Returns:
            Populated ``BirdWeatherDetection``.
        """
        ts = datetime.fromisoformat(raw["timestamp"])
        species = raw.get("species") or {}
        sscape = raw.get("soundscape") or {}
        station = raw.get("station") or {}

        bg_status = raw.get("background_sound_classification_status")
        bg_tags = raw.get("background_sound_classification_tags")
        background_sound_clf: str | None = None
        if bg_status is not None or bg_tags:
            background_sound_clf = json.dumps({
                "status": bg_status,
                "tags": bg_tags or [],
            })

        return cls(
            detection_id=raw["id"],
            detected_at=ts,
            observation_date=ts.date(),
            confidence=raw.get("confidence", 0.0),
            score=min(raw["score"], 1.0) if raw.get("score") is not None else None,
            probability=raw.get("probability"),
            certainty=raw.get("certainty"),
            eclipse=bool(raw.get("eclipse")) if raw.get("eclipse") is not None else None,
            species_id=species.get(
                "id", raw.get("speciesId", ""),
            ),
            station_id=station.get("id", ""),
            soundscape_id=sscape.get("id"),
            audio_url=sscape.get("url"),
            audio_filename=sscape.get(
                "downloadFilename",
            ),
            audio_filesize_bytes=sscape.get("filesize"),
            audio_start_time_s=sscape.get("startTime"),
            audio_end_time_s=sscape.get("endTime"),
            soundscape_at=(
                datetime.fromisoformat(
                    sscape["timestamp"],
                )
                if sscape.get("timestamp")
                else None
            ),
            background_sound_clf=background_sound_clf,
            station_weather=(
                json.dumps(raw["station_weather"])
                if raw.get("station_weather")
                else None
            ),
            station_air_pollution=(
                json.dumps(raw["station_air_pollution"])
                if raw.get("station_air_pollution")
                else None
            ),
            station_sensors=(
                json.dumps(raw["station_sensors"])
                if raw.get("station_sensors")
                else None
            ),
        )

    # -- Helpers -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for DataFrame or DB insert."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
        }

    @staticmethod
    def column_names() -> list[str]:
        """Ordered column names matching the SQL."""
        return [
            f.name
            for f in fields(BirdWeatherDetection)
        ]

    @staticmethod
    def pandas_dtypes() -> dict[str, str]:
        """Dtype map for ``pandas.DataFrame``."""
        return {
            "detection_id": "string",
            "detected_at": "datetime64[ns, UTC]",
            "observation_date": "object",
            "confidence": "Float64",
            "score": "Float64",
            "probability": "Float64",
            "certainty": "string",
            "eclipse": "boolean",
            "species_id": "string",
            "station_id": "string",
            "soundscape_id": "string",
            "audio_url": "string",
            "audio_filename": "string",
            "audio_filesize_bytes": "Int64",
            "audio_start_time_s": "Float64",
            "audio_end_time_s": "Float64",
            "soundscape_at": "datetime64[ns, UTC]",
            "background_sound_clf": "string",
            "station_weather": "string",
            "station_air_pollution": "string",
            "station_sensors": "string",
        }
