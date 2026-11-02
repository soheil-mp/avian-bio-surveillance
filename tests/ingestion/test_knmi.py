"""
Test Suite for KNMI Data Ingestion Module

This module contains comprehensive tests for all KNMI API clients and the
ingestion manager. Tests use mocking to avoid actual API calls.

Run tests with: pytest tests/ingestion/test_knmi.py -v
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import json

import pytest
import requests

# Import the module under test
from avian_biosurveillance.ingestion.knmi import (
    DataFormat,
    # Data Models
    Dataset,
    DatasetFile,
    EDRAPIClient,
    EDRCollection,
    FDSNDataSelectClient,
    FDSNEventClient,
    FDSNStationClient,
    # Configuration
    KNMIConfig,
    # Main Manager
    KNMIIngestionManager,
    Location,
    NotificationServiceClient,
    # API Clients
    OpenDataAPIClient,
    SeismicEvent,
    ShakemapsClient,
    WMSAPIClient,
    # Convenience Functions
    create_ingestion_manager,
    download_latest_observations,
    get_recent_earthquakes,
    query_weather_at_location,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_data_dir(tmp_path):
    """Create a temporary data directory for tests."""
    data_dir = tmp_path / "data" / "raw" / "knmi"
    data_dir.mkdir(parents=True)
    return data_dir


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set mock environment variables for API keys."""
    monkeypatch.setenv("KNMI_OPEN_DATA_API", "test_open_data_key")
    monkeypatch.setenv("KNMI_EDR", "test_edr_key")
    monkeypatch.setenv("KNMI_NOTIFICATION", "test_notification_key")
    monkeypatch.setenv("KNMI_WEB_MAP_SERVICE", "test_wms_key")


@pytest.fixture
def config(temp_data_dir, mock_env_vars):
    """Create a test configuration."""
    cfg = KNMIConfig()
    cfg.data_dir = temp_data_dir
    return cfg


@pytest.fixture
def sample_dataset():
    """Create a sample dataset for testing."""
    return Dataset(
        name="10-minute-in-situ-meteorological-observations",
        version="1.0",
        description="Test dataset",
    )


@pytest.fixture
def sample_files_response():
    """Sample API response for file listing."""
    return {
        "files": [
            {
                "filename": "KMDS__OPER_P___10M_OBS_L2_202401011200.nc",
                "size": 1234567,
                "lastModified": "2024-01-01T12:05:00Z",
                "created": "2024-01-01T12:00:00Z",
            },
            {
                "filename": "KMDS__OPER_P___10M_OBS_L2_202401011210.nc",
                "size": 1234568,
                "lastModified": "2024-01-01T12:15:00Z",
                "created": "2024-01-01T12:10:00Z",
            },
        ],
        "nextPageToken": None,
    }


@pytest.fixture
def sample_locations_response():
    """Sample GeoJSON response for locations."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "0-20000-0-06260",
                "geometry": {"type": "Point", "coordinates": [4.7733, 52.3017]},
                "properties": {
                    "name": "Schiphol Airport",
                    "height_above_mean_sea_level": -3.2,
                    "station_type": "automatic",
                },
            },
            {
                "type": "Feature",
                "id": "0-20000-0-06344",
                "geometry": {"type": "Point", "coordinates": [4.9167, 52.3833]},
                "properties": {
                    "name": "Amsterdam",
                    "height_above_mean_sea_level": 1.4,
                    "station_type": "automatic",
                },
            },
        ],
    }


@pytest.fixture
def sample_coverage_json():
    """Sample CoverageJSON response."""
    return {
        "type": "Coverage",
        "domain": {
            "type": "Domain",
            "domainType": "PointSeries",
            "axes": {
                "x": {"values": [4.7733]},
                "y": {"values": [52.3017]},
                "t": {
                    "values": [
                        "2024-01-01T12:00:00Z",
                        "2024-01-01T12:10:00Z",
                        "2024-01-01T12:20:00Z",
                    ]
                },
            },
        },
        "ranges": {
            "ta": {"type": "NdArray", "dataType": "float", "values": [5.2, 5.3, 5.1]},
            "rh": {
                "type": "NdArray",
                "dataType": "float",
                "values": [85.0, 84.5, 86.0],
            },
        },
    }


@pytest.fixture
def sample_events_text():
    """Sample FDSN event text response."""
    return """#EventID|Time|Latitude|Longitude|Depth/km|Author|Catalog|Contributor|ContributorID|MagType|Magnitude|MagAuthor|EventLocationName
