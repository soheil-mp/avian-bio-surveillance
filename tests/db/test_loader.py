"""Tests for avian_biosurveillance.db.loader module.

Covers:
- SQL generation correctness (column lists, ON CONFLICT clauses)
- Chunked batching logic
- Empty-input short-circuit
- All five upsert functions
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import psycopg2.extras

import avian_biosurveillance.db.loaders.birdweather as _bwd_loader
import avian_biosurveillance.db.loaders.knmi as _kwo_loader
import avian_biosurveillance.db.loaders.sovon as _svm_loader
from avian_biosurveillance.db import loaders as loader
from avian_biosurveillance.db.models import (
    BirdWeatherDetection,
    KNMIWeatherObservation,
)


# Expose private constants for tests


class loader:
    _BWD_SQL = _bwd_loader._BWD_SQL
    _BWD_COLUMNS = _bwd_loader._BWD_COLUMNS
    _SPECIES_SQL = _bwd_loader._SPECIES_SQL
    _SPECIES_COLUMNS = _bwd_loader._SPECIES_COLUMNS
    _STATION_SQL = _bwd_loader._STATION_SQL
    _STATION_COLUMNS = _bwd_loader._STATION_COLUMNS
    _KWO_SQL = _kwo_loader._KWO_SQL
    _SVM_SQL = _svm_loader._SVM_SQL
    _SVM_COLUMNS = _svm_loader._SVM_COLUMNS
    _chunked = staticmethod(_bwd_loader.chunked)
    upsert_birdweather_detections = staticmethod(
        _bwd_loader.upsert_birdweather_detections
    )
    upsert_birdweather_species = staticmethod(_bwd_loader.upsert_birdweather_species)
    upsert_birdweather_stations = staticmethod(_bwd_loader.upsert_birdweather_stations)
    upsert_knmi_observations = staticmethod(_kwo_loader.upsert_knmi_observations)
    upsert_sovon_mortality = staticmethod(_svm_loader.upsert_sovon_mortality)


# =============================================================================
# Helpers
# =============================================================================


def _make_bwd(**overrides) -> BirdWeatherDetection:
    defaults = {
        "detection_id": "det1",
        "detected_at": datetime(2024, 6, 15, 8, 0, tzinfo=UTC),
        "observation_date": date(2024, 6, 15),
        "species_id": "sp42",
        "station_id": "100",
    }
    defaults.update(overrides)
    return BirdWeatherDetection(**defaults)


def _make_kwo(**overrides) -> KNMIWeatherObservation:
    defaults = {
        "observed_at": datetime(2024, 6, 15, 8, 0, tzinfo=UTC),
        "observation_date": date(2024, 6, 15),
        "station_id": "KNMI001",
    }
    defaults.update(overrides)
    return KNMIWeatherObservation(**defaults)


def _mock_pool():
    """Create a mock ConnectionPool with a cursor context manager."""
    pool = MagicMock()
    cursor = MagicMock()
    pool.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    pool.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pool, cursor


# =============================================================================
# SQL structure checks
# =============================================================================


class TestSQLGeneration:
    """Verify the generated SQL strings are well-formed."""

    def test_bwd_sql_contains_on_conflict(self):
        assert "ON CONFLICT (detection_id, detected_at)" in loader._BWD_SQL

    def test_bwd_sql_all_columns_present(self):
        for col in loader._BWD_COLUMNS:
            assert col in loader._BWD_SQL

    def test_kwo_sql_contains_on_conflict(self):
        assert "ON CONFLICT (station_id, observed_at)" in loader._KWO_SQL

    def test_svm_sql_contains_on_conflict(self):
        assert (
            "ON CONFLICT ON CONSTRAINT uq_sovon_species_year_month" in loader._SVM_SQL
        )

    def test_species_sql_targets_species_tbl(self):
        assert "INSERT INTO species_tbl" in loader._SPECIES_SQL
        assert "ON CONFLICT (species_id)" in loader._SPECIES_SQL
        assert "phylopic_svg_url" in loader._SPECIES_SQL
        assert "phylopic_thumb_url" in loader._SPECIES_SQL

    def test_station_sql_targets_birdweather_stations_tbl(self):
        assert "INSERT INTO birdweather_stations_tbl" in loader._STATION_SQL
        assert "ON CONFLICT (station_id)" in loader._STATION_SQL

    def test_bwd_update_excludes_pk(self):
        assert "detection_id = EXCLUDED.detection_id" not in loader._BWD_SQL
        assert "detected_at = EXCLUDED.detected_at" not in loader._BWD_SQL

    def test_kwo_update_excludes_pk(self):
        assert "station_id = EXCLUDED.station_id" not in loader._KWO_SQL
        assert "observed_at = EXCLUDED.observed_at" not in loader._KWO_SQL


# =============================================================================
# Chunking
# =============================================================================


class TestChunking:
    """Verify the internal _chunked helper."""

    def test_exact_chunk(self):
        result = list(loader._chunked([1, 2, 3, 4], 2))
        assert result == [[1, 2], [3, 4]]

    def test_remainder_chunk(self):
        result = list(loader._chunked([1, 2, 3], 2))
        assert result == [[1, 2], [3]]

    def test_empty_input(self):
        result = list(loader._chunked([], 5))
        assert result == []

    def test_single_element(self):
        result = list(loader._chunked([42], 5))
        assert result == [[42]]


# =============================================================================
# Upsert functions — empty input
# =============================================================================


class TestEmptyInput:
    """All upsert functions should return 0 for empty sequences."""

    def test_bwd_empty(self):
        assert loader.upsert_birdweather_detections([]) == 0

    def test_kwo_empty(self):
        assert loader.upsert_knmi_observations([]) == 0

    def test_svm_empty(self):
        assert loader.upsert_sovon_mortality([]) == 0

    def test_species_empty(self):
        assert loader.upsert_birdweather_species([]) == 0

    def test_stations_empty(self):
        assert loader.upsert_birdweather_stations([]) == 0


# =============================================================================
# Upsert functions — single record
# =============================================================================


class TestUpsertSingleRecord:
    """Verify each upsert function calls execute_values correctly."""

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_bwd_single(self, mock_exec):
        pool, cursor = _mock_pool()
        result = loader.upsert_birdweather_detections([_make_bwd()], pool=pool)
        assert result == 1
        mock_exec.assert_called_once()
        args = mock_exec.call_args
        assert "birdweather_tbl" in args[0][1]
        assert len(args[0][2]) == 1  # one row

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_kwo_single(self, mock_exec):
        pool, cursor = _mock_pool()
        result = loader.upsert_knmi_observations([_make_kwo()], pool=pool)
        assert result == 1
        mock_exec.assert_called_once()

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_species_single(self, mock_exec):
        pool, cursor = _mock_pool()
        result = loader.upsert_birdweather_species(
            [
                {
                    "species_id": "sp1",
                    "common_name": "Barn Swallow",
                    "scientific_name": "Hirundo rustica",
                    "prediction_area": {"type": "Polygon"},
                    "range_map": {"type": "FeatureCollection", "features": []},
                }
            ],
            pool=pool,
        )
        assert result == 1
        mock_exec.assert_called_once()
        args = mock_exec.call_args
        assert "species_tbl" in args[0][1]
        row = args[0][2][0]
        col_idx = {c: i for i, c in enumerate(loader._SPECIES_COLUMNS)}
        assert row[col_idx["species_id"]] == "sp1"
        assert isinstance(row[col_idx["prediction_area"]], psycopg2.extras.Json)
        assert row[col_idx["phylopic_svg_url"]] is None
        assert row[col_idx["phylopic_attribution"]] == ""
        assert row[col_idx["phylopic_uuid"]] == ""

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_station_single(self, mock_exec):
        pool, cursor = _mock_pool()
        result = loader.upsert_birdweather_stations(
            [
                {
                    "station_id": "station-1",
                    "station_name": "Test Station",
                    "station_country": "Netherlands",
                    "lat": 52.1,
                    "lon": 5.1,
                }
            ],
            pool=pool,
        )
        assert result == 1
        mock_exec.assert_called_once()
        args = mock_exec.call_args
        assert "birdweather_stations_tbl" in args[0][1]
        row = args[0][2][0]
        col_idx = {c: i for i, c in enumerate(loader._STATION_COLUMNS)}
        assert row[col_idx["station_id"]] == "station-1"
        assert row[col_idx["lat"]] == 52.1

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_svm_single(self, mock_exec):
        pool, cursor = _mock_pool()
        record = {
            "species_english": "Eurasian Blackbird",
            "species_dutch": "Merel",
            "sovon_species_id": None,
            "year": 2020,
            "month": 6,
            "dead_bird_count": 15,
            "n_total": None,
            "source_image": None,
        }
        result = loader.upsert_sovon_mortality([record], pool=pool)
        assert result == 1
        mock_exec.assert_called_once()


# =============================================================================
# Upsert — batching
# =============================================================================


class TestUpsertBatching:
    """Verify multi-chunk batching works correctly."""

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_bwd_two_chunks(self, mock_exec):
        pool, cursor = _mock_pool()
        records = [_make_bwd(detection_id=f"det{i}") for i in range(7)]
        result = loader.upsert_birdweather_detections(records, pool=pool, chunk_size=3)
        assert result == 7
        # 3 chunks: 3 + 3 + 1
        assert mock_exec.call_count == 3

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_kwo_exact_chunk(self, mock_exec):
        pool, cursor = _mock_pool()
        records = [_make_kwo(station_id=f"S{i}") for i in range(6)]
        result = loader.upsert_knmi_observations(records, pool=pool, chunk_size=3)
        assert result == 6
        assert mock_exec.call_count == 2


# =============================================================================
# Value tuple extraction
# =============================================================================


class TestValueExtraction:
    """Verify the correct values are extracted from dataclasses."""

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_bwd_values_match_columns(self, mock_exec):
        pool, cursor = _mock_pool()
        bwd = _make_bwd(confidence=0.95, score=8.5)
        loader.upsert_birdweather_detections([bwd], pool=pool)

        values = mock_exec.call_args[0][2]
        row = values[0]
        # Map column to position
        col_idx = {c: i for i, c in enumerate(loader._BWD_COLUMNS)}
        assert row[col_idx["detection_id"]] == "det1"
        assert row[col_idx["confidence"]] == 0.95
        assert row[col_idx["score"]] == 8.5
        assert row[col_idx["species_id"]] == "sp42"

    @patch(
        "avian_biosurveillance.db.loaders.birdweather.psycopg2.extras.execute_values"
    )
    def test_svm_missing_keys_default_to_none(self, mock_exec):
        pool, cursor = _mock_pool()
        record = {
            "species_english": "Song Thrush",
            "year": 2021,
            "month": 3,
            "dead_bird_count": 0,
        }
        loader.upsert_sovon_mortality([record], pool=pool)
        values = mock_exec.call_args[0][2]
        row = values[0]
        col_idx = {c: i for i, c in enumerate(loader._SVM_COLUMNS)}
        assert row[col_idx["species_dutch"]] is None
        assert row[col_idx["n_total"]] is None
