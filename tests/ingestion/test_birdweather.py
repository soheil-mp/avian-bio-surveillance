"""Comprehensive tests for the BirdWeather Data Ingestion Module.

Tests cover:
- BirdWeatherClient queries and filtering
- All filter variants (Gt, Lt, Gte, Lte)
- Nested station analytics
- Legacy parameter backwards compatibility
- BirdWeatherSubscription WebSocket class
- DataIngestor pipeline
- AudioCollector with parallel downloads
"""

import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.ingestion.birdweather import (
    DEFAULT_NE,
    DEFAULT_SW,
    QUERY_DETECTIONS_COMPREHENSIVE,
    QUERY_STATION_SINGLE,
    AudioCollector,
    BirdWeatherClient,
    BirdWeatherSubscription,
    CollectionStats,
    DataIngestor,
    main,
)


# Add src to sys.path to ensure we can import the module if it's not installed
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src"))
)


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_client():
    """Create a BirdWeatherClient with mocked query execution."""
    client = BirdWeatherClient()
    client._execute_query = MagicMock()
    return client


@pytest.fixture
def ingestor(tmp_path):
    """Create a DataIngestor with mocked client."""
    with patch(
        "avian_biosurveillance.ingestion.birdweather.BirdWeatherClient"
    ) as MockClient:
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        ingestor = DataIngestor(output_dir=tmp_path)
        ingestor.client = mock_instance
        yield ingestor


@pytest.fixture
def sample_station():
    """Sample station data for testing."""
    return {
        "id": "12345",
        "name": "Test Station",
        "type": "puc",
        "location": "Amsterdam, Netherlands",
        "timezone": "Europe/Amsterdam",
        "country": "Netherlands",
        "coords": {"lat": 52.37, "lon": 4.89},
        "weather": {"temp": 15.5, "humidity": 70},
        "counts": {"species": 25, "detections": 150},
        "detectionCounts": [
            {
                "speciesId": "1",
                "count": 50,
                "bins": [{"key": "2025-01-01", "count": 25}],
            }
        ],
        "timeOfDayDetectionCounts": [
            {"speciesId": "1", "count": 50, "bins": [{"key": 8, "count": 15}]}
        ],
    }


@pytest.fixture
def sample_detection():
    """Sample detection data for testing."""
    return {
        "id": "det123",
        "timestamp": "2025-01-05T08:30:00Z",
        "score": 0.95,
        "confidence": 0.88,
        "probability": 0.92,
        "certainty": "almostCertain",
        "speciesId": "sp456",
        "species": {
            "id": "sp456",
            "commonName": "European Robin",
            "scientificName": "Erithacus rubecula",
        },
        "station": {"id": "12345", "name": "Test Station"},
        "soundscape": {
            "id": "ss789",
            "url": "https://example.com/audio.mp3",
            "duration": 15,
        },
    }


# =============================================================================
# BIRDWEATHER CLIENT TESTS
# =============================================================================