knmi2024abcd|2024-01-15T10:30:00.000Z|52.5|4.5|3.0|KNMI||KNMI||ML|2.5|KNMI|Netherlands
knmi2024efgh|2024-01-10T08:15:00.000Z|53.0|6.5|2.5|KNMI||KNMI||ML|1.8|KNMI|Groningen
"""


@pytest.fixture
def sample_shakemaps_list():
    """Sample shakemaps event list."""
    return [
        {"eventid": "knmi2024abcd", "time": "2024-01-15T10:30:00Z", "magnitude": 2.5},
        {"eventid": "knmi2024efgh", "time": "2024-01-10T08:15:00Z", "magnitude": 1.8},
    ]


# ============================================================================
# Configuration Tests
# ============================================================================


class TestKNMIConfig:
    """Tests for KNMIConfig class."""

    def test_config_loads_env_vars(self, mock_env_vars):
        """Test that config loads API keys from environment."""
        config = KNMIConfig()
        assert config.open_data_api_key == "test_open_data_key"
        assert config.edr_api_key == "test_edr_key"
        assert config.notification_api_key == "test_notification_key"
        assert config.wms_api_key == "test_wms_key"

    def test_config_default_urls(self, mock_env_vars):
        """Test default API URLs are set correctly."""
        config = KNMIConfig()
        assert "api.dataplatform.knmi.nl" in config.open_data_base_url
        assert "api.dataplatform.knmi.nl/edr" in config.edr_base_url
        assert config.notification_broker == "mqtt.dataplatform.knmi.nl"
        assert config.notification_port == 443

    def test_config_creates_data_dir(self, temp_data_dir, mock_env_vars):
        """Test that config creates data directory."""
        config = KNMIConfig()
        config.data_dir = temp_data_dir / "new_subdir"
        config.__post_init__()
        assert config.data_dir.exists()

    def test_get_subdir_creates_nested_dirs(self, config):
        """Test that get_subdir creates nested directories."""
        subdir = config.get_subdir("level1", "level2", "level3")
        assert subdir.exists()
        assert subdir == config.data_dir / "level1" / "level2" / "level3"


# ============================================================================
# Data Model Tests
# ============================================================================


class TestDataModels:
    """Tests for data model classes."""

    def test_dataset_file_from_api_response(self):
        """Test DatasetFile creation from API response."""
        response = {
            "filename": "test_file.nc",
            "size": 12345,
            "lastModified": "2024-01-01T12:00:00Z",
            "created": "2024-01-01T11:00:00Z",
        }

        file = DatasetFile.from_api_response(response)

        assert file.filename == "test_file.nc"
        assert file.size == 12345
        assert file.last_modified is not None
        assert file.created is not None

    def test_dataset_file_handles_missing_dates(self):
        """Test DatasetFile handles missing date fields."""
        response = {"filename": "test_file.nc", "size": 12345}

        file = DatasetFile.from_api_response(response)

        assert file.filename == "test_file.nc"
        assert file.last_modified is None
        assert file.created is None

    def test_dataset_full_name(self, sample_dataset):
        """Test Dataset full_name property."""
        assert (
            sample_dataset.full_name
            == "10-minute-in-situ-meteorological-observations/1.0"
        )

    def test_location_creation(self):
        """Test Location creation."""
        loc = Location(
            id="test-001",
            name="Test Station",
            latitude=52.37,
            longitude=4.89,
            elevation=10.0,
            properties={"type": "automatic"},
        )

        assert loc.id == "test-001"
        assert loc.latitude == 52.37
        assert loc.properties["type"] == "automatic"

    def test_seismic_event_creation(self):
        """Test SeismicEvent creation."""
        event = SeismicEvent(
            event_id="knmi2024test",
            time=datetime(2024, 1, 15, 10, 30, tzinfo=UTC),
            latitude=52.5,
            longitude=4.5,
            depth=3.0,
            magnitude=2.5,
            magnitude_type="ML",
            description="Test earthquake",
        )

        assert event.event_id == "knmi2024test"
        assert event.magnitude == 2.5
        assert event.magnitude_type == "ML"

    def test_data_format_enum(self):
        """Test DataFormat enum values."""
        assert DataFormat.JSON.value == "json"
        assert DataFormat.NETCDF.value == "nc"
        assert DataFormat.MINISEED.value == "mseed"


# ============================================================================
# Open Data API Client Tests
# ============================================================================


class TestOpenDataAPIClient:
    """Tests for OpenDataAPIClient."""

    def test_headers_include_api_key(self, config):
        """Test that headers include authorization."""
        client = OpenDataAPIClient(config)
        headers = client._get_headers()
        assert "Authorization" in headers
        assert headers["Authorization"] == config.open_data_api_key

    @patch.object(requests.Session, "request")
    def test_list_files(
        self, mock_request, config, sample_dataset, sample_files_response
    ):
        """Test listing files from a dataset."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_files_response
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = OpenDataAPIClient(config)
        files, next_token = client.list_files(sample_dataset, max_keys=10)

        assert len(files) == 2
        assert files[0].filename == "KMDS__OPER_P___10M_OBS_L2_202401011200.nc"
        assert next_token is None

        # Verify request was made correctly
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert "files" in call_args.kwargs["url"]

    @patch.object(requests.Session, "request")
    def test_list_files_with_pagination(self, mock_request, config, sample_dataset):
        """Test file listing with pagination."""
        response_with_token = {
            "files": [{"filename": "file1.nc", "size": 100}],
            "nextPageToken": "abc123",
        }
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_with_token
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = OpenDataAPIClient(config)
        files, next_token = client.list_files(sample_dataset)

        assert next_token == "abc123"

    @patch.object(requests.Session, "request")
    def test_get_file_url(self, mock_request, config, sample_dataset):
        """Test getting temporary download URL."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "temporaryDownloadUrl": "https://storage.example.com/file.nc?token=xyz"
        }
        mock_response.headers = {}
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = OpenDataAPIClient(config)
        url = client.get_file_url(sample_dataset, "test_file.nc")

        assert url is not None
        assert "storage" in url

    @patch.object(requests.Session, "request")
    def test_get_file_url_logs_deprecation(self, mock_request, config, sample_dataset):
        """Test that deprecation warnings are logged."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "temporaryDownloadUrl": "https://example.com/file"
        }
        mock_response.headers = {
            "X-KNMI-Deprecation": "Dataset will be deprecated on 2025-01-01"
        }
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = OpenDataAPIClient(config)

        with patch("avian_biosurveillance.ingestion.knmi.logger") as mock_logger:
            client.get_file_url(sample_dataset, "test_file.nc")
            mock_logger.warning.assert_called()

    @patch.object(requests.Session, "get")
    @patch.object(OpenDataAPIClient, "get_file_url")
    def test_download_file(self, mock_get_url, mock_get, config, sample_dataset):
        """Test downloading a file."""
        mock_get_url.return_value = "https://storage.example.com/file.nc"

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.iter_content = Mock(return_value=[b"test data"])
        mock_response.raise_for_status = Mock()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_get.return_value = mock_response

        client = OpenDataAPIClient(config)
        filepath = client.download_file(sample_dataset, "test_file.nc")

        assert filepath is not None
        assert "test_file.nc" in str(filepath)

    @patch.object(OpenDataAPIClient, "list_files")
    @patch.object(OpenDataAPIClient, "download_file")
    def test_download_latest_files(
        self, mock_download, mock_list, config, sample_dataset
    ):
        """Test downloading latest files."""
        mock_list.return_value = (
            [
                DatasetFile(filename="file1.nc", size=100),
                DatasetFile(filename="file2.nc", size=200),
            ],
            None,
        )
        mock_download.side_effect = [
            config.data_dir / "file1.nc",
            config.data_dir / "file2.nc",
        ]

        client = OpenDataAPIClient(config)
        paths = client.download_latest_files(sample_dataset, count=2)

        assert len(paths) == 2
        mock_list.assert_called_once_with(sample_dataset, max_keys=2, sorting="desc")


