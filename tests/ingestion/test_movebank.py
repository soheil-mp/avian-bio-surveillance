"""Tests for the Movebank Data Ingestion Module.

Tests cover:
- MovebankClient API methods, CSV parsing, retry, and license handling
- MovebankCollector orchestration (study events, metadata, target species)
- MovebankEvent data model (from_csv_row, to_dict)
- DB loader (upsert_movebank_events)
- CLI argument parsing
- Incremental sync, reduction profiles
"""

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from avian_biosurveillance.db.models.movebank import MovebankEvent
from avian_biosurveillance.ingestion.movebank import (
    BASE_URL,
    DEFAULT_EVENT_ATTRIBUTES,
    GPS_SENSOR_TYPE_ID,
    REDUCTION_PROFILES,
    CollectionStats,
    MovebankClient,
    MovebankCollector,
    _load_target_species,
    _mb_timestamp,
)


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_client():
    """Create a MovebankClient with mocked HTTP session."""
    with patch.dict(
        "os.environ",
        {
            "MOVEBANK_USERNAME": "testuser",
            "MOVEBANK_PASSWORD": "testpass",
        },
    ):
        client = MovebankClient(username="testuser", password="testpass")
    client._session = MagicMock()
    return client


@pytest.fixture
def sample_event_csv_row():
    """Sample Movebank event as a CSV-parsed row dict."""
    return {
        "event_id": "123456789",
        "study_id": "2911040",
        "timestamp": "2026-06-15 14:30:00.000",
        "individual_id": "4001",
        "individual_local_identifier": "Blackbird-NL-01",
        "individual_taxon_canonical_name": "Turdus merula",
        "tag_id": "5001",
        "tag_local_identifier": "TAG-001",
        "deployment_id": "6001",
        "sensor_type_id": "653",
        "location_lat": "52.3702",
        "location_long": "4.8952",
        "ground_speed": "2.5",
        "heading": "135.0",
        "height_above_ellipsoid": "15.3",
        "visible": "true",
    }


@pytest.fixture
def sample_event_csv_minimal():
    """Minimal event row with only required fields."""
    return {
        "event_id": "999",
        "timestamp": "2026-01-01 00:00:00.000",
    }


@pytest.fixture
def sample_study_row():
    """Sample study CSV row."""
    return {
        "id": "2911040",
        "name": "Blackbird migration Netherlands",
        "taxon_ids": "Turdus merula",
        "number_of_individuals": "25",
        "number_of_deployed_locations": "1500",
        "main_location_lat": "52.37",
        "main_location_long": "4.89",
        "principal_investigator_name": "Dr. Test",
        "study_objective": "Track Turdus merula movements in NL",
    }


@pytest.fixture
def sample_csv_text():
    """Raw CSV text as returned by Movebank API."""
    return (
        "event_id,timestamp,location_lat,location_long,individual_id,"
        "individual_local_identifier,visible\n"
        "100,2026-06-15 14:30:00.000,52.37,4.89,4001,Bird-01,true\n"
        "101,2026-06-15 15:30:00.000,52.38,4.90,4001,Bird-01,true\n"
        "102,2026-06-15 16:30:00.000,52.39,4.91,4002,Bird-02,false\n"
    )


@pytest.fixture
def collector(tmp_path):
    """Create a MovebankCollector with mocked client."""
    with patch.dict(
        "os.environ",
        {
            "MOVEBANK_USERNAME": "testuser",
            "MOVEBANK_PASSWORD": "testpass",
        },
    ):
        coll = MovebankCollector(
            output_dir=tmp_path, username="testuser", password="testpass"
        )
    coll.client = MagicMock(spec=MovebankClient)
    return coll


# =============================================================================
# MovebankClient Tests
# =============================================================================