class TestBirdWeatherClient:
    """Tests for BirdWeatherClient class."""

    def test_fetch_counts(self, mock_client):
        """Test fetching aggregate counts."""
        mock_client._execute_query.return_value = {
            "data": {
                "counts": {
                    "detections": 1000,
                    "species": 50,
                    "stations": 25,
                    "birdnet": 200,
                    "breakdown": {"stations": [{"type": "puc", "count": 20}]},
                }
            }
        }

        period = {"from": "2025-01-01", "to": "2025-01-08"}
        result = mock_client.fetch_counts(period=period)

        mock_client._execute_query.assert_called_once()
        assert result["detections"] == 1000
        assert result["species"] == 50
        assert "breakdown" in result

    def test_fetch_station_with_new_parameters(self, mock_client, sample_station):
        """Test fetching single station with new parameters (sensor history, detections)."""
        mock_client._execute_query.return_value = {"data": {"station": sample_station}}

        result = mock_client.fetch_station(
            station_id="12345",
            period={"from": "2025-01-01", "to": "2025-01-08"},
            top_species_limit=30,
            sensor_history_limit=96,
            detections_first=100,
        )

        # Verify correct parameters passed
        call_args = mock_client._execute_query.call_args
        variables = call_args[0][1]

        assert variables["id"] == "12345"
        assert variables["topSpeciesLimit"] == 30
        assert variables["sensorHistoryLimit"] == 96
        assert variables["detectionsFirst"] == 100

        # Verify nested analytics returned
        assert "detectionCounts" in result
        assert "timeOfDayDetectionCounts" in result

    def test_fetch_detections_all_filter_variants(self, mock_client, sample_detection):
        """Test fetching detections with all filter variants (Gt, Lt, Gte, Lte)."""
        mock_client._execute_query.return_value = {
            "data": {
                "detections": {
                    "nodes": [sample_detection],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "totalCount": 1,
                    "speciesCount": 1,
                }
            }
        }

        # Test with all filter variants
        detections = list(
            mock_client.fetch_detections(
                start=datetime(2025, 1, 1),
                end=datetime(2025, 1, 8),
                score_gt=0.5,
                score_lt=1.0,
                score_gte=0.6,
                score_lte=0.99,
                confidence_gt=0.3,
                confidence_lte=0.95,
                probability_gte=0.7,
                override_station_filters=True,
                ne={"lat": 53.7, "lon": 7.22},
                sw={"lat": 50.75, "lon": 3.33},
            )
        )

        # Verify correct parameters passed
        call_args = mock_client._execute_query.call_args
        variables = call_args[0][1]

        assert variables["scoreGt"] == 0.5
        assert variables["scoreLt"] == 1.0
        assert variables["scoreGte"] == 0.6
        assert variables["scoreLte"] == 0.99
        assert variables["confidenceGt"] == 0.3
        assert variables["confidenceLte"] == 0.95
        assert variables["probabilityGte"] == 0.7
        assert variables["overrideStationFilters"] is True
        assert variables["ne"] == {"lat": 53.7, "lon": 7.22}
        assert variables["sw"] == {"lat": 50.75, "lon": 3.33}

        assert len(detections) == 1
        assert detections[0]["id"] == "det123"

    def test_fetch_detections_legacy_parameters(self, mock_client, sample_detection):
        """Test that legacy min_* parameters still work for backwards compatibility."""
        mock_client._execute_query.return_value = {
            "data": {
                "detections": {
                    "nodes": [sample_detection],
                    "pageInfo": {"hasNextPage": False},
                    "totalCount": 1,
                    "speciesCount": 1,
                }
            }
        }

        # Use legacy parameters
        detections = list(
            mock_client.fetch_detections(
                start=datetime(2025, 1, 1),
                end=datetime(2025, 1, 8),
                min_score=0.8,
                min_confidence=0.7,
                min_probability=0.6,
            )
        )

        # Verify legacy params mapped to _gte variants
        call_args = mock_client._execute_query.call_args
        variables = call_args[0][1]

        assert variables["scoreGte"] == 0.8
        assert variables["confidenceGte"] == 0.7
        assert variables["probabilityGte"] == 0.6

    def test_fetch_daily_detection_counts(self, mock_client):
        """Test fetching daily detection counts."""
        mock_client._execute_query.return_value = {
            "data": {
                "dailyDetectionCounts": [
                    {"date": "2025-01-01", "dayOfYear": 1, "total": 50, "counts": []},
                    {"date": "2025-01-02", "dayOfYear": 2, "total": 75, "counts": []},
                ]
            }
        }

        period = {"from": "2025-01-01", "to": "2025-01-08"}
        result = mock_client.fetch_daily_detection_counts(period=period)

        assert len(result) == 2
        assert result[0]["date"] == "2025-01-01"
        assert result[1]["total"] == 75

    def test_fetch_time_of_day_detection_counts(self, mock_client):
        """Test fetching time-of-day detection counts."""
        mock_client._execute_query.return_value = {
            "data": {
                "timeOfDayDetectionCounts": [
                    {"speciesId": "1", "count": 100, "bins": [{"key": 6, "count": 25}]}
                ]
            }
        }

        result = mock_client.fetch_time_of_day_detection_counts(
            period={"from": "2025-01-01", "to": "2025-01-08"}, score_gte=0.8
        )

        assert len(result) == 1
        assert result[0]["count"] == 100
        assert result[0]["bins"][0]["key"] == 6

    def test_fetch_top_species(self, mock_client):
        """Test fetching top species."""
        mock_client._execute_query.return_value = {
            "data": {
                "topSpecies": [
                    {
                        "speciesId": "1",
                        "count": 500,
                        "averageProbability": 0.85,
                        "species": {"commonName": "European Robin"},
                        "breakdown": {
                            "almostCertain": 300,
                            "veryLikely": 150,
                            "uncertain": 50,
                            "unlikely": 0,
                        },
                    }
                ]
            }
        }

        result = mock_client.fetch_top_species(
            limit=10, period={"from": "2025-01-01", "to": "2025-01-08"}
        )

        assert len(result) == 1
        assert result[0]["count"] == 500
        assert result[0]["breakdown"]["almostCertain"] == 300

    def test_fetch_all_stations_pagination(self, mock_client):
        """Test station fetching with pagination."""
        # First page
        page1 = {
            "data": {
                "stations": {
                    "nodes": [{"id": "1", "name": "Station 1"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                    "totalCount": 2,
                }
            }
        }
        # Second page
        page2 = {
            "data": {
                "stations": {
                    "nodes": [{"id": "2", "name": "Station 2"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "totalCount": 2,
                }
            }
        }
        mock_client._execute_query.side_effect = [page1, page2]

        stations = list(mock_client.fetch_all_stations(ne=DEFAULT_NE, sw=DEFAULT_SW))

        assert len(stations) == 2
        assert stations[0]["id"] == "1"
        assert stations[1]["id"] == "2"
        assert mock_client._execute_query.call_count == 2

    def test_fetch_all_stations_without_bbox(self, mock_client):
        """Global station listing should work without passing bbox filters."""
        mock_client._execute_query.return_value = {
            "data": {
                "stations": {
                    "nodes": [{"id": "1", "name": "Station 1"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "totalCount": 1,
                }
            }
        }

        stations = list(mock_client.fetch_all_stations())

        assert len(stations) == 1
        _, variables = mock_client._execute_query.call_args[0]
        assert variables["ne"] is None
        assert variables["sw"] is None


# =============================================================================
# SUBSCRIPTION TESTS
# =============================================================================


class TestBirdWeatherSubscription:
    """Tests for BirdWeatherSubscription class."""

    def test_subscription_initialization(self):
        """Test subscription class initializes correctly."""
        sub = BirdWeatherSubscription()

        assert sub.ws_url == "wss://app.birdweather.com/cable"
        assert sub.on_detection is None
        assert sub._running is False

    def test_subscription_query_contains_all_filters(self):
        """Test that subscription query contains all filter parameters."""
        sub = BirdWeatherSubscription()
        query = sub.SUBSCRIPTION_QUERY

        # Verify all filter variants are present
        assert "$scoreGt: Float" in query
        assert "$scoreLt: Float" in query
        assert "$scoreGte: Float" in query
        assert "$scoreLte: Float" in query
        assert "$confidenceGt: Float" in query
        assert "$confidenceLte: Float" in query
        assert "$probabilityGte: Float" in query
        assert "$overrideStationFilters: Boolean" in query
        assert "$timeOfDayGte: Int" in query
        assert "$timeOfDayLte: Int" in query

    def test_subscription_requires_websocket_client(self):
        """Test that subscription raises ImportError if websocket-client not installed."""
        sub = BirdWeatherSubscription()

        with patch.dict("sys.modules", {"websocket": None}):
            with pytest.raises(ImportError) as exc_info:
                sub.subscribe(callback=lambda x: None)

            assert "websocket-client" in str(exc_info.value)

    def test_subscription_builds_correct_variables(self):
        """Test that subscription builds correct filter variables."""
        # Create a mock websocket module
        mock_websocket = MagicMock()
        mock_ws_app = MagicMock()
        mock_websocket.WebSocketApp.return_value = mock_ws_app

        # Patch the import inside the subscribe method
        with patch.dict("sys.modules", {"websocket": mock_websocket}):
            sub = BirdWeatherSubscription()

            # Subscribe with various filters (non-blocking)
            sub.subscribe(
                callback=lambda x: None,
                station_ids=["12345"],
                score_gte=0.8,
                confidence_gte=0.7,
                countries=["Netherlands"],
                blocking=False,
            )

            # Verify WebSocketApp was created
            mock_websocket.WebSocketApp.assert_called_once()

            # Stop the subscription
            sub.stop()

    def test_is_running_property(self):
        """Test is_running method."""
        sub = BirdWeatherSubscription()

        assert sub.is_running() is False

        sub._running = True
        assert sub.is_running() is True


# =============================================================================
# DATA INGESTOR TESTS
# =============================================================================


class TestDataIngestor:
    """Tests for DataIngestor class."""

    def test_ingestor_creates_output_directory(self, tmp_path):
        """Test that ingestor creates output directory if it doesn't exist."""
        output_dir = tmp_path / "new_dir" / "nested"

        with patch("avian_biosurveillance.ingestion.birdweather.BirdWeatherClient"):
            ingestor = DataIngestor(output_dir=output_dir)

        assert output_dir.exists()

    def test_ingest_species_metadata(self, ingestor):
        """Test species metadata ingestion."""
        ingestor.client.fetch_all_species = MagicMock(
            return_value=iter(
                [
                    {
                        "id": "1",
                        "commonName": "Robin",
                        "scientificName": "Erithacus rubecula",
                    },
                    {
                        "id": "2",
                        "commonName": "Blackbird",
                        "scientificName": "Turdus merula",
                    },
                ]
            )
        )

        count = ingestor.ingest_species_metadata()

        assert count == 2

        # Verify file created
        files = list(ingestor.output_dir.glob("species_metadata.jsonl"))
        assert len(files) == 1

        with open(files[0]) as f:
            lines = f.readlines()
            assert len(lines) == 2
            data = json.loads(lines[0])
            assert data["commonName"] == "Robin"
            assert "_ingested_at" in data

    def test_ingest_stations(self, ingestor):
        """Test stations ingestion."""
        ingestor.client.fetch_all_stations = MagicMock(
            return_value=iter(
                [
                    {"id": "1", "name": "Station A", "country": "Netherlands"},
                    {"id": "2", "name": "Station B", "country": "Belgium"},
                ]
            )
        )

        stations = ingestor.ingest_stations(ne=DEFAULT_NE, sw=DEFAULT_SW)

        assert len(stations) == 2

        # Verify file created
        from datetime import date

        files = list(ingestor.output_dir.glob(f"stations_{date.today()}.jsonl"))
        assert len(files) == 1

    def test_ingest_aggregates(self, ingestor):
        """Test aggregate data ingestion."""
        ingestor.client.fetch_counts = MagicMock(
            return_value={"detections": 1000, "species": 50, "stations": 25}
        )
        ingestor.client.fetch_daily_detection_counts = MagicMock(
            return_value=[{"date": "2025-01-01", "total": 100}]
        )
        ingestor.client.fetch_time_of_day_detection_counts = MagicMock(
            return_value=[{"speciesId": "1", "count": 50}]
        )
        ingestor.client.fetch_top_species = MagicMock(
            return_value=[{"speciesId": "1", "count": 200}]
        )
        ingestor.client.fetch_top_birdnet_species = MagicMock(
            return_value=[{"speciesId": "1", "count": 100}]
        )

        period = {"from": "2025-01-01", "to": "2025-01-08"}
        ingestor.ingest_aggregates(period=period)

        # Verify all methods called
        ingestor.client.fetch_counts.assert_called_once()
        ingestor.client.fetch_daily_detection_counts.assert_called_once()
        ingestor.client.fetch_time_of_day_detection_counts.assert_called_once()
        ingestor.client.fetch_top_species.assert_called_once()
        ingestor.client.fetch_top_birdnet_species.assert_called_once()

        # Verify files created
        from datetime import date

        assert len(list(ingestor.output_dir.glob(f"counts_{date.today()}.json"))) == 1
        assert (
            len(list(ingestor.output_dir.glob(f"daily_counts_{date.today()}.jsonl")))
            == 1
        )
        assert (
            len(
                list(
                    ingestor.output_dir.glob(f"time_of_day_counts_{date.today()}.jsonl")
                )
            )
            == 1
        )
        assert (
            len(list(ingestor.output_dir.glob(f"top_species_{date.today()}.jsonl")))
            == 1
        )

    def test_write_jsonl_adds_ingested_at(self, ingestor):
        """Test that _write_jsonl adds ingestion timestamp."""
        records = [{"id": "1", "name": "Test"}]
        filepath = ingestor.output_dir / "test.jsonl"

        count = ingestor._write_jsonl(filepath, records)

        assert count == 1

        with open(filepath) as f:
            data = json.loads(f.readline())
            assert "_ingested_at" in data
            # Verify timestamp is valid ISO format
            datetime.fromisoformat(data["_ingested_at"])


# =============================================================================
# QUERY VALIDATION TESTS
# =============================================================================


class TestQueryStructure:
    """Tests to verify GraphQL query structure and completeness."""

    def test_detections_query_has_all_filter_variants(self):
        """Test that detections query includes all filter parameter variants."""
        query = QUERY_DETECTIONS_COMPREHENSIVE

        # Score variants
        assert "$scoreGt: Float" in query
        assert "$scoreLt: Float" in query
        assert "$scoreGte: Float" in query
        assert "$scoreLte: Float" in query

        # Confidence variants
        assert "$confidenceGt: Float" in query
        assert "$confidenceLt: Float" in query
        assert "$confidenceGte: Float" in query
        assert "$confidenceLte: Float" in query

        # Probability variants
        assert "$probabilityGt: Float" in query
        assert "$probabilityLt: Float" in query
        assert "$probabilityGte: Float" in query
        assert "$probabilityLte: Float" in query

        # New parameters
        assert "$overrideStationFilters: Boolean" in query
        assert "$ne: InputLocation" in query
        assert "$sw: InputLocation" in query

    def test_station_query_has_nested_analytics(self):
        """Test that station query includes nested detection analytics."""
        query = QUERY_STATION_SINGLE

        assert "detectionCounts(period: $period)" in query
        assert "timeOfDayDetectionCounts(period: $period)" in query
        assert "detections(first: $detectionsFirst)" in query
        assert "$sensorHistoryLimit: Int" in query

    def test_station_query_has_sensor_history(self):
        """Test that station query includes sensor history with pagination."""
        query = QUERY_STATION_SINGLE

        assert "environmentHistory(first: $sensorHistoryLimit)" in query
        assert "lightHistory(first: $sensorHistoryLimit)" in query
        assert "accelHistory(first: $sensorHistoryLimit)" in query
        assert "locationHistory(first: $sensorHistoryLimit)" in query
        assert "magHistory(first: $sensorHistoryLimit)" in query
        assert "systemHistory(first: $sensorHistoryLimit)" in query


# =============================================================================
# INTEGRATION-STYLE TESTS (with mocking)
# =============================================================================


class TestIntegration:
    """Integration-style tests simulating full workflows."""

    def test_full_detection_fetch_workflow(self, mock_client, sample_detection):
        """Test complete detection fetch workflow with pagination."""
        # Simulate multi-page response
        page1 = {
            "data": {
                "detections": {
                    "nodes": [sample_detection],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                    "totalCount": 2,
                    "speciesCount": 1,
                }
            }
        }
        sample_detection2 = sample_detection.copy()
        sample_detection2["id"] = "det456"
        page2 = {
            "data": {
                "detections": {
                    "nodes": [sample_detection2],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "totalCount": 2,
                    "speciesCount": 1,
                }
            }
        }
        mock_client._execute_query.side_effect = [page1, page2]

        # Omit start/end to use the simpler pagination path (no time-slicing)
        detections = list(mock_client.fetch_detections(score_gte=0.7))

        assert len(detections) == 2
        assert detections[0]["id"] == "det123"
        assert detections[1]["id"] == "det456"

    def test_ingestor_run_pipeline(self, ingestor):
        """Test full pipeline run method."""
        # Mock all required methods
        ingestor.client.fetch_all_stations = MagicMock(
            return_value=iter([{"id": "1", "name": "Station A"}])
        )
        ingestor.client.fetch_detections = MagicMock(return_value=iter([]))
        ingestor.client.fetch_all_species = MagicMock(
            return_value=iter([{"id": "1", "commonName": "Robin"}])
        )
        ingestor.client.fetch_counts = MagicMock(return_value={"detections": 100})
        ingestor.client.fetch_daily_detection_counts = MagicMock(return_value=[])
        ingestor.client.fetch_time_of_day_detection_counts = MagicMock(return_value=[])
        ingestor.client.fetch_top_species = MagicMock(return_value=[])
        ingestor.client.fetch_top_birdnet_species = MagicMock(return_value=[])
        ingestor.client.fetch_birdnet_sightings = MagicMock(return_value=iter([]))

        # Run with all options
        ingestor.run(
            days=1,
            ingest_species=True,
            ingest_aggregates=True,
            min_score=0.8,  # Test legacy parameter passthrough
        )

        # Verify key methods called
        ingestor.client.fetch_all_stations.assert_called()
        ingestor.client.fetch_all_species.assert_called()
        ingestor.client.fetch_counts.assert_called()


# =============================================================================
# AUDIO COLLECTOR TESTS
# =============================================================================


class TestAudioCollector:
    """Tests for AudioCollector class (parallel audio downloads)."""

    @pytest.fixture
    def collector(self, tmp_path):
        """Create an AudioCollector with mocked client."""
        with patch(
            "avian_biosurveillance.ingestion.birdweather.BirdWeatherClient"
        ) as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value = mock_instance
            collector = AudioCollector(output_dir=tmp_path)
            collector.client = mock_instance
            yield collector

    def test_collector_creates_output_directory(self, tmp_path):
        """Test that collector creates output directory."""
        output_dir = tmp_path / "new_dir"

        with patch("avian_biosurveillance.ingestion.birdweather.BirdWeatherClient"):
            collector = AudioCollector(output_dir=output_dir)

        assert output_dir.exists()

    def test_collection_stats_dataclass(self):
        """Test CollectionStats dataclass."""
        stats = CollectionStats()

        assert stats.species_queried == 0
        assert stats.detections_found == 0
        assert stats.audio_downloaded == 0
        assert stats.audio_cached == 0
        assert stats.audio_failed == 0

        # Test to_dict
        d = stats.to_dict()
        assert "run" in d
        assert "started_at" in d["run"]
        assert "results" in d
        assert "detections_found" in d["results"]

    def test_collect_with_hours(self, collector, sample_detection):
        """Test collect with hours parameter."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.client.download_file = MagicMock(return_value=(True, "downloaded"))

        stats = collector.collect(
            hours=1, download_audio=False, max_detections=1, enrich_weather=False
        )

        assert stats.detections_found == 1
        collector.client.fetch_detections.assert_called_once()

    def test_collect_with_date_range(self, collector, sample_detection):
        """Test collect with start_time/end_time parameters."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)

        start = datetime(2026, 1, 1)
        end = datetime(2026, 1, 15)

        stats = collector.collect(
            start_time=start,
            end_time=end,
            download_audio=False,
            enrich_weather=False,
        )

        # Verify date range was passed correctly
        call_args = collector.client.fetch_detections.call_args
        assert call_args.kwargs["start"] == start
        assert call_args.kwargs["end"] == end

    def test_collect_with_species_filter(self, collector, sample_detection):
        """Test collect with species_ids filter."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)

        stats = collector.collect(
            hours=1,
            species_ids=["11", "6"],
            download_audio=False,
            enrich_weather=False,
        )

        # Verify species filter was passed
        call_args = collector.client.fetch_detections.call_args
        assert call_args.kwargs["species_ids"] == ["11", "6"]

    def test_collect_with_country_filter(self, collector, sample_detection):
        """Test collect with country filter."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)

        stats = collector.collect(
            hours=1,
            country="Netherlands",
            download_audio=False,
            enrich_weather=False,
        )

        # Verify country filter was passed
        call_args = collector.client.fetch_detections.call_args
        assert call_args.kwargs["countries"] == ["Netherlands"]

    def test_collect_with_continent_filter(self, collector, sample_detection):
        """Test collect with continent filter."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)

        stats = collector.collect(
            hours=1,
            continent="Europe",
            download_audio=False,
            enrich_weather=False,
        )

        # Verify continent filter was passed
        call_args = collector.client.fetch_detections.call_args
        assert call_args.kwargs["continents"] == ["Europe"]

    def test_collect_saves_detections_jsonl(
        self, collector, sample_detection, tmp_path
    ):
        """Test that collect saves detections to JSONL file."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.output_dir = tmp_path

        stats = collector.collect(hours=1, download_audio=False, enrich_weather=False)

        # Check that detections.jsonl was created
        today = datetime.now().strftime("%Y-%m-%d")
        detections_file = tmp_path / today / "detections.jsonl"
        assert detections_file.exists()

        with open(detections_file) as f:
            data = json.loads(f.readline())
            assert data["id"] == "det123"
            assert "_collected_at" in data

    def test_collect_saves_species_list(self, collector, sample_detection, tmp_path):
        """Test that collect saves species list to metadata JSON file."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.output_dir = tmp_path

        stats = collector.collect(hours=1, download_audio=False, enrich_weather=False)

        today = datetime.now().strftime("%Y-%m-%d")
        metadata_file = tmp_path / today / "metadata.json"
        assert metadata_file.exists()

        with open(metadata_file) as f:
            data = json.load(f)
            species = data.get("results", {}).get("species", [])
            assert len(species) >= 1

    def test_collect_database_target_skips_local_detection_files(
        self, collector, sample_detection, tmp_path
    ):
        """Successful database sync should clean up the staged local bundle."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.client.fetch_station_metadata = MagicMock(return_value=None)
        collector.output_dir = tmp_path

        with (
            patch(
                "avian_biosurveillance.db.models.BirdWeatherDetection.from_jsonl",
                side_effect=lambda det: det,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_species",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_stations",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_detections",
                return_value=1,
            ) as mock_upsert,
        ):
            stats = collector.collect(
                hours=1,
                target="database",
                download_audio=False,
                enrich_weather=False,
            )

        today = datetime.now().strftime("%Y-%m-%d")
        date_dir = tmp_path / today
        assert stats.detections_found == 1
        assert not date_dir.exists()
        mock_upsert.assert_called_once()

    def test_collect_database_target_persists_before_audio_downloads(
        self, collector, sample_detection
    ):
        """Database sync should happen only after the local audio stage finishes."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.client.fetch_station_metadata = MagicMock(return_value=None)

        download_checks = []

        def _fake_download(*_args, **_kwargs):
            download_checks.append(mock_upsert.called)
            return True, "downloaded"

        collector.client.download_file = MagicMock(side_effect=_fake_download)

        with (
            patch(
                "avian_biosurveillance.db.models.BirdWeatherDetection.from_jsonl",
                side_effect=lambda det: det,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_species",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_stations",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_detections",
                return_value=1,
            ) as mock_upsert,
            patch("avian_biosurveillance.db.minio_client.AudioStore"),
        ):
            collector.collect(
                hours=1,
                target="database",
                download_audio=True,
                workers=1,
                enrich_weather=False,
            )

        mock_upsert.assert_called_once()
        assert download_checks == [False]

    def test_collect_database_target_retains_local_bundle_when_sync_fails(
        self, collector, sample_detection, tmp_path
    ):
        """Failed DB sync should leave the staged local bundle on disk for retry."""
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.client.fetch_station_metadata = MagicMock(return_value=None)
        collector.output_dir = tmp_path

        with (
            patch(
                "avian_biosurveillance.db.models.BirdWeatherDetection.from_jsonl",
                side_effect=lambda det: det,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_species",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_stations",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_detections",
                side_effect=RuntimeError("db down"),
            ),
        ):
            stats = collector.collect(
                hours=1,
                target="database",
                download_audio=False,
                enrich_weather=False,
            )

        today = datetime.now().strftime("%Y-%m-%d")
        date_dir = tmp_path / today
        manifest = json.loads((date_dir / "manifest.json").read_text(encoding="utf-8"))

        assert date_dir.exists()
        assert manifest["load"]["status"] == "failed"
        assert "db down" in manifest["load"]["error"]
        assert any("bundle sync failed" in message for message in stats.errors)

    def test_collect_database_target_skips_malformed_db_records(
        self, collector, sample_detection
    ):
        """One malformed detection should not abort the whole DB upsert batch."""
        bad_detection = {"id": "bad-det"}
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection, bad_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=2)
        collector.client.fetch_station_metadata = MagicMock(return_value=None)

        def _fake_from_jsonl(det):
            if det.get("id") == "bad-det":
                raise ValueError("missing timestamp")
            return det

        with (
            patch(
                "avian_biosurveillance.db.models.BirdWeatherDetection.from_jsonl",
                side_effect=_fake_from_jsonl,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_species",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_stations",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_detections",
                return_value=1,
            ) as mock_upsert,
        ):
            stats = collector.collect(
                hours=1,
                target="database",
                download_audio=False,
                enrich_weather=False,
            )

        assert stats.detections_found == 2
        mock_upsert.assert_called_once_with([sample_detection])

    def test_collect_database_target_persists_species_and_station_metadata(
        self, collector
    ):
        """Database target should seed species and station metadata before detections."""
        detection = {
            "id": "det123",
            "timestamp": "2025-01-05T08:30:00Z",
            "score": 0.95,
            "confidence": 0.88,
            "probability": 0.92,
            "speciesId": "sp456",
            "species": {
                "id": "sp456",
                "commonName": "European Robin",
                "scientificName": "Erithacus rubecula",
            },
            "station": {
                "id": "12345",
                "name": "Test Station",
                "type": "puc",
                "country": "Netherlands",
                "state": "Noord-Holland",
                "coords": {"lat": 52.37, "lon": 4.89},
            },
        }
        station_payload = {
            "id": "12345",
            "name": "Test Station",
            "type": "puc",
            "location": "Amsterdam, Netherlands",
            "timezone": "Europe/Amsterdam",
            "continent": "Europe",
            "country": "Netherlands",
            "state": "Noord-Holland",
            "audioUrl": "https://example.com/station.mp3",
            "videoUrl": "https://example.com/station.mp4",
            "source": "birdweather_api",
            "locationPrivacy": "public",
            "coords": {"lat": 52.37, "lon": 4.89},
        }
        collector.client.fetch_detections = MagicMock(return_value=iter([detection]))
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.client.fetch_station_metadata = MagicMock(
            return_value=station_payload
        )

        call_order = []
        species_rows = []
        station_rows = []

        def _fake_species_upsert(rows):
            call_order.append("species")
            species_rows.extend(rows)
            return len(rows)

        def _fake_station_upsert(rows):
            call_order.append("stations")
            station_rows.extend(rows)
            return len(rows)

        def _fake_detection_upsert(rows):
            call_order.append("detections")
            return len(rows)

        with (
            patch(
                "avian_biosurveillance.db.models.BirdWeatherDetection.from_jsonl",
                side_effect=lambda det: det,
            ),
            patch(
                "avian_biosurveillance.ingestion.birdweather.resolve_phylopic",
                return_value=MagicMock(
                    svg_url="https://example.com/robin.svg",
                    thumb_url="https://example.com/robin-thumb.png",
                    attribution="PhyloPic test attribution",
                    uuid="phylopic-robin",
                ),
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_species",
                side_effect=_fake_species_upsert,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_stations",
                side_effect=_fake_station_upsert,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_detections",
                side_effect=_fake_detection_upsert,
            ),
        ):
            collector.collect(
                hours=1,
                target="database",
                download_audio=False,
                enrich_weather=False,
            )

        assert call_order == ["species", "stations", "detections"]
        assert len(species_rows) == 1
        species_row = species_rows[0]
        assert species_row["species_id"] == "sp456"
        assert species_row["common_name"] == "European Robin"
        assert species_row["scientific_name"] == "Erithacus rubecula"
        assert species_row["phylopic_svg_url"] == "https://example.com/robin.svg"
        assert (
            species_row["phylopic_thumb_url"] == "https://example.com/robin-thumb.png"
        )
        assert species_row["phylopic_attribution"] == "PhyloPic test attribution"
        assert species_row["phylopic_uuid"] == "phylopic-robin"
        assert species_row["phylopic_fetched_at"] is not None
        assert len(station_rows) == 1
        row = station_rows[0]
        assert row["station_id"] == "12345"
        assert row["station_name"] == "Test Station"
        assert row["station_location"] == "Amsterdam, Netherlands"
        assert row["station_timezone"] == "Europe/Amsterdam"
        assert row["station_continent"] == "Europe"
        assert row["lat"] == 52.37
        assert row["lon"] == 4.89

    def test_collect_database_target_falls_back_to_detection_station_metadata(
        self, collector
    ):
        """Station API failures should still persist basic detection-derived metadata."""
        detection = {
            "id": "det123",
            "timestamp": "2025-01-05T08:30:00Z",
            "score": 0.95,
            "confidence": 0.88,
            "probability": 0.92,
            "speciesId": "sp456",
            "species": {
                "id": "sp456",
                "commonName": "European Robin",
            },
            "station": {
                "id": "12345",
                "name": "Fallback Station",
                "type": "puc",
                "country": "Netherlands",
                "state": "Noord-Holland",
                "coords": {"lat": 52.37, "lon": 4.89},
            },
        }
        collector.client.fetch_detections = MagicMock(return_value=iter([detection]))
        collector.client.get_detections_count = MagicMock(return_value=1)
        collector.client.fetch_station_metadata = MagicMock(
            side_effect=RuntimeError("station api unavailable")
        )

        station_rows = []

        def _fake_station_upsert(rows):
            station_rows.extend(rows)
            return len(rows)

        with (
            patch(
                "avian_biosurveillance.db.models.BirdWeatherDetection.from_jsonl",
                side_effect=lambda det: det,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_species",
                return_value=1,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_stations",
                side_effect=_fake_station_upsert,
            ),
            patch(
                "avian_biosurveillance.db.loaders.upsert_birdweather_detections",
                return_value=1,
            ),
        ):
            collector.collect(
                hours=1,
                target="database",
                download_audio=False,
                enrich_weather=False,
            )

        assert len(station_rows) == 1
        row = station_rows[0]
        assert row["station_id"] == "12345"
        assert row["station_name"] == "Fallback Station"
        assert row["station_country"] == "Netherlands"
        assert row["station_state"] == "Noord-Holland"
        assert row["lat"] == 52.37
        assert row["lon"] == 4.89
        assert row["station_location"] is None
        assert row["station_timezone"] is None

    def test_download_single_audio_success(self, collector, tmp_path):
        """Test _download_single_audio helper method."""
        collector.client.download_file = MagicMock(return_value=(True, "downloaded"))

        task = {
            "url": "https://example.com/audio.flac",
            "audio_path": str(tmp_path / "test.flac"),
            "expected_size": 1000,
        }

        success, status = collector._download_single_audio(task)

        assert success is True
        assert status == "downloaded"

    def test_download_single_audio_already_exists(self, collector, tmp_path):
        """Test _download_single_audio skips existing files."""
        # Create a file with expected size
        audio_file = tmp_path / "existing.flac"
        audio_file.write_bytes(b"x" * 1000)

        task = {
            "url": "https://example.com/audio.flac",
            "audio_path": str(audio_file),
            "expected_size": 1000,
        }

        success, status = collector._download_single_audio(task)

        assert success is True
        assert status == "already_exists"

    def test_collect_with_workers_parameter(self, collector, sample_detection):
        """Test collect respects workers parameter."""
        # Test with 5 workers
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        stats = collector.collect(
            hours=1, workers=5, download_audio=False, enrich_weather=False
        )
        assert stats.detections_found == 1

        # Reset mock for second call with 20 workers
        sample_detection2 = sample_detection.copy()
        sample_detection2["id"] = "det789"
        collector.client.fetch_detections = MagicMock(
            return_value=iter([sample_detection2])
        )
        collector.client.get_detections_count = MagicMock(return_value=1)
        stats = collector.collect(
            hours=1, workers=20, download_audio=False, enrich_weather=False
        )
        assert stats.detections_found == 1

    def test_collect_max_detections_limit(self, collector):
        """Test collect respects max_detections limit."""
        # Return more detections than the limit
        detections = [
            {
                "id": f"det{i}",
                "speciesId": "1",
                "species": {"id": "1", "commonName": "Robin"},
            }
            for i in range(100)
        ]
        collector.client.fetch_detections = MagicMock(return_value=iter(detections))
        collector.client.get_detections_count = MagicMock(return_value=100)

        stats = collector.collect(
            hours=1, max_detections=10, download_audio=False, enrich_weather=False
        )

        assert stats.detections_found == 10
        assert stats.detections_scanned == 10

    def test_collect_max_detections_limits_api_scan_on_resume(
        self, collector, tmp_path
    ):
        """Test max_detections caps API scanning even when early records are duplicates."""
        collector.output_dir = tmp_path

        today = datetime.now().strftime("%Y-%m-%d")
        date_dir = tmp_path / today
        date_dir.mkdir(parents=True, exist_ok=True)
        detections_file = date_dir / "detections.jsonl"
        with open(detections_file, "w", encoding="utf-8") as f:
            for det_id in ("det0", "det1"):
                f.write(json.dumps({"id": det_id}) + "\n")

        yielded_ids = []

        def detection_stream():
            for det_id in ("det0", "det1", "det2", "det3"):
                yielded_ids.append(det_id)
                yield {
                    "id": det_id,
                    "speciesId": "1",
                    "species": {"id": "1", "commonName": "Robin"},
                }

        collector.client.fetch_detections = MagicMock(
            side_effect=lambda **_: detection_stream()
        )
        collector.client.get_detections_count = MagicMock(return_value=4)

        stats = collector.collect(
            hours=1, max_detections=2, download_audio=False, enrich_weather=False
        )

        assert yielded_ids == ["det0", "det1"]
        assert stats.detections_found == 0
        assert stats.detections_scanned == 2
        with open(detections_file, encoding="utf-8") as f:
            assert [json.loads(line)["id"] for line in f] == ["det0", "det1"]

    def test_main_applies_max_detections_per_day_across_date_range(self, tmp_path):
        """Test date-range mode applies max_detections independently to each day."""
        stats_day_1 = CollectionStats(detections_found=2, detections_scanned=2)
        stats_day_2 = CollectionStats(detections_found=1, detections_scanned=1)
        stats_day_3 = CollectionStats(detections_found=3, detections_scanned=3)

        mock_collector = MagicMock()
        mock_collector.collect.side_effect = [stats_day_1, stats_day_2, stats_day_3]

        argv = [
            "birdweather",
            "--start-date",
            "2025-01-01",
            "--end-date",
            "2025-01-03",
            "--max-detections",
            "3",
            "--output-dir",
            str(tmp_path),
            "--no-audio",
            "--no-enrich-weather",
        ]

        with (
            patch.object(sys, "argv", argv),
            patch(
                "avian_biosurveillance.ingestion.birdweather.AudioCollector",
                return_value=mock_collector,
            ),
        ):
            main()

        assert mock_collector.collect.call_count == 3
        first_call = mock_collector.collect.call_args_list[0].kwargs
        second_call = mock_collector.collect.call_args_list[1].kwargs
        third_call = mock_collector.collect.call_args_list[2].kwargs
        assert first_call["max_detections"] == 3
        assert second_call["max_detections"] == 3
        assert third_call["max_detections"] == 3

    def test_main_defaults_to_target_species_when_not_specified(self, tmp_path):
        """Test CLI defaults to configured target BirdWeather species."""
        mock_collector = MagicMock()
        default_species_ids = list(species_registry.birdweather_ids)

        argv = [
            "birdweather",
            "--hours",
            "24",
            "--output-dir",
            str(tmp_path),
            "--no-audio",
            "--no-enrich-weather",
        ]

        with (
            patch.object(sys, "argv", argv),
            patch(
                "avian_biosurveillance.ingestion.birdweather.AudioCollector",
                return_value=mock_collector,
            ),
        ):
            main()

        collect_call = mock_collector.collect.call_args.kwargs
        assert collect_call["species_ids"] == default_species_ids

    def test_main_preserves_explicit_species_ids(self, tmp_path):
        """Test explicit CLI species filters override configured defaults."""
        mock_collector = MagicMock()
        argv = [
            "birdweather",
            "--hours",
            "24",
            "--species-id",
            "35",
            "--species-id",
            "61",
            "--output-dir",
            str(tmp_path),
            "--no-audio",
            "--no-enrich-weather",
        ]

        with (
            patch.object(sys, "argv", argv),
            patch(
                "avian_biosurveillance.ingestion.birdweather.AudioCollector",
                return_value=mock_collector,
            ),
        ):
            main()

        collect_call = mock_collector.collect.call_args.kwargs
        assert collect_call["species_ids"] == ["35", "61"]