# ============================================================================
# EDR API Client Tests
# ============================================================================


class TestEDRAPIClient:
    """Tests for EDRAPIClient."""

    def test_headers_include_api_key(self, config):
        """Test that headers include authorization."""
        client = EDRAPIClient(config)
        headers = client._get_headers()
        assert headers["Authorization"] == config.edr_api_key

    def test_edr_collection_enum_values(self):
        """Test EDRCollection enum has expected values."""
        assert (
            EDRCollection.TEN_MIN_OBSERVATIONS.value
            == "10-minute-in-situ-meteorological-observations"
        )
        assert EDRCollection.PRECIPITATION.value == "Rd1"
        assert EDRCollection.EVAPORATION.value == "EV24"
        assert EDRCollection.TEMP_MEAN.value == "Tg1"

    @patch.object(requests.Session, "request")
    def test_get_collections(self, mock_request, config):
        """Test getting available collections."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "collections": [
                {"id": "collection1", "title": "Test Collection 1"},
                {"id": "collection2", "title": "Test Collection 2"},
            ]
        }
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = EDRAPIClient(config)
        collections = client.get_collections()

        assert len(collections) == 2

        # Verify metadata was saved
        metadata_file = config.data_dir / "edr" / "metadata" / "collections.json"
        assert metadata_file.exists()

    @patch.object(requests.Session, "request")
    def test_get_locations(self, mock_request, config, sample_locations_response):
        """Test getting measurement locations."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_locations_response
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = EDRAPIClient(config)
        locations = client.get_locations(EDRCollection.TEN_MIN_OBSERVATIONS)

        assert len(locations) == 2
        assert locations[0].id == "0-20000-0-06260"
        assert locations[0].name == "Schiphol Airport"
        assert locations[0].latitude == 52.3017
        assert locations[0].longitude == 4.7733
        assert locations[0].elevation == -3.2

    @patch.object(requests.Session, "request")
    def test_get_locations_falls_back_to_cached_geojson(
        self,
        mock_request,
        config,
        sample_locations_response,
    ):
        """Test cached station list fallback when the locations endpoint fails."""
        cache_path = (
            config.data_dir
            / "edr"
            / EDRCollection.TEN_MIN_OBSERVATIONS.value
            / "locations.geojson"
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(sample_locations_response), encoding="utf-8")

        mock_request.side_effect = requests.exceptions.HTTPError("403 forbidden")

        client = EDRAPIClient(config)
        locations = client.get_locations(EDRCollection.TEN_MIN_OBSERVATIONS)

        assert len(locations) == 2
        assert locations[0].id == "0-20000-0-06260"
        assert locations[1].id == sample_locations_response["features"][1]["id"]

    def test_coverage_to_observations_keeps_station_metadata(
        self,
        config,
        sample_coverage_json,
    ):
        """Station metadata should survive into unified KNMI records."""
        manager = KNMIIngestionManager(config)

        record = manager._coverage_to_observations(
            sample_coverage_json,
            "0-20000-0-06260",
            "Schiphol Airport",
            -3.2,
        )

        assert record["station_id"] == "0-20000-0-06260"
        assert record["station_name"] == "Schiphol Airport"
        assert record["elevation_m"] == -3.2

    @patch("avian_biosurveillance.db.loaders.upsert_knmi_observations")
    @patch("avian_biosurveillance.db.models.KNMIWeatherObservation.from_edr_coverage")
    def test_persist_edr_to_db_preserves_station_metadata(
        self,
        mock_from_edr_coverage,
        mock_upsert_knmi_observations,
        config,
        sample_coverage_json,
    ):
        """DB persistence should pass station names/elevation to the model."""
        mock_from_edr_coverage.return_value = []

        manager = KNMIIngestionManager(config)
        manager._persist_edr_to_db(
            {
                "edr_coverages": [
                    {
                        "location_id": "0-20000-0-06260",
                        "location_name": "Schiphol Airport",
                        "location_elevation_m": -3.2,
                        "coverage": sample_coverage_json,
                    }
                ]
            }
        )

        mock_from_edr_coverage.assert_called_once_with(
            sample_coverage_json,
            "0-20000-0-06260",
            "Schiphol Airport",
            -3.2,
        )
        mock_upsert_knmi_observations.assert_not_called()

    @patch.object(requests.Session, "request")
    def test_query_location(self, mock_request, config, sample_coverage_json):
        """Test querying data at a location."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_coverage_json
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = EDRAPIClient(config)
        data = client.query_location(
            EDRCollection.TEN_MIN_OBSERVATIONS,
            "0-20000-0-06260",
            datetime_range="2024-01-01T00:00:00Z/2024-01-01T12:00:00Z",
            parameters=["ta", "rh"],
        )

        assert data["type"] == "Coverage"
        assert "ta" in data["ranges"]

        # Verify query params
        call_args = mock_request.call_args
        assert "datetime" in call_args.kwargs.get("params", {})
        assert "parameter-name" in call_args.kwargs.get("params", {})

    @patch.object(requests.Session, "request")
    def test_query_position(self, mock_request, config, sample_coverage_json):
        """Test querying data at a coordinate position."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_coverage_json
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = EDRAPIClient(config)
        data = client.query_position(
            EDRCollection.TEN_MIN_OBSERVATIONS,
            coords=(4.89, 52.37),  # (lon, lat)
            datetime_range="2024-01-01T00:00:00Z/2024-01-01T12:00:00Z",
        )

        # Verify POINT coords in params
        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert "coords" in params
        assert "POINT" in params["coords"]

    @patch.object(requests.Session, "request")
    def test_query_area(self, mock_request, config, sample_coverage_json):
        """Test querying data in a polygon area."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_coverage_json
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = EDRAPIClient(config)
        polygon = [
            (3.37, 50.75),
            (7.21, 50.75),
            (7.21, 53.47),
            (3.37, 53.47),
            (3.37, 50.75),
        ]

        data = client.query_area(EDRCollection.PRECIPITATION, polygon_coords=polygon)

        # Verify POLYGON coords in params
        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert "POLYGON" in params["coords"]


# ============================================================================
# WMS API Client Tests
# ============================================================================


class TestWMSAPIClient:
    """Tests for WMSAPIClient."""

    def test_headers_include_api_key(self, config):
        """Test that headers include authorization."""
        client = WMSAPIClient(config)
        headers = client._get_headers()
        assert headers["Authorization"] == config.wms_api_key

    @patch.object(requests.Session, "request")
    def test_get_capabilities(self, mock_request, config):
        """Test getting WMS capabilities."""
        capabilities_xml = """<?xml version="1.0"?>
        <WMS_Capabilities version="1.3.0">
            <Service><Name>WMS</Name></Service>
        </WMS_Capabilities>"""

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = capabilities_xml
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = WMSAPIClient(config)
        result = client.get_capabilities("https://wms.example.com/wms")

        assert "WMS_Capabilities" in result

        # Verify request params
        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert params["SERVICE"] == "WMS"
        assert params["REQUEST"] == "GetCapabilities"

    @patch.object(requests.Session, "request")
    def test_get_map(self, mock_request, config):
        """Test getting a map image."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.iter_content = Mock(
            return_value=[b"\x89PNG\r\n\x1a\n"]
        )  # PNG header
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = WMSAPIClient(config)
        filepath = client.get_map(
            service_url="https://wms.example.com/wms",
            layers=["radar"],
            bbox=(3.37, 50.75, 7.21, 53.47),
            width=800,
            height=600,
        )

        assert filepath.exists()
        assert filepath.suffix == ".png"

        # Verify request params
        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert params["REQUEST"] == "GetMap"
        assert params["LAYERS"] == "radar"
        assert params["WIDTH"] == 800

    @patch.object(requests.Session, "request")
    def test_get_map_with_time(self, mock_request, config):
        """Test getting a map image with time parameter."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.iter_content = Mock(return_value=[b"image data"])
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = WMSAPIClient(config)
        client.get_map(
            service_url="https://wms.example.com/wms",
            layers=["radar"],
            bbox=(3.37, 50.75, 7.21, 53.47),
            time="2024-01-15T12:00:00Z",
        )

        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert params["TIME"] == "2024-01-15T12:00:00Z"

    @patch.object(requests.Session, "request")
    def test_get_feature_info(self, mock_request, config):
        """Test getting feature info."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"features": [{"properties": {"value": 42}}]}
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = WMSAPIClient(config)
        result = client.get_feature_info(
            service_url="https://wms.example.com/wms",
            layers=["radar"],
            bbox=(3.37, 50.75, 7.21, 53.47),
            x=400,
            y=300,
        )

        assert "features" in result

        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert params["REQUEST"] == "GetFeatureInfo"
        assert params["I"] == 400
        assert params["J"] == 300


