"""Tests for the eBird Data Ingestion Module.

Tests cover:
- EBirdClient API methods and retry logic
- EBirdCollector orchestration (recent, historic, nearby, hotspots)
- EBirdObservation data model (from_api, to_dict)
- CLI argument parsing
- Deduplication logic
- Target species loading
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from avian_biosurveillance.db.models.ebird import EBirdObservation
from avian_biosurveillance.ingestion.ebird import (
    CollectionStats,
    EBirdClient,
    EBirdCollector,
)


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_client():
    """Create an EBirdClient with mocked HTTP session."""
    with patch.dict("os.environ", {"EBIRD_API_KEY": "test-key-123"}):
        client = EBirdClient(api_key="test-key-123")
    client._session = MagicMock()
    return client


@pytest.fixture
def sample_observation():
    """Sample eBird observation as returned by the API."""
    return {
        "speciesCode": "eurbla",
        "comName": "Eurasian Blackbird",
        "sciName": "Turdus merula",
        "locId": "L1234567",
        "locName": "Amsterdam Vondelpark",
        "obsDt": "2026-03-14 08:30",
        "howMany": 3,
        "lat": 52.3580,
        "lng": 4.8686,
        "obsValid": True,
        "obsReviewed": False,
        "locationPrivate": False,
        "subId": "S99000001",
    }


@pytest.fixture
def sample_notable_observation():
    """Sample notable observation with full detail."""
    return {
        "speciesCode": "litowe1",
        "comName": "Little Owl",
        "sciName": "Athene noctua",
        "locId": "L2345678",
        "locName": "Kinderdijk",
        "obsDt": "2026-03-13 19:45",
        "howMany": 1,
        "lat": 51.8833,
        "lng": 4.6333,
        "obsValid": True,
        "obsReviewed": True,
        "locationPrivate": False,
        "subId": "S99000002",
        "subnational2Code": "NL-ZH-588",
        "subnational2Name": "Molenlanden",
        "subnational1Code": "NL-ZH",
        "subnational1Name": "South Holland",
        "countryCode": "NL",
        "countryName": "Netherlands",
        "userDisplayName": "Test Birder",
        "obsId": "OBS900000001",
        "checklistId": "CL99001",
        "presenceNoted": False,
        "hasComments": True,
        "hasRichMedia": False,
    }


@pytest.fixture
def multiple_observations(sample_observation):
    """Multiple observations including a duplicate."""
    obs2 = {**sample_observation, "speciesCode": "sonthr1", "comName": "Song Thrush"}
    obs3 = {
        **sample_observation,
        "speciesCode": "eurrob1",
        "comName": "European Robin",
        "subId": "S99000003",
    }
    # Duplicate of obs1
    obs_dup = {**sample_observation}
    return [sample_observation, obs2, obs3, obs_dup]


@pytest.fixture
def collector(tmp_path):
    """Create an EBirdCollector with mocked client."""
    with patch.dict("os.environ", {"EBIRD_API_KEY": "test-key-123"}):
        coll = EBirdCollector(output_dir=tmp_path, api_key="test-key-123")
    coll.client = MagicMock(spec=EBirdClient)
    return coll


# =============================================================================
# EBirdClient Tests
# =============================================================================


class TestEBirdClient:
    """Tests for the EBirdClient HTTP wrapper."""

    def test_init_with_api_key(self):
        """Client initializes with explicit API key."""
        client = EBirdClient(api_key="my-key")
        assert client.api_key == "my-key"

    def test_init_uses_default_timeout(self):
        """Client uses the repository default timeout when unset."""
        client = EBirdClient(api_key="my-key")
        assert client.timeout == 60
        assert client.historic_timeout == 180

    def test_init_from_env(self):
        """Client reads API key from environment."""
        with patch.dict("os.environ", {"EBIRD_API_KEY": "env-key"}):
            client = EBirdClient()
        assert client.api_key == "env-key"

    def test_init_uses_timeout_from_env(self):
        """Client reads request timeout from environment."""
        with patch.dict(
            "os.environ",
            {
                "EBIRD_API_KEY": "env-key",
                "EBIRD_REQUEST_TIMEOUT_SECONDS": "120",
            },
        ):
            client = EBirdClient()
        assert client.timeout == 120
        assert client.historic_timeout == 180

    def test_init_uses_historic_timeout_from_env(self):
        """Client reads the historic request timeout from environment."""
        with patch.dict(
            "os.environ",
            {
                "EBIRD_API_KEY": "env-key",
                "EBIRD_HISTORIC_REQUEST_TIMEOUT_SECONDS": "240",
            },
        ):
            client = EBirdClient()
        assert client.timeout == 60
        assert client.historic_timeout == 240

    def test_init_timeout_env_falls_back_on_invalid_value(self):
        """Invalid timeout env values should fall back to the default."""
        with patch.dict(
            "os.environ",
            {
                "EBIRD_API_KEY": "env-key",
                "EBIRD_REQUEST_TIMEOUT_SECONDS": "invalid",
            },
        ):
            client = EBirdClient()
        assert client.timeout == 60
        assert client.historic_timeout == 180

    def test_init_no_key_raises(self):
        """Client raises ValueError when no API key available."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="eBird API key required"):
                EBirdClient()

    def test_recent_observations_params(self, mock_client):
        """recent_observations passes correct params."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.recent_observations("NL", back=7, hotspot=True)

        call_args = mock_client._session.get.call_args
        assert "/data/obs/NL/recent" in call_args[0][0]
        params = call_args[1]["params"]
        assert params["back"] == 7
        assert params["hotspot"] == "true"
        assert call_args[1]["timeout"] == mock_client.timeout

    def test_recent_species_observations(self, mock_client):
        """recent_species_observations uses correct URL path."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.recent_species_observations("NL", "eurbla", back=14)

        url = mock_client._session.get.call_args[0][0]
        assert "/data/obs/NL/recent/eurbla" in url

    def test_notable_observations_detail(self, mock_client):
        """recent_notable_observations passes detail param."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.recent_notable_observations("NL", detail="full")

        params = mock_client._session.get.call_args[1]["params"]
        assert params["detail"] == "full"

    def test_historic_observations_date_path(self, mock_client):
        """historic_observations builds correct date path."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.historic_observations("NL", date(2026, 3, 14))

        url = mock_client._session.get.call_args[0][0]
        assert "/data/obs/NL/historic/2026/3/14" in url
        assert mock_client._session.get.call_args[1]["timeout"] == mock_client.historic_timeout

    def test_nearby_observations_geo_params(self, mock_client):
        """nearby_observations passes lat/lng/dist."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.nearby_observations(52.37, 4.89, dist=30, back=7)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["lat"] == 52.37
        assert params["lng"] == 4.89
        assert params["dist"] == 30
        assert params["back"] == 7

    def test_back_clamped_to_30(self, mock_client):
        """back parameter is clamped to max 30."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.recent_observations("NL", back=100)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["back"] == 30

    def test_dist_clamped_to_50(self, mock_client):
        """dist parameter is clamped to max 50."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.nearby_observations(52.0, 4.0, dist=100)

        params = mock_client._session.get.call_args[1]["params"]
        assert params["dist"] == 50

    def test_hotspots_in_region(self, mock_client):
        """hotspots_in_region calls correct endpoint."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.hotspots_in_region("NL", back=7)

        url = mock_client._session.get.call_args[0][0]
        assert "/ref/hotspot/NL" in url

    def test_taxonomy_species_filter(self, mock_client):
        """taxonomy passes species filter correctly."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.taxonomy(species=["eurbla", "sonthr1"])

        params = mock_client._session.get.call_args[1]["params"]
        assert params["species"] == "eurbla,sonthr1"

    def test_checklist_endpoint(self, mock_client):
        """checklist uses correct path."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.checklist("S99000001")

        url = mock_client._session.get.call_args[0][0]
        assert "/product/checklist/view/S99000001" in url

    def test_region_info(self, mock_client):
        """region_info hits correct endpoint."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        mock_client._session.get.return_value = mock_resp

        mock_client.region_info("NL")

        url = mock_client._session.get.call_args[0][0]
        assert "/ref/region/info/NL" in url


# =============================================================================
# EBirdObservation Model Tests
# =============================================================================


class TestEBirdObservation:
    """Tests for the EBirdObservation dataclass."""

    def test_from_api_simple(self, sample_observation):
        """Parse a simple observation dict."""
        obs = EBirdObservation.from_api(sample_observation)

        assert obs.sub_id == "S99000001"
        assert obs.species_code == "eurbla"
        assert obs.common_name == "Eurasian Blackbird"
        assert obs.scientific_name == "Turdus merula"
        assert obs.how_many == 3
        assert obs.loc_id == "L1234567"
        assert obs.lat == pytest.approx(52.358)
        assert obs.lng == pytest.approx(4.8686)
        assert obs.obs_valid is True
        assert obs.obs_reviewed is False
        assert obs.observation_date == date(2026, 3, 14)

    def test_from_api_full_detail(self, sample_notable_observation):
        """Parse a full-detail notable observation."""
        obs = EBirdObservation.from_api(sample_notable_observation)

        assert obs.country_code == "NL"
        assert obs.subnational1_code == "NL-ZH"
        assert obs.user_display_name == "Test Birder"
        assert obs.checklist_id == "CL99001"
        assert obs.has_comments is True
        assert obs.has_rich_media is False

    def test_from_api_date_only_format(self):
        """Handle date-only obsDt format (no time)."""
        raw = {
            "speciesCode": "eurbla",
            "comName": "Eurasian Blackbird",
            "obsDt": "2026-03-14",
            "subId": "S100",
            "locId": "L100",
            "lat": 52.0,
            "lng": 4.0,
        }
        obs = EBirdObservation.from_api(raw)
        assert obs.observation_date == date(2026, 3, 14)

    def test_from_api_missing_how_many(self):
        """howMany can be None (presence-only checklist)."""
        raw = {
            "speciesCode": "eurbla",
            "comName": "Eurasian Blackbird",
            "obsDt": "2026-03-14 08:00",
            "subId": "S101",
            "locId": "L101",
            "lat": 52.0,
            "lng": 4.0,
            "presenceNoted": True,
        }
        obs = EBirdObservation.from_api(raw)
        assert obs.how_many is None
        assert obs.presence_noted is True

    def test_to_dict(self, sample_observation):
        """to_dict returns all fields."""
        obs = EBirdObservation.from_api(sample_observation)
        d = obs.to_dict()

        assert d["sub_id"] == "S99000001"
        assert d["species_code"] == "eurbla"

    def test_column_names(self):
        """column_names returns all dataclass field names."""
        cols = EBirdObservation.column_names()
        assert "sub_id" in cols
        assert "species_code" in cols

    def test_pandas_dtypes(self):
        """pandas_dtypes covers all columns."""
        dtypes = EBirdObservation.pandas_dtypes()
        cols = EBirdObservation.column_names()
        for col in cols:
            assert col in dtypes, f"Missing dtype for {col}"

    def test_frozen_immutable(self, sample_observation):
        """Dataclass is immutable."""
        obs = EBirdObservation.from_api(sample_observation)
        with pytest.raises(AttributeError):
            obs.sub_id = "changed"


# =============================================================================
# EBirdCollector Tests
# =============================================================================


class TestEBirdCollector:
    """Tests for the EBirdCollector orchestration class."""

    def test_collect_recent_all_species(self, collector, sample_observation):
        """Collect all species in a region."""
        collector.client.recent_observations.return_value = [sample_observation]

        stats = collector.collect_recent("NL", back=7)

        assert stats.observations_fetched == 1
        assert stats.species_count == 1
        assert stats.api_calls == 1
        collector.client.recent_observations.assert_called_once()

    def test_collect_recent_specific_species(self, collector, sample_observation):
        """Collect specific species by code."""
        collector.client.recent_species_observations.return_value = [sample_observation]

        stats = collector.collect_recent(
            "NL",
            species_codes=["eurbla"],
        )

        assert stats.observations_fetched == 1
        collector.client.recent_species_observations.assert_called_once()

    def test_collect_recent_notable(self, collector, sample_notable_observation):
        """Collect notable observations."""
        collector.client.recent_notable_observations.return_value = [
            sample_notable_observation,
        ]

        stats = collector.collect_recent("NL", notable=True)

        assert stats.notable_count == 1
        collector.client.recent_notable_observations.assert_called_once()

    def test_collect_recent_target_species(self, collector, sample_observation):
        """Collect target species from the shared species registry wrapper."""
        with patch(
            "avian_biosurveillance.ingestion.ebird._load_target_species",
            return_value=[
                {"english": "Eurasian Blackbird", "ebird_code": "eurbla"},
                {"english": "Song Thrush", "ebird_code": "sonthr1"},
            ],
        ):
            collector.client.recent_species_observations.return_value = [
                sample_observation,
            ]
            collector.collect_recent(
                "NL",
                target_species_only=True,
            )

        assert collector.client.recent_species_observations.call_count == 2

    def test_collect_historic_single_date(self, collector, sample_observation):
        """Collect historic observations for one date."""
        collector.client.historic_observations.return_value = [sample_observation]

        stats = collector.collect_historic(
            "NL",
            obs_date=date(2026, 3, 14),
        )

        assert stats.observations_fetched == 1
        collector.client.historic_observations.assert_called_once()

    def test_collect_historic_date_range(self, collector, sample_observation):
        """Collect historic observations for a date range."""
        collector.client.historic_observations.return_value = [sample_observation]

        stats = collector.collect_historic(
            "NL",
            start_date=date(2026, 3, 10),
            end_date=date(2026, 3, 12),
        )

        # 3 days: 10, 11, 12
        assert collector.client.historic_observations.call_count == 3

    def test_collect_nearby(self, collector, sample_observation):
        """Collect nearby observations."""
        collector.client.nearby_observations.return_value = [sample_observation]

        stats = collector.collect_nearby(
            52.37,
            4.89,
            dist=25,
            back=7,
        )

        assert stats.observations_fetched == 1
        collector.client.nearby_observations.assert_called_once()

    def test_collect_hotspots(self, collector, tmp_path):
        """Collect hotspot metadata."""
        collector.output_dir = tmp_path
        collector.client.hotspots_in_region.return_value = [
            {"locId": "L123", "locName": "Test Hotspot", "lat": 52.0, "lng": 4.0},
        ]

        stats = collector.collect_hotspots("NL")

        assert stats.hotspots_fetched == 1
        assert (tmp_path / "hotspots" / "NL_hotspots.json").exists()

    def test_deduplication(self, collector, multiple_observations):
        """Duplicate observations are removed."""
        unique = collector._deduplicate(multiple_observations)

        # 4 observations, but obs1 and obs_dup share (subId, speciesCode)
        assert len(unique) == 3

    def test_save_local_creates_files(self, collector, sample_observation, tmp_path):
        """Local save creates JSONL, species_list, and metadata files."""
        collector.output_dir = tmp_path
        collector.client.recent_observations.return_value = [sample_observation]

        stats = collector.collect_recent("NL")

        output_dir = list((tmp_path / "NL").iterdir())[0]
        assert (output_dir / "observations.jsonl").exists()
        assert (output_dir / "species_list.json").exists()
        assert (output_dir / "metadata.json").exists()

        # Verify JSONL content
        lines = (output_dir / "observations.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        obs_data = json.loads(lines[0])
        assert obs_data["speciesCode"] == "eurbla"

    def test_api_error_handling(self, collector):
        """API errors are counted, not raised."""
        import requests

        err_resp = MagicMock()
        err_resp.status_code = 404
        collector.client.recent_species_observations.side_effect = requests.HTTPError(
            response=err_resp
        )

        stats = collector.collect_recent(
            "NL",
            species_codes=["invalid_code"],
        )

        assert stats.api_errors == 1
        assert stats.observations_fetched == 0


# =============================================================================
# CollectionStats Tests
# =============================================================================


class TestCollectionStats:
    """Tests for collection statistics tracking."""

    def test_defaults(self):
        stats = CollectionStats()
        assert stats.observations_fetched == 0
        assert stats.api_calls == 0

    def test_to_dict(self):
        stats = CollectionStats()
        stats.observations_fetched = 42
        stats.species_count = 5
        d = stats.to_dict()
        assert d["observations_fetched"] == 42
        assert d["species_count"] == 5
        assert "elapsed_seconds" in d

    def test_elapsed_time(self):
        stats = CollectionStats()
        assert stats.elapsed_s >= 0


# =============================================================================
# CLI Tests
# =============================================================================


class TestCLI:
    """Tests for the CLI argument parsing."""

    def test_default_region(self):
        """Default region is NL when not specified."""
        from avian_biosurveillance.ingestion.ebird import DEFAULT_REGION

        assert DEFAULT_REGION == "NL"

    def test_main_runs_without_error(self):
        """main() runs with --help without error."""
        from avian_biosurveillance.ingestion.ebird import main

        with patch("sys.argv", ["ebird", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0
