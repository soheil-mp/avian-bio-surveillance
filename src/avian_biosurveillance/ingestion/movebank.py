"""Movebank REST API Ingestion Module.

Connects to the `Movebank REST API <https://github.com/movebank/movebank-api-doc>`_
to fetch animal tracking data (GPS locations, accelerometer, etc.) for bird
movement studies relevant to avian biosurveillance.

Key capabilities:

* **Study discovery** – search & list accessible tracking studies by taxon.
* **Event data** – GPS locations and sensor measurements per individual.
* **Reference data** – individual, tag, deployment, and sensor metadata.
* **Reduced profiles** – daily, long-distance, 30-day, or 0.25°-grid events.
* **Incremental sync** – fetch only events updated since a given timestamp.

Data is saved to ``data/raw/movebank/`` as JSONL (events) or JSON (metadata).
Optionally, events can be upserted into TimescaleDB via ``--persist-to-db``.

Usage::

    # List accessible studies containing target species
    python -m avian_biosurveillance.ingestion.movebank --list-studies

    # Download GPS events for a study
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040

    # Download GPS events for a study with specific individuals
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040 --individual "4264-84830852"

    # Download GPS events for a time range
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040 --start-date 2026-01-01 --end-date 2026-01-31

    # Search studies by taxon name
    python -m avian_biosurveillance.ingestion.movebank --search-taxon "Turdus merula"

    # Download reference data (individuals, tags, deployments) for a study
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040 --reference-data

    # Download all target-species studies (default when no mode specified)
    python -m avian_biosurveillance.ingestion.movebank --target-species

    # Download from ALL accessible studies
    python -m avian_biosurveillance.ingestion.movebank --all-studies

    # Collect from every available sensor type (not just first match)
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040 --all-sensors

    # Use reduced data profile (daily events)
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040 --reduction-profile EURING_01

    # Persist to TimescaleDB
    python -m avian_biosurveillance.ingestion.movebank --study-id 2911040 --persist-to-db
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
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

BASE_URL = "https://www.movebank.org/movebank/service"

# Rate-limit: Movebank allows 1 concurrent request per IP.
_REQUEST_DELAY_S = 1.0

# GPS sensor type ID
GPS_SENSOR_TYPE_ID = 653
_DEFAULT_MOVEBANK_TIMEOUT_SECONDS = 180

# Sensor types that commonly produce location data
LOCATION_SENSOR_TYPES: dict[int, str] = {
    653: "GPS",
    673: "Radio Transmitter",
    82798: "Argos Doppler Shift",
    2299894820: "Sigfox Geolocation",
    3886361: "Solar Geolocator",
    2365683: "Acceleration",
    397: "Bird Ring",
}

# Reverse lookup: case-insensitive sensor name → sensor type ID.
# Used to parse the ``sensor_type_ids`` string from study metadata
# (e.g. ``"GPS,Acceleration"``) into numeric IDs.
_SENSOR_NAME_TO_ID: dict[str, int] = {
    name.lower(): sid for sid, name in LOCATION_SENSOR_TYPES.items()
}

# Movebank timestamp format: yyyyMMddHHmmssSSS
_MB_TS_FMT = "%Y%m%d%H%M%S000"

# Recommended event attributes for GPS data (flattened into movebank_tbl columns)
DEFAULT_EVENT_ATTRIBUTES = (
    "individual_local_identifier",
    "tag_local_identifier",
    "individual_taxon_canonical_name",
    "timestamp",
    "location_long",
    "location_lat",
    "visible",
    "ground_speed",
    "heading",
    "height_above_ellipsoid",
    "sensor_type_id",
    "individual_id",
    "deployment_id",
    "tag_id",
    "event_id",
    # Event-level quality / provenance
    "update_ts",
    "import_marked_outlier",
    "comments",
    # Extended altitude & barometry (sensor-agnostic)
    "height_above_msl",
    "barometric_pressure",
    # Environmental tag sensors
    "external_temperature",
)

# Reduction profiles
REDUCTION_PROFILES = {
    "EURING_01": "Daily events (≥24h between events)",
    "EURING_02": "Long-distance events (≥50km between events)",
    "EURING_03": "Events for last 30 days",
    "EURING_04": "0.25-degree grid events",
}

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


def _target_movebank_taxa() -> set[str]:
    """Return normalized Movebank taxon names allowed in source rows."""
    taxa: set[str] = set()
    for species in _load_target_species():
        taxon = species.get("movebank_taxon") or species.get("scientific")
        if taxon:
            taxa.add(str(taxon).strip().lower())
    return taxa


def _mb_timestamp(dt: date | datetime) -> str:
    """Convert a date or datetime to Movebank timestamp format."""
    if isinstance(dt, datetime):
        return dt.strftime(_MB_TS_FMT)
    return datetime(dt.year, dt.month, dt.day).strftime(_MB_TS_FMT)


def _mb_timestamp_end(dt: date | datetime) -> str:
    """Convert an inclusive end bound to Movebank timestamp format.

    Date inputs represent whole calendar days in the orchestration layer, so
    the API end bound needs to advance to the next midnight to include the
    requested final day.
    """
    if isinstance(dt, datetime):
        return dt.strftime(_MB_TS_FMT)
    return (datetime(dt.year, dt.month, dt.day) + timedelta(days=1)).strftime(
        _MB_TS_FMT
    )


# ---------------------------------------------------------------------------
# Collection Statistics
# ---------------------------------------------------------------------------


@dataclass
class CollectionStats:
    """Tracks ingestion metrics for a single run."""

    events_fetched: int = 0
    studies_found: int = 0
    individuals_count: int = 0
    deployments_count: int = 0
    api_calls: int = 0
    api_errors: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_fetched": self.events_fetched,
            "studies_found": self.studies_found,
            "individuals_count": self.individuals_count,
            "deployments_count": self.deployments_count,
            "api_calls": self.api_calls,
            "api_errors": self.api_errors,
            "elapsed_seconds": round(self.elapsed_s, 1),
        }


# ---------------------------------------------------------------------------
# Movebank API Client
# ---------------------------------------------------------------------------


class MovebankClient:
    """HTTP client for the Movebank REST API.

    Handles authentication (HTTP Basic or token-based), retry with
    exponential back-off, CSV response parsing, and rate-limiting.

    Args:
        username: Movebank account username. Falls back to ``MOVEBANK_USERNAME``.
        password: Movebank account password. Falls back to ``MOVEBANK_PASSWORD``.
        max_retries: Number of retries on transient HTTP errors.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        max_retries: int = 5,
        timeout: int | None = None,
    ) -> None:
        self.username = username or os.environ.get("MOVEBANK_USERNAME", "")
        self.password = password or os.environ.get("MOVEBANK_PASSWORD", "")
        if not self.username or not self.password:
            raise ValueError(
                "Movebank credentials required. Set MOVEBANK_USERNAME and "
                "MOVEBANK_PASSWORD in .env or pass username=/password=."
            )
        self.max_retries = max_retries
        if timeout is None:
            raw_timeout = os.environ.get(
                "MOVEBANK_REQUEST_TIMEOUT_SECONDS",
                str(_DEFAULT_MOVEBANK_TIMEOUT_SECONDS),
            )
            try:
                timeout = max(1, int(raw_timeout))
            except ValueError:
                timeout = _DEFAULT_MOVEBANK_TIMEOUT_SECONDS
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = (self.username, self.password)
        self._session.headers.update(
            {
                "User-Agent": "AvianBiosurveillance/2.0 (Research)",
                "Accept": "text/csv, application/json",
            }
        )
        self._api_token: str | None = None

    # -- Core request helpers ------------------------------------------

    def _get_csv(
        self,
        params: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Execute a GET request to the direct-read endpoint, parse CSV.

        Args:
            params: Query parameters for the request.

        Returns:
            List of dicts (one per CSV row).

        Raises:
            requests.HTTPError: After all retries are exhausted.
        """
        url = f"{BASE_URL}/direct-read"
        last_err: Exception | None = None
        req_params = dict(params)  # copy to avoid mutating caller's dict

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(
                    url,
                    params=req_params,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                time.sleep(_REQUEST_DELAY_S)

                # Check for license-terms prompt
                if "accept-license" in resp.headers:
                    return self._handle_license(resp, req_params)

                # Check response body
                text = resp.text.strip()
                if "No data available" in text:
                    logger.debug("No data available: %s", text[:200])
                    return []
                if text.startswith("<"):
                    # HTML body on a 200 means Movebank returned an error/rate-limit
                    # page rather than CSV data.  Treat it as a retryable failure.
                    logger.warning(
                        "HTML response on attempt %d/%d (likely rate-limited or "
                        "auth issue): %s",
                        attempt,
                        self.max_retries,
                        text[:300],
                    )
                    last_err = RuntimeError(f"Unexpected HTML response: {text[:120]}")
                    wait = 60 * attempt  # same cadence as 429
                    time.sleep(wait)
                    continue

                return self._parse_csv(text)

            except requests.exceptions.HTTPError as e:
                last_err = e
                status = e.response.status_code if e.response is not None else 0

                if status == 403:
                    resp_body = e.response.text[:500] if e.response is not None else ""
                    # License terms may arrive on a 403
                    if e.response is not None and "License Terms:" in e.response.text:
                        logger.info("403 with license terms – accepting")
                        return self._handle_license(e.response, req_params)

                    # Try token-based auth on first 403
                    if self._api_token is None:
                        try:
                            self.request_token()
                            req_params["api-token"] = self._api_token
                            logger.info("Obtained API token, retrying request")
                        except Exception as tok_err:
                            logger.warning("Token request also failed: %s", tok_err)

                    wait = 30 * attempt
                    logger.warning(
                        "HTTP 403 – retrying in %ds (attempt %d/%d): %s",
                        wait,
                        attempt,
                        self.max_retries,
                        resp_body[:200],
                    )
                    time.sleep(wait)

                elif status in {429, 500, 502, 503, 504}:
                    # 429 / 503: use a long cooldown — Movebank outages
                    # and rate-limit windows can last several minutes.
                    # 500/502/504: moderate exponential backoff.
                    if status in {429, 503}:
                        wait = 60 * attempt
                    else:
                        wait = min(30 * attempt, 180)
                    logger.warning(
                        "HTTP %d – retrying in %ds (attempt %d/%d)",
                        status,
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
                    "Request error: %s – retrying in %ds (%d/%d)",
                    e,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)

        # Provide a helpful message for persistent 403
        if isinstance(last_err, requests.exceptions.HTTPError):
            status = (
                last_err.response.status_code if last_err.response is not None else 0
            )
            if status == 403:
                raise requests.exceptions.HTTPError(
                    f"403 Forbidden after {self.max_retries} attempts. "
                    "Verify MOVEBANK_USERNAME / MOVEBANK_PASSWORD in .env, "
                    "ensure the account is active at movebank.org, and check "
                    "that the requested studies are accessible.",
                    response=last_err.response,
                ) from last_err

        raise last_err  # type: ignore[misc]

    def _get_json(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Execute a GET request to the json-auth endpoint.

        Args:
            params: Query parameters for the request.

        Returns:
            Parsed JSON response.
        """
        url = f"{BASE_URL}/json-auth"
        last_err: Exception | None = None
        req_params = dict(params)

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(
                    url,
                    params=req_params,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                time.sleep(_REQUEST_DELAY_S)
                return resp.json()

            except requests.exceptions.HTTPError as e:
                last_err = e
                status = e.response.status_code if e.response is not None else 0

                if status == 403:
                    if self._api_token is None:
                        try:
                            self.request_token()
                            req_params["api-token"] = self._api_token
                            logger.info("Obtained API token, retrying JSON request")
                        except Exception as tok_err:
                            logger.warning("Token request also failed: %s", tok_err)
                    wait = 30 * attempt
                    logger.warning(
                        "HTTP 403 on JSON – retrying in %ds (%d/%d)",
                        wait,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(wait)

                elif status in {429, 500, 502, 503, 504}:
                    wait = 60 * attempt if status == 429 else 2**attempt
                    logger.warning(
                        "HTTP %d on JSON – retrying in %ds (%d/%d)",
                        status,
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
                    "Request error: %s – retrying in %ds (%d/%d)",
                    e,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)

        if isinstance(last_err, requests.exceptions.HTTPError):
            status = (
                last_err.response.status_code if last_err.response is not None else 0
            )
            if status == 403:
                raise requests.exceptions.HTTPError(
                    f"403 Forbidden after {self.max_retries} attempts. "
                    "Verify MOVEBANK_USERNAME / MOVEBANK_PASSWORD in .env.",
                    response=last_err.response,
                ) from last_err

        raise last_err  # type: ignore[misc]

    @staticmethod
    def _parse_csv(text: str) -> list[dict[str, str]]:
        """Parse CSV text into list of dicts."""
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)

    def _handle_license(
        self,
        resp: requests.Response,
        params: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Accept license terms and retry the request.

        Movebank may require users to accept study-specific license terms
        before data can be accessed. The md5 hash of the terms is sent back.
        """
        license_text = resp.text
        md5_hash = hashlib.md5(license_text.encode("utf-8")).hexdigest()
        logger.info("Accepting license terms (md5=%s) for study", md5_hash)

        params_with_license = {**params, "license-md5": md5_hash}
        resp2 = self._session.get(
            f"{BASE_URL}/direct-read",
            params=params_with_license,
            timeout=self.timeout,
        )
        resp2.raise_for_status()
        time.sleep(_REQUEST_DELAY_S)

        text = resp2.text.strip()
        if text.startswith("<") or "No data available" in text:
            return []
        return self._parse_csv(text)

    # -- Token authentication ------------------------------------------

    def request_token(self) -> str:
        """Request an API token for token-based authentication.

        Returns:
            API token string.
        """
        resp = self._session.get(
            f"{BASE_URL}/direct-read",
            params={"service": "request-token"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        self._api_token = resp.text.strip()
        logger.info("Obtained Movebank API token")
        return self._api_token

    # ==================================================================
    # Study discovery
    # ==================================================================

    def list_studies(
        self,
        *,
        i_have_download_access: bool = False,
        i_can_see_data: bool = False,
        attributes: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """List studies available in Movebank.

        Args:
            i_have_download_access: Only studies with download permission.
            i_can_see_data: Only studies with view permission.
            attributes: Specific study attributes to return.

        Returns:
            List of study dicts.
        """
        params: dict[str, Any] = {"entity_type": "study"}
        if i_have_download_access:
            params["i_have_download_access"] = "true"
        if i_can_see_data:
            params["i_can_see_data"] = "true"
        if attributes:
            params["attributes"] = ",".join(attributes)
        try:
            return self._get_csv(params)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 403 and i_have_download_access and not i_can_see_data:
                fallback_params = dict(params)
                fallback_params.pop("i_have_download_access", None)
                fallback_params["i_can_see_data"] = "true"
                logger.warning(
                    "Movebank rejected study discovery with "
                    "i_have_download_access=true; retrying with "
                    "i_can_see_data=true instead."
                )
                return self._get_csv(fallback_params)
            raise

    def get_study(self, study_id: int) -> list[dict[str, str]]:
        """Get metadata for a specific study.

        Args:
            study_id: Movebank study ID.

        Returns:
            Study metadata dict(s).
        """
        return self._get_csv(
            {
                "entity_type": "study",
                "study_id": study_id,
            }
        )

    # Attributes fetched during study discovery / search.
    STUDY_SEARCH_ATTRS: list[str] = [
        "id",
        "name",
        "taxon_ids",
        "sensor_type_ids",
        "number_of_deployed_locations",
        "number_of_individuals",
        "main_location_lat",
        "main_location_long",
        "timestamp_first_deployed_location",
        "timestamp_last_deployed_location",
        "principal_investigator_name",
        "study_objective",
    ]

    def search_studies_by_taxon(
        self,
        taxon_name: str,
        *,
        _cached_studies: list[dict[str, str]] | None = None,
        _taxon_id_cache: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """Search for studies containing a specific taxon.

        Searches the study ``taxon_ids``, ``name``, and
        ``study_objective`` fields.  The ``taxon_ids`` column contains
        **comma-separated scientific names** (e.g.
        ``"Turdus merula,Sturnus vulgaris"``), not numeric ITIS IDs.

        Args:
            taxon_name: Scientific name to search for (case-insensitive).
            _cached_studies: Pre-fetched study list to avoid repeated
                API calls when searching for multiple species.

        Returns:
            Matching study dicts.
        """
        all_studies = _cached_studies or self.list_studies(
            i_have_download_access=True,
            attributes=self.STUDY_SEARCH_ATTRS,
        )
        needle = taxon_name.strip().lower()

        matches: list[dict[str, str]] = []
        for s in all_studies:
            name_lower = (s.get("name", "") or "").lower()
            objective_lower = (s.get("study_objective", "") or "").lower()
            raw_taxon_ids = (s.get("taxon_ids", "") or "").lower()

            # Match against comma-separated species names in taxon_ids
            taxon_list = [t.strip() for t in raw_taxon_ids.split(",")]
            if needle in taxon_list:
                matches.append(s)
                continue

            # Fall back to free-text match on study name/objective
            if needle in name_lower or needle in objective_lower:
                matches.append(s)

        return matches

    # ==================================================================
    # Reference data (individuals, tags, deployments, sensors)
    # ==================================================================

    def get_individuals(
        self,
        study_id: int,
        *,
        attributes: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """Get animals in a study.

        Args:
            study_id: Movebank study ID.
            attributes: Specific attributes to return.

        Returns:
            List of individual dicts.
        """
        params: dict[str, Any] = {
            "entity_type": "individual",
            "study_id": study_id,
        }
        if attributes:
            params["attributes"] = ",".join(attributes)
        return self._get_csv(params)

    def get_tags(
        self,
        study_id: int,
        *,
        attributes: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """Get tags in a study.

        Args:
            study_id: Movebank study ID.
            attributes: Specific attributes to return.

        Returns:
            List of tag dicts.
        """
        params: dict[str, Any] = {
            "entity_type": "tag",
            "study_id": study_id,
        }
        if attributes:
            params["attributes"] = ",".join(attributes)
        return self._get_csv(params)

    def get_deployments(
        self,
        study_id: int,
        *,
        attributes: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """Get deployments in a study.

        Args:
            study_id: Movebank study ID.
            attributes: Specific attributes to return.

        Returns:
            List of deployment dicts.
        """
        params: dict[str, Any] = {
            "entity_type": "deployment",
            "study_id": study_id,
        }
        if attributes:
            params["attributes"] = ",".join(attributes)
        return self._get_csv(params)

    def get_sensors(
        self,
        study_id: int,
    ) -> list[dict[str, str]]:
        """Get tag sensors in a study.

        Args:
            study_id: Movebank study ID.

        Returns:
            List of sensor dicts.
        """
        return self._get_csv(
            {
                "entity_type": "sensor",
                "tag_study_id": study_id,
            }
        )

    def get_study_attributes(
        self,
        study_id: int,
        sensor_type_id: int = GPS_SENSOR_TYPE_ID,
    ) -> list[dict[str, str]]:
        """Get available event attributes for a sensor in a study.

        Args:
            study_id: Movebank study ID.
            sensor_type_id: Sensor type (default GPS=653).

        Returns:
            List of attribute description dicts.
        """
        return self._get_csv(
            {
                "entity_type": "study_attribute",
                "study_id": study_id,
                "sensor_type_id": sensor_type_id,
            }
        )

    # ==================================================================
    # Event data (tracking/sensor measurements)
    # ==================================================================

    def get_events(
        self,
        study_id: int,
        *,
        sensor_type_id: int = GPS_SENSOR_TYPE_ID,
        individual_local_identifiers: list[str] | None = None,
        attributes: tuple[str, ...] | list[str] = DEFAULT_EVENT_ATTRIBUTES,
        timestamp_start: date | datetime | None = None,
        timestamp_end: date | datetime | None = None,
        event_reduction_profile: str | None = None,
    ) -> list[dict[str, str]]:
        """Get event data (GPS locations, etc.) from a study.

        Args:
            study_id: Movebank study ID.
            sensor_type_id: Sensor type (default GPS=653).
            individual_local_identifiers: Filter to specific animals.
            attributes: Event attributes to include.
            timestamp_start: Start of time window.
            timestamp_end: End of time window. Date inputs are treated as an
                inclusive calendar day and expanded to the next midnight.
            event_reduction_profile: Reduction profile (EURING_01-04).

        Returns:
            List of event dicts.
        """
        params: dict[str, Any] = {
            "entity_type": "event",
            "study_id": study_id,
            "sensor_type_id": sensor_type_id,
            "attributes": ",".join(attributes),
        }

        if individual_local_identifiers:
            # The ``requests`` library serialises a list value into
            # repeated query-string keys, which is exactly what the
            # Movebank CSV endpoint expects:
            #   ?individual_local_identifier=A&individual_local_identifier=B
            params["individual_local_identifier"] = individual_local_identifiers

        if timestamp_start:
            params["timestamp_start"] = _mb_timestamp(timestamp_start)
        if timestamp_end:
            params["timestamp_end"] = _mb_timestamp_end(timestamp_end)
        if event_reduction_profile:
            params["event_reduction_profile"] = event_reduction_profile

        return self._get_csv(params)

    def get_events_json(
        self,
        study_id: int,
        *,
        sensor_type: str = "gps",
        individual_local_identifiers: list[str] | None = None,
        max_events_per_individual: int | None = None,
        timestamp_start: int | None = None,
        timestamp_end: int | None = None,
        attributes: list[str] | None = None,
        event_reduction_profile: str | None = None,
    ) -> dict[str, Any]:
        """Get event data via JSON endpoint (richer structure).

        Args:
            study_id: Movebank study ID.
            sensor_type: Sensor type name (default ``gps``).
            individual_local_identifiers: Filter animals.
            max_events_per_individual: Limit events per animal.
            timestamp_start: Start time (ms since epoch).
            timestamp_end: End time (ms since epoch).
            attributes: Event attributes to include.
            event_reduction_profile: Reduction profile.

        Returns:
            JSON response with ``individuals`` array.
        """
        params: dict[str, Any] = {
            "study_id": study_id,
            "sensor_type": sensor_type,
        }
        if individual_local_identifiers:
            params["individual_local_identifiers"] = individual_local_identifiers
        if max_events_per_individual:
            params["max_events_per_individual"] = max_events_per_individual
        if timestamp_start is not None:
            params["timestamp_start"] = timestamp_start
        if timestamp_end is not None:
            params["timestamp_end"] = timestamp_end
        if attributes:
            params["attributes"] = ",".join(attributes)
        if event_reduction_profile:
            params["event_reduction_profile"] = event_reduction_profile

        return self._get_json(params)

    def get_events_updated_since(
        self,
        study_id: int,
        since: datetime,
        *,
        sensor_type_id: int = GPS_SENSOR_TYPE_ID,
        attributes: tuple[str, ...] | list[str] = DEFAULT_EVENT_ATTRIBUTES,
    ) -> list[dict[str, str]]:
        """Get events added or updated since a timestamp (incremental sync).

        Args:
            study_id: Movebank study ID.
            since: Fetch updates from this datetime forward.
            sensor_type_id: Sensor type.
            attributes: Event attributes to include.

        Returns:
            List of updated event dicts.
        """
        params: dict[str, Any] = {
            "entity_type": "event",
            "study_id": study_id,
            "sensor_type_id": sensor_type_id,
            "attributes": ",".join(attributes),
            "update_ts_start": _mb_timestamp(since),
        }
        return self._get_csv(params)

    # ==================================================================
    # Taxonomy
    # ==================================================================

    def get_sensor_types(self) -> list[dict[str, str]]:
        """List all sensor types in Movebank.

        Returns:
            List of sensor type dicts.
        """
        return self._get_csv({"entity_type": "tag_type"})

    def get_taxon(self, taxon_name: str) -> list[dict[str, str]]:
        """Look up a taxon by canonical (scientific) name.

        The Movebank API returns the *entire* taxonomy catalogue (~90 k
        entries) regardless of the ``taxon_canonical_name`` parameter,
        so we filter locally for an exact match on ``canonical_name``.

        Results are cached in ``_taxon_cache`` to avoid re-fetching
        the full catalogue on every species lookup.

        Args:
            taxon_name: Scientific name, e.g. ``"Turdus merula"``.

        Returns:
            List of matching taxon dicts (usually 0 or 1 entries).
        """
        # Lazily populate per-client taxon cache
        if not hasattr(self, "_taxon_cache"):
            self._taxon_cache: list[dict[str, str]] = self._get_csv(
                {
                    "entity_type": "taxon",
                }
            )
            logger.debug(
                "Cached %d taxa from Movebank taxonomy endpoint",
                len(self._taxon_cache),
            )

        needle = taxon_name.strip().lower()
        return [
            t
            for t in self._taxon_cache
            if (t.get("canonical_name") or "").lower() == needle
        ]


# ---------------------------------------------------------------------------
# Data Collector (Orchestrator)
# ---------------------------------------------------------------------------


class MovebankCollector:
    """Orchestrates Movebank data collection and local persistence.

    Mirrors the pattern from the BirdWeather and eBird modules:
    collects events/metadata, writes JSONL + metadata, and optionally
    upserts to TimescaleDB.

    Args:
        output_dir: Root directory for raw data (``data/raw/movebank``).
        username: Optional Movebank username override.
        password: Optional Movebank password override.
    """

    def __init__(
        self,
        output_dir: Path | str = "data/raw/movebank",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.client = MovebankClient(username=username, password=password)

    # -- Public methods ------------------------------------------------

    def collect_study_events(
        self,
        study_id: int,
        *,
        sensor_type_id: int = GPS_SENSOR_TYPE_ID,
        individual_ids: list[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        reduction_profile: str | None = None,
        try_all_sensors: bool = False,
        all_sensors: bool = False,
        target: str = "local",
        known_sensor_type_ids: str | None = None,
    ) -> CollectionStats:
        """Collect GPS events from a study.

        Args:
            study_id: Movebank study ID.
            sensor_type_id: Sensor type (default GPS).
            individual_ids: Filter to specific animal identifiers.
            start_date: Start of time window.
            end_date: End of time window.
            reduction_profile: Reduction profile (EURING_01-04).
            try_all_sensors: If primary sensor returns 0, try other
                location-capable sensor types.
            all_sensors: Collect from **every** available location-
                capable sensor type instead of stopping at the first
                type that returns data.
            target: Storage target — ``"local"`` (default) or ``"database"``.
            known_sensor_type_ids: Comma-separated sensor names or IDs
                from study metadata (e.g. ``"GPS,Acceleration"``).
                Used as fallback when the sensor API returns empty.

        Returns:
            Collection statistics.
        """
        persist_to_db = target == "database"
        save_local = target == "local"

        # --all-sensors implies try_all_sensors
        if all_sensors:
            try_all_sensors = True
        stats = CollectionStats()

        # Discover available sensors for this study
        available_sensors: dict[int, str] = {}
        try:
            sensors = self.client.get_sensors(study_id)
            stats.api_calls += 1
            seen_ids: set[int] = set()
            for s in sensors:
                try:
                    sid = int(s.get("sensor_type_id", 0))
                except (ValueError, TypeError):
                    continue
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    label = LOCATION_SENSOR_TYPES.get(sid, f"sensor-{sid}")
                    available_sensors[sid] = label
            if available_sensors:
                logger.info(
                    "Study %d available sensors: %s",
                    study_id,
                    ", ".join(f"{v} ({k})" for k, v in available_sensors.items()),
                )
            else:
                logger.warning("Study %d: no sensors found", study_id)
        except (requests.HTTPError, Exception) as e:
            logger.debug("Could not fetch sensors for study %d: %s", study_id, e)

        # Fallback: use sensor_type_ids from study metadata when the
        # sensor API returned nothing (transient failure / rate-limit).
        if not available_sensors and known_sensor_type_ids:
            for token in known_sensor_type_ids.split(","):
                token = token.strip()
                if not token:
                    continue
                # Try name lookup first (e.g. "GPS"), then numeric parse.
                resolved = _SENSOR_NAME_TO_ID.get(token.lower())
                if resolved is None:
                    try:
                        resolved = int(token)
                    except (ValueError, TypeError):
                        continue
                if resolved and resolved not in available_sensors:
                    label = LOCATION_SENSOR_TYPES.get(resolved, f"sensor-{resolved}")
                    available_sensors[resolved] = label
            if available_sensors:
                logger.info(
                    "Study %d: using study-metadata sensor fallback: %s",
                    study_id,
                    ", ".join(f"{v} ({k})" for k, v in available_sensors.items()),
                )

        # Build ordered list of sensor types to try
        sensor_order: list[int] = [sensor_type_id]
        if try_all_sensors:
            for sid in LOCATION_SENSOR_TYPES:
                if sid != sensor_type_id and sid in available_sensors:
                    sensor_order.append(sid)

        events: list[dict[str, str]] = []
        used_sensor: int = sensor_type_id
        collected_sensors: list[int] = []

        for sid in sensor_order:
            if sid not in available_sensors and available_sensors:
                logger.debug(
                    "Study %d: skipping sensor %d (%s) – not available",
                    study_id,
                    sid,
                    LOCATION_SENSOR_TYPES.get(sid, "unknown"),
                )
                continue

            logger.info(
                "Collecting events for study %d (sensor=%d %s)",
                study_id,
                sid,
                LOCATION_SENSOR_TYPES.get(sid, ""),
            )

            try:
                fetched = self.client.get_events(
                    study_id,
                    sensor_type_id=sid,
                    individual_local_identifiers=individual_ids,
                    timestamp_start=start_date,
                    timestamp_end=end_date,
                    event_reduction_profile=reduction_profile,
                )
                stats.api_calls += 1
            except requests.HTTPError as e:
                stats.api_errors += 1
                logger.warning(
                    "Failed to fetch events for study %d sensor %d: %s",
                    study_id,
                    sid,
                    e,
                )
                continue

            # Filter out empty locations
            raw_count = len(fetched)
            fetched = [
                e for e in fetched if e.get("location_lat") and e.get("location_long")
            ]

            if raw_count > 0 and len(fetched) == 0:
                logger.info(
                    "Study %d sensor %d: %d events returned but all "
                    "lack location data (filtered out)",
                    study_id,
                    sid,
                    raw_count,
                )
            elif raw_count == 0:
                logger.info(
                    "Study %d sensor %d: 0 events returned",
                    study_id,
                    sid,
                )

            if fetched:
                events.extend(fetched)
                if not collected_sensors:
                    used_sensor = sid
                collected_sensors.append(sid)
                if not all_sensors:
                    break  # first-match mode: stop after first success

        stats.events_fetched = len(events)
        stats.individuals_count = len(
            {e.get("individual_local_identifier", "") for e in events}
        )

        sensor_label = (
            ", ".join(LOCATION_SENSOR_TYPES.get(s, str(s)) for s in collected_sensors)
            if collected_sensors
            else LOCATION_SENSOR_TYPES.get(used_sensor, str(used_sensor))
        )
        logger.info(
            "Fetched %d events for %d individuals (sensors: %s)",
            stats.events_fetched,
            stats.individuals_count,
            sensor_label,
        )

        # Merge reference data (individual, tag, deployment) into events
        # so that DB columns like individual_sex, tag_model, etc. are populated.
        if events:
            self._merge_reference_data(events, study_id, stats)

        # Safety filter: drop any events whose taxon is NOT in
        # config/target_species.json. Mixed-species studies (e.g. an
        # urban-birds study that contains one target species plus
        # several non-targets) must never contaminate the DB.
        if events:
            allowed = _target_movebank_taxa()
            if allowed:
                before = len(events)
                kept: list[dict[str, str]] = []
                dropped_taxa: dict[str, int] = {}
                for ev in events:
                    taxon = (
                        (ev.get("individual_taxon_canonical_name") or "")
                        .strip()
                        .lower()
                    )
                    if taxon and taxon in allowed:
                        kept.append(ev)
                    else:
                        dropped_taxa[taxon or "<unknown>"] = (
                            dropped_taxa.get(taxon or "<unknown>", 0) + 1
                        )
                if dropped_taxa:
                    logger.info(
                        "Study %d: dropped %d/%d events from non-target taxa: %s",
                        study_id,
                        before - len(kept),
                        before,
                        ", ".join(
                            f"{k}={v}"
                            for k, v in sorted(
                                dropped_taxa.items(), key=lambda x: -x[1]
                            )
                        ),
                    )
                events = kept
                stats.events_fetched = len(events)
                stats.individuals_count = len(
                    {ev.get("individual_local_identifier", "") for ev in events}
                )

        # Stamp raw fetch time on events so local JSONL keeps collection
        # provenance even though the database uses domain timestamps.
        now_iso = datetime.now().isoformat()
        for event in events:
            event.setdefault("_collected_at", now_iso)
            event.setdefault("_study_id", str(study_id))

        # DB upsert BEFORE the local-complete-day filter.
        # The upsert is idempotent (ON CONFLICT … DO UPDATE), so
        # re-inserting events that are already in the DB is safe.
        # Previously this was placed after the filter, which meant
        # events from "complete" local days were never sent to the DB.
        if persist_to_db and events:
            self._persist_to_db(events, study_id)

        # Skip events for days already marked complete on disk.
        if save_local and events:
            complete_days = self._find_complete_study_days(study_id)
            if complete_days:
                before = len(events)
                events = [
                    e
                    for e in events
                    if e.get("timestamp", "")[:10] not in complete_days
                ]
                skipped = before - len(events)
                if skipped:
                    logger.info(
                        "Skipped %d events from %d already-complete day(s) "
                        "for study %d.",
                        skipped,
                        len(complete_days),
                        study_id,
                    )
                    stats.events_fetched = len(events)

        if save_local and events:
            self._save_events_local_by_date(events, stats, study_id)

        self._log_summary(stats, f"study={study_id}")
        return stats

    def collect_study_metadata(
        self,
        study_id: int,
        *,
        target: str = "local",
    ) -> CollectionStats:
        """Collect reference data (individuals, tags, deployments) for a study.

        Args:
            study_id: Movebank study ID.
            target: Storage target.

        Returns:
            Collection statistics.
        """
        persist_to_db = target == "database"
        save_local = target == "local"

        stats = CollectionStats()

        logger.info("Collecting reference data for study %d", study_id)

        try:
            study_info = self.client.get_study(study_id)
            stats.api_calls += 1
        except requests.HTTPError as e:
            stats.api_errors += 1
            logger.error("Failed to fetch study %d: %s", study_id, e)
            return stats

        try:
            individuals = self.client.get_individuals(study_id)
            stats.api_calls += 1
            stats.individuals_count = len(individuals)
        except requests.HTTPError as e:
            stats.api_errors += 1
            individuals = []
            logger.warning("Failed to fetch individuals: %s", e)

        try:
            deployments = self.client.get_deployments(study_id)
            stats.api_calls += 1
            stats.deployments_count = len(deployments)
        except requests.HTTPError as e:
            stats.api_errors += 1
            deployments = []
            logger.warning("Failed to fetch deployments: %s", e)

        try:
            tags = self.client.get_tags(study_id)
            stats.api_calls += 1
        except requests.HTTPError as e:
            stats.api_errors += 1
            tags = []
            logger.warning("Failed to fetch tags: %s", e)

        if save_local:
            directory = self.output_dir / str(study_id) / "reference"
            directory.mkdir(parents=True, exist_ok=True)

            for name, data in [
                ("study", study_info),
                ("individuals", individuals),
                ("deployments", deployments),
                ("tags", tags),
            ]:
                out_file = directory / f"{name}.json"
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)

            logger.info("Saved reference data to %s", directory)

        # Persist to TimescaleDB (reference data merged into movebank_tbl)
        if persist_to_db:
            logger.info(
                "DB target selected: reference data for study %d has no "
                "standalone DB persistence path and was not written locally.",
                study_id,
            )

        stats.studies_found = 1
        self._log_summary(stats, f"study={study_id} (reference)")
        return stats

    def discover_target_species_studies(
        self,
        *,
        target: str = "local",
    ) -> CollectionStats:
        """Search for Movebank studies containing our target species.

        Fetches the full accessible study list **once** and filters
        locally for each target species, avoiding redundant API calls.

        Args:
            target: ``"local"`` (disk only) or ``"database"`` (disk + DB).

        Returns:
            Collection statistics.
        """
        stats, unique_studies = self._discover_target_species_studies_data()

        if unique_studies:
            out_file = self._save_discovery_results(
                "target_species_studies.json",
                unique_studies,
            )
            logger.info("Saved study discovery to %s", out_file)

        self._log_summary(stats, "target species discovery")
        return stats

    def collect_target_species_events(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        reduction_profile: str | None = None,
        try_all_sensors: bool = True,
        all_sensors: bool = False,
        target: str = "local",
    ) -> CollectionStats:
        """Collect GPS events for all target species studies.

        First discovers studies, then fetches events from each.

        Args:
            start_date: Start of time window.
            end_date: End of time window.
            reduction_profile: Reduction profile.
            try_all_sensors: Try alternative location-capable sensors
                when GPS returns 0 events.
            all_sensors: Collect from every available sensor type.
            target: Storage target — ``"local"`` (default) or ``"database"``.

        Returns:
            Aggregated collection statistics.
        """
        total_stats = CollectionStats()

        # Discover studies
        disc_stats, studies = self._discover_target_species_studies_data()
        total_stats.api_calls += disc_stats.api_calls
        total_stats.api_errors += disc_stats.api_errors
        total_stats.studies_found = disc_stats.studies_found

        if studies:
            out_file = self._save_discovery_results(
                "target_species_studies.json",
                studies,
            )
            logger.info("Saved study discovery to %s", out_file)

        if not studies:
            logger.warning("No Movebank studies found for configured target species")
            return total_stats

        skipped = 0
        for study in tqdm(studies, desc="Studies", unit="study"):
            study_id = study.get("id")
            if not study_id:
                continue
            try:
                study_id_int = int(study_id)
            except (ValueError, TypeError):
                continue

            # Skip studies with no deployed locations
            n_locs = study.get("number_of_deployed_locations", "")
            if n_locs and str(n_locs).isdigit() and int(n_locs) == 0:
                logger.info(
                    "Skipping study %d (%s): 0 deployed locations",
                    study_id_int,
                    study.get("name", ""),
                )
                skipped += 1
                continue

            s = self.collect_study_events(
                study_id_int,
                start_date=start_date,
                end_date=end_date,
                reduction_profile=reduction_profile,
                try_all_sensors=try_all_sensors,
                all_sensors=all_sensors,
                target=target,
                known_sensor_type_ids=study.get("sensor_type_ids"),
            )
            total_stats.events_fetched += s.events_fetched
            total_stats.individuals_count += s.individuals_count
            total_stats.api_calls += s.api_calls
            total_stats.api_errors += s.api_errors

        if skipped:
            logger.info("Skipped %d studies with 0 deployed locations", skipped)

        # Flag silent no-op runs so Dagster/ops dashboards can distinguish
        # "collector ran fine but no publishers had new data" from a real
        # pipeline issue. Only warn if we actually visited studies.
        if (
            total_stats.events_fetched == 0
            and total_stats.studies_found > 0
            and total_stats.api_errors == 0
        ):
            logger.warning(
                "Movebank: 0 events fetched across %d target-species "
                "studies (window %s → %s). No upstream data in this "
                "window — not an error; upstream studies have not "
                "published new events for the requested range.",
                total_stats.studies_found,
                start_date,
                end_date,
            )

        self._log_summary(total_stats, "target species (all)")
        return total_stats

    def collect_all_studies(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        reduction_profile: str | None = None,
        try_all_sensors: bool = True,
        all_sensors: bool = False,
        target: str = "local",
    ) -> CollectionStats:
        """Download events from every accessible Movebank study.

        Lists all studies the authenticated user can see and iterates
        through each, collecting events. Studies with zero deployed
        locations are skipped.

        Args:
            start_date: Start of time window.
            end_date: End of time window.
            reduction_profile: Reduction profile.
            try_all_sensors: Try alternative sensors when GPS returns 0.
            all_sensors: Collect from every available sensor type.
            target: Storage target — ``"local"`` (default) or ``"database"``.

        Returns:
            Aggregated collection statistics.
        """
        save_local = target == "local"

        total_stats = CollectionStats()

        studies = self.client.list_studies(
            i_have_download_access=True,
            attributes=[
                "id",
                "name",
                "taxon_ids",
                "sensor_type_ids",
                "number_of_individuals",
                "number_of_deployed_locations",
                "principal_investigator_name",
            ],
        )
        total_stats.api_calls += 1
        total_stats.studies_found = len(studies)
        logger.info("Found %d downloadable studies", len(studies))

        if save_local:
            out_file = self._save_discovery_results("all_studies.json", studies)
            logger.info("Saved full study list to %s", out_file)

        skipped = 0
        for study in tqdm(studies, desc="All studies", unit="study"):
            study_id = study.get("id")
            if not study_id:
                continue
            try:
                study_id_int = int(study_id)
            except (ValueError, TypeError):
                continue

            n_locs = study.get("number_of_deployed_locations", "")
            if n_locs and str(n_locs).isdigit() and int(n_locs) == 0:
                logger.debug(
                    "Skipping study %d (%s): 0 deployed locations",
                    study_id_int,
                    study.get("name", ""),
                )
                skipped += 1
                continue

            s = self.collect_study_events(
                study_id_int,
                start_date=start_date,
                end_date=end_date,
                reduction_profile=reduction_profile,
                try_all_sensors=try_all_sensors,
                all_sensors=all_sensors,
                target=target,
                known_sensor_type_ids=study.get("sensor_type_ids"),
            )
            total_stats.events_fetched += s.events_fetched
            total_stats.individuals_count += s.individuals_count
            total_stats.api_calls += s.api_calls
            total_stats.api_errors += s.api_errors

        if skipped:
            logger.info("Skipped %d studies with 0 deployed locations", skipped)

        self._log_summary(total_stats, "all studies")
        return total_stats

    # -- Private helpers -----------------------------------------------

    def _find_complete_study_days(self, study_id: int) -> set[str]:
        """Return date labels (YYYY-MM-DD) already marked complete on disk."""
        study_dir = self.output_dir / str(study_id)
        if not study_dir.exists():
            return set()
        complete: set[str] = set()
        for day_dir in study_dir.iterdir():
            if not day_dir.is_dir():
                continue
            meta_path = day_dir / "metadata.json"
            if meta_path.exists():
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        if json.load(fh).get("completed"):
                            complete.add(day_dir.name)
                except (json.JSONDecodeError, KeyError, OSError):
                    pass
        return complete

    def _discover_target_species_studies_data(
        self,
    ) -> tuple[CollectionStats, list[dict[str, str]]]:
        """Return discovered target-species studies without requiring disk I/O."""
        stats = CollectionStats()
        all_studies: list[dict[str, str]] = []

        disc_cache = self.output_dir / "discovery" / "target_species_studies.json"
        try:
            cached_studies = self.client.list_studies(
                i_have_download_access=True,
                attributes=self.client.STUDY_SEARCH_ATTRS,
            )
            stats.api_calls += 1
            logger.info(
                "Fetched %d accessible studies for species search",
                len(cached_studies),
            )
        except requests.RequestException as api_err:
            if isinstance(api_err, requests.HTTPError):
                status = (
                    api_err.response.status_code if api_err.response is not None else 0
                )
                if status not in {502, 503, 504}:
                    raise

            if disc_cache.exists():
                logger.warning(
                    "Movebank study discovery failed (%s) — falling back to "
                    "cached study list at %s",
                    api_err,
                    disc_cache,
                )
                with open(disc_cache, encoding="utf-8") as fh:
                    prev_studies = json.load(fh)
                stats.studies_found = len(prev_studies)
                return stats, prev_studies

            raise

        for species in tqdm(
            _load_target_species(),
            desc="Searching species",
            unit="spp",
        ):
            taxon = species.get("movebank_taxon") or species.get("scientific")
            if not taxon:
                continue
            taxon = str(taxon).strip()
            try:
                studies = self.client.search_studies_by_taxon(
                    taxon,
                    _cached_studies=cached_studies,
                )
                for study in studies:
                    study["_search_species"] = taxon
                all_studies.extend(studies)
                logger.debug(
                    "%s: found %d studies",
                    taxon,
                    len(studies),
                )
            except requests.HTTPError as e:
                stats.api_errors += 1
                logger.warning("Failed searching for %s: %s", taxon, e)

        seen: set[str] = set()
        unique_studies: list[dict[str, str]] = []
        for study in all_studies:
            sid = study.get("id", "")
            if sid and sid not in seen:
                seen.add(sid)
                unique_studies.append(study)

        stats.studies_found = len(unique_studies)
        logger.info(
            "Found %d unique studies for target species",
            len(unique_studies),
        )
        return stats, unique_studies

    def _save_discovery_results(
        self,
        filename: str,
        studies: list[dict[str, str]],
    ) -> Path:
        """Persist study discovery output when local artifacts are requested."""
        directory = self.output_dir / "discovery"
        directory.mkdir(parents=True, exist_ok=True)
        out_file = directory / filename
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(studies, f, indent=2, default=str)
        return out_file

    def _save_events_local_by_date(
        self,
        events: list[dict[str, str]],
        stats: CollectionStats,
        study_id: int,
    ) -> None:
        """Group events by their timestamp date and save each day separately."""
        from collections import defaultdict

        by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
        for event in events:
            ts = event.get("timestamp", "")
            # Movebank timestamps: "2024-06-15 12:30:00.000"
            day = ts[:10] if len(ts) >= 10 else "unknown"
            by_date[day].append(event)

        for day_label in sorted(by_date):
            day_events = by_date[day_label]
            day_stats = CollectionStats(
                events_fetched=len(day_events),
                individuals_count=len(
                    {e.get("individual_local_identifier", "") for e in day_events}
                ),
                api_calls=stats.api_calls,
                api_errors=stats.api_errors,
            )
            self._save_events_local(
                day_events,
                day_stats,
                study_id,
                day_label,
            )

        logger.info(
            "Saved events across %d day(s) for study %d",
            len(by_date),
            study_id,
        )

    def _save_events_local(
        self,
        events: list[dict[str, str]],
        stats: CollectionStats,
        study_id: int,
        date_label: str,
    ) -> None:
        """Write events as JSONL and metadata as JSON."""
        directory = self.output_dir / str(study_id) / date_label
        directory.mkdir(parents=True, exist_ok=True)

        # Events JSONL (_collected_at already stamped in collect_study_events)
        events_file = directory / "events.jsonl"
        with open(events_file, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, default=str) + "\n")
        logger.info("Saved %d events to %s", len(events), events_file)

        # Individual summary
        individual_summary: dict[str, int] = {}
        for event in events:
            ident = event.get("individual_local_identifier", "unknown")
            individual_summary[ident] = individual_summary.get(ident, 0) + 1

        summary_file = directory / "individuals_summary.json"
        with open(summary_file, "w", encoding="utf-8") as f:
            json.dump(individual_summary, f, indent=2)

        # Metadata
        metadata = {
            "study_id": study_id,
            "date": date_label,
            "generated_at": datetime.now().isoformat(),
            "stats": stats.to_dict(),
            "individual_distribution": individual_summary,
            "completed": True,
        }
        meta_file = directory / "metadata.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, default=str)

    def _merge_reference_data(
        self,
        events: list[dict[str, str]],
        study_id: int,
        stats: CollectionStats,
    ) -> None:
        """Fetch individual/tag/deployment reference data and merge into events.

        Enriches each event dict in-place with fields like
        ``individual_sex``, ``individual_life_stage``, ``tag_manufacturer_name``,
        ``tag_model``, ``tag_serial_number``, ``deployment_local_identifier``,
        ``deploy_on_*``, and ``deploy_off_timestamp``.
        """
        # Build lookup tables keyed by internal ID.
        ind_map: dict[str, dict[str, str]] = {}
        tag_map: dict[str, dict[str, str]] = {}
        dep_map: dict[str, dict[str, str]] = {}

        try:
            individuals = self.client.get_individuals(study_id)
            stats.api_calls += 1
            for ind in individuals:
                iid = ind.get("id", "")
                if iid:
                    ind_map[iid] = ind
            stats.individuals_count = max(stats.individuals_count, len(ind_map))
        except Exception as e:
            logger.debug("Could not fetch individuals for study %d: %s", study_id, e)

        try:
            tags = self.client.get_tags(study_id)
            stats.api_calls += 1
            for t in tags:
                tid = t.get("id", "")
                if tid:
                    tag_map[tid] = t
        except Exception as e:
            logger.debug("Could not fetch tags for study %d: %s", study_id, e)

        try:
            deployments = self.client.get_deployments(study_id)
            stats.api_calls += 1
            stats.deployments_count = len(deployments)
            for dep in deployments:
                did = dep.get("id", "")
                if did:
                    dep_map[did] = dep
        except Exception as e:
            logger.debug("Could not fetch deployments for study %d: %s", study_id, e)

        if not (ind_map or tag_map or dep_map):
            return

        merged = 0
        for event in events:
            changed = False

            # Individual reference
            iid = event.get("individual_id", "")
            if iid and iid in ind_map:
                ind = ind_map[iid]
                if not event.get("individual_sex"):
                    event["individual_sex"] = ind.get("sex", "")
                if not event.get("individual_life_stage"):
                    event["individual_life_stage"] = ind.get("animal_life_stage", "")
                if not event.get("individual_taxon_canonical_name"):
                    event["individual_taxon_canonical_name"] = ind.get(
                        "individual_taxon_canonical_name", ""
                    )
                # Extended individual attributes (flattened into movebank_tbl).
                for src, dst in (
                    ("comments", "individual_comments"),
                    ("ring_id", "individual_ring_id"),
                    ("number_of_events", "individual_number_of_events"),
                    (
                        "number_of_deployments",
                        "individual_number_of_deployments",
                    ),
                    ("taxon_detail", "individual_taxon_detail"),
                ):
                    if not event.get(dst) and ind.get(src):
                        event[dst] = ind.get(src, "")
                changed = True

            # Tag reference
            tid = event.get("tag_id", "")
            if tid and tid in tag_map:
                tag = tag_map[tid]
                if not event.get("tag_manufacturer_name"):
                    event["tag_manufacturer_name"] = tag.get("manufacturer_name", "")
                if not event.get("tag_model"):
                    event["tag_model"] = tag.get("model", "")
                if not event.get("tag_serial_number"):
                    event["tag_serial_number"] = tag.get("serial_no", "")
                for src, dst in (("comments", "tag_comments"),):
                    if not event.get(dst) and tag.get(src):
                        event[dst] = tag.get(src, "")
                changed = True

            # Deployment reference
            did = event.get("deployment_id", "")
            if did and did in dep_map:
                dep = dep_map[did]
                if not event.get("deployment_local_identifier"):
                    event["deployment_local_identifier"] = dep.get(
                        "local_identifier", ""
                    )
                if not event.get("deploy_on_timestamp"):
                    event["deploy_on_timestamp"] = dep.get("deploy_on_timestamp", "")
                if not event.get("deploy_off_timestamp"):
                    event["deploy_off_timestamp"] = dep.get("deploy_off_timestamp", "")
                if not event.get("deploy_on_latitude"):
                    event["deploy_on_latitude"] = dep.get("deploy_on_latitude", "")
                if not event.get("deploy_on_longitude"):
                    event["deploy_on_longitude"] = dep.get("deploy_on_longitude", "")
                if not event.get("individual_life_stage"):
                    event["individual_life_stage"] = dep.get("animal_life_stage", "")
                # Extended deployment attributes.
                for src, dst in (
                    ("comments", "deployment_comments"),
                    (
                        "location_accuracy_comments",
                        "location_accuracy_comments",
                    ),
                    ("manipulation_type", "manipulation_type"),
                    ("study_site", "study_site"),
                    ("tag_readout_method", "tag_readout_method"),
                    ("alt_project_id", "alt_project_id"),
                    ("attachment_type", "attachment_type"),
                    ("animal_mass", "animal_mass_g"),
                ):
                    if not event.get(dst) and dep.get(src):
                        event[dst] = dep.get(src, "")
                changed = True

            if changed:
                merged += 1

        logger.info(
            "Merged reference data into %d/%d events for study %d "
            "(individuals=%d, tags=%d, deployments=%d)",
            merged,
            len(events),
            study_id,
            len(ind_map),
            len(tag_map),
            len(dep_map),
        )

    def _persist_to_db(
        self,
        events: list[dict[str, str]],
        study_id: int,
    ) -> None:
        """Upsert events to TimescaleDB via the DB loader."""
        try:
            from avian_biosurveillance.db.loaders.movebank import (
                upsert_movebank_events,
            )
            from avian_biosurveillance.db.models.movebank import MovebankEvent

            records = []
            for event in events:
                try:
                    records.append(MovebankEvent.from_csv_row(event, study_id=study_id))
                except Exception as e:
                    logger.debug("Skipping malformed event: %s", e)

            if records:
                count = upsert_movebank_events(records)
                logger.info("Upserted %d Movebank events to DB", count)

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
        logger.info("MOVEBANK COLLECTION COMPLETE – %s", label)
        logger.info("=" * 60)
        logger.info("Events       : %d", stats.events_fetched)
        if stats.studies_found:
            logger.info("Studies      : %d", stats.studies_found)
        logger.info("Individuals  : %d", stats.individuals_count)
        if stats.deployments_count:
            logger.info("Deployments  : %d", stats.deployments_count)
        logger.info("API calls    : %d (errors: %d)", stats.api_calls, stats.api_errors)
        logger.info("Elapsed      : %.1fs", stats.elapsed_s)
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for Movebank data ingestion."""
    parser = argparse.ArgumentParser(
        description="Movebank REST API data ingestion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --list-studies\n"
            "  %(prog)s --study-id 2911040\n"
            "  %(prog)s --study-id 2911040 --start-date 2026-01-01\n"
            "  %(prog)s --search-taxon 'Turdus merula'\n"
            "  %(prog)s --target-species\n"
        ),
    )

    # Study selection
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument(
        "--study-id",
        type=int,
        help="Movebank study ID to fetch events from",
    )
    grp.add_argument(
        "--list-studies",
        action="store_true",
        help="List accessible studies",
    )
    grp.add_argument(
        "--search-taxon",
        type=str,
        help="Search studies by taxon name (scientific or common)",
    )
    grp.add_argument(
        "--target-species",
        action="store_true",
        help="Discover and fetch events for all target species",
    )
    grp.add_argument(
        "--all-studies",
        action="store_true",
        help="Download events from ALL accessible studies",
    )

    # Filters
    parser.add_argument(
        "--individual",
        type=str,
        action="append",
        dest="individuals",
        help="Animal local identifier (repeatable)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--sensor-type",
        type=int,
        default=GPS_SENSOR_TYPE_ID,
        help=f"Sensor type ID (default: {GPS_SENSOR_TYPE_ID} = GPS)",
    )
    parser.add_argument(
        "--reduction-profile",
        choices=list(REDUCTION_PROFILES.keys()),
        help="Data reduction profile",
    )

    # Reference data
    parser.add_argument(
        "--reference-data",
        action="store_true",
        help="Download reference data (individuals, tags, deployments)",
    )
    parser.add_argument(
        "--gps-only",
        action="store_true",
        help="Only fetch GPS events; skip other sensor types like Radio Transmitter, Sigfox",
    )
    parser.add_argument(
        "--all-sensors",
        action="store_true",
        help="Collect from every available location-capable sensor type "
        "(not just the first that returns data)",
    )

    # Output
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/raw/movebank",
        help="Output directory (default: data/raw/movebank)",
    )
    parser.add_argument(
        "--target",
        choices=["local", "database"],
        default="local",
        help="Storage target: local (disk only, default) or database (TimescaleDB only)",
    )

    # Verbosity
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    collector = MovebankCollector(output_dir=args.output_dir)

    # Parse dates
    start_dt = date.fromisoformat(args.start_date) if args.start_date else None
    end_dt = date.fromisoformat(args.end_date) if args.end_date else None

    if args.list_studies:
        studies = collector.client.list_studies(
            i_have_download_access=True,
            attributes=[
                "id",
                "name",
                "taxon_ids",
                "number_of_individuals",
                "number_of_deployed_locations",
                "principal_investigator_name",
            ],
        )
        print(f"\nFound {len(studies)} downloadable studies:\n")
        for s in studies[:50]:
            print(f"  [{s.get('id', '')}] {s.get('name', 'N/A')}")
            print(
                f"    Individuals: {s.get('number_of_individuals', '?')}"
                f" | Locations: {s.get('number_of_deployed_locations', '?')}"
                f" | PI: {s.get('principal_investigator_name', '?')}"
            )
        if len(studies) > 50:
            print(f"\n  ... and {len(studies) - 50} more")

    elif args.search_taxon:
        studies = collector.client.search_studies_by_taxon(args.search_taxon)
        print(f"\nFound {len(studies)} studies matching '{args.search_taxon}':\n")
        for s in studies:
            print(f"  [{s.get('id', '')}] {s.get('name', 'N/A')}")

    elif args.target_species:
        collector.collect_target_species_events(
            start_date=start_dt,
            end_date=end_dt,
            reduction_profile=args.reduction_profile,
            try_all_sensors=not args.gps_only,
            all_sensors=args.all_sensors,
            target=args.target,
        )

    elif args.all_studies:
        collector.collect_all_studies(
            start_date=start_dt,
            end_date=end_dt,
            reduction_profile=args.reduction_profile,
            try_all_sensors=not args.gps_only,
            all_sensors=args.all_sensors,
            target=args.target,
        )

    elif args.study_id:
        if args.reference_data:
            collector.collect_study_metadata(
                args.study_id,
                target=args.target,
            )

        collector.collect_study_events(
            args.study_id,
            sensor_type_id=args.sensor_type,
            individual_ids=args.individuals,
            start_date=start_dt,
            end_date=end_dt,
            reduction_profile=args.reduction_profile,
            try_all_sensors=not args.gps_only,
            all_sensors=args.all_sensors,
            target=args.target,
        )

    else:
        # Default: run target-species collection when no mode specified.
        logger.info(
            "No mode specified – defaulting to --target-species",
        )
        collector.collect_target_species_events(
            start_date=start_dt,
            end_date=end_dt,
            reduction_profile=args.reduction_profile,
            try_all_sensors=not args.gps_only,
            all_sensors=args.all_sensors,
            target=args.target,
        )


if __name__ == "__main__":
    main()