# ============================================================================
# FDSN API Client Tests
# ============================================================================


class TestFDSNDataSelectClient:
    """Tests for FDSNDataSelectClient."""

    def test_headers_empty_for_keyless_api(self, config):
        """Test that headers are empty (keyless API)."""
        client = FDSNDataSelectClient(config)
        headers = client._get_headers()
        assert headers == {}

    def test_base_url(self, config):
        """Test base URL is correct."""
        client = FDSNDataSelectClient(config)
        assert "fdsnws/dataselect/1" in client.base_url

    @patch.object(requests.Session, "request")
    def test_query(self, mock_request, config):
        """Test querying waveform data."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.iter_content = Mock(return_value=[b"miniseed data"])
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = FDSNDataSelectClient(config)
        filepath = client.query(
            network="NL",
            station="VKB",
            channel="BHZ",
            starttime=datetime(2024, 1, 1),
            endtime=datetime(2024, 1, 1, 1, 0),
        )

        assert filepath.exists()
        assert filepath.suffix == ".mseed"

        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert params["network"] == "NL"
        assert params["station"] == "VKB"


class TestFDSNStationClient:
    """Tests for FDSNStationClient."""

    def test_headers_empty_for_keyless_api(self, config):
        """Test that headers are empty (keyless API)."""
        client = FDSNStationClient(config)
        headers = client._get_headers()
        assert headers == {}

    @patch.object(requests.Session, "request")
    def test_query_json_format(self, mock_request, config):
        """Test querying stations in JSON format."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"networks": [{"code": "NL", "stations": []}]}
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = FDSNStationClient(config)
        result = client.query(network="NL", format="json")

        assert "networks" in result

    @patch.object(requests.Session, "request")
    def test_query_text_format(self, mock_request, config):
        """Test querying stations in text format."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "NL|Station|52.0|5.0|0.0|Test Station"
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = FDSNStationClient(config)
        result = client.query(network="NL", format="text")

        assert "NL" in result

    @patch.object(FDSNStationClient, "query")
    def test_get_all_stations(self, mock_query, config):
        """Test convenience method for getting all stations."""
        mock_query.return_value = {"networks": []}

        client = FDSNStationClient(config)
        client.get_all_stations(network="NL")

        mock_query.assert_called_once_with(network="NL", level="station", format="json")


class TestFDSNEventClient:
    """Tests for FDSNEventClient."""

    def test_headers_empty_for_keyless_api(self, config):
        """Test that headers are empty (keyless API)."""
        client = FDSNEventClient(config)
        headers = client._get_headers()
        assert headers == {}

    @patch.object(requests.Session, "request")
    def test_query_text_format_parses_events(
        self, mock_request, config, sample_events_text
    ):
        """Test parsing events from text format."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = sample_events_text
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = FDSNEventClient(config)
        events = client.query(format="text")

        assert len(events) == 2
        assert events[0].event_id == "knmi2024abcd"
        assert events[0].magnitude == 2.5
        assert events[1].event_id == "knmi2024efgh"

    @patch.object(requests.Session, "request")
    def test_query_with_filters(self, mock_request, config, sample_events_text):
        """Test query with various filters."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = sample_events_text
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = FDSNEventClient(config)
        client.query(
            starttime=datetime(2024, 1, 1),
            endtime=datetime(2024, 1, 31),
            minmagnitude=1.5,
            maxmagnitude=5.0,
            minlatitude=50.0,
            maxlatitude=54.0,
            limit=100,
            format="text",
        )

        call_args = mock_request.call_args
        params = call_args.kwargs.get("params", {})
        assert params["minmagnitude"] == 1.5
        assert params["maxmagnitude"] == 5.0
        assert params["limit"] == 100

    @patch.object(FDSNEventClient, "query")
    def test_get_recent_events(self, mock_query, config):
        """Test convenience method for recent events."""
        mock_query.return_value = []

        client = FDSNEventClient(config)
        client.get_recent_events(days=30, min_magnitude=2.0)

        mock_query.assert_called_once()
        call_kwargs = mock_query.call_args.kwargs
        assert call_kwargs["minmagnitude"] == 2.0
        assert call_kwargs["format"] == "text"


# ============================================================================
# Shakemaps Client Tests
# ============================================================================


class TestShakemapsClient:
    """Tests for ShakemapsClient."""

    def test_headers_empty_for_keyless_api(self, config):
        """Test that headers are empty (keyless API)."""
        client = ShakemapsClient(config)
        headers = client._get_headers()
        assert headers == {}

    @patch.object(requests.Session, "request")
    def test_list_events(self, mock_request, config, sample_shakemaps_list):
        """Test listing shakemap events."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_shakemaps_list
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = ShakemapsClient(config)
        events = client.list_events()

        assert len(events) == 2
        assert events[0]["eventid"] == "knmi2024abcd"

    @patch.object(requests.Session, "request")
    def test_get_event_files(self, mock_request, config):
        """Test getting files for an event."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "files": ["grid.nc", "event.xml", "rrsm.json", "rrsm.csv"]
        }
        mock_response.raise_for_status = Mock()
        mock_request.return_value = mock_response

        client = ShakemapsClient(config)
        files = client.get_event_files("knmi2024abcd")

        assert "grid.nc" in files
        assert "event.xml" in files

    @patch.object(requests.Session, "get")
    def test_download_file(self, mock_get, config):
        """Test downloading a shakemap file."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.iter_content = Mock(return_value=[b"netcdf data"])
        mock_response.raise_for_status = Mock()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_get.return_value = mock_response

        client = ShakemapsClient(config)
        filepath = client.download_file("knmi2024abcd", "grid.nc")

        assert filepath.exists()
        assert "knmi2024abcd" in str(filepath)
        assert filepath.name == "grid.nc"