class TestMovebankClient:
    """Tests for the MovebankClient HTTP wrapper."""

    def test_init_with_credentials(self):
        """Client initializes with explicit credentials."""
        client = MovebankClient(username="user", password="pass")
        assert client.username == "user"
        assert client.password == "pass"
        assert client.timeout == 180

    def test_init_from_env(self):
        """Client reads credentials from environment."""
        with patch.dict(
            "os.environ",
            {
                "MOVEBANK_USERNAME": "envuser",
                "MOVEBANK_PASSWORD": "envpass",
            },
        ):
            client = MovebankClient()
        assert client.username == "envuser"
        assert client.password == "envpass"

    def test_init_uses_timeout_from_env(self):
        """Client reads request timeout from environment."""
        with patch.dict(
            "os.environ",
            {
                "MOVEBANK_USERNAME": "envuser",
                "MOVEBANK_PASSWORD": "envpass",
                "MOVEBANK_REQUEST_TIMEOUT_SECONDS": "240",
            },
        ):
            client = MovebankClient()
        assert client.timeout == 240

    def test_init_timeout_env_falls_back_on_invalid_value(self):
        """Invalid timeout env values should fall back to the default."""
        with patch.dict(
            "os.environ",
            {
                "MOVEBANK_USERNAME": "envuser",
                "MOVEBANK_PASSWORD": "envpass",
                "MOVEBANK_REQUEST_TIMEOUT_SECONDS": "invalid",
            },
        ):
            client = MovebankClient()
        assert client.timeout == 180

    def test_init_no_credentials_raises(self):
        """Client raises ValueError when no credentials available."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="Movebank credentials required"):
                MovebankClient()

    def test_session_has_basic_auth(self):
        """Session is configured with HTTP Basic auth."""
        client = MovebankClient(username="user", password="pass")
        assert client._session.auth == ("user", "pass")

    def test_csv_parsing(self, sample_csv_text):
        """_parse_csv correctly parses CSV text."""
        rows = MovebankClient._parse_csv(sample_csv_text)
        assert len(rows) == 3
        assert rows[0]["event_id"] == "100"
        assert rows[0]["location_lat"] == "52.37"
        assert rows[2]["individual_local_identifier"] == "Bird-02"

    def test_list_studies(self, mock_client):
        """list_studies passes correct entity_type."""
        mock_resp = MagicMock()
        mock_resp.text = "id,name\n1,Study1\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.list_studies()

        call_args = mock_client._session.get.call_args
        params = call_args[1]["params"]
        assert params["entity_type"] == "study"
        assert len(result) == 1

    def test_list_studies_with_filters(self, mock_client):
        """list_studies passes download/visibility filters."""
        mock_resp = MagicMock()
        mock_resp.text = "id,name\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        mock_client.list_studies(
            i_have_download_access=True,
            i_can_see_data=True,
            attributes=["id", "name"],
        )

        params = mock_client._session.get.call_args[1]["params"]
        assert params["i_have_download_access"] == "true"
        assert params["i_can_see_data"] == "true"
        assert params["attributes"] == "id,name"

    def test_list_studies_falls_back_to_visible_studies_on_403(self, mock_client):
        """403 on strict download access falls back to viewable studies."""
        err_resp = MagicMock()
        err_resp.status_code = 403
        mock_client._get_csv = MagicMock(
            side_effect=[
                requests.HTTPError(response=err_resp),
                [{"id": "1", "name": "Visible Study"}],
            ]
        )

        result = mock_client.list_studies(
            i_have_download_access=True,
            attributes=["id", "name"],
        )

        assert result == [{"id": "1", "name": "Visible Study"}]
        assert mock_client._get_csv.call_count == 2
        first_params = mock_client._get_csv.call_args_list[0].args[0]
        second_params = mock_client._get_csv.call_args_list[1].args[0]
        assert first_params["i_have_download_access"] == "true"
        assert second_params["i_can_see_data"] == "true"
        assert "i_have_download_access" not in second_params

    def test_get_study(self, mock_client):
        """get_study passes study_id."""
        mock_resp = MagicMock()
        mock_resp.text = "id,name\n2911040,Test Study\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_study(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "study"
        assert params["study_id"] == 2911040
        assert result[0]["id"] == "2911040"

    def test_get_individuals(self, mock_client):
        """get_individuals fetches individual entity type."""
        mock_resp = MagicMock()
        mock_resp.text = "id,local_identifier\n4001,Bird-01\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_individuals(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "individual"
        assert params["study_id"] == 2911040
        assert len(result) == 1

    def test_get_tags(self, mock_client):
        """get_tags fetches tag entity type."""
        mock_resp = MagicMock()
        mock_resp.text = "id,local_identifier\n5001,TAG-001\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_tags(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "tag"

    def test_get_deployments(self, mock_client):
        """get_deployments fetches deployment entity type."""
        mock_resp = MagicMock()
        mock_resp.text = "id,individual_id\n6001,4001\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_deployments(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "deployment"

    def test_get_sensors(self, mock_client):
        """get_sensors fetches sensor entity type."""
        mock_resp = MagicMock()
        mock_resp.text = "id,sensor_type_id\n7001,653\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_sensors(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "sensor"
        assert params["tag_study_id"] == 2911040

    def test_get_study_attributes(self, mock_client):
        """get_study_attributes fetches study_attribute entity type."""
        mock_resp = MagicMock()
        mock_resp.text = "data_type,short_name\nstring,visible\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_study_attributes(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "study_attribute"
        assert params["sensor_type_id"] == GPS_SENSOR_TYPE_ID

    def test_get_events_default_params(self, mock_client):
        """get_events sends correct default parameters."""
        mock_resp = MagicMock()
        mock_resp.text = (
            "event_id,timestamp,location_lat,location_long\n"
            "100,2026-06-15 14:30:00.000,52.37,4.89\n"
        )
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_events(2911040)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "event"
        assert params["study_id"] == 2911040
        assert params["sensor_type_id"] == GPS_SENSOR_TYPE_ID
        assert "attributes" in params
        assert len(result) == 1

    def test_get_events_with_time_range(self, mock_client):
        """get_events passes timestamp_start and timestamp_end."""
        mock_resp = MagicMock()
        mock_resp.text = "event_id,timestamp\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        mock_client.get_events(
            2911040,
            timestamp_start=date(2026, 1, 1),
            timestamp_end=date(2026, 6, 30),
        )

        params = mock_client._session.get.call_args[1]["params"]
        assert params["timestamp_start"] == "20260101000000000"
        assert params["timestamp_end"] == "20260701000000000"

    def test_get_events_keeps_explicit_datetime_end_exact(self, mock_client):
        """Explicit datetime end bounds should not be shifted."""
        mock_resp = MagicMock()
        mock_resp.text = "event_id,timestamp\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        mock_client.get_events(
            2911040,
            timestamp_start=datetime(2026, 1, 1, 0, 0, 0),
            timestamp_end=datetime(2026, 6, 30, 12, 30, 0),
        )

        params = mock_client._session.get.call_args[1]["params"]
        assert params["timestamp_start"] == "20260101000000000"
        assert params["timestamp_end"] == "20260630123000000"

    def test_get_events_with_individual_filter(self, mock_client):
        """get_events filters by individual identifier (list value)."""
        mock_resp = MagicMock()
        mock_resp.text = "event_id,timestamp\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        mock_client.get_events(
            2911040,
            individual_local_identifiers=["Bird-01"],
        )

        params = mock_client._session.get.call_args[1]["params"]
        assert params["individual_local_identifier"] == ["Bird-01"]

    def test_get_events_with_multiple_individuals(self, mock_client):
        """get_events passes list of identifiers for multi-individual filter."""
        mock_resp = MagicMock()
        mock_resp.text = "event_id,timestamp\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        mock_client.get_events(
            2911040,
            individual_local_identifiers=["Bird-01", "Bird-02", "Bird-03"],
        )

        params = mock_client._session.get.call_args[1]["params"]
        assert params["individual_local_identifier"] == [
            "Bird-01",
            "Bird-02",
            "Bird-03",
        ]

    def test_get_events_with_reduction_profile(self, mock_client):
        """get_events passes event_reduction_profile."""
        mock_resp = MagicMock()
        mock_resp.text = "event_id,timestamp\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        mock_client.get_events(
            2911040,
            event_reduction_profile="EURING_01",
        )

        params = mock_client._session.get.call_args[1]["params"]
        assert params["event_reduction_profile"] == "EURING_01"

    def test_get_events_json(self, mock_client):
        """get_events_json hits json-auth endpoint."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"individuals": []}
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_events_json(2911040)

        url = mock_client._session.get.call_args[0][0]
        assert "json-auth" in url
        assert "individuals" in result

    def test_get_events_updated_since(self, mock_client):
        """get_events_updated_since passes update_ts_start."""
        mock_resp = MagicMock()
        mock_resp.text = "event_id,timestamp\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        since = datetime(2026, 6, 1, 0, 0, 0)
        mock_client.get_events_updated_since(2911040, since)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["update_ts_start"] == "20260601000000000"

    def test_search_studies_by_taxon(self, mock_client, sample_study_row):
        """search_studies_by_taxon matches species name in taxon_ids."""
        cached_studies = [
            {
                "id": "2911040",
                "name": "Blackbird NL",
                "taxon_ids": "Turdus merula",
                "study_objective": "Track blackbirds",
            },
            {
                "id": "999",
                "name": "Robin study",
                "taxon_ids": "Erithacus rubecula",
                "study_objective": "Robin tracking",
            },
        ]

        result = mock_client.search_studies_by_taxon(
            "Turdus merula",
            _cached_studies=cached_studies,
        )

        assert len(result) == 1
        assert result[0]["id"] == "2911040"

    def test_search_studies_by_taxon_name_match(self, mock_client):
        """search_studies_by_taxon matches on study name/objective too."""
        cached_studies = [
            {
                "id": "500",
                "name": "Turdus merula migration",
                "taxon_ids": "",
                "study_objective": "",
            },
        ]

        result = mock_client.search_studies_by_taxon(
            "Turdus merula",
            _cached_studies=cached_studies,
        )

        assert len(result) == 1
        assert result[0]["id"] == "500"

    def test_search_studies_by_taxon_multi_species(self, mock_client):
        """search_studies_by_taxon matches within comma-separated taxon_ids."""
        cached_studies = [
            {
                "id": "700",
                "name": "Multi Bird Study",
                "taxon_ids": "Sturnus vulgaris,Turdus merula,Prunella modularis",
                "study_objective": "",
            },
        ]

        result = mock_client.search_studies_by_taxon(
            "Turdus merula",
            _cached_studies=cached_studies,
        )

        assert len(result) == 1
        assert result[0]["id"] == "700"

    def test_get_sensor_types(self, mock_client):
        """get_sensor_types fetches tag_type entity."""
        mock_resp = MagicMock()
        mock_resp.text = "id,name\n653,GPS\n"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_sensor_types()

        params = mock_client._session.get.call_args[1]["params"]
        assert params["entity_type"] == "tag_type"

    def test_get_taxon(self, mock_client):
        """get_taxon returns matching taxa filtered by canonical_name."""
        mock_resp = MagicMock()
        mock_resp.text = (
            "id,canonical_name,tsn\n"
            "2309998536,test,\n"
            "15396,Turdus merula,179757\n"
            "15398,Turdus migratorius,179759\n"
        )
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.get_taxon("Turdus merula")

        assert len(result) == 1
        assert result[0]["id"] == "15396"
        assert result[0]["canonical_name"] == "Turdus merula"

    def test_get_taxon_caches(self, mock_client):
        """get_taxon caches the full taxonomy and reuses it."""
        mock_resp = MagicMock()
        mock_resp.text = (
            "id,canonical_name\n15396,Turdus merula\n28450,Turdus philomelos\n"
        )
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        r1 = mock_client.get_taxon("Turdus merula")
        r2 = mock_client.get_taxon("Turdus philomelos")

        assert len(r1) == 1
        assert r1[0]["id"] == "15396"
        assert len(r2) == 1
        assert r2[0]["id"] == "28450"
        # Only one HTTP call despite two get_taxon calls
        assert mock_client._session.get.call_count == 1

    def test_request_token(self, mock_client):
        """request_token fetches a token."""
        mock_resp = MagicMock()
        mock_resp.text = "abc123token"
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        token = mock_client.request_token()

        assert token == "abc123token"
        assert mock_client._api_token == "abc123token"
        params = mock_client._session.get.call_args[1]["params"]
        assert params["service"] == "request-token"

    def test_no_data_html_response(self, mock_client):
        """Empty/HTML responses return empty list."""
        mock_resp = MagicMock()
        mock_resp.text = "<html>No data available</html>"
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        mock_client._session.get.return_value = mock_resp

        result = mock_client.list_studies()
        assert result == []

    def test_retry_on_server_error(self, mock_client):
        """Client retries on 5xx errors."""
        import requests

        err_resp = MagicMock()
        err_resp.status_code = 503
        ok_resp = MagicMock()
        ok_resp.text = "id,name\n1,Study\n"
        ok_resp.raise_for_status = MagicMock()
        ok_resp.headers = {}

        mock_client._session.get.side_effect = [
            requests.HTTPError(response=err_resp),
            ok_resp,
        ]

        result = mock_client.list_studies()
        assert len(result) == 1
        assert mock_client._session.get.call_count == 2

    def test_license_handling(self, mock_client):
        """Client accepts license terms when prompted."""
        # First response has accept-license header
        license_resp = MagicMock()
        license_resp.headers = {"accept-license": "true"}
        license_resp.text = "License terms text here"
        license_resp.raise_for_status = MagicMock()

        # Second response (after license acceptance) has data
        data_resp = MagicMock()
        data_resp.text = "event_id,timestamp\n100,2026-01-01 00:00:00.000\n"
        data_resp.raise_for_status = MagicMock()
        data_resp.headers = {}

        mock_client._session.get.side_effect = [license_resp, data_resp]

        result = mock_client.get_events(2911040)

        assert len(result) == 1
        # Should have been called twice: original + license acceptance
        assert mock_client._session.get.call_count == 2
        # Second call should include license-md5
        second_params = mock_client._session.get.call_args_list[1][1]["params"]
        assert "license-md5" in second_params


