"""Bulk upsert loaders for BirdWeather data."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.birdweather import BirdWeatherDetection


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000


def _metadata_row_values(
    record: Mapping[str, Any],
    columns: Sequence[str],
    *,
    json_columns: frozenset[str] = frozenset(),
    null_defaults: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Extract row values for metadata upserts.

    JSON-like values are wrapped for psycopg2 JSONB adaptation.
    """
    values: list[Any] = []
    for column in columns:
        value = record.get(column)
        if value is None and null_defaults and column in null_defaults:
            value = null_defaults[column]
        if column in json_columns and value is not None:
            value = psycopg2.extras.Json(value)
        values.append(value)
    return tuple(values)


# =====================================================================
# 1. BirdWeather Detections
# =====================================================================

# Columns we INSERT.
# Reference attributes are normalised out of birdweather_tbl:
#   * species_*  → species_tbl                (loader: upsert_birdweather_species)
#   * station_*  → birdweather_stations_tbl   (loader: upsert_birdweather_stations)
# BirdWeather interaction URLs (favorite_url / flag_url / vote_url) are not
# carried — they are useless to surveillance. The two-column AST background
# sound classification was collapsed into a single JSONB envelope
# ``background_sound_clf = {"status": ..., "tags": [...]}``.
_BWD_COLUMNS = (
    "detection_id",
    "detected_at",
    "observation_date",
    "confidence",
    "score",
    "probability",
    "certainty",
    "eclipse",
    "species_id",
    "station_id",
    "soundscape_id",
    "audio_url",
    "audio_filename",
    "audio_filesize_bytes",
    "audio_start_time_s",
    "audio_end_time_s",
    "soundscape_at",
    "background_sound_clf",
    "station_weather",
    "station_air_pollution",
    "station_sensors",
)

# Columns to update on conflict (everything except PK)
_BWD_CONFLICT_COLS = ("detection_id", "detected_at")
_BWD_UPDATE_COLS = tuple(c for c in _BWD_COLUMNS if c not in _BWD_CONFLICT_COLS)

_BWD_SQL = (
    f"INSERT INTO birdweather_tbl ({', '.join(_BWD_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (detection_id, detected_at) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _BWD_UPDATE_COLS)
)


# =====================================================================
# 2. BirdWeather Species Metadata
# =====================================================================

_SPECIES_COLUMNS = (
    "species_id",
    "common_name",
    "scientific_name",
    "alpha_code",
    "alpha6_code",
    "ebird_code",
    "species_color",
    "species_image_url",
    "species_thumbnail_url",
    "birdweather_url",
    "ebird_url",
    "ml_url",
    "wikipedia_url",
    "wikipedia_summary",
    "prediction_area",
    "range_map",
    "phylopic_svg_url",
    "phylopic_thumb_url",
    "phylopic_attribution",
    "phylopic_uuid",
    "phylopic_fetched_at",
)

_SPECIES_JSON_COLUMNS = frozenset({"prediction_area", "range_map"})
_SPECIES_NULL_DEFAULTS = {
    "phylopic_attribution": "",
    "phylopic_uuid": "",
}

_SPECIES_SQL = (
    f"INSERT INTO species_tbl ({', '.join(_SPECIES_COLUMNS)}) "
    f"VALUES %s "
    "ON CONFLICT (species_id) DO UPDATE SET "
    + ", ".join(
        f"{column} = COALESCE(EXCLUDED.{column}, species_tbl.{column})"
        for column in _SPECIES_COLUMNS
        if column != "species_id"
    )
)


def upsert_birdweather_species(
    records: Sequence[Mapping[str, Any]],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert BirdWeather species metadata into ``species_tbl``."""
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [
            _metadata_row_values(
                record,
                _SPECIES_COLUMNS,
                json_columns=_SPECIES_JSON_COLUMNS,
                null_defaults=_SPECIES_NULL_DEFAULTS,
            )
            for record in chunk
        ]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _SPECIES_SQL, values)
            total += len(values)
        logger.debug("species_tbl: upserted %d rows (total %d)", len(values), total)

    logger.info("species_tbl: upserted %d rows total", total)
    return total


# =====================================================================
# 3. BirdWeather Station Metadata
# =====================================================================

_STATION_COLUMNS = (
    "station_id",
    "station_name",
    "station_type",
    "station_country",
    "station_state",
    "station_location",
    "station_timezone",
    "station_continent",
    "lat",
    "lon",
)

_STATION_SQL = (
    f"INSERT INTO birdweather_stations_tbl ({', '.join(_STATION_COLUMNS)}) "
    f"VALUES %s "
    "ON CONFLICT (station_id) DO UPDATE SET "
    + ", ".join(
        f"{column} = COALESCE(EXCLUDED.{column}, birdweather_stations_tbl.{column})"
        for column in _STATION_COLUMNS
        if column != "station_id"
    )
)


def upsert_birdweather_stations(
    records: Sequence[Mapping[str, Any]],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert BirdWeather station metadata into ``birdweather_stations_tbl``."""
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [_metadata_row_values(record, _STATION_COLUMNS) for record in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _STATION_SQL, values)
            total += len(values)
        logger.debug(
            "birdweather_stations_tbl: upserted %d rows (total %d)",
            len(values),
            total,
        )

    logger.info("birdweather_stations_tbl: upserted %d rows total", total)
    return total


def upsert_birdweather_detections(
    records: Sequence[BirdWeatherDetection],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert BirdWeather detections into ``birdweather_tbl``.

    Args:
        records: Dataclass instances to upsert.
        pool: Connection pool (defaults to module-level singleton).
        chunk_size: Rows per INSERT batch.

    Returns:
        Total number of rows upserted.
    """
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [tuple(getattr(r, c) for c in _BWD_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _BWD_SQL, values)
            total += len(values)
        logger.debug("birdweather_tbl: upserted %d rows (total %d)", len(values), total)

    logger.info("birdweather_tbl: upserted %d rows total", total)
    return total