# ============================================================================
# Notification Service Client Tests
# ============================================================================


class TestNotificationServiceClient:
    """Tests for NotificationServiceClient."""

    def test_client_initialization(self, config):
        """Test client initialization."""
        client = NotificationServiceClient(config)
        assert client.config == config
        assert client.client is None
        assert client._message_callback is None

    def test_client_id_is_uuid(self, config):
        """Test that client ID is a valid UUID."""
        import uuid

        client = NotificationServiceClient(config)
        # Should not raise
        uuid.UUID(client.client_id)

    @patch(
        "avian_biosurveillance.ingestion.knmi.NotificationServiceClient._ensure_paho"
    )
    def test_connect_creates_mqtt_client(self, mock_ensure, config):
        """Test that connect creates an MQTT client."""
        mock_mqtt = Mock()
        mock_mqtt.MQTTv5 = 5
        mock_mqtt.CallbackAPIVersion.VERSION2 = 2
        mock_mqtt.Client.return_value = Mock()

        mock_properties = Mock()
        mock_properties.Properties.return_value = Mock()
        mock_properties.PacketTypes.CONNECT = 1

        mock_ensure.return_value = (mock_mqtt, mock_properties)

        client = NotificationServiceClient(config)
        client.connect()

        mock_mqtt.Client.assert_called_once()

    def test_subscribe_raises_without_connection(self, config):
        """Test that subscribe raises error if not connected."""
        client = NotificationServiceClient(config)

        with pytest.raises(RuntimeError, match="Not connected"):
            client.subscribe("test/topic")

    def test_start_listening_raises_without_connection(self, config):
        """Test that start_listening raises error if not connected."""
        client = NotificationServiceClient(config)

        with pytest.raises(RuntimeError, match="Not connected"):
            client.start_listening()