# =============================================================================
# MovebankEvent Model Tests
# =============================================================================


class TestMovebankEvent:
    """Tests for the MovebankEvent dataclass."""

    def test_from_csv_row_full(self, sample_event_csv_row):
        """Parse a full event CSV row."""
        event = MovebankEvent.from_csv_row(sample_event_csv_row, study_id=2911040)

        assert event.event_id == 123456789
        assert event.study_id == 2911040
        assert event.timestamp == datetime(2026, 6, 15, 14, 30, 0)
        assert event.individual_id == 4001
        assert event.individual_local_identifier == "Blackbird-NL-01"
        assert event.individual_taxon_canonical_name == "Turdus merula"
        assert event.tag_id == 5001
        assert event.tag_local_identifier == "TAG-001"
        assert event.deployment_id == 6001
        assert event.sensor_type_id == 653
        assert event.location_lat == pytest.approx(52.3702)
        assert event.location_long == pytest.approx(4.8952)
        assert event.ground_speed == pytest.approx(2.5)
        assert event.heading == pytest.approx(135.0)
        assert event.height_above_ellipsoid == pytest.approx(15.3)
        assert event.visible is True

    def test_from_csv_row_minimal(self, sample_event_csv_minimal):
        """Parse a minimal event with just required fields."""
        event = MovebankEvent.from_csv_row(sample_event_csv_minimal, study_id=100)

        assert event.event_id == 999
        assert event.study_id == 100
        assert event.timestamp == datetime(2026, 1, 1)
        assert event.individual_id is None
        assert event.location_lat is None
        assert event.visible is None

    def test_from_csv_row_study_id_from_row(self):
        """study_id can come from row if not overridden."""
        row = {
            "event_id": "1",
            "study_id": "5555",
            "timestamp": "2026-01-01 00:00:00.000",
        }
        event = MovebankEvent.from_csv_row(row)
        assert event.study_id == 5555

    def test_from_csv_row_study_id_override(self):
        """Explicit study_id overrides row value."""
        row = {
            "event_id": "1",
            "study_id": "5555",
            "timestamp": "2026-01-01 00:00:00.000",
        }
        event = MovebankEvent.from_csv_row(row, study_id=7777)
        assert event.study_id == 7777

    def test_from_csv_row_missing_timestamp_raises(self):
        """Missing or invalid timestamp raises ValueError."""
        row = {"event_id": "1"}
        with pytest.raises(ValueError, match="timestamp"):
            MovebankEvent.from_csv_row(row, study_id=1)

    def test_from_csv_row_empty_string_timestamp_raises(self):
        """Empty string timestamp raises ValueError."""
        row = {"event_id": "1", "timestamp": ""}
        with pytest.raises(ValueError, match="timestamp"):
            MovebankEvent.from_csv_row(row, study_id=1)

    def test_from_csv_row_missing_event_id_generates_hash(self):
        """Missing event_id generates a deterministic hash-based ID."""
        row = {
            "timestamp": "2026-06-15 14:30:00.000",
            "individual_id": "4001",
        }
        event = MovebankEvent.from_csv_row(row, study_id=100)
        assert event.event_id > 0
        assert isinstance(event.event_id, int)

    def test_from_csv_row_visible_false(self):
        """Visible flag 'false' is parsed correctly."""
        row = {
            "event_id": "1",
            "timestamp": "2026-01-01 00:00:00.000",
            "visible": "false",
        }
        event = MovebankEvent.from_csv_row(row, study_id=1)
        assert event.visible is False

    def test_from_csv_row_empty_visible(self):
        """Empty visible field is None."""
        row = {
            "event_id": "1",
            "timestamp": "2026-01-01 00:00:00.000",
            "visible": "",
        }
        event = MovebankEvent.from_csv_row(row, study_id=1)
        assert event.visible is None

    def test_from_csv_row_date_only_timestamp(self):
        """Date-only timestamp without time is parsed."""
        row = {
            "event_id": "1",
            "timestamp": "2026-06-15",
        }
        event = MovebankEvent.from_csv_row(row, study_id=1)
        assert event.timestamp == datetime(2026, 6, 15)

    def test_from_csv_row_quoted_strings(self):
        """Quoted string fields have quotes stripped."""
        row = {
            "event_id": "1",
            "timestamp": "2026-01-01 00:00:00.000",
            "individual_local_identifier": '"Bird "Alpha"  "',
        }
        event = MovebankEvent.from_csv_row(row, study_id=1)
        assert event.individual_local_identifier is not None
        assert not event.individual_local_identifier.startswith('"')

    def test_to_dict(self, sample_event_csv_row):
        """to_dict returns all fields."""
        event = MovebankEvent.from_csv_row(sample_event_csv_row, study_id=2911040)
        d = event.to_dict()

        assert d["event_id"] == 123456789
        assert d["study_id"] == 2911040
        assert d["location_lat"] == pytest.approx(52.3702)

    def test_column_names(self):
        """column_names returns all dataclass field names."""
        cols = MovebankEvent.column_names()
        assert "event_id" in cols
        assert "study_id" in cols
        assert "timestamp" in cols
        assert "location_lat" in cols

    def test_pandas_dtypes(self):
        """pandas_dtypes covers all columns."""
        dtypes = MovebankEvent.pandas_dtypes()
        cols = MovebankEvent.column_names()
        for col in cols:
            assert col in dtypes, f"Missing dtype for {col}"

    def test_frozen_immutable(self, sample_event_csv_row):
        """Dataclass is immutable."""
        event = MovebankEvent.from_csv_row(sample_event_csv_row, study_id=2911040)
        with pytest.raises(AttributeError):
            event.event_id = 9999


