"""eBird API 2.0 Ingestion Module.

Connects to the `eBird API 2.0 <https://documenter.getpostman.com/view/664302/S1ENwy59>`_
to fetch bird observation data (citizen-science checklists) for a given region,
hotspot or species.

Key capabilities:

* **Recent observations** – last 1-30 days for any region or location.
* **Notable observations** – locally/nationally rare species alerts.
* **Historic observations** – observations on a specific calendar date.
* **Nearby observations** – observations within a radius of coordinates.
* **Hotspot metadata** – information on eBird hotspot locations.
* **Taxonomy lookup** – resolve species codes and scientific names.
* **Regional statistics** – species lists and top-100 contributors.

Data is saved to ``data/raw/ebird/`` as JSONL (one line per observation).
Optionally, observations can be upserted into TimescaleDB via ``--persist-to-db``.

Usage::

    # Recent observations in the Netherlands (last 14 days)
    python -m avian_biosurveillance.ingestion.ebird --region NL --back 14

    # Recent observations of a species
    python -m avian_biosurveillance.ingestion.ebird --region NL --species eurbla

    # Historic observations on a date
    python -m avian_biosurveillance.ingestion.ebird --region NL --date 2026-01-15

    # Nearby observations
    python -m avian_biosurveillance.ingestion.ebird --lat 52.37 --lon 4.89 --dist 25

    # Notable observations (rare/unusual)
    python -m avian_biosurveillance.ingestion.ebird --region NL --notable --back 7

    # Target species from config only
    python -m avian_biosurveillance.ingestion.ebird --region NL --target-species

    # Hotspot information
    python -m avian_biosurveillance.ingestion.ebird --hotspots NL

    # Persist to TimescaleDB
    python -m avian_biosurveillance.ingestion.ebird --region NL --persist-to-db
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.config.species import registry as _sp_registry
from avian_biosurveillance.utils.progress import tqdm


load_runtime_env()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://api.ebird.org/v2"

HEADERS = {
    "User-Agent": "AvianBiosurveillance/2.0 (Research; contact@example.com)",
    "Accept": "application/json",
}

# Rate-limit: eBird API allows ~100 requests/minute for most endpoints.
# We add a small sleep between paginated calls to stay well under the limit.
_REQUEST_DELAY_S = 0.25
_DEFAULT_REQUEST_TIMEOUT_S = 60
_REQUEST_TIMEOUT_ENV = "EBIRD_REQUEST_TIMEOUT_SECONDS"
_DEFAULT_HISTORIC_REQUEST_TIMEOUT_S = 180
_HISTORIC_REQUEST_TIMEOUT_ENV = "EBIRD_HISTORIC_REQUEST_TIMEOUT_SECONDS"

# Default region: Netherlands
DEFAULT_REGION = "NL"

def _resolve_request_timeout(
    timeout: int | None,
    *,
    env_name: str = _REQUEST_TIMEOUT_ENV,
    default_timeout: int = _DEFAULT_REQUEST_TIMEOUT_S,
) -> int:
    """Resolve the effective eBird HTTP request timeout."""
    if timeout is not None:
        return timeout

    raw_value = os.environ.get(env_name, "").strip()
    if not raw_value:
        return default_timeout

    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r; expected a positive integer.",
            env_name,
            raw_value,
        )
        return default_timeout

    if parsed < 1:
        logger.warning(
            "Ignoring %s=%r; expected a positive integer.",
            env_name,
            raw_value,
        )
        return default_timeout

    return parsed


def _load_target_species() -> list[dict[str, Any]]:
    """Return target-species records from the shared species registry."""
    return [
        {
            "english": species.english,
            "dutch": species.dutch,
            "scientific": species.scientific,
            "sovon_id": species.sovon_id,
            "birdweather_id": species.birdweather_id,
            "ebird_code": species.ebird_code,
            "movebank_taxon": species.movebank_taxon_name,
        }
        for species in _sp_registry
    ]


# ---------------------------------------------------------------------------
# Collection Statistics
# ---------------------------------------------------------------------------


@dataclass
class CollectionStats:
    """Tracks ingestion metrics for a single run."""

    observations_fetched: int = 0
    species_count: int = 0
    locations_count: int = 0
    notable_count: int = 0
    hotspots_fetched: int = 0
    api_calls: int = 0
    api_errors: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations_fetched": self.observations_fetched,
            "species_count": self.species_count,
            "locations_count": self.locations_count,
            "notable_count": self.notable_count,
            "hotspots_fetched": self.hotspots_fetched,
            "api_calls": self.api_calls,
            "api_errors": self.api_errors,
            "elapsed_seconds": round(self.elapsed_s, 1),
        }


# ---------------------------------------------------------------------------
# eBird API Client
# ---------------------------------------------------------------------------


class EBirdClient:
    """HTTP client for the eBird API 2.0.

    Handles authentication, retry with exponential back-off, and
    rate-limiting across all ``/v2/`` endpoints.

    Args:
        api_key: eBird API token. Falls back to ``EBIRD_API_KEY`` env var.
        max_retries: Number of retries on transient HTTP errors.
        timeout: Request timeout in seconds.
        historic_timeout: Historic observations request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_retries: int = 3,
        timeout: int | None = None,
        historic_timeout: int | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("EBIRD_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "eBird API key required. Set EBIRD_API_KEY in .env or pass api_key=."
            )
        self.max_retries = max_retries
        self.timeout = _resolve_request_timeout(timeout)
        self.historic_timeout = _resolve_request_timeout(
            historic_timeout,
            env_name=_HISTORIC_REQUEST_TIMEOUT_ENV,
            default_timeout=max(self.timeout, _DEFAULT_HISTORIC_REQUEST_TIMEOUT_S),
        )
        self._session = requests.Session()
        self._session.headers.update(
            {
                **HEADERS,
                "X-eBirdApiToken": self.api_key,
            }
        )

    # -- Core request helper -------------------------------------------

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Execute a GET request with retry and back-off.

        Args:
            path: URL path relative to ``BASE_URL`` (e.g. ``/data/obs/NL/recent``).
            params: Optional query parameters.
            timeout: Optional timeout override in seconds.

        Returns:
            Parsed JSON response (list or dict).

        Raises:
            requests.RequestException: After all retries are exhausted.
        """
        url = f"{BASE_URL}{path}"
        last_err: Exception | None = None
        effective_timeout = self.timeout if timeout is None else timeout

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(
                    url,
                    params=params,
                    timeout=effective_timeout,
                )
                resp.raise_for_status()
                time.sleep(_REQUEST_DELAY_S)
                return resp.json()
            except requests.exceptions.HTTPError as e:
                last_err = e
                status = e.response.status_code if e.response is not None else 0
                if status in {429, 500, 502, 503, 504}:
                    wait = 2**attempt
                    logger.warning(
                        "HTTP %d on %s – retrying in %ds (attempt %d/%d)",
                        status,
                        path,
                        wait,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(wait)
                else:
                    raise
            except requests.exceptions.RequestException as e:
                last_err = e
                wait = 2**attempt
                logger.warning(
                    "Request error on %s: %s – retrying in %ds (%d/%d)",
                    path,
                    e,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)

        raise last_err  # type: ignore[misc]

    # ==================================================================
    # data/obs – Observations
    # ==================================================================

    def recent_observations(
        self,
        region: str,
        *,
        back: int = 14,
        cat: str | None = None,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Recent observations in a region (up to 30 days).

        ``GET /data/obs/{regionCode}/recent``

        Args:
            region: Country, subnational1/2 or location code (e.g. ``NL``, ``US-NY``).
            back: Days back (1-30, default 14).
            cat: Taxonomic category filter (e.g. ``species``).
            hotspot: Only return hotspot observations.
            include_provisional: Include unreviewed observations.
            max_results: Limit results (1-10000).
            locale: Language for common names.

        Returns:
            List of observation dicts.
        """
        params: dict[str, Any] = {
            "back": min(max(back, 1), 30),
            "sppLocale": locale,
        }
        if cat:
            params["cat"] = cat
        if hotspot:
            params["hotspot"] = "true"
        if include_provisional:
            params["includeProvisional"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get(f"/data/obs/{region}/recent", params)

    def recent_species_observations(
        self,
        region: str,
        species_code: str,
        *,
        back: int = 14,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Recent observations of a specific species in a region.

        ``GET /data/obs/{regionCode}/recent/{speciesCode}``

        Args:
            region: Region code.
            species_code: eBird species code (e.g. ``eurbla`` for Eurasian Blackbird).
            back: Days back (1-30).
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of observation dicts.
        """
        params: dict[str, Any] = {
            "back": min(max(back, 1), 30),
            "sppLocale": locale,
        }
        if hotspot:
            params["hotspot"] = "true"
        if include_provisional:
            params["includeProvisional"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get(
            f"/data/obs/{region}/recent/{species_code}",
            params,
        )

    def recent_notable_observations(
        self,
        region: str,
        *,
        back: int = 14,
        detail: str = "simple",
        hotspot: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Recent notable (rare/unusual) observations in a region.

        ``GET /data/obs/{regionCode}/recent/notable``

        Args:
            region: Region code.
            back: Days back (1-30).
            detail: ``simple`` or ``full``.
            hotspot: Only hotspot observations.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of notable observation dicts.
        """
        params: dict[str, Any] = {
            "back": min(max(back, 1), 30),
            "detail": detail,
            "sppLocale": locale,
        }
        if hotspot:
            params["hotspot"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get(
            f"/data/obs/{region}/recent/notable",
            params,
        )

    def historic_observations(
        self,
        region: str,
        date: date,
        *,
        cat: str | None = None,
        detail: str = "simple",
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Historic observations on a specific date.

        ``GET /data/obs/{regionCode}/historic/{y}/{m}/{d}``

        Args:
            region: Region code.
            date: Calendar date.
            cat: Taxonomic category filter.
            detail: ``simple`` or ``full``.
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of observation dicts.
        """
        path = f"/data/obs/{region}/historic/{date.year}/{date.month}/{date.day}"
        params: dict[str, Any] = {
            "detail": detail,
            "sppLocale": locale,
        }
        if cat:
            params["cat"] = cat
        if hotspot:
            params["hotspot"] = "true"
        if include_provisional:
            params["includeProvisional"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get(path, params, timeout=self.historic_timeout)

    def nearby_observations(
        self,
        lat: float,
        lng: float,
        *,
        dist: int = 25,
        back: int = 14,
        cat: str | None = None,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Recent nearby observations within a radius.

        ``GET /data/obs/geo/recent``

        Args:
            lat: Latitude (-90 to 90).
            lng: Longitude (-180 to 180).
            dist: Radius in km (max 50).
            back: Days back (1-30).
            cat: Taxonomic category filter.
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of observation dicts.
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "dist": min(max(dist, 0), 50),
            "back": min(max(back, 1), 30),
            "sppLocale": locale,
        }
        if cat:
            params["cat"] = cat
        if hotspot:
            params["hotspot"] = "true"
        if include_provisional:
            params["includeProvisional"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get("/data/obs/geo/recent", params)

    def nearby_species_observations(
        self,
        species_code: str,
        lat: float,
        lng: float,
        *,
        dist: int = 25,
        back: int = 14,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Recent nearby observations of a specific species.

        ``GET /data/obs/geo/recent/{speciesCode}``

        Args:
            species_code: eBird species code.
            lat: Latitude.
            lng: Longitude.
            dist: Radius in km (max 50).
            back: Days back (1-30).
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of observation dicts.
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "dist": min(max(dist, 0), 50),
            "back": min(max(back, 1), 30),
            "sppLocale": locale,
        }
        if hotspot:
            params["hotspot"] = "true"
        if include_provisional:
            params["includeProvisional"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get(
            f"/data/obs/geo/recent/{species_code}",
            params,
        )

    def nearby_notable_observations(
        self,
        lat: float,
        lng: float,
        *,
        dist: int = 25,
        back: int = 14,
        detail: str = "simple",
        hotspot: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Recent notable observations near a location.

        ``GET /data/obs/geo/recent/notable``

        Args:
            lat: Latitude.
            lng: Longitude.
            dist: Radius in km (max 50).
            back: Days back (1-30).
            detail: ``simple`` or ``full``.
            hotspot: Only hotspot observations.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of notable observation dicts.
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "dist": min(max(dist, 0), 50),
            "back": min(max(back, 1), 30),
            "detail": detail,
            "sppLocale": locale,
        }
        if hotspot:
            params["hotspot"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get("/data/obs/geo/recent/notable", params)

    def nearest_species_observations(
        self,
        species_code: str,
        lat: float,
        lng: float,
        *,
        dist: int = 50,
        back: int = 14,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Nearest observations of a species (sorted by distance).

        ``GET /data/nearest/geo/recent/{speciesCode}``

        Args:
            species_code: eBird species code.
            lat: Latitude.
            lng: Longitude.
            dist: Max distance in km (max 50).
            back: Days back (1-30).
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results.
            locale: Language for common names.

        Returns:
            List of observation dicts sorted by distance.
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "dist": min(max(dist, 0), 50),
            "back": min(max(back, 1), 30),
            "sppLocale": locale,
        }
        if hotspot:
            params["hotspot"] = "true"
        if include_provisional:
            params["includeProvisional"] = "true"
        if max_results:
            params["maxResults"] = min(max_results, 10000)

        return self._get(
            f"/data/nearest/geo/recent/{species_code}",
            params,
        )

    # ==================================================================
    # product – Checklists, Species Lists, Top Contributors
    # ==================================================================

    def checklist(self, sub_id: str) -> dict[str, Any]:
        """View a specific checklist.

        ``GET /product/checklist/view/{subId}``

        Args:
            sub_id: Checklist identifier (e.g. ``S63619695``).

        Returns:
            Checklist dict with observations.
        """
        return self._get(f"/product/checklist/view/{sub_id}")

    def regional_species_list(
        self,
        region: str,
    ) -> list[str]:
        """Species ever recorded in a region.

        ``GET /product/spplist/{regionCode}``

        Args:
            region: Region code.

        Returns:
            List of species codes.
        """
        return self._get(f"/product/spplist/{region}")

    def top_100_contributors(
        self,
        region: str,
        date: date,
        *,
        ranked_by: str = "spp",
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Top 100 contributors for a region on a date.

        ``GET /product/top100/{regionCode}/{y}/{m}/{d}``

        Args:
            region: Region code.
            date: Calendar date.
            ranked_by: ``spp`` (species count) or ``cl`` (checklist count).
            max_results: Number of results (1-100).

        Returns:
            List of contributor dicts.
        """
        path = f"/product/top100/{region}/{date.year}/{date.month}/{date.day}"
        params = {"rankedBy": ranked_by, "maxResults": min(max_results, 100)}
        return self._get(path, params)

    def recent_checklists(
        self,
        region: str,
        *,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Recent checklists submitted in a region.

        ``GET /product/lists/{regionCode}``

        Args:
            region: Region code.
            max_results: Number of results (1-200).

        Returns:
            List of checklist summary dicts.
        """
        params = {"maxResults": min(max_results, 200)}
        return self._get(f"/product/lists/{region}", params)

    # ==================================================================
    # ref/hotspot – Hotspot Metadata
    # ==================================================================

    def hotspots_in_region(
        self,
        region: str,
        *,
        back: int | None = None,
        fmt: str = "json",
    ) -> list[dict[str, Any]]:
        """List hotspots in a region.

        ``GET /ref/hotspot/{regionCode}``

        Args:
            region: Region code.
            back: Only hotspots with recent observations (days, 1-30).
            fmt: Response format (``json`` or ``csv``).

        Returns:
            List of hotspot dicts.
        """
        params: dict[str, Any] = {"fmt": fmt}
        if back:
            params["back"] = min(max(back, 1), 30)
        return self._get(f"/ref/hotspot/{region}", params)

    def nearby_hotspots(
        self,
        lat: float,
        lng: float,
        *,
        dist: int = 25,
        back: int | None = None,
        fmt: str = "json",
    ) -> list[dict[str, Any]]:
        """List hotspots near a location.

        ``GET /ref/hotspot/geo``

        Args:
            lat: Latitude.
            lng: Longitude.
            dist: Radius in km (max 50).
            back: Only hotspots with recent observations (days, 1-30).
            fmt: Response format.

        Returns:
            List of hotspot dicts.
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lng": lng,
            "dist": min(max(dist, 0), 50),
            "fmt": fmt,
        }
        if back:
            params["back"] = min(max(back, 1), 30)
        return self._get("/ref/hotspot/geo", params)

    def hotspot_info(self, loc_id: str) -> dict[str, Any]:
        """Get information about a specific hotspot.

        ``GET /ref/hotspot/info/{locId}``

        Args:
            loc_id: Location identifier (e.g. ``L295658``).

        Returns:
            Hotspot info dict.
        """
        return self._get(f"/ref/hotspot/info/{loc_id}")

    # ==================================================================
    # ref/taxonomy – Species Taxonomy
    # ==================================================================

    def taxonomy(
        self,
        *,
        cat: str = "species",
        species: list[str] | None = None,
        locale: str = "en",
    ) -> list[dict[str, Any]]:
        """Get eBird taxonomy.

        ``GET /ref/taxonomy/ebird``

        Args:
            cat: Taxonomic category (``species``, ``issf``, ``slash``, etc.).
            species: Restrict to these species codes.
            locale: Language for common names.

        Returns:
            List of taxon dicts.
        """
        params: dict[str, Any] = {"cat": cat, "locale": locale, "fmt": "json"}
        if species:
            params["species"] = ",".join(species)
        return self._get("/ref/taxonomy/ebird", params)

    def taxonomy_forms(self, species_code: str) -> list[str]:
        """Get sub-species forms for a species.

        ``GET /ref/taxonomy/forms/{speciesCode}``

        Args:
            species_code: eBird species code.

        Returns:
            List of form codes.
        """
        return self._get(f"/ref/taxonomy/forms/{species_code}")

    def taxonomy_versions(self) -> list[dict[str, Any]]:
        """List available taxonomy versions.

        ``GET /ref/taxonomy/versions``

        Returns:
            List of version dicts.
        """
        return self._get("/ref/taxonomy/versions")

    # ==================================================================
    # ref/region – Region Info
    # ==================================================================

    def region_info(
        self,
        region: str,
        *,
        fmt: str = "json",
    ) -> dict[str, Any]:
        """Get information about a region.

        ``GET /ref/region/info/{regionCode}``

        Args:
            region: Region code.
            fmt: Response format.

        Returns:
            Region info dict.
        """
        return self._get(f"/ref/region/info/{region}", {"regionNameFormat": "full"})

    def sub_regions(
        self,
        region_type: str,
        parent_region: str,
    ) -> list[dict[str, Any]]:
        """List sub-regions of a given type within a parent region.

        ``GET /ref/region/list/{regionType}/{parentRegionCode}``

        Args:
            region_type: ``country``, ``subnational1``, or ``subnational2``.
            parent_region: Parent region code.

        Returns:
            List of sub-region dicts.
        """
        return self._get(f"/ref/region/list/{region_type}/{parent_region}")

    # ==================================================================
    # ref/geo – Adjacent Regions
    # ==================================================================

    def adjacent_regions(self, region: str) -> list[dict[str, Any]]:
        """Get regions adjacent to a given region.

        ``GET /ref/adjacent/{regionCode}``

        Args:
            region: Region code.

        Returns:
            List of adjacent region dicts.
        """
        return self._get(f"/ref/adjacent/{region}")


# ---------------------------------------------------------------------------
# Data Collector (Orchestrator)
# ---------------------------------------------------------------------------


class EBirdCollector:
    """Orchestrates eBird data collection and local persistence.

    Mirrors the ``AudioCollector`` pattern from the BirdWeather module:
    collects observations, writes JSONL + metadata, and optionally
    upserts to TimescaleDB.

    Args:
        output_dir: Root directory for raw data (``data/raw/ebird``).
        api_key: Optional override for eBird API key.
    """

    def __init__(
        self,
        output_dir: Path | str = "data/raw/ebird",
        api_key: str | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.client = EBirdClient(api_key=api_key)

    # -- Public methods ------------------------------------------------

    def collect_recent(
        self,
        region: str = DEFAULT_REGION,
        *,
        back: int = 14,
        species_codes: list[str] | None = None,
        notable: bool = False,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        target_species_only: bool = False,
        target: str = "local",
    ) -> CollectionStats:
        """Collect recent observations for a region.

        Args:
            region: eBird region code.
            back: Days back (1-30).
            species_codes: Filter to specific species codes.
            notable: Fetch notable observations instead.
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed observations.
            max_results: Limit total results.
            target_species_only: Only fetch target species from config.
            target: Storage target — ``"local"`` (default) or ``"database"``.

        Returns:
            Collection statistics.
        """
        persist_to_db = target == "database"
        save_local = target == "local"

        stats = CollectionStats()
        all_observations: list[dict[str, Any]] = []

        # Resolve species list
        codes_to_fetch = species_codes or []
        if target_species_only:
            target_sp = _load_target_species()
            codes_to_fetch = [s["ebird_code"] for s in target_sp if s.get("ebird_code")]
            if not codes_to_fetch:
                logger.warning(
                    "No eBird codes found in target_species.json – "
                    "fetching all species in region %s",
                    region,
                )

        logger.info(
            "Collecting %s observations for region=%s, back=%d days",
            "notable" if notable else "recent",
            region,
            back,
        )

        if notable:
            # Notable observations (no per-species endpoint)
            obs = self.client.recent_notable_observations(
                region,
                back=back,
                detail="full",
                hotspot=hotspot,
                max_results=max_results,
            )
            stats.api_calls += 1
            stats.notable_count = len(obs)
            all_observations.extend(obs)
            logger.info("Fetched %d notable observations", len(obs))

        elif codes_to_fetch:
            # Per-species fetching
            for code in tqdm(codes_to_fetch, desc="Species", unit="spp"):
                try:
                    obs = self.client.recent_species_observations(
                        region,
                        code,
                        back=back,
                        hotspot=hotspot,
                        include_provisional=include_provisional,
                        max_results=max_results,
                    )
                    stats.api_calls += 1
                    all_observations.extend(obs)
                    logger.debug("Species %s: %d observations", code, len(obs))
                except requests.HTTPError as e:
                    stats.api_errors += 1
                    logger.warning("Failed to fetch species %s: %s", code, e)
        else:
            # All species in region
            obs = self.client.recent_observations(
                region,
                back=back,
                hotspot=hotspot,
                include_provisional=include_provisional,
                max_results=max_results,
            )
            stats.api_calls += 1
            all_observations.extend(obs)
            logger.info("Fetched %d observations for region %s", len(obs), region)

        # Deduplicate by (subId, speciesCode)
        all_observations = self._deduplicate(all_observations)

        # Skip observations for days already marked complete on disk.
        # This avoids overwriting known-good files and redundant DB
        # upserts while still letting the API call refresh recent days.
        complete_days = self._find_complete_days(region, back) if save_local else set()
        if complete_days:
            before = len(all_observations)
            all_observations = [
                o
                for o in all_observations
                if o.get("obsDt", "")[:10] not in complete_days
            ]
            skipped = before - len(all_observations)
            if skipped:
                logger.info(
                    "Skipped %d observations from %d already-complete day(s).",
                    skipped,
                    len(complete_days),
                )

        stats.observations_fetched = len(all_observations)
        stats.species_count = len({o.get("speciesCode") for o in all_observations})
        stats.locations_count = len({o.get("locId") for o in all_observations})

        # Persist
        if save_local:
            self._save_local_by_date(all_observations, stats, region)

        if persist_to_db and all_observations:
            self._persist_to_db(all_observations, stats)

        self._log_summary(stats, region)
        return stats

    def _fetch_historic_day_observations(
        self,
        region: str,
        obs_date: date,
        *,
        hotspot: bool,
        include_provisional: bool,
        max_results: int | None,
    ) -> tuple[list[dict[str, Any]], int]:
        try:
            return (
                self.client.historic_observations(
                    region,
                    obs_date,
                    detail="simple",
                    hotspot=hotspot,
                    include_provisional=include_provisional,
                    max_results=max_results,
                ),
                1,
            )
        except requests.RequestException as exc:
            if "-" in region or len(region) != 2:
                raise

            subregions = self.client.sub_regions("subnational1", region)
            subregion_codes = [
                entry.get("code") for entry in subregions if entry.get("code")
            ]
            if not subregion_codes:
                raise

            logger.warning(
                "Historic fetch for %s on %s failed: %s; retrying %d subregion(s).",
                region,
                obs_date,
                exc,
                len(subregion_codes),
            )

            observations: list[dict[str, Any]] = []
            successful_calls = 0
            for subregion in subregion_codes:
                try:
                    observations.extend(
                        self.client.historic_observations(
                            subregion,
                            obs_date,
                            detail="simple",
                            hotspot=hotspot,
                            include_provisional=include_provisional,
                            max_results=max_results,
                        )
                    )
                    successful_calls += 1
                except requests.RequestException as sub_exc:
                    msg = (
                        f"Historic subregion fetch failed for {subregion} "
                        f"on {obs_date}: {sub_exc}"
                    )
                    raise requests.RequestException(msg) from sub_exc

            return observations, successful_calls

    def collect_historic(
        self,
        region: str = DEFAULT_REGION,
        *,
        obs_date: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        species_codes: list[str] | None = None,
        target_species_only: bool = False,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        skip_existing: bool = True,
        target: str = "local",
    ) -> CollectionStats:
        """Collect historic observations for specific date(s).

        Each day is fetched, filtered, and saved independently so that
        already-downloaded days can be skipped (resumable).

        Args:
            region: eBird region code.
            obs_date: Single date to fetch.
            start_date: Start of date range.
            end_date: End of date range.
            species_codes: Filter to specific species codes.
            target_species_only: Only keep target species from config.
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results per day.
            skip_existing: Skip days that already have data on disk.
            target: ``"local"`` (disk only) or ``"database"`` (disk + DB).

        Returns:
            Collection statistics.
        """
        persist_to_db = target == "database"
        save_local = target == "local"

        stats = CollectionStats()

        # Resolve species filter
        codes_filter: set[str] = set()
        if species_codes:
            codes_filter = {c.lower() for c in species_codes}
        elif target_species_only:
            target_sp = _load_target_species()
            codes_filter = {
                s["ebird_code"].lower() for s in target_sp if s.get("ebird_code")
            }
            if not codes_filter:
                logger.warning(
                    "No eBird codes in target_species.json – fetching all species",
                )

        # Determine dates
        dates: list[date] = []
        if obs_date:
            dates = [obs_date]
        elif start_date and end_date:
            current = start_date
            while current <= end_date:
                dates.append(current)
                current += timedelta(days=1)
        else:
            dates = [date.today()]

        # Skip days that already exist on disk
        skipped = 0
        if skip_existing and save_local:
            remaining: list[date] = []
            for d in dates:
                day_dir = self.output_dir / region / d.isoformat()
                if (day_dir / "observations.jsonl").exists():
                    skipped += 1
                else:
                    remaining.append(d)
            if skipped:
                logger.info(
                    "Skipping %d day(s) already on disk, %d remaining",
                    skipped,
                    len(remaining),
                )
            dates = remaining

        if not dates:
            logger.info("All %d day(s) already downloaded – nothing to do", skipped)
            return stats

        logger.info(
            "Collecting historic observations: %d day(s) for region=%s",
            len(dates),
            region,
        )

        all_species: set[str] = set()
        all_locations: set[str] = set()

        for d in tqdm(dates, desc="Days", unit="day"):
            try:
                obs, api_calls = self._fetch_historic_day_observations(
                    region,
                    d,
                    hotspot=hotspot,
                    include_provisional=include_provisional,
                    max_results=max_results,
                )
                stats.api_calls += api_calls
            except requests.RequestException as e:
                stats.api_errors += 1
                logger.warning("Failed date %s: %s", d, e)
                continue

            # Filter to requested species
            if codes_filter:
                obs = [
                    o
                    for o in obs
                    if (o.get("speciesCode") or "").lower() in codes_filter
                ]

            obs = self._deduplicate(obs)
            logger.debug("Date %s: %d observations", d, len(obs))

            stats.observations_fetched += len(obs)
            all_species.update(o.get("speciesCode", "") for o in obs)
            all_locations.update(o.get("locId", "") for o in obs)

            # Save this day immediately
            if save_local and obs:
                day_stats = CollectionStats(
                    observations_fetched=len(obs),
                    species_count=len({o.get("speciesCode") for o in obs}),
                    locations_count=len({o.get("locId") for o in obs}),
                    api_calls=api_calls,
                )
                self._save_local(obs, day_stats, region, d.isoformat())

            if persist_to_db and obs:
                self._persist_to_db(obs, stats)

        stats.species_count = len(all_species - {""})
        stats.locations_count = len(all_locations - {""})
        self._log_summary(stats, region)
        return stats

    def collect_nearby(
        self,
        lat: float,
        lng: float,
        *,
        dist: int = 25,
        back: int = 14,
        species_codes: list[str] | None = None,
        notable: bool = False,
        hotspot: bool = False,
        include_provisional: bool = False,
        max_results: int | None = None,
        target: str = "local",
    ) -> CollectionStats:
        """Collect observations near a geographic point.

        Args:
            lat: Latitude.
            lng: Longitude.
            dist: Search radius in km (max 50).
            back: Days back (1-30).
            species_codes: Filter to specific species.
            notable: Fetch notable observations.
            hotspot: Only hotspot observations.
            include_provisional: Include unreviewed.
            max_results: Limit results.
            target: ``"local"`` (disk only) or ``"database"`` (disk + DB).

        Returns:
            Collection statistics.
        """
        persist_to_db = target == "database"
        save_local = target == "local"

        stats = CollectionStats()
        all_observations: list[dict[str, Any]] = []

        logger.info(
            "Collecting nearby observations: lat=%.4f, lng=%.4f, dist=%dkm, back=%dd",
            lat,
            lng,
            dist,
            back,
        )

        if notable:
            obs = self.client.nearby_notable_observations(
                lat,
                lng,
                dist=dist,
                back=back,
                detail="full",
                hotspot=hotspot,
                max_results=max_results,
            )
            stats.api_calls += 1
            stats.notable_count = len(obs)
            all_observations.extend(obs)
        elif species_codes:
            for code in tqdm(species_codes, desc="Species", unit="spp"):
                try:
                    obs = self.client.nearby_species_observations(
                        code,
                        lat,
                        lng,
                        dist=dist,
                        back=back,
                        hotspot=hotspot,
                        include_provisional=include_provisional,
                        max_results=max_results,
                    )
                    stats.api_calls += 1
                    all_observations.extend(obs)
                except requests.HTTPError as e:
                    stats.api_errors += 1
                    logger.warning("Failed species %s: %s", code, e)
        else:
            obs = self.client.nearby_observations(
                lat,
                lng,
                dist=dist,
                back=back,
                hotspot=hotspot,
                include_provisional=include_provisional,
                max_results=max_results,
            )
            stats.api_calls += 1
            all_observations.extend(obs)

        all_observations = self._deduplicate(all_observations)
        stats.observations_fetched = len(all_observations)
        stats.species_count = len({o.get("speciesCode") for o in all_observations})
        stats.locations_count = len({o.get("locId") for o in all_observations})

        if save_local:
            self._save_local_by_date(
                all_observations,
                stats,
                f"geo_{lat:.2f}_{lng:.2f}",
            )

        if persist_to_db and all_observations:
            self._persist_to_db(all_observations, stats)

        self._log_summary(stats, f"({lat:.4f}, {lng:.4f})")
        return stats

    def collect_hotspots(
        self,
        region: str = DEFAULT_REGION,
        *,
        back: int | None = None,
        target: str = "local",
    ) -> CollectionStats:
        """Collect hotspot metadata for a region.

        Args:
            region: eBird region code.
            back: Only hotspots with observations in last N days.
            target: ``"local"`` (disk only) or ``"database"`` (disk + DB).

        Returns:
            Collection statistics.
        """
        save_local = target == "local"

        stats = CollectionStats()
        logger.info("Collecting hotspots for region=%s", region)

        hotspots = self.client.hotspots_in_region(region, back=back)
        stats.api_calls += 1
        stats.hotspots_fetched = len(hotspots)

        if save_local:
            directory = self.output_dir / "hotspots"
            directory.mkdir(parents=True, exist_ok=True)
            out_file = directory / f"{region}_hotspots.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(hotspots, f, indent=2, default=str)
            logger.info("Saved %d hotspots to %s", len(hotspots), out_file)

        # Persist to TimescaleDB (hotspot data merged into ebird_tbl)
        if target == "database" and hotspots:
            logger.info(
                "DB target selected: hotspot metadata is not persisted "
                "to disk or DB for %d hotspots.",
                len(hotspots),
            )

        logger.info("Hotspots: %d fetched for %s", stats.hotspots_fetched, region)
        return stats

    # -- Private helpers -----------------------------------------------

    def _find_complete_days(self, region: str, back: int) -> set[str]:
        """Return date labels (YYYY-MM-DD) already marked complete on disk."""
        complete: set[str] = set()
        today = date.today()
        for i in range(back):
            d = (today - timedelta(days=i)).isoformat()
            meta_path = self.output_dir / region / d / "metadata.json"
            if meta_path.exists():
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        if json.load(fh).get("completed"):
                            complete.add(d)
                except (json.JSONDecodeError, KeyError, OSError):
                    pass
        return complete

    def _save_local_by_date(
        self,
        observations: list[dict[str, Any]],
        stats: CollectionStats,
        region_label: str,
    ) -> None:
        """Group observations by observation date and save each day separately."""
        from collections import defaultdict

        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for obs in observations:
            obs_dt = obs.get("obsDt", "")
            # obsDt is typically "YYYY-MM-DD HH:MM" — extract date part
            day = obs_dt[:10] if len(obs_dt) >= 10 else "unknown"
            by_date[day].append(obs)

        for day_label in sorted(by_date):
            day_obs = by_date[day_label]
            day_stats = CollectionStats(
                observations_fetched=len(day_obs),
                species_count=len({o.get("speciesCode") for o in day_obs}),
                locations_count=len({o.get("locId") for o in day_obs}),
                api_calls=stats.api_calls,
                api_errors=stats.api_errors,
            )
            self._save_local(day_obs, day_stats, region_label, day_label)

        logger.info(
            "Saved observations across %d day(s) for %s",
            len(by_date),
            region_label,
        )

    @staticmethod
    def _deduplicate(
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove duplicate observations based on (subId, speciesCode)."""
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for obs in observations:
            key = (obs.get("subId", ""), obs.get("speciesCode", ""))
            if key not in seen:
                seen.add(key)
                unique.append(obs)
        return unique

    def _save_local(
        self,
        observations: list[dict[str, Any]],
        stats: CollectionStats,
        region_label: str,
        date_label: str,
    ) -> None:
        """Write observations as JSONL and metadata as JSON."""
        directory = self.output_dir / region_label / date_label
        directory.mkdir(parents=True, exist_ok=True)

        # Observations JSONL
        obs_file = directory / "observations.jsonl"
        with open(obs_file, "w", encoding="utf-8") as f:
            for obs in observations:
                obs["_collected_at"] = datetime.now().isoformat()
                f.write(json.dumps(obs, default=str) + "\n")
        logger.info("Saved %d observations to %s", len(observations), obs_file)

        # Species summary
        species_summary = {}
        for obs in observations:
            code = obs.get("speciesCode", "unknown")
            if code not in species_summary:
                species_summary[code] = {
                    "speciesCode": code,
                    "comName": obs.get("comName", ""),
                    "sciName": obs.get("sciName", ""),
                    "count": 0,
                }
            species_summary[code]["count"] += 1

        species_file = directory / "species_list.json"
        with open(species_file, "w", encoding="utf-8") as f:
            json.dump(list(species_summary.values()), f, indent=2)

        # Metadata
        metadata = {
            "region": region_label,
            "date": date_label,
            "generated_at": datetime.now().isoformat(),
            "stats": stats.to_dict(),
            "species_distribution": {
                code: info["count"] for code, info in species_summary.items()
            },
            "completed": True,
        }
        meta_file = directory / "metadata.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, default=str)

    def _persist_to_db(
        self,
        observations: list[dict[str, Any]],
        stats: CollectionStats,
    ) -> None:
        """Upsert observations to TimescaleDB via the DB loader."""
        try:
            from avian_biosurveillance.db.loaders.ebird import upsert_ebird_observations
            from avian_biosurveillance.db.models.ebird import EBirdObservation

            records = []
            for obs in observations:
                try:
                    records.append(EBirdObservation.from_api(obs))
                except Exception as e:
                    logger.debug("Skipping malformed observation: %s", e)

            if records:
                count = upsert_ebird_observations(records)
                logger.info("Upserted %d eBird observations to DB", count)

        except ImportError:
            logger.error(
                "DB modules not available. Install psycopg2-binary and "
                "ensure database is configured."
            )
        except Exception as e:
            logger.error("DB upsert failed: %s", e)

    @staticmethod
    def _log_summary(stats: CollectionStats, label: str) -> None:
        """Log a summary banner."""
        logger.info("\n" + "=" * 60)
        logger.info("eBIRD COLLECTION COMPLETE – %s", label)
        logger.info("=" * 60)
        logger.info("Observations : %d", stats.observations_fetched)
        logger.info("Species      : %d", stats.species_count)
        logger.info("Locations    : %d", stats.locations_count)
        if stats.notable_count:
            logger.info("Notable      : %d", stats.notable_count)
        if stats.hotspots_fetched:
            logger.info("Hotspots     : %d", stats.hotspots_fetched)
        logger.info("API calls    : %d (errors: %d)", stats.api_calls, stats.api_errors)
        logger.info("Elapsed      : %.1fs", stats.elapsed_s)
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint for eBird data ingestion."""
    parser = argparse.ArgumentParser(
        description="eBird API 2.0 Data Ingestion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Recent observations in the Netherlands
  python -m avian_biosurveillance.ingestion.ebird --region NL --back 14

  # Recent observations of a specific species
  python -m avian_biosurveillance.ingestion.ebird --region NL --species eurbla

  # Target species only (from config/target_species.json)
  python -m avian_biosurveillance.ingestion.ebird --region NL --target-species

  # Notable (rare) observations
  python -m avian_biosurveillance.ingestion.ebird --region NL --notable --back 7

  # Historic observations on a date
  python -m avian_biosurveillance.ingestion.ebird --region NL --date 2026-01-15

  # Historic date range
  python -m avian_biosurveillance.ingestion.ebird --region NL --start-date 2026-01-01 --end-date 2026-01-15

  # Historic date range for target species only
  python -m avian_biosurveillance.ingestion.ebird --region NL --start-date 2026-01-01 --end-date 2026-01-15 --target-species

  # Nearby observations (lat/lon + radius)
  python -m avian_biosurveillance.ingestion.ebird --lat 52.37 --lon 4.89 --dist 25

  # Hotspot metadata
  python -m avian_biosurveillance.ingestion.ebird --hotspots NL

  # Persist to TimescaleDB
  python -m avian_biosurveillance.ingestion.ebird --region NL --persist-to-db

Data Structure:
  data/raw/ebird/
  └── NL/
      ├── 2026-03-12/
      │   ├── observations.jsonl
      │   ├── species_list.json
      │   └── metadata.json
      └── 2026-03-13/
          ├── observations.jsonl
          ├── species_list.json
          └── metadata.json
        """,
    )

    # Mode selection
    parser.add_argument(
        "--region",
        type=str,
        help="eBird region code (e.g. NL, US-NY, US-NY-109). Default: NL",
    )
    parser.add_argument(
        "--hotspots",
        type=str,
        metavar="REGION",
        help="Fetch hotspot metadata for a region",
    )

    # Species filters
    parser.add_argument(
        "--species",
        type=str,
        action="append",
        help="eBird species code to filter (repeatable, e.g. --species eurbla --species sonthr1)",
    )
    parser.add_argument(
        "--target-species",
        action="store_true",
        help="Only fetch species from config/target_species.json",
    )

    # Time filters
    parser.add_argument(
        "--back",
        type=int,
        default=14,
        help="Days back for recent observations (1-30, default: 14)",
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Historic observations on this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start of historic date range (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End of historic date range (YYYY-MM-DD)",
    )

    # Geo filters (nearby mode)
    parser.add_argument("--lat", type=float, help="Latitude for nearby search")
    parser.add_argument("--lon", type=float, help="Longitude for nearby search")
    parser.add_argument(
        "--dist",
        type=int,
        default=25,
        help="Search radius in km for nearby mode (max 50, default: 25)",
    )

    # Observation filters
    parser.add_argument(
        "--notable",
        action="store_true",
        help="Fetch notable (rare/unusual) observations",
    )
    parser.add_argument(
        "--hotspot-only",
        action="store_true",
        help="Only return observations from hotspots",
    )
    parser.add_argument(
        "--include-provisional",
        action="store_true",
        help="Include unreviewed observations",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        help="Limit number of observations per request (1-10000)",
    )

    # Output options
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/raw/ebird",
        help="Output directory (default: data/raw/ebird)",
    )
    parser.add_argument(
        "--target",
        choices=["local", "database"],
        default="local",
        help="Storage target: local (disk only, default) or database (TimescaleDB only)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download days that already exist on disk (default: skip them)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    collector = EBirdCollector(
        output_dir=Path(args.output_dir),
    )

    # --- Hotspot mode ---
    if args.hotspots:
        collector.collect_hotspots(
            region=args.hotspots,
            back=args.back if args.back != 14 else None,
            target=args.target,
        )
        return

    # --- Nearby mode ---
    if args.lat is not None and args.lon is not None:
        collector.collect_nearby(
            lat=args.lat,
            lng=args.lon,
            dist=args.dist,
            back=args.back,
            species_codes=args.species,
            notable=args.notable,
            hotspot=args.hotspot_only,
            include_provisional=args.include_provisional,
            max_results=args.max_results,
            target=args.target,
        )
        return

    # --- Historic mode ---
    if args.date or (args.start_date and args.end_date):
        obs_date = None
        start_date = None
        end_date = None

        if args.date:
            obs_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.start_date:
            start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        if args.end_date:
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()

        collector.collect_historic(
            region=args.region or DEFAULT_REGION,
            obs_date=obs_date,
            start_date=start_date,
            end_date=end_date,
            species_codes=args.species,
            target_species_only=args.target_species,
            hotspot=args.hotspot_only,
            include_provisional=args.include_provisional,
            max_results=args.max_results,
            skip_existing=not args.no_skip_existing,
            target=args.target,
        )
        return

    # --- Default: recent mode ---
    collector.collect_recent(
        region=args.region or DEFAULT_REGION,
        back=args.back,
        species_codes=args.species,
        notable=args.notable,
        hotspot=args.hotspot_only,
        include_provisional=args.include_provisional,
        max_results=args.max_results,
        target_species_only=args.target_species,
        target=args.target,
    )


if __name__ == "__main__":
    main()