# ============================================================================
# Ingestion Manager Tests
# ============================================================================


class TestKNMIIngestionManager:
    """Tests for KNMIIngestionManager."""

    def test_manager_initialization(self, config):
        """Test manager initializes all clients."""
        manager = KNMIIngestionManager(config)

        assert isinstance(manager.open_data, OpenDataAPIClient)
        assert isinstance(manager.edr, EDRAPIClient)
        assert isinstance(manager.wms, WMSAPIClient)
        assert isinstance(manager.fdsn_dataselect, FDSNDataSelectClient)
        assert isinstance(manager.fdsn_station, FDSNStationClient)
        assert isinstance(manager.fdsn_event, FDSNEventClient)
        assert isinstance(manager.shakemaps, ShakemapsClient)

    def test_notifications_lazy_loading(self, config):
        """Test notification client is lazy-loaded."""
        manager = KNMIIngestionManager(config)

        # Should not be initialized yet
        assert manager._notification_client is None

        # Access triggers initialization
        notifications = manager.notifications
        assert notifications is not None
        assert manager._notification_client is notifications

    @patch.object(OpenDataAPIClient, "download_latest_files")
    @patch.object(EDRAPIClient, "get_locations")
    @patch.object(EDRAPIClient, "query_location")
    def test_ingest_meteorological_observations(
        self, mock_query, mock_locations, mock_download, config
    ):
        """Test meteorological data ingestion."""
        mock_download.return_value = [config.data_dir / "file1.nc"]
        mock_locations.return_value = [
            Location(id="loc1", name="Station 1", latitude=52.0, longitude=4.0)
        ]
        mock_query.return_value = {"domain": {"axes": {"t": {"values": [1, 2, 3]}}}}

        manager = KNMIIngestionManager(config)
        results = manager.ingest_meteorological_observations(hours_back=24)

        assert "open_data_files" in results
        assert "edr_data" in results
        mock_download.assert_called_once()

    @patch.object(FDSNStationClient, "get_all_stations")
    @patch.object(FDSNEventClient, "get_recent_events")
    @patch.object(ShakemapsClient, "list_events")
    def test_ingest_seismological_data(
        self, mock_shakemaps, mock_events, mock_stations, config
    ):
        """Test seismological data ingestion."""
        mock_stations.return_value = {"networks": []}
        mock_events.return_value = []
        mock_shakemaps.return_value = []

        manager = KNMIIngestionManager(config)
        results = manager.ingest_seismological_data(
            days_back=30, include_waveforms=False
        )

        assert "stations" in results
        assert "events" in results
        assert "shakemaps" in results

    @patch.object(EDRAPIClient, "get_collection_metadata")
    @patch.object(EDRAPIClient, "query_area")
    def test_ingest_gridded_data(self, mock_query, mock_metadata, config):
        """Test gridded data ingestion."""
        mock_metadata.return_value = {"title": "Test Collection"}
        mock_query.return_value = {"type": "Coverage"}

        manager = KNMIIngestionManager(config)
        results = manager.ingest_gridded_data(
            collections=[EDRCollection.TEMP_MEAN], days_back=7
        )

        assert "collections" in results
        assert EDRCollection.TEMP_MEAN.value in results["collections"]

    @patch.object(KNMIIngestionManager, "ingest_meteorological_observations")
    @patch.object(KNMIIngestionManager, "ingest_seismological_data")
    @patch.object(KNMIIngestionManager, "ingest_gridded_data")
    def test_full_ingestion(self, mock_gridded, mock_seismo, mock_meteo, config):
        """Test full data ingestion."""
        mock_meteo.return_value = {"status": "ok"}
        mock_seismo.return_value = {"status": "ok"}
        mock_gridded.return_value = {"status": "ok"}

        manager = KNMIIngestionManager(config)
        results = manager.full_ingestion(
            include_seismological=True, include_gridded=True, days_back=7
        )

        assert "timestamp" in results
        assert "meteorological" in results
        assert "seismological" in results
        assert "gridded" in results

        # Verify summary file was saved
        summary_file = config.data_dir / "metadata" / "ingestion_summary.json"
        assert summary_file.exists()