# =============================================================================
# MovebankCollector Tests
# =============================================================================


class TestMovebankCollector:
    """Tests for the MovebankCollector orchestration class."""

    def test_collect_study_events(self, collector, sample_event_csv_row):
        """Collect events for a study."""
        collector.client.get_events.return_value = [sample_event_csv_row]
        collector.client.get_sensors.return_value = [{"sensor_type_id": "653"}]
        collector.client.get_individuals.return_value = [
            {"id": "4001", "sex": "m", "animal_life_stage": "adult"}
        ]
        collector.client.get_tags.return_value = [
            {"id": "5001", "manufacturer_name": "e-obs", "model": "GPS-01"}
        ]
        collector.client.get_deployments.return_value = [
            {"id": "6001", "local_identifier": "DEP-01"}
        ]

        stats = collector.collect_study_events(
            2911040,
        )

        assert stats.events_fetched == 1
        assert stats.individuals_count == 1
        # get_sensors + get_events + get_individuals + get_tags + get_deployments
        assert stats.api_calls == 5
        collector.client.get_events.assert_called_once()

    def test_collect_study_events_with_filters(self, collector, sample_event_csv_row):
        """Collect events with individual/time filters."""
        collector.client.get_events.return_value = [sample_event_csv_row]

        stats = collector.collect_study_events(
            2911040,
            individual_ids=["Blackbird-NL-01"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 6, 30),
        )

        assert stats.events_fetched == 1
        call_kwargs = collector.client.get_events.call_args[1]
        assert call_kwargs["individual_local_identifiers"] == ["Blackbird-NL-01"]
        assert call_kwargs["timestamp_start"] == date(2026, 1, 1)

    def test_collect_study_events_with_reduction(self, collector, sample_event_csv_row):
        """Collect events with reduction profile."""
        collector.client.get_events.return_value = [sample_event_csv_row]

        stats = collector.collect_study_events(
            2911040,
            reduction_profile="EURING_01",
        )

        call_kwargs = collector.client.get_events.call_args[1]
        assert call_kwargs["event_reduction_profile"] == "EURING_01"

    def test_collect_study_events_filters_empty_locations(self, collector):
        """Events without location data are filtered out."""
        # Use a taxon that is actually in config/target_species.json so the
        # safety filter at the end of ``collect_study_events`` does not drop
        # both rows for being non-target species.
        events_with_empty = [
            {
                "event_id": "1",
                "timestamp": "2026-01-01 00:00:00.000",
                "location_lat": "52.37",
                "location_long": "4.89",
                "individual_local_identifier": "Bird-01",
                "individual_taxon_canonical_name": "Turdus merula",
            },
            {
                "event_id": "2",
                "timestamp": "2026-01-01 01:00:00.000",
                "location_lat": "",
                "location_long": "",
                "individual_local_identifier": "Bird-01",
                "individual_taxon_canonical_name": "Turdus merula",
            },
        ]
        collector.client.get_events.return_value = events_with_empty

        stats = collector.collect_study_events(2911040)

        assert stats.events_fetched == 1

    def test_collect_study_events_save_local(
        self, collector, sample_event_csv_row, tmp_path
    ):
        """Events are saved locally as JSONL + metadata."""
        collector.output_dir = tmp_path
        collector.client.get_events.return_value = [sample_event_csv_row]

        stats = collector.collect_study_events(2911040)

        # Should create a directory under the study ID
        study_dir = tmp_path / "2911040"
        assert study_dir.exists()
        # Should have at least one date directory with events.jsonl
        date_dirs = list(study_dir.iterdir())
        assert len(date_dirs) >= 1
        events_file = list(date_dirs[0].glob("events.jsonl"))
        assert len(events_file) == 1

    def test_collect_study_events_api_error(self, collector):
        """API errors are counted, not raised."""
        import requests

        err_resp = MagicMock()
        err_resp.status_code = 403
        collector.client.get_events.side_effect = requests.HTTPError(
            response=err_resp,
        )

        stats = collector.collect_study_events(2911040)

        assert stats.api_errors == 1
        assert stats.events_fetched == 0

    def test_merge_reference_data_enriches_events(self, collector):
        """_merge_reference_data populates individual/tag/deployment fields."""
        events = [
            {
                "event_id": "1",
                "timestamp": "2026-01-01 00:00:00.000",
                "individual_id": "4001",
                "tag_id": "5001",
                "deployment_id": "6001",
            },
        ]
        collector.client.get_individuals.return_value = [
            {
                "id": "4001",
                "sex": "m",
                "animal_life_stage": "adult",
                "individual_taxon_canonical_name": "Turdus merula",
            },
        ]
        collector.client.get_tags.return_value = [
            {
                "id": "5001",
                "manufacturer_name": "e-obs",
                "model": "GPS-01",
                "serial_no": "SN-123",
            },
        ]
        collector.client.get_deployments.return_value = [
            {
                "id": "6001",
                "local_identifier": "DEP-01",
                "deploy_on_timestamp": "2025-01-01 00:00:00.000",
                "deploy_on_latitude": "52.37",
                "deploy_on_longitude": "4.89",
            },
        ]

        stats = CollectionStats()
        collector._merge_reference_data(events, 2911040, stats)

        e = events[0]
        assert e["individual_sex"] == "m"
        assert e["individual_life_stage"] == "adult"
        assert e["individual_taxon_canonical_name"] == "Turdus merula"
        assert e["tag_manufacturer_name"] == "e-obs"
        assert e["tag_model"] == "GPS-01"
        assert e["tag_serial_number"] == "SN-123"
        assert e["deployment_local_identifier"] == "DEP-01"
        assert e["deploy_on_latitude"] == "52.37"
        assert stats.api_calls == 3  # individuals + tags + deployments

    def test_merge_reference_data_partial_failure(self, collector):
        """_merge_reference_data continues when some ref fetches fail."""
        import requests

        events = [
            {
                "event_id": "1",
                "timestamp": "2026-01-01 00:00:00.000",
                "individual_id": "4001",
                "tag_id": "5001",
                "deployment_id": "6001",
            },
        ]
        err = requests.HTTPError(response=MagicMock(status_code=403))
        collector.client.get_individuals.side_effect = err
        collector.client.get_tags.return_value = [
            {"id": "5001", "model": "GPS-01"},
        ]
        collector.client.get_deployments.return_value = []

        stats = CollectionStats()
        collector._merge_reference_data(events, 2911040, stats)

        # Tag data still merged despite individuals failure
        assert events[0]["tag_model"] == "GPS-01"
        # Only tags + deployments succeeded
        assert stats.api_calls == 2

    def test_collected_at_stamped_on_events(self, collector, sample_event_csv_row):
        """collect_study_events stamps _collected_at on every event."""
        collector.client.get_events.return_value = [sample_event_csv_row]

        collector.collect_study_events(2911040)

        assert "_collected_at" in sample_event_csv_row
        assert "_study_id" in sample_event_csv_row
        assert sample_event_csv_row["_study_id"] == str(2911040)

    def test_collect_study_metadata(self, collector, tmp_path):
        """Collect reference metadata for a study."""
        collector.output_dir = tmp_path
        collector.client.get_study.return_value = [{"id": "2911040", "name": "Test"}]
        collector.client.get_individuals.return_value = [{"id": "1"}]
        collector.client.get_deployments.return_value = [{"id": "2"}]
        collector.client.get_tags.return_value = [{"id": "3"}]

        stats = collector.collect_study_metadata(2911040)

        assert stats.studies_found == 1
        assert stats.individuals_count == 1
        assert stats.deployments_count == 1
        assert stats.api_calls == 4

        # Check reference files
        ref_dir = tmp_path / "2911040" / "reference"
        assert (ref_dir / "study.json").exists()
        assert (ref_dir / "individuals.json").exists()
        assert (ref_dir / "deployments.json").exists()
        assert (ref_dir / "tags.json").exists()

    def test_collect_study_metadata_partial_failure(self, collector, tmp_path):
        """Metadata collection continues on partial failure."""
        import requests

        collector.output_dir = tmp_path
        collector.client.get_study.return_value = [{"id": "2911040"}]
        err_resp = MagicMock()
        err_resp.status_code = 403
        collector.client.get_individuals.side_effect = requests.HTTPError(
            response=err_resp,
        )
        collector.client.get_deployments.return_value = [{"id": "2"}]
        collector.client.get_tags.return_value = []

        stats = collector.collect_study_metadata(2911040)

        assert stats.studies_found == 1
        assert stats.api_errors == 1
        # Still proceeds with remaining calls
        assert stats.deployments_count == 1

    def test_discover_target_species_studies(self, collector, tmp_path):
        """Discover studies for target species."""
        collector.output_dir = tmp_path

        # Mock search returning studies for different species
        def mock_search(taxon, **kwargs):
            if "merula" in taxon.lower():
                return [{"id": "100", "name": "Blackbird study"}]
            return []

        collector.client.search_studies_by_taxon.side_effect = mock_search

        with patch(
            "avian_biosurveillance.ingestion.movebank._load_target_species",
            return_value=[
                {"english": "Eurasian Blackbird", "scientific": "Turdus merula"},
                {"english": "Song Thrush", "scientific": "Turdus philomelos"},
            ],
        ):
            stats = collector.discover_target_species_studies()

        assert stats.studies_found == 1
        disc_file = tmp_path / "discovery" / "target_species_studies.json"
        assert disc_file.exists()

    def test_discover_target_species_studies_saves_cache_for_database_target(
        self,
        collector,
        tmp_path,
    ):
        """Database-target discovery should still persist the fallback cache."""
        collector.output_dir = tmp_path

        def mock_search(taxon, **kwargs):
            if "merula" in taxon.lower():
                return [{"id": "100", "name": "Blackbird study"}]
            return []

        collector.client.search_studies_by_taxon.side_effect = mock_search

        with patch(
            "avian_biosurveillance.ingestion.movebank._load_target_species",
            return_value=[
                {"english": "Eurasian Blackbird", "scientific": "Turdus merula"},
            ],
        ):
            stats = collector.discover_target_species_studies(target="database")

        assert stats.studies_found == 1
        disc_file = tmp_path / "discovery" / "target_species_studies.json"
        assert disc_file.exists()

    def test_discover_target_species_studies_uses_cached_results_on_request_error(
        self,
        collector,
        tmp_path,
    ):
        """Transient request failures should fall back to cached target-species studies."""
        collector.output_dir = tmp_path
        disc_dir = tmp_path / "discovery"
        disc_dir.mkdir(parents=True)
        disc_file = disc_dir / "target_species_studies.json"
        disc_file.write_text(
            json.dumps([{"id": "100", "name": "Cached study"}]),
            encoding="utf-8",
        )

        collector.client.list_studies.side_effect = requests.ConnectTimeout(
            "timed out",
        )

        stats = collector.discover_target_species_studies(target="database")

        assert stats.studies_found == 1

    def test_discover_deduplicates_studies(self, collector, tmp_path):
        """Duplicate studies from different species are deduplicated."""
        collector.output_dir = tmp_path

        # Same study returned for multiple species
        collector.client.search_studies_by_taxon.return_value = [
            {"id": "100", "name": "Multi-species study"},
        ]

        with patch(
            "avian_biosurveillance.ingestion.movebank._load_target_species",
            return_value=[
                {"english": "Sp1", "scientific": "Species one"},
                {"english": "Sp2", "scientific": "Species two"},
            ],
        ):
            stats = collector.discover_target_species_studies()

        # Only 1 unique study despite matching 2 species
        assert stats.studies_found == 1


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestHelpers:
    """Tests for module-level helper functions."""

    def test_mb_timestamp_from_date(self):
        """Format a date to Movebank timestamp."""
        ts = _mb_timestamp(date(2026, 6, 15))
        assert ts == "20260615000000000"

    def test_mb_timestamp_from_datetime(self):
        """Format a datetime to Movebank timestamp."""
        ts = _mb_timestamp(datetime(2026, 6, 15, 14, 30, 45))
        assert ts == "20260615143045000"

    def test_load_target_species(self):
        """Load target species from config."""
        species = _load_target_species()
        assert len(species) > 0
        # All should have movebank_taxon
        for sp in species:
            assert "movebank_taxon" in sp
            assert "scientific" in sp

    def test_load_target_species_uses_central_registry(self):
        """Movebank target loading goes through the central species registry."""
        species = _load_target_species()

        assert all("movebank_taxon" in item for item in species)
        assert all(item["movebank_taxon"] for item in species)