# ============================================================================
# Convenience Function Tests
# ============================================================================


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @patch.object(KNMIIngestionManager, "__init__", return_value=None)
    def test_create_ingestion_manager(self, mock_init, tmp_path):
        """Test creating ingestion manager with custom data dir."""
        _ = create_ingestion_manager(data_dir=str(tmp_path / "knmi"))

    @patch("avian_biosurveillance.ingestion.knmi.create_ingestion_manager")
    def test_download_latest_observations(self, mock_create):
        """Test download_latest_observations convenience function."""
        mock_manager = Mock()
        mock_manager.open_data.download_latest_files.return_value = [
            Path("mock-file.nc")
        ]
        mock_create.return_value = mock_manager

        result = download_latest_observations(count=5)

        mock_manager.open_data.download_latest_files.assert_called_once()

    @patch("avian_biosurveillance.ingestion.knmi.create_ingestion_manager")
    def test_get_recent_earthquakes(self, mock_create):
        """Test get_recent_earthquakes convenience function."""
        mock_manager = Mock()
        mock_manager.fdsn_event.get_recent_events.return_value = []
        mock_create.return_value = mock_manager

        result = get_recent_earthquakes(days=30, min_magnitude=2.0)

        mock_manager.fdsn_event.get_recent_events.assert_called_once_with(
            days=30, min_magnitude=2.0
        )

    @patch("avian_biosurveillance.ingestion.knmi.create_ingestion_manager")
    def test_query_weather_at_location(self, mock_create):
        """Test query_weather_at_location convenience function."""
        mock_manager = Mock()
        mock_manager.edr.query_position.return_value = {"type": "Coverage"}
        mock_create.return_value = mock_manager

        result = query_weather_at_location(
            latitude=52.37, longitude=4.89, hours_back=24, parameters=["ta", "rh"]
        )

        mock_manager.edr.query_position.assert_called_once()
        call_kwargs = mock_manager.edr.query_position.call_args.kwargs
        assert call_kwargs["coords"] == (4.89, 52.37)  # (lon, lat)
        assert call_kwargs["parameters"] == ["ta", "rh"]


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestErrorHandling:
    """Tests for error handling."""

    @patch.object(requests.Session, "request")
    def test_api_error_handling(self, mock_request, config):
        """Test handling of API errors."""
        mock_request.side_effect = requests.exceptions.RequestException(
            "Connection failed"
        )

        client = OpenDataAPIClient(config)

        with pytest.raises(requests.exceptions.RequestException):
            client.list_files(Dataset(name="test", version="1.0"))

    @patch.object(requests.Session, "request")
    def test_http_error_handling(self, mock_request, config):
        """Test handling of HTTP errors."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "Not Found"
        )
        mock_request.return_value = mock_response

        client = OpenDataAPIClient(config)

        with pytest.raises(requests.exceptions.HTTPError):
            client.list_files(Dataset(name="nonexistent", version="1.0"))

    def test_parse_invalid_event_text(self, config):
        """Test parsing invalid event text gracefully."""
        client = FDSNEventClient(config)

        invalid_text = """#Header