# =============================================================================
# CollectionStats Tests
# =============================================================================


class TestCollectionStats:
    """Tests for collection statistics tracking."""

    def test_defaults(self):
        stats = CollectionStats()
        assert stats.events_fetched == 0
        assert stats.api_calls == 0

    def test_to_dict(self):
        stats = CollectionStats()
        stats.events_fetched = 42
        stats.studies_found = 3
        d = stats.to_dict()
        assert d["events_fetched"] == 42
        assert d["studies_found"] == 3
        assert "elapsed_seconds" in d

    def test_elapsed_time(self):
        stats = CollectionStats()
        assert stats.elapsed_s >= 0


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Tests for module-level constants."""

    def test_base_url(self):
        assert "movebank.org" in BASE_URL

    def test_gps_sensor_type_id(self):
        assert GPS_SENSOR_TYPE_ID == 653

    def test_default_event_attributes(self):
        assert "timestamp" in DEFAULT_EVENT_ATTRIBUTES
        assert "location_lat" in DEFAULT_EVENT_ATTRIBUTES
        assert "location_long" in DEFAULT_EVENT_ATTRIBUTES
        assert "individual_local_identifier" in DEFAULT_EVENT_ATTRIBUTES
        assert "eobs_status" not in DEFAULT_EVENT_ATTRIBUTES
        assert "behavioural_classification" not in DEFAULT_EVENT_ATTRIBUTES

    def test_reduction_profiles(self):
        assert "EURING_01" in REDUCTION_PROFILES
        assert "EURING_02" in REDUCTION_PROFILES
        assert "EURING_03" in REDUCTION_PROFILES
        assert "EURING_04" in REDUCTION_PROFILES


# =============================================================================
# CLI Tests
# =============================================================================


class TestCLI:
    """Tests for the CLI argument parsing."""

    def test_main_runs_help(self):
        """main() runs with --help without error."""
        from avian_biosurveillance.ingestion.movebank import main

        with patch("sys.argv", ["movebank", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_main_no_args_defaults_to_target_species(self):
        """main() defaults to --target-species when no args given."""
        from avian_biosurveillance.ingestion.movebank import main

        with (
            patch("sys.argv", ["movebank"]),
            patch(
                "avian_biosurveillance.ingestion.movebank.MovebankCollector"
            ) as mock_cls,
        ):
            mock_collector = mock_cls.return_value
            mock_collector.collect_target_species_events.return_value = (
                CollectionStats()
            )
            main()
            mock_collector.collect_target_species_events.assert_called_once()