invalid|line|data
also|invalid"""

        events = client._parse_text_events(invalid_text)
        # Should return empty list or skip invalid lines
        assert isinstance(events, list)


# ============================================================================
# Integration-style Tests (with mocked external calls)
# ============================================================================


class TestIntegration:
    """Integration-style tests that test multiple components together."""

    @patch.object(requests.Session, "request")
    @patch.object(requests.Session, "get")
    def test_full_workflow_open_data(
        self, mock_get, mock_request, config, sample_dataset
    ):
        """Test complete workflow for Open Data API."""
        # Mock list files response
        list_response = Mock()
        list_response.status_code = 200
        list_response.json.return_value = {
            "files": [{"filename": "test.nc", "size": 1000}],
            "nextPageToken": None,
        }
        list_response.raise_for_status = Mock()

        # Mock get URL response
        url_response = Mock()
        url_response.status_code = 200
        url_response.json.return_value = {
            "temporaryDownloadUrl": "https://storage.example.com/test.nc"
        }
        url_response.headers = {}
        url_response.raise_for_status = Mock()

        mock_request.side_effect = [list_response, url_response]

        # Mock download
        download_response = Mock()
        download_response.status_code = 200
        download_response.iter_content = Mock(return_value=[b"data"])
        download_response.raise_for_status = Mock()
        download_response.__enter__ = Mock(return_value=download_response)
        download_response.__exit__ = Mock(return_value=False)
        mock_get.return_value = download_response

        # Execute workflow
        client = OpenDataAPIClient(config)
        files, _ = client.list_files(sample_dataset)
        assert len(files) == 1

        filepath = client.download_file(sample_dataset, files[0].filename)
        assert filepath.exists()

    @patch.object(requests.Session, "request")
    def test_edr_workflow(
        self, mock_request, config, sample_locations_response, sample_coverage_json
    ):
        """Test complete workflow for EDR API."""
        # First call: get locations
        locations_response = Mock()
        locations_response.status_code = 200
        locations_response.json.return_value = sample_locations_response
        locations_response.raise_for_status = Mock()

        # Second call: query location
        query_response = Mock()
        query_response.status_code = 200
        query_response.json.return_value = sample_coverage_json
        query_response.raise_for_status = Mock()

        mock_request.side_effect = [locations_response, query_response]

        # Execute workflow
        client = EDRAPIClient(config)
        locations = client.get_locations(EDRCollection.TEN_MIN_OBSERVATIONS)
        assert len(locations) == 2

        data = client.query_location(
            EDRCollection.TEN_MIN_OBSERVATIONS,
            locations[0].id,
            datetime_range="2024-01-01T00:00:00Z/2024-01-01T12:00:00Z",
        )
        assert data["type"] == "Coverage"


# ============================================================================
# Pytest Configuration
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
