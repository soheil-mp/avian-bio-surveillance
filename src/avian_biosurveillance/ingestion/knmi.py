"""
KNMI Data Ingestion Module

This module provides comprehensive data ingestion from all KNMI (Royal Netherlands
Meteorological Institute) APIs:

1. Open Data API - Download files from published datasets
2. EDR API - Environmental Data Retrieval with spatio-temporal queries
3. Notification Service - Real-time MQTT notifications for new data
4. WMS API - Web Map Service for geospatial imagery
5. FDSN APIs (keyless) - Seismological data (DataSelect, Station, Event)
6. Shakemaps API (keyless) - Earthquake ground motion data

Environment Variables Required (.env):
- KNMI_OPEN_DATA_API: API key for Open Data API
- KNMI_EDR: API key for EDR API
- KNMI_NOTIFICATION: API key for Notification Service
- KNMI_WEB_MAP_SERVICE: API key for WMS API

Data is saved to: data/raw/knmi/
"""

import asyncio
import json
import logging
import os
import ssl
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as datetime_date
from enum import Enum
from pathlib import Path
from typing import Any

import requests

from avian_biosurveillance.config.runtime import load_runtime_env


load_runtime_env()

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================


@dataclass
class KNMIConfig:
    """Configuration for KNMI API access."""

    # API Keys (loaded from environment)
    open_data_api_key: str = field(
        default_factory=lambda: os.getenv("KNMI_OPEN_DATA_API", "")
    )
    edr_api_key: str = field(default_factory=lambda: os.getenv("KNMI_EDR", ""))
    notification_api_key: str = field(
        default_factory=lambda: os.getenv("KNMI_NOTIFICATION", "")
    )
    wms_api_key: str = field(
        default_factory=lambda: os.getenv("KNMI_WEB_MAP_SERVICE", "")
    )

    # Base URLs
    open_data_base_url: str = "https://api.dataplatform.knmi.nl/open-data/v1"
    edr_base_url: str = "https://api.dataplatform.knmi.nl/edr/v1"
    wms_base_url: str = "https://wms.dataplatform.knmi.nl/wms"
    fdsn_base_url: str = "https://api.dataplatform.knmi.nl/fdsnws"
    shakemaps_base_url: str = "https://api.dataplatform.knmi.nl/shakemaps/7"
    notification_broker: str = "mqtt.dataplatform.knmi.nl"
    notification_port: int = 443

    # Data storage
    data_dir: Path = field(default_factory=lambda: Path("data/raw/knmi"))

    # Rate limiting
    max_requests_per_second: int = 10
    max_concurrent_downloads: int = 5

    def __post_init__(self):
        """Ensure data directory exists."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_subdir(self, *subdirs: str) -> Path:
        """Get or create a subdirectory under the data directory."""
        path = self.data_dir.joinpath(*subdirs)
        path.mkdir(parents=True, exist_ok=True)
        return path


# ============================================================================
# Data Models
# ============================================================================


class DataFormat(Enum):
    """Supported data formats."""

    JSON = "json"
    XML = "xml"
    CSV = "csv"
    NETCDF = "nc"
    GEOJSON = "geojson"
    COVERAGE_JSON = "covjson"
    MINISEED = "mseed"
    QUAKEML = "quakeml"
    PNG = "png"
    JPEG = "jpeg"
    TEXT = "text"


@dataclass
class DatasetFile:
    """Represents a file in a KNMI dataset."""

    filename: str
    size: int
    last_modified: datetime | None = None
    created: datetime | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetFile":
        return cls(
            filename=data.get("filename", ""),
            size=data.get("size", 0),
            last_modified=datetime.fromisoformat(
                data["lastModified"].replace("Z", "+00:00")
            )
            if data.get("lastModified")
            else None,
            created=datetime.fromisoformat(data["created"].replace("Z", "+00:00"))
            if data.get("created")
            else None,
        )


@dataclass
class Dataset:
    """Represents a KNMI dataset."""

    name: str
    version: str
    description: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.name}/{self.version}"


@dataclass
class Location:
    """Represents a measurement location."""

    id: str
    name: str
    latitude: float
    longitude: float
    elevation: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class SeismicEvent:
    """Represents a seismic event."""

    event_id: str
    time: datetime
    latitude: float
    longitude: float
    depth: float
    magnitude: float
    magnitude_type: str | None = None
    description: str | None = None


# ============================================================================
# Base API Client
# ============================================================================


class BaseAPIClient(ABC):
    """Base class for KNMI API clients."""

    def __init__(self, config: KNMIConfig):
        self.config = config
        self.session = requests.Session()

    @abstractmethod
    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        pass

    def _make_request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        stream: bool = False,
        timeout: int = 30,
        _retries: int = 4,
        _backoff_base: float = 5.0,
    ) -> requests.Response:
        """Make an HTTP request with error handling and exponential backoff.

        Retries up to ``_retries`` times on 429 (rate-limit) and 403
        (often a transient quota enforcement) with exponential back-off:
        wait = backoff_base * 2**attempt  (5 s, 10 s, 20 s, 40 s).
        """
        request_headers = self._get_headers()
        if headers:
            request_headers.update(headers)

        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(_retries + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=request_headers,
                    stream=stream,
                    timeout=timeout,
                )
                response.raise_for_status()
                return response
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None

                # Non-retryable: 404 no-data (KNMI convention) — re-raise immediately
                if (
                    status == 404
                    and exc.response is not None
                    and "no data" in exc.response.text.lower()
                ):
                    raise

                # Retryable: rate limit (429) or transient quota (403)
                if status in (403, 429) and attempt < _retries:
                    wait = _backoff_base * (2**attempt)
                    logger.warning(
                        f"HTTP {status} for {url} — retrying in {wait:.0f}s "
                        f"(attempt {attempt + 1}/{_retries})"
                    )
                    time.sleep(wait)
                    last_exc = exc
                    continue

                logger.error(f"Request failed: {exc}")
                raise
            except requests.exceptions.RequestException as exc:
                logger.error(f"Request failed: {exc}")
                raise

        raise last_exc

    def _download_file(
        self, url: str, filepath: Path, chunk_size: int = 8192, use_auth: bool = False
    ) -> Path:
        """Download a file from URL."""
        headers = self._get_headers() if use_auth else {}

        try:
            with self.session.get(url, headers=headers, stream=True) as r:
                r.raise_for_status()
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        f.write(chunk)
            logger.info(f"Downloaded: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Download failed for {url}: {e}")
            raise

    def _save_json(self, data: Any, filepath: Path) -> Path:
        """Save data as JSON file."""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        logger.info(f"Saved JSON: {filepath}")
        return filepath

    def _save_text(self, data: str, filepath: Path) -> Path:
        """Save data as text file."""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(data)
        logger.info(f"Saved text: {filepath}")
        return filepath


# ============================================================================
# Open Data API Client
# ============================================================================


class OpenDataAPIClient(BaseAPIClient):
    """
    Client for KNMI Open Data API.

    Allows downloading files from datasets published on KNMI Data Platform.
    Documentation: https://developer.dataplatform.knmi.nl/open-data-api
    """

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": self.config.open_data_api_key}

    def list_files(
        self,
        dataset: Dataset,
        max_keys: int = 10,
        order_by: str = "created",
        sorting: str = "desc",
        begin: str | None = None,
        next_page_token: str | None = None,
    ) -> tuple[list[DatasetFile], str | None]:
        """
        List files in a dataset.

        Args:
            dataset: The dataset to query
            max_keys: Maximum number of files to return (default 10, max 500)
            order_by: Order by 'filename', 'lastModified', or 'created'
            sorting: 'asc' or 'desc'
            begin: Start listing after this filename/timestamp
            next_page_token: Token for pagination

        Returns:
            Tuple of (list of DatasetFile objects, next page token or None)
        """
        url = f"{self.config.open_data_base_url}/datasets/{dataset.name}/versions/{dataset.version}/files"

        params = {"maxKeys": max_keys, "orderBy": order_by, "sorting": sorting}
        if begin:
            params["begin"] = begin
        if next_page_token:
            params["nextPageToken"] = next_page_token

        response = self._make_request("GET", url, params=params)
        data = response.json()

        if "error" in data:
            logger.error(f"API error: {data['error']}")
            return [], None

        files = [DatasetFile.from_api_response(f) for f in data.get("files", [])]
        return files, data.get("nextPageToken")

    def get_file_url(self, dataset: Dataset, filename: str) -> str | None:
        """Get temporary download URL for a file."""
        url = f"{self.config.open_data_base_url}/datasets/{dataset.name}/versions/{dataset.version}/files/{filename}/url"

        response = self._make_request("GET", url)
        data = response.json()

        # Check for deprecation warning
        if "X-KNMI-Deprecation" in response.headers:
            logger.warning(
                f"Deprecation notice: {response.headers['X-KNMI-Deprecation']}"
            )

        return data.get("temporaryDownloadUrl")

    def download_file(
        self, dataset: Dataset, filename: str, output_dir: Path | None = None
    ) -> Path:
        """Download a single file from a dataset."""
        download_url = self.get_file_url(dataset, filename)
        if not download_url:
            raise ValueError(f"Could not get download URL for {filename}")

        if output_dir is None:
            output_dir = self.config.get_subdir(
                "open_data", dataset.name, dataset.version
            )

        filepath = output_dir / filename
        return self._download_file(download_url, filepath)

    def download_latest_files(
        self, dataset: Dataset, count: int = 10, output_dir: Path | None = None
    ) -> list[Path]:
        """Download the latest N files from a dataset."""
        files, _ = self.list_files(dataset, max_keys=count, sorting="desc")

        downloaded = []
        for file in files:
            try:
                path = self.download_file(dataset, file.filename, output_dir)
                downloaded.append(path)
            except Exception as e:
                logger.error(f"Failed to download {file.filename}: {e}")

        return downloaded

    def download_files_since(
        self, dataset: Dataset, since: datetime, output_dir: Path | None = None
    ) -> list[Path]:
        """Download all files created since a given datetime."""
        timestamp = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        downloaded = []
        next_token = None

        while True:
            files, next_token = self.list_files(
                dataset,
                max_keys=100,
                order_by="created",
                sorting="asc",
                begin=timestamp,
                next_page_token=next_token,
            )

            for file in files:
                try:
                    path = self.download_file(dataset, file.filename, output_dir)
                    downloaded.append(path)
                except Exception as e:
                    logger.error(f"Failed to download {file.filename}: {e}")

            if not next_token:
                break

        return downloaded

    async def download_full_dataset(
        self, dataset: Dataset, output_dir: Path | None = None, max_workers: int = 5
    ) -> list[Path]:
        """
        Download an entire dataset asynchronously.

        Note: For large datasets, request a bulk API key from KNMI.
        """
        if output_dir is None:
            output_dir = self.config.get_subdir(
                "open_data", dataset.name, dataset.version
            )

        # Collect all filenames
        all_files = []
        next_token = None

        while True:
            files, next_token = self.list_files(
                dataset, max_keys=500, next_page_token=next_token
            )
            all_files.extend(files)

            if not next_token:
                break

        logger.info(f"Found {len(all_files)} files in dataset {dataset.full_name}")

        # Download files concurrently
        loop = asyncio.get_event_loop()
        executor = ThreadPoolExecutor(max_workers=max_workers)

        async def download_single(file: DatasetFile) -> Path | None:
            try:
                return await loop.run_in_executor(
                    executor,
                    lambda: self.download_file(dataset, file.filename, output_dir),
                )
            except Exception as e:
                logger.error(f"Failed to download {file.filename}: {e}")
                return None

        tasks = [download_single(f) for f in all_files]
        results = await asyncio.gather(*tasks)

        return [r for r in results if r is not None]


# ============================================================================
# EDR API Client
# ============================================================================


class EDRCollection(Enum):
    """Available EDR collections."""

    TEN_MIN_OBSERVATIONS = "10-minute-in-situ-meteorological-observations"
    HOURLY_VALIDATED = "hourly-in-situ-meteorological-observations-validated"
    DAILY_VALIDATED = "daily-in-situ-meteorological-observations-validated"
    TEMP_MEAN = "Tg1"
    TEMP_MAX = "Tx1"
    TEMP_MIN = "Tn1"
    PRECIPITATION = "Rd1"
    EVAPORATION = "EV24"
    WIND = "wins50"


class EDRAPIClient(BaseAPIClient):
    """
    Client for KNMI Environmental Data Retrieval (EDR) API.

    Allows spatio-temporal queries on meteorological datasets.
    Documentation: https://developer.dataplatform.knmi.nl/edr-api
    """

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": self.config.edr_api_key}

    def get_collections(self, save_result: bool = True) -> list[dict[str, Any]]:
        """Get list of available collections."""
        url = f"{self.config.edr_base_url}/collections"
        response = self._make_request("GET", url)
        data = response.json()

        # Save collections metadata
        if save_result:
            self._save_json(
                data, self.config.get_subdir("edr", "metadata") / "collections.json"
            )

        return data.get("collections", [])

    def get_collection_metadata(
        self,
        collection: EDRCollection | str,
        save_result: bool = True,
    ) -> dict[str, Any]:
        """Get metadata for a specific collection."""
        collection_name = (
            collection.value if isinstance(collection, EDRCollection) else collection
        )
        url = f"{self.config.edr_base_url}/collections/{collection_name}"

        response = self._make_request("GET", url)
        data = response.json()

        # Save collection metadata
        if save_result:
            self._save_json(
                data,
                self.config.get_subdir("edr", "metadata")
                / f"{collection_name}_metadata.json",
            )

        return data

    def get_locations(
        self,
        collection: EDRCollection | str,
        save_result: bool = True,
    ) -> list[Location]:
        """Get all measurement locations for a collection."""
        collection_name = (
            collection.value if isinstance(collection, EDRCollection) else collection
        )
        url = f"{self.config.edr_base_url}/collections/{collection_name}/locations"

        cache_path = self.config.get_subdir("edr", collection_name) / "locations.geojson"

        try:
            response = self._make_request("GET", url)
            data = response.json()
        except Exception:
            if not cache_path.exists():
                raise
            logger.warning(
                "Falling back to cached KNMI locations from %s after request failure.",
                cache_path,
                exc_info=True,
            )
            with open(cache_path, encoding="utf-8") as fh:
                data = json.load(fh)

        # Save locations data
        if save_result:
            self._save_json(
                data,
                cache_path,
            )

        locations = []
        for feature in data.get("features", []):
            coords = feature.get("geometry", {}).get("coordinates", [0, 0])
            properties = feature.get("properties", {})
            elevation = properties.get("height_above_mean_sea_level")
            if elevation is None:
                elevation = properties.get("elevation")
            locations.append(
                Location(
                    id=feature.get("id", ""),
                    name=properties.get("name", ""),
                    latitude=coords[1] if len(coords) > 1 else 0,
                    longitude=coords[0] if len(coords) > 0 else 0,
                    elevation=(float(elevation) if elevation is not None else None),
                    properties=properties,
                )
            )

        return locations

    def query_location(
        self,
        collection: EDRCollection | str,
        location_id: str,
        datetime_range: str | None = None,
        parameters: list[str] | None = None,
        target: str = "local",
        save_query: bool = True,
    ) -> dict[str, Any]:
        """
        Query data for a specific location.

        Args:
            collection: The collection to query
            location_id: Station/location ID (e.g., "0-20000-0-06260")
            datetime_range: ISO datetime range (e.g., "2024-01-01T00:00:00Z/2024-01-02T00:00:00Z")
            parameters: List of parameter names (e.g., ["ta", "rh", "ff"])
            target: Storage target — ``"local"`` (default) or ``"database"``.
            save_query: Save individual query result to disk. Set to ``False``
                when the caller handles persistence (e.g. unified daily files).

        Returns:
            CoverageJSON data
        """
        collection_name = (
            collection.value if isinstance(collection, EDRCollection) else collection
        )
        url = f"{self.config.edr_base_url}/collections/{collection_name}/locations/{location_id}"

        params = {}
        if datetime_range:
            params["datetime"] = datetime_range
        if parameters:
            params["parameter-name"] = ",".join(parameters)

        response = self._make_request("GET", url, params=params)
        data = response.json()

        # Save the query result (local mode only)
        if save_query:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{location_id}_{timestamp}.json"
            self._save_json(
                data,
                self.config.get_subdir("edr", collection_name, "queries") / filename,
            )

        return data

    def query_position(
        self,
        collection: EDRCollection | str,
        coords: tuple[float, float],
        datetime_range: str | None = None,
        parameters: list[str] | None = None,
        save_query: bool = True,
    ) -> dict[str, Any]:
        """
        Query data at a specific coordinate position.

        Args:
            collection: The collection to query
            coords: (longitude, latitude) tuple
            datetime_range: ISO datetime range
            parameters: List of parameter names

        Returns:
            CoverageJSON data
        """
        collection_name = (
            collection.value if isinstance(collection, EDRCollection) else collection
        )
        url = f"{self.config.edr_base_url}/collections/{collection_name}/position"

        params = {"coords": f"POINT({coords[0]} {coords[1]})"}
        if datetime_range:
            params["datetime"] = datetime_range
        if parameters:
            params["parameter-name"] = ",".join(parameters)

        response = self._make_request("GET", url, params=params)
        data = response.json()

        # Save the query result
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"position_{coords[0]}_{coords[1]}_{timestamp}.json"
        if save_query:
            self._save_json(
                data,
                self.config.get_subdir("edr", collection_name, "queries") / filename,
            )

        return data

    def query_area(
        self,
        collection: EDRCollection | str,
        polygon_coords: list[tuple[float, float]],
        datetime_range: str | None = None,
        parameters: list[str] | None = None,
        save_query: bool = True,
    ) -> dict[str, Any]:
        """
        Query data within a polygon area.

        Args:
            collection: The collection to query
            polygon_coords: List of (longitude, latitude) tuples defining the polygon
            datetime_range: ISO datetime range
            parameters: List of parameter names
        """
        collection_name = (
            collection.value if isinstance(collection, EDRCollection) else collection
        )
        url = f"{self.config.edr_base_url}/collections/{collection_name}/area"

        coords_str = ", ".join([f"{lon} {lat}" for lon, lat in polygon_coords])
        params = {"coords": f"POLYGON(({coords_str}))"}
        if datetime_range:
            params["datetime"] = datetime_range
        if parameters:
            params["parameter-name"] = ",".join(parameters)

        response = self._make_request("GET", url, params=params)
        data = response.json()

        # Save the query result
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if save_query:
            self._save_json(
                data,
                self.config.get_subdir("edr", collection_name, "queries")
                / f"area_{timestamp}.json",
            )

        return data

    def download_all_locations_data(
        self,
        collection: EDRCollection | str,
        datetime_range: str,
        parameters: list[str] | None = None,
    ) -> list[Path]:
        """Download data for all locations in a collection."""
        locations = self.get_locations(collection)
        saved_files = []

        for loc in locations:
            try:
                data = self.query_location(
                    collection,
                    loc.id,
                    datetime_range=datetime_range,
                    parameters=parameters,
                )
                saved_files.append(
                    self.config.get_subdir(
                        "edr",
                        collection.value
                        if isinstance(collection, EDRCollection)
                        else collection,
                        "queries",
                    )
                )
            except Exception as e:
                logger.error(f"Failed to query location {loc.id}: {e}")

        return saved_files


# ============================================================================
# WMS API Client
# ============================================================================


class WMSAPIClient(BaseAPIClient):
    """
    Client for KNMI Web Map Service (WMS) API.

    Provides geospatial imagery for mapping applications.
    Documentation: https://developer.dataplatform.knmi.nl/wms
    """

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": self.config.wms_api_key}

    def get_capabilities(self, service_url: str, version: str = "1.3.0") -> str:
        """
        Get WMS capabilities document.

        Args:
            service_url: The WMS service URL for the dataset
            version: WMS version (1.0.0, 1.1.1, or 1.3.0)
        """
        params = {"SERVICE": "WMS", "REQUEST": "GetCapabilities", "VERSION": version}

        response = self._make_request("GET", service_url, params=params)

        # Save capabilities document
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = (
            self.config.get_subdir("wms", "capabilities")
            / f"capabilities_{timestamp}.xml"
        )
        self._save_text(response.text, filepath)

        return response.text

    def get_map(
        self,
        service_url: str,
        layers: list[str],
        bbox: tuple[float, float, float, float],
        width: int = 800,
        height: int = 600,
        crs: str = "EPSG:4326",
        format: str = "image/png",
        time: str | None = None,
        version: str = "1.3.0",
        output_filename: str | None = None,
    ) -> Path:
        """
        Get a map image from WMS.

        Args:
            service_url: The WMS service URL
            layers: List of layer names to include
            bbox: Bounding box (minx, miny, maxx, maxy)
            width: Image width in pixels
            height: Image height in pixels
            crs: Coordinate reference system
            format: Image format (image/png, image/jpeg, etc.)
            time: ISO timestamp for temporal data
            version: WMS version
            output_filename: Optional custom filename

        Returns:
            Path to the saved image file
        """
        params = {
            "SERVICE": "WMS",
            "REQUEST": "GetMap",
            "VERSION": version,
            "LAYERS": ",".join(layers),
            "BBOX": ",".join(map(str, bbox)),
            "WIDTH": width,
            "HEIGHT": height,
            "CRS" if version >= "1.3.0" else "SRS": crs,
            "FORMAT": format,
            "TRANSPARENT": "TRUE",
        }

        if time:
            params["TIME"] = time

        response = self._make_request("GET", service_url, params=params, stream=True)

        # Determine file extension
        ext = "png" if "png" in format else "jpeg" if "jpeg" in format else "img"

        if output_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"map_{layers[0]}_{timestamp}.{ext}"

        filepath = self.config.get_subdir("wms", "maps") / output_filename

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info(f"Saved WMS map: {filepath}")
        return filepath

    def get_feature_info(
        self,
        service_url: str,
        layers: list[str],
        bbox: tuple[float, float, float, float],
        x: int,
        y: int,
        width: int = 800,
        height: int = 600,
        info_format: str = "application/json",
    ) -> dict[str, Any]:
        """Get feature information at a specific pixel location."""
        params = {
            "SERVICE": "WMS",
            "REQUEST": "GetFeatureInfo",
            "VERSION": "1.3.0",
            "LAYERS": ",".join(layers),
            "QUERY_LAYERS": ",".join(layers),
            "BBOX": ",".join(map(str, bbox)),
            "WIDTH": width,
            "HEIGHT": height,
            "I": x,
            "J": y,
            "INFO_FORMAT": info_format,
            "CRS": "EPSG:4326",
        }

        response = self._make_request("GET", service_url, params=params)

        if "json" in info_format:
            data = response.json()
            # Save feature info
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._save_json(
                data,
                self.config.get_subdir("wms", "feature_info")
                / f"info_{timestamp}.json",
            )
            return data
        else:
            return {"raw": response.text}

    def download_time_series(
        self,
        service_url: str,
        layers: list[str],
        bbox: tuple[float, float, float, float],
        start_time: datetime,
        end_time: datetime,
        time_step: timedelta,
        width: int = 800,
        height: int = 600,
    ) -> list[Path]:
        """Download a series of map images over time."""
        saved_files = []
        current_time = start_time

        while current_time <= end_time:
            try:
                timestamp = current_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                filename = (
                    f"map_{layers[0]}_{current_time.strftime('%Y%m%d_%H%M%S')}.png"
                )

                filepath = self.get_map(
                    service_url=service_url,
                    layers=layers,
                    bbox=bbox,
                    width=width,
                    height=height,
                    time=timestamp,
                    output_filename=filename,
                )
                saved_files.append(filepath)
            except Exception as e:
                logger.error(f"Failed to download map for {current_time}: {e}")

            current_time += time_step

        return saved_files


# ============================================================================
# FDSN API Clients (Keyless)
# ============================================================================


class FDSNDataSelectClient(BaseAPIClient):
    """
    Client for KNMI FDSN DataSelect Web Service.

    Provides access to raw seismological waveform data in Mini-SEED format.
    Documentation: https://api.dataplatform.knmi.nl/fdsnws/dataselect/1/

    This API is keyless - no authentication required.
    """

    def _get_headers(self) -> dict[str, str]:
        return {}  # Keyless API

    @property
    def base_url(self) -> str:
        return f"{self.config.fdsn_base_url}/dataselect/1"

    def query(
        self,
        network: str = "NL",
        station: str | None = None,
        location: str | None = None,
        channel: str | None = None,
        starttime: datetime | None = None,
        endtime: datetime | None = None,
        output_filename: str | None = None,
    ) -> Path:
        """
        Query and download waveform data.

        Args:
            network: Network code (default: NL for Netherlands)
            station: Station code (e.g., "VKB")
            location: Location code
            channel: Channel code (e.g., "BHZ")
            starttime: Start time for data
            endtime: End time for data
            output_filename: Custom output filename

        Returns:
            Path to downloaded Mini-SEED file
        """
        url = f"{self.base_url}/query"

        params = {"network": network, "nodata": 404}
        if station:
            params["station"] = station
        if location:
            params["location"] = location
        if channel:
            params["channel"] = channel
        if starttime:
            params["starttime"] = starttime.strftime("%Y-%m-%dT%H:%M:%S")
        if endtime:
            params["endtime"] = endtime.strftime("%Y-%m-%dT%H:%M:%S")

        response = self._make_request("GET", url, params=params, stream=True)

        if output_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"waveform_{network}_{station or 'all'}_{timestamp}.mseed"

        filepath = self.config.get_subdir("fdsn", "dataselect") / output_filename

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.info(f"Downloaded waveform data: {filepath}")
        return filepath


class FDSNStationClient(BaseAPIClient):
    """
    Client for KNMI FDSN Station Web Service.

    Provides access to seismological network, station, and channel metadata.
    Documentation: https://api.dataplatform.knmi.nl/fdsnws/station/1/

    This API is keyless - no authentication required.
    """

    def _get_headers(self) -> dict[str, str]:
        return {}  # Keyless API

    @property
    def base_url(self) -> str:
        return f"{self.config.fdsn_base_url}/station/1"

    def query(
        self,
        network: str = "NL",
        station: str | None = None,
        channel: str | None = None,
        level: str = "station",
        format: str = "text",
        starttime: datetime | None = None,
        endtime: datetime | None = None,
        minlatitude: float | None = None,
        maxlatitude: float | None = None,
        minlongitude: float | None = None,
        maxlongitude: float | None = None,
        save_result: bool = True,
    ) -> dict[str, Any] | str:
        """
        Query station metadata.

        Args:
            network: Network code
            station: Station code (wildcards * and ? allowed)
            channel: Channel code
            level: Detail level (network, station, channel, response)
            format: Output format (text, xml, json, sc3ml)
            starttime: Filter by active time range
            endtime: Filter by active time range
            minlatitude/maxlatitude: Filter by latitude
            minlongitude/maxlongitude: Filter by longitude

        Returns:
            Station metadata in requested format
        """
        url = f"{self.base_url}/query"

        params = {"network": network, "level": level, "format": format, "nodata": 404}
        if station:
            params["station"] = station
        if channel:
            params["channel"] = channel
        if starttime:
            params["starttime"] = starttime.strftime("%Y-%m-%dT%H:%M:%S")
        if endtime:
            params["endtime"] = endtime.strftime("%Y-%m-%dT%H:%M:%S")
        if minlatitude is not None:
            params["minlatitude"] = minlatitude
        if maxlatitude is not None:
            params["maxlatitude"] = maxlatitude
        if minlongitude is not None:
            params["minlongitude"] = minlongitude
        if maxlongitude is not None:
            params["maxlongitude"] = maxlongitude

        response = self._make_request("GET", url, params=params)

        # Save the response
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = (
            "json"
            if format == "json"
            else "xml"
            if format in ["xml", "fdsnxml", "sc3ml"]
            else "txt"
        )
        filename = f"stations_{network}_{timestamp}.{ext}"
        filepath = self.config.get_subdir("fdsn", "station") / filename

        if format == "json":
            data = response.json()
            if save_result:
                self._save_json(data, filepath)
            return data
        else:
            if save_result:
                self._save_text(response.text, filepath)
            return response.text

    def get_all_stations(
        self, network: str = "NL", save_result: bool = True
    ) -> dict[str, Any]:
        """Get all stations in a network as JSON."""
        if save_result:
            return self.query(network=network, level="station", format="json")
        return self.query(
            network=network,
            level="station",
            format="json",
            save_result=False,
        )

    def get_station_channels(
        self, network: str, station: str, save_result: bool = True
    ) -> dict[str, Any]:
        """Get all channels for a specific station."""
        if save_result:
            return self.query(
                network=network, station=station, level="channel", format="json"
            )
        return self.query(
            network=network,
            station=station,
            level="channel",
            format="json",
            save_result=False,
        )


class FDSNEventClient(BaseAPIClient):
    """
    Client for KNMI FDSN Event Web Service.

    Provides access to seismological event parameters (earthquake catalog).
    Documentation: https://api.dataplatform.knmi.nl/fdsnws/event/1/

    This API is keyless - no authentication required.
    """

    def _get_headers(self) -> dict[str, str]:
        return {}  # Keyless API

    @property
    def base_url(self) -> str:
        return f"{self.config.fdsn_base_url}/event/1"

    def query(
        self,
        starttime: datetime | None = None,
        endtime: datetime | None = None,
        minmagnitude: float | None = None,
        maxmagnitude: float | None = None,
        mindepth: float | None = None,
        maxdepth: float | None = None,
        minlatitude: float | None = None,
        maxlatitude: float | None = None,
        minlongitude: float | None = None,
        maxlongitude: float | None = None,
        limit: int | None = None,
        format: str = "text",
        orderby: str = "time",
        save_result: bool = True,
    ) -> list[SeismicEvent] | str | dict[str, Any]:
        """
        Query seismic events (earthquakes).

        Args:
            starttime: Start of time range
            endtime: End of time range
            minmagnitude/maxmagnitude: Filter by magnitude
            mindepth/maxdepth: Filter by depth (km)
            minlatitude/maxlatitude: Filter by latitude
            minlongitude/maxlongitude: Filter by longitude
            limit: Maximum number of events to return
            format: Output format (text, xml, json, csv, qml)
            orderby: Sort order (time, time-asc, magnitude, magnitude-asc)

        Returns:
            List of SeismicEvent objects (for text/csv), or raw response
        """
        url = f"{self.base_url}/query"

        params = {"format": format, "orderby": orderby, "nodata": 404}
        if starttime:
            params["starttime"] = starttime.strftime("%Y-%m-%dT%H:%M:%S")
        if endtime:
            params["endtime"] = endtime.strftime("%Y-%m-%dT%H:%M:%S")
        if minmagnitude is not None:
            params["minmagnitude"] = minmagnitude
        if maxmagnitude is not None:
            params["maxmagnitude"] = maxmagnitude
        if mindepth is not None:
            params["mindepth"] = mindepth
        if maxdepth is not None:
            params["maxdepth"] = maxdepth
        if minlatitude is not None:
            params["minlatitude"] = minlatitude
        if maxlatitude is not None:
            params["maxlatitude"] = maxlatitude
        if minlongitude is not None:
            params["minlongitude"] = minlongitude
        if maxlongitude is not None:
            params["maxlongitude"] = maxlongitude
        if limit is not None:
            params["limit"] = limit

        try:
            response = self._make_request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            # nodata=404 is standard FDSN: 404 means "no matching events"
            if exc.response is not None and exc.response.status_code == 404:
                logger.info("FDSN query returned 404 (no events found).")
                if format == "text":
                    return []
                elif format == "json":
                    return {}
                return ""
            raise

        # Save the response
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = format if format in ["json", "csv", "xml"] else "txt"
        filename = f"events_{timestamp}.{ext}"
        filepath = self.config.get_subdir("fdsn", "events") / filename

        if format == "json":
            data = response.json()
            if save_result:
                self._save_json(data, filepath)
            return data
        elif format == "csv":
            if save_result:
                self._save_text(response.text, filepath)
            return response.text
        elif format == "text":
            if save_result:
                self._save_text(response.text, filepath)
            # Parse text format into SeismicEvent objects
            events = self._parse_text_events(response.text)
            return events
        else:
            if save_result:
                self._save_text(response.text, filepath)
            return response.text

    def _parse_text_events(self, text: str) -> list[SeismicEvent]:
        """Parse text format event response into SeismicEvent objects."""
        events = []
        lines = text.strip().split("\n")

        for line in lines:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 11:
                try:
                    # Parse timestamp with multiple fallback methods
                    time_str = parts[1].strip()
                    event_time = None

                    # Try standard ISO format first
                    try:
                        event_time = datetime.fromisoformat(
                            time_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass

                    # Try parsing with fractional seconds variations
                    if event_time is None:
                        for fmt in [
                            "%Y-%m-%dT%H:%M:%S.%f",
                            "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S.%f",
                            "%Y-%m-%d %H:%M:%S",
                        ]:
                            try:
                                # Pad fractional seconds if needed (e.g., .6 -> .600000)
                                if "." in time_str and "T" in time_str:
                                    base, frac = time_str.split(".")
                                    # Remove any timezone suffix
                                    frac = frac.rstrip("Z").split("+")[0].split("-")[0]
                                    frac = frac.ljust(6, "0")[:6]
                                    time_str_padded = f"{base}.{frac}"
                                else:
                                    time_str_padded = time_str
                                event_time = datetime.strptime(time_str_padded, fmt)
                                break
                            except ValueError:
                                continue

                    if event_time is None:
                        logger.warning(f"Could not parse timestamp: {parts[1].strip()}")
                        continue

                    # Parse magnitude safely
                    mag_str = parts[10].strip() if len(parts) > 10 else ""
                    try:
                        magnitude = float(mag_str) if mag_str else 0.0
                    except ValueError:
                        magnitude = 0.0

                    # Parse depth safely
                    depth_str = parts[4].strip()
                    try:
                        depth = float(depth_str) if depth_str else 0.0
                    except ValueError:
                        depth = 0.0

                    events.append(
                        SeismicEvent(
                            event_id=parts[0].strip(),
                            time=event_time,
                            latitude=float(parts[2].strip()),
                            longitude=float(parts[3].strip()),
                            depth=depth,
                            magnitude=magnitude,
                            magnitude_type=parts[9].strip() if len(parts) > 9 else None,
                            description=parts[12].strip() if len(parts) > 12 else None,
                        )
                    )
                except (ValueError, IndexError) as e:
                    logger.warning(
                        f"Failed to parse event line: {line[:100]}..., error: {e}"
                    )

        return events

    def get_recent_events(
        self,
        days: int = 30,
        min_magnitude: float = 0.0,
        save_result: bool = True,
    ) -> list[SeismicEvent]:
        """Get recent seismic events."""
        endtime = datetime.now(UTC)
        starttime = endtime - timedelta(days=days)

        query_kwargs = {
            "starttime": starttime,
            "endtime": endtime,
            "minmagnitude": min_magnitude,
            "format": "text",
        }
        if not save_result:
            query_kwargs["save_result"] = False
        return self.query(**query_kwargs)

    def get_all_events(self, save_result: bool = True) -> list[SeismicEvent]:
        """Get all events in the catalog."""
        if save_result:
            return self.query(format="text")
        return self.query(format="text", save_result=False)


# ============================================================================
# Shakemaps API Client (Keyless)
# ============================================================================


class ShakemapsClient(BaseAPIClient):
    """
    Client for KNMI ShakeMap Web Service.

    Provides access to ShakeMap files for earthquake ground motion visualization.
    Documentation: https://api.dataplatform.knmi.nl/shakemaps/7/

    This API is keyless - no authentication required.
    """

    def _get_headers(self) -> dict[str, str]:
        return {}  # Keyless API

    @property
    def base_url(self) -> str:
        return self.config.shakemaps_base_url

    def list_events(self, save_result: bool = True) -> list[dict[str, Any]]:
        """Get list of all events with available ShakeMaps."""
        url = f"{self.base_url}/list"
        response = self._make_request("GET", url)
        data = response.json()

        # Save event list
        if save_result:
            self._save_json(
                data,
                self.config.get_subdir("shakemaps", "metadata") / "event_list.json",
            )

        # API may return events in a nested structure (dict with "events" key) or as a list
        if isinstance(data, dict):
            # Try common keys for event lists
            events = data.get("events") or data.get("data") or data.get("list") or []
            if isinstance(events, list):
                return events
            # If still a dict, return it as a single-item list
            return [data] if data else []
        elif isinstance(data, list):
            return data
        else:
            return []

    def get_event_files(self, event_id: str) -> list[str]:
        """Get list of available files for an event."""
        url = f"{self.base_url}/files"
        params = {"eventid": event_id}

        response = self._make_request("GET", url, params=params)
        data = response.json()

        return data.get("files", [])

    def download_file(
        self, event_id: str, filename: str, output_dir: Path | None = None
    ) -> Path:
        """
        Download a specific ShakeMap file.

        Available filenames:
        - event_dat.xml: Input peak ground motion parameters
        - event.xml: Input earthquake parameters
        - grid.nc: NetCDF grid with PGA, PGV, PSA values
        - rrsm.json: Peak ground motion in RRSM JSON format
        - rrsm.csv: Peak ground motion in CSV format
        """
        url = f"{self.base_url}/download/{event_id}/{filename}"

        if output_dir is None:
            output_dir = self.config.get_subdir("shakemaps", "data", event_id)

        filepath = output_dir / filename
        return self._download_file(url, filepath)

    def download_all_event_files(self, event_id: str) -> list[Path]:
        """Download all available files for an event."""
        files = self.get_event_files(event_id)
        downloaded = []

        for filename in files:
            try:
                path = self.download_file(event_id, filename)
                downloaded.append(path)
            except Exception as e:
                logger.error(f"Failed to download {filename} for event {event_id}: {e}")

        return downloaded

    def download_all_shakemaps(self) -> dict[str, list[Path]]:
        """Download all ShakeMap data for all events."""
        events = self.list_events()
        all_downloads = {}

        for event in events:
            event_id = event.get("eventid") or event.get("id")
            if event_id:
                try:
                    downloaded = self.download_all_event_files(event_id)
                    all_downloads[event_id] = downloaded
                except Exception as e:
                    logger.error(f"Failed to download files for event {event_id}: {e}")

        return all_downloads


# ============================================================================
# Notification Service Client
# ============================================================================


class NotificationServiceClient:
    """
    Client for KNMI Notification Service.

    Receives real-time MQTT notifications when new data is published.
    Documentation: https://developer.dataplatform.knmi.nl/notification-service

    Requires paho-mqtt>=2.0 library.
    """

    def __init__(self, config: KNMIConfig):
        self.config = config
        self.client = None
        self.client_id = str(uuid.uuid4())
        self._message_callback: Callable | None = None

    def _ensure_paho(self):
        """Ensure paho-mqtt is available."""
        try:
            import paho.mqtt.client as mqtt_client
            import paho.mqtt.properties as properties

            return mqtt_client, properties
        except ImportError:
            raise ImportError(
                "paho-mqtt>=2.0 is required for the Notification Service. "
                "Install it with: pip install paho-mqtt>=2.0"
            )

    def connect(
        self,
        on_message: Callable[[str, bytes], None] | None = None,
        client_id: str | None = None,
    ):
        """
        Connect to the KNMI notification service.

        Args:
            on_message: Callback function(topic, payload) for received messages
            client_id: Custom client ID (use same ID to resume sessions)
        """
        mqtt_client, properties = self._ensure_paho()

        if client_id:
            self.client_id = client_id

        self._message_callback = on_message

        def on_connect(c, userdata, flags, reason_code, props=None):
            logger.info("Connected to KNMI Notification Service")
            logger.info(f"Session present: {flags.session_present}")

        def on_message_handler(c, userdata, message):
            logger.info(f"Received message on topic {message.topic}")

            # Save message to file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            topic_safe = message.topic.replace("/", "_")
            filename = f"{topic_safe}_{timestamp}.json"

            output_dir = self.config.get_subdir("notifications", "messages")

            try:
                payload = json.loads(message.payload)
            except json.JSONDecodeError:
                payload = {"raw": message.payload.decode("utf-8", errors="replace")}

            with open(output_dir / filename, "w") as f:
                json.dump(
                    {
                        "topic": message.topic,
                        "timestamp": timestamp,
                        "payload": payload,
                    },
                    f,
                    indent=2,
                )

            if self._message_callback:
                self._message_callback(message.topic, message.payload)

        self.client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            protocol=mqtt_client.MQTTv5,
            transport="websockets",
        )

        self.client.tls_set(tls_version=ssl.PROTOCOL_TLS)

        connect_properties = properties.Properties(properties.PacketTypes.CONNECT)
        connect_properties.SessionExpiryInterval = 3600  # 1 hour max

        self.client.username_pw_set("token", self.config.notification_api_key)
        self.client.on_connect = on_connect
        self.client.on_message = on_message_handler

        self.client.connect(
            host=self.config.notification_broker,
            port=self.config.notification_port,
            keepalive=60,
            clean_start=False,
            properties=connect_properties,
        )

        return self

    def subscribe(self, topic: str, qos: int = 1):
        """
        Subscribe to a notification topic.

        Topic structure: dataplatform/file/v1/{dataset_name}/{version}/{created|updated}

        Examples:
            - "dataplatform/file/v1/10-minute-in-situ-meteorological-observations/1.0/created"
            - "dataplatform/file/v1/radar-reflectivity-composites/2.0/#"  (wildcard)
        """
        if not self.client:
            raise RuntimeError("Not connected. Call connect() first.")

        def on_subscribe(c, userdata, mid, reason_codes, props):
            logger.info(f"Subscribed to topic: {topic}")

        self.client.on_subscribe = on_subscribe
        self.client.subscribe(topic, qos=qos)

    def subscribe_to_dataset(
        self,
        dataset_name: str,
        version: str = "1.0",
        event_type: str = "#",  # "#" for both created and updated
    ):
        """
        Subscribe to notifications for a specific dataset.

        Args:
            dataset_name: Name of the dataset
            version: Dataset version
            event_type: "created", "updated", or "#" for both
        """
        topic = f"dataplatform/file/v1/{dataset_name}/{version}/{event_type}"
        self.subscribe(topic)

    def start_listening(self, blocking: bool = True):
        """
        Start listening for notifications.

        Args:
            blocking: If True, blocks forever. If False, runs in background.
        """
        if not self.client:
            raise RuntimeError("Not connected. Call connect() first.")

        if blocking:
            self.client.loop_forever()
        else:
            self.client.loop_start()

    def stop(self):
        """Stop listening and disconnect."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


# ============================================================================
# Main Ingestion Manager
# ============================================================================


class KNMIIngestionManager:
    """
    Main class for managing KNMI data ingestion across all APIs.

    Provides a unified interface to download and manage data from all KNMI services.
    """

    def __init__(self, config: KNMIConfig | None = None):
        self.config = config or KNMIConfig()

        # Initialize all API clients
        self.open_data = OpenDataAPIClient(self.config)
        self.edr = EDRAPIClient(self.config)
        self.wms = WMSAPIClient(self.config)
        self.fdsn_dataselect = FDSNDataSelectClient(self.config)
        self.fdsn_station = FDSNStationClient(self.config)
        self.fdsn_event = FDSNEventClient(self.config)
        self.shakemaps = ShakemapsClient(self.config)
        self._notification_client: NotificationServiceClient | None = None

    @property
    def notifications(self) -> NotificationServiceClient:
        """Lazy-load notification client (requires paho-mqtt)."""
        if self._notification_client is None:
            self._notification_client = NotificationServiceClient(self.config)
        return self._notification_client

    def ingest_meteorological_observations(
        self,
        dataset_name: str = "10-minute-in-situ-meteorological-observations",
        version: str = "1.0",
        hours_back: int = 24,
        target: str = "local",
    ) -> dict[str, Any]:
        """
        Ingest recent meteorological observation data.

        Downloads data from both Open Data API (files) and EDR API (structured queries).

        Args:
            dataset_name: Name of the Open Data dataset.
            version: Dataset version.
            hours_back: Number of hours to look back.
            target: Storage target — ``"local"`` (default) or ``"database"``.

        Returns:
            Dictionary with ingestion results including in-memory CoverageJSON
            payloads under ``edr_coverages`` (used by the DB persist path).
        """
        save_local = target == "local"
        results: dict[str, Any] = {
            "open_data_files": [],
            "edr_data": [],
            "edr_coverages": [],  # in-memory CoverageJSON for direct DB persist
        }

        dataset = Dataset(name=dataset_name, version=version)

        # Download recent files via Open Data API
        if save_local:
            logger.info(f"Downloading recent files from {dataset.full_name}")
            try:
                files = self.open_data.download_latest_files(dataset, count=10)
                results["open_data_files"] = [str(f) for f in files]
            except Exception as e:
                logger.error(f"Failed to download Open Data files: {e}")
        else:
            logger.info(
                "Database target selected: skipping KNMI Open Data file downloads."
            )

        # Query data via EDR API
        logger.info("Querying EDR API for observation data")
        try:
            # Get all locations
            locations = self.edr.get_locations(
                EDRCollection.TEN_MIN_OBSERVATIONS,
                save_result=save_local,
            )
            results["locations_count"] = len(locations)

            # Query recent data for a sample of locations
            end_time = datetime.now(UTC)
            start_time = end_time - timedelta(hours=hours_back)
            datetime_range = f"{start_time.isoformat()}/{end_time.isoformat()}"

            for loc in locations:  # Query all locations
                try:
                    data = self.edr.query_location(
                        EDRCollection.TEN_MIN_OBSERVATIONS,
                        loc.id,
                        datetime_range=datetime_range,
                        # temp, humidity, wind, pressure
                        parameters=["ta", "rh", "ff", "dd", "pp"],
                        target=target,
                        save_query=save_local,
                    )
                    # EDR may return CoverageCollection or single Coverage
                    coverages = data.get("coverages", [data])
                    total_points = sum(
                        len(
                            c.get("domain", {})
                            .get("axes", {})
                            .get("t", {})
                            .get("values", [])
                        )
                        for c in coverages
                    )
                    results["edr_data"].append(
                        {
                            "location_id": loc.id,
                            "location_name": loc.name,
                            "location_elevation_m": loc.elevation,
                            "data_points": total_points,
                        }
                    )
                    # Keep individual coverages in-memory for direct DB persist
                    for cov in coverages:
                        results["edr_coverages"].append(
                            {
                                "location_id": loc.id,
                                "location_name": loc.name,
                                "location_elevation_m": loc.elevation,
                                "coverage": cov,
                            }
                        )
                except requests.exceptions.HTTPError as e:
                    if (
                        hasattr(e, "response")
                        and e.response is not None
                        and e.response.status_code == 404
                        and "no data" in e.response.text.lower()
                    ):
                        logger.debug(f"No Observation Data found for location {loc.id}")
                    else:
                        logger.error(f"Failed to query location {loc.id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to query location {loc.id}: {e}")

        except Exception as e:
            logger.error(f"Failed to query EDR API: {e}")

        return results

    def ingest_seismological_data(
        self,
        days_back: int = 30,
        include_waveforms: bool = False,
        target: str = "local",
    ) -> dict[str, Any]:
        """
        Ingest seismological data from FDSN and Shakemaps APIs.
        """
        save_local = target == "local"
        results = {"stations": [], "events": [], "shakemaps": [], "waveforms": []}

        # Get all stations
        logger.info("Fetching seismic station metadata")
        try:
            stations = self.fdsn_station.get_all_stations(save_result=save_local)
            results["stations"] = stations
        except Exception as e:
            logger.error(f"Failed to get stations: {e}")

        # Get recent events
        logger.info(f"Fetching seismic events from last {days_back} days")
        try:
            events = self.fdsn_event.get_recent_events(
                days=days_back,
                save_result=save_local,
            )
            results["events"] = [
                {
                    "id": e.event_id,
                    "time": str(e.time),
                    "lat": e.latitude,
                    "lon": e.longitude,
                    "depth": e.depth,
                    "magnitude": e.magnitude,
                }
                for e in events
            ]
        except Exception as e:
            logger.error(f"Failed to get events: {e}")

        # Get shakemaps
        logger.info("Fetching ShakeMap data")
        try:
            shakemap_events = self.shakemaps.list_events(save_result=save_local)
            results["shakemaps_available"] = len(shakemap_events)

            if save_local:
                # Download data for recent events
                for event in shakemap_events[:5]:  # Sample 5 events
                    event_id = event.get("eventid") or event.get("id")
                    if event_id:
                        try:
                            files = self.shakemaps.download_all_event_files(event_id)
                            results["shakemaps"].append(
                                {"event_id": event_id, "files": [str(f) for f in files]}
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to download shakemaps for {event_id}: {e}"
                            )
            elif shakemap_events:
                logger.info(
                    "Database target selected: skipping ShakeMap file downloads."
                )
        except Exception as e:
            logger.error(f"Failed to get shakemaps: {e}")

        # Optionally download waveform data
        if include_waveforms and save_local:
            logger.info("Downloading sample waveform data")
            try:
                end_time = datetime.now(UTC)
                start_time = end_time - timedelta(hours=1)

                # Use a specific station to avoid 413 Request Entity Too Large
                # DBN (De Bilt) is a commonly available station
                waveform_file = self.fdsn_dataselect.query(
                    network="NL", station="DBN", starttime=start_time, endtime=end_time
                )
                results["waveforms"].append(str(waveform_file))
            except Exception as e:
                logger.error(f"Failed to download waveforms: {e}")
        elif include_waveforms:
            logger.info("Database target selected: skipping local waveform downloads.")

        return results

    def ingest_gridded_data(
        self,
        collections: list[EDRCollection] | None = None,
        days_back: int = 7,
        target: str = "local",
    ) -> dict[str, Any]:
        """
        Ingest gridded meteorological datasets (temperature, precipitation, etc.).

        Note: Gridded collections (Tg1, Tx1, Tn1, Rd1, Ev24) may have limited query support.
        This method first tries area query, then falls back to position query at De Bilt.
        """
        if collections is None:
            collections = [
                EDRCollection.TEMP_MEAN,
                EDRCollection.TEMP_MAX,
                EDRCollection.TEMP_MIN,
                EDRCollection.PRECIPITATION,
                EDRCollection.EVAPORATION,
            ]

        results = {"collections": {}}
        save_local = target == "local"

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=days_back)
        datetime_range = (
            f"{start_time.date().isoformat()}/{end_time.date().isoformat()}"
        )

        # De Bilt coordinates for fallback position query
        de_bilt_coords = (5.18, 52.10)  # (lon, lat)

        # Netherlands bounding box for area query
        netherlands_bbox = [
            (3.37, 50.75),  # SW
            (7.21, 50.75),  # SE
            (7.21, 53.47),  # NE
            (3.37, 53.47),  # NW
            (3.37, 50.75),  # Close polygon
        ]

        for collection in collections:
            logger.info(f"Ingesting gridded data from {collection.value}")
            try:
                # Try to get collection metadata (but don't fail if unavailable)
                metadata = {}
                data_queries = {}
                try:
                    metadata = self.edr.get_collection_metadata(
                        collection,
                        save_result=save_local,
                    )
                    data_queries = metadata.get("data_queries", {})
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 404:
                        logger.warning(
                            f"Collection metadata not found for {collection.value}, trying queries directly"
                        )
                    else:
                        raise
                except Exception as e:
                    logger.warning(
                        f"Could not get metadata for {collection.value}: {e}"
                    )

                data = None
                query_method = None
                last_error = None

                # Try position query first (more likely to be supported for gridded data)
                try:
                    data = self.edr.query_position(
                        collection,
                        coords=de_bilt_coords,
                        datetime_range=datetime_range,
                        save_query=save_local,
                    )
                    query_method = "position"
                except requests.exceptions.HTTPError as e:
                    last_error = e
                    if e.response.status_code == 404:
                        logger.debug(
                            f"Position query not available for {collection.value}"
                        )
                    else:
                        logger.debug(
                            f"Position query failed for {collection.value}: {e}"
                        )
                except Exception as e:
                    last_error = e
                    logger.debug(f"Position query failed for {collection.value}: {e}")

                # Try area query as fallback
                if data is None:
                    try:
                        data = self.edr.query_area(
                            collection,
                            polygon_coords=netherlands_bbox,
                            datetime_range=datetime_range,
                            save_query=save_local,
                        )
                        query_method = "area"
                    except requests.exceptions.HTTPError as e:
                        last_error = e
                        if e.response.status_code == 404:
                            logger.debug(
                                f"Area query not available for {collection.value}"
                            )
                    except Exception as e:
                        last_error = e
                        logger.debug(f"Area query failed for {collection.value}: {e}")

                if data is not None:
                    results["collections"][collection.value] = {
                        "metadata": metadata.get("title", collection.value),
                        "query_status": "success",
                        "query_method": query_method,
                        "datetime_range": datetime_range,
                    }
                else:
                    # Log why it failed
                    error_msg = (
                        str(last_error) if last_error else "No query method succeeded"
                    )
                    logger.warning(
                        f"No EDR query available for {collection.value}: {error_msg}"
                    )
                    results["collections"][collection.value] = {
                        "metadata": metadata.get("title", collection.value)
                        if metadata
                        else collection.value,
                        "query_status": "no_edr_support",
                        "note": "Collection may only support WMS/file access, not EDR queries",
                    }

            except Exception as e:
                logger.error(f"Failed to ingest {collection.value}: {e}")
                results["collections"][collection.value] = {
                    "query_status": "failed",
                    "error": str(e),
                }

        return results

    def full_ingestion(
        self,
        include_seismological: bool = True,
        include_gridded: bool = True,
        include_wms_samples: bool = False,
        days_back: int = 7,
        target: str = "local",
    ) -> dict[str, Any]:
        """
        Perform a full data ingestion from all available KNMI sources.

        This downloads and saves all possible data points to data/raw/knmi/.

        Args:
            include_seismological: Whether to include seismological data.
            include_gridded: Whether to include gridded datasets.
            include_wms_samples: Whether to download WMS map samples.
            days_back: Number of days of history to retrieve.
            target: Storage target — ``"local"`` (default) or ``"database"``.
        """
        persist_to_db = target == "database"
        save_local = target == "local"
        results = {
            "timestamp": datetime.now(UTC).isoformat(),
            "meteorological": {},
            "seismological": {},
            "gridded": {},
            "wms": {},
        }

        logger.info("KNMI full ingestion starting")

        # Meteorological observations
        logger.info("[1/4] Meteorological observations...")
        try:
            results["meteorological"] = self.ingest_meteorological_observations(
                hours_back=24,
                target=target,
            )
        except Exception as e:
            logger.error(f"Meteorological ingestion failed: {e}")
            results["meteorological"] = {"error": str(e)}

        # Seismological data
        if include_seismological:
            logger.info("[2/4] Seismological data...")
            try:
                results["seismological"] = self.ingest_seismological_data(
                    days_back=days_back,
                    include_waveforms=True,
                    target=target,
                )
            except Exception as e:
                logger.error(f"Seismological ingestion failed: {e}")
                results["seismological"] = {"error": str(e)}
        else:
            logger.info("[2/4] Seismological data — skipped")

        # Gridded data
        if include_gridded:
            logger.info("[3/4] Gridded datasets...")
            try:
                results["gridded"] = self.ingest_gridded_data(
                    days_back=days_back,
                    target=target,
                )
            except Exception as e:
                logger.error(f"Gridded data ingestion failed: {e}")
                results["gridded"] = {"error": str(e)}
        else:
            logger.info("[3/4] Gridded data — skipped")

        # WMS samples
        if include_wms_samples:
            logger.info(
                "[4/4] WMS map samples — requires specific dataset URLs, skipping"
            )
        else:
            logger.info("[4/4] WMS samples — skipped")

        # Save ingestion summary (local mode only)
        if save_local:
            summary_path = self.config.get_subdir("metadata") / "ingestion_summary.json"
            with open(summary_path, "w") as f:
                json.dump(results, f, indent=2, default=str)
        else:
            summary_path = None

        # Persist EDR observations to TimescaleDB (optional)
        if persist_to_db:
            self._persist_edr_to_db(results.get("meteorological", {}))

        logger.info("KNMI ingestion complete")
        if summary_path:
            logger.info("Summary saved to: %s", summary_path)

        return results

    def persist_observations(self, results: dict[str, Any]) -> None:
        """Persist ingestion results to TimescaleDB.

        Public entry-point for the orchestration layer.  Passes the
        *results* dict directly to :meth:`_persist_edr_to_db`, which
        reads the ``edr_coverages`` key populated by
        :meth:`ingest_meteorological_observations`.

        Args:
            results: The dict returned by
                :meth:`ingest_meteorological_observations`.
        """
        self._persist_edr_to_db(results)

    def _persist_edr_to_db(self, met_results: dict[str, Any]) -> None:
        """Persist in-memory EDR CoverageJSON data to TimescaleDB.

        Uses the ``edr_coverages`` list collected during
        :meth:`ingest_meteorological_observations` — each entry contains
        a ``location_id`` and the raw CoverageJSON ``coverage`` dict.
        """
        try:
            from avian_biosurveillance.db.loaders import upsert_knmi_observations
            from avian_biosurveillance.db.models import KNMIWeatherObservation

            coverages = met_results.get("edr_coverages", [])
            records: list[KNMIWeatherObservation] = []
            for entry in coverages:
                location_id = entry["location_id"]
                location_name = entry.get("location_name")
                location_elevation_m = entry.get("location_elevation_m")
                coverage = entry["coverage"]
                records.extend(
                    KNMIWeatherObservation.from_edr_coverage(
                        coverage,
                        location_id,
                        location_name,
                        location_elevation_m,
                    )
                )

            if records:
                n = upsert_knmi_observations(records)
                logger.info(f"DB: upserted {n} KNMI weather observations")
            else:
                logger.info("DB: no KNMI observations to persist")
        except Exception:
            logger.exception("DB upsert for KNMI observations failed")

    # -------------------------------------------------------------------------
    # Date-range ingestion helpers
    # -------------------------------------------------------------------------

    # All parameters we query from the EDR 10-minute collection.
    _EDR_PARAMETERS: list[str] = ["ta", "rh", "ff", "dd", "pp", "R1H", "qg", "n"]

    # Minimum pause between per-station EDR requests (seconds).
    # Keeps the request rate under ~10 req/s, well within KNMI's quota.
    _REQUEST_DELAY_S: float = 0.15

    # A day is "complete" when at least this fraction of stations have data.
    # Below the threshold the file is marked incomplete and re-ingested on
    # the next run, preventing silent data loss from partial outages.
    _COMPLETENESS_THRESHOLD: float = 0.80

    # Human-readable names map for the unified file statistics section.
    _PARAM_FIELD_MAP: dict[str, str] = {
        "ta": "temperature_c",
        "rh": "relative_humidity_pct",
        "ff": "wind_speed_ms",
        "dd": "wind_direction_deg",
        "pp": "pressure_hpa",
        "R1H": "precipitation_mm",
        "qg": "solar_radiation_wm2",
        "n": "cloud_cover_okta",
    }

    @staticmethod
    # type: ignore[name-defined]
    def _day_datetime_range(date: "datetime_date") -> str:
        """Return an ISO 8601 interval string for a full UTC calendar day.

        Example: ``"2024-01-15T00:00:00Z/2024-01-15T23:59:59Z"``
        """
        return f"{date.isoformat()}T00:00:00Z/{date.isoformat()}T23:59:59Z"

    @staticmethod
    def _safe_stats(values: list[float]) -> dict[str, Any]:
        """Return min/max/mean/null_count for a list that may contain None."""
        clean = [v for v in values if v is not None]
        null_count = len(values) - len(clean)
        if not clean:
            return {"min": None, "max": None, "mean": None, "null_count": null_count}
        return {
            "min": round(min(clean), 4),
            "max": round(max(clean), 4),
            "mean": round(sum(clean) / len(clean), 4),
            "null_count": null_count,
        }

    def _coverage_to_observations(
        self,
        coverage: dict[str, Any],
        location_id: str,
        location_name: str | None,
        location_elevation_m: float | None = None,
    ) -> dict[str, Any]:
        """Convert a single CoverageJSON coverage to the unified station record.

        Returns a dict with keys: station_id, station_name, latitude, longitude,
        elevation_m, observation_count, completeness_pct, statistics, observations.
        """
        domain = coverage.get("domain", {})
        axes = domain.get("axes", {})
        timestamps: list[str] = axes.get("t", {}).get("values", [])
        x_vals = axes.get("x", {}).get("values")
        y_vals = axes.get("y", {}).get("values")
        lat = y_vals[0] if y_vals else None
        lon = x_vals[0] if x_vals else None

        ranges = coverage.get("ranges", {})

        # Build the raw time-series keyed by EDR parameter code → field name.
        param_series: dict[str, list[float | None]] = {}
        for edr_param, field_name in self._PARAM_FIELD_MAP.items():
            vals = ranges.get(edr_param, {}).get("values")
            if vals is None:
                vals = [None] * len(timestamps)
            param_series[field_name] = vals

        # Build per-observation dicts.
        observations: list[dict[str, Any]] = []
        for i, ts in enumerate(timestamps):
            obs: dict[str, Any] = {"timestamp": ts}
            for field_name, series in param_series.items():
                obs[field_name] = series[i] if i < len(series) else None
            observations.append(obs)

        # Per-field statistics.
        statistics: dict[str, Any] = {
            field_name: self._safe_stats(series)
            for field_name, series in param_series.items()
        }

        # Expected number of 10-min intervals in a full day.
        expected_obs = 144  # 24 h × 6 per hour
        completeness_pct = (
            round(len(observations) / expected_obs * 100, 1)
            if expected_obs > 0
            else 0.0
        )

        return {
            "station_id": location_id,
            "station_name": location_name,
            "latitude": lat,
            "longitude": lon,
            "elevation_m": location_elevation_m,
            "observation_count": len(observations),
            "completeness_pct": completeness_pct,
            "statistics": statistics,
            "observations": observations,
        }

    def _build_unified_daily_file(
        self,
        date: "datetime_date",  # type: ignore[name-defined]
        station_records: list[dict[str, Any]],
        ingested_at: str,
        collection: str,
        *,
        total_queried: int = 0,
        failed_stations: list[str] | None = None,
        no_data_stations: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build the content of ``daily_unified.json`` for one date.

        Args:
            date: The calendar date being described.
            station_records: List of per-station dicts from
                ``_coverage_to_observations``.
            ingested_at: ISO timestamp of when the ingestion ran.
            collection: EDR collection name queried.
            total_queried: Total number of stations attempted.
            failed_stations: Station IDs that raised non-404 errors.
            no_data_stations: Station IDs that returned 404 "no data".

        Returns:
            Dict ready for JSON serialisation.
        """
        failed_stations = failed_stations or []
        no_data_stations = no_data_stations or []

        total_obs = sum(s["observation_count"] for s in station_records)
        stations_with_data = sum(
            1 for s in station_records if s["observation_count"] > 0
        )

        # Determine completeness relative to the number of stations queried.
        queried = total_queried or len(station_records)
        completeness_pct = (
            round(stations_with_data / queried * 100, 1) if queried > 0 else 0.0
        )
        is_complete = (
            stations_with_data > 0
            and completeness_pct >= self._COMPLETENESS_THRESHOLD * 100
        )

        # Network-wide statistics per field (across all stations).
        network_stats: dict[str, Any] = {}
        for field_name in self._PARAM_FIELD_MAP.values():
            all_vals: list[float] = []
            for s in station_records:
                series_stats = s["statistics"].get(field_name, {})
                # Re-compute from raw observations for accuracy.
                all_vals.extend(
                    obs[field_name]
                    for obs in s["observations"]
                    if obs.get(field_name) is not None
                )
            if all_vals:
                network_stats[field_name] = {
                    **self._safe_stats(all_vals),
                    "station_count": sum(
                        1
                        for s in station_records
                        if any(
                            obs.get(field_name) is not None for obs in s["observations"]
                        )
                    ),
                }
            else:
                network_stats[field_name] = {
                    "min": None,
                    "max": None,
                    "mean": None,
                    "station_count": 0,
                }

        return {
            "metadata": {
                "date": date.isoformat(),
                "ingested_at": ingested_at,
                "source": "KNMI EDR API",
                "api_base_url": self.config.edr_base_url,
                "collection": collection,
                "parameters_queried": self._EDR_PARAMETERS,
                "parameter_descriptions": {
                    "ta": "Air temperature at 1.5 m (°C)",
                    "rh": "Relative humidity (%)",
                    "ff": "Wind speed at 10 m (m/s)",
                    "dd": "Wind direction (°)",
                    "pp": "Air pressure at MSL (hPa)",
                    "R1H": "Hourly precipitation (mm)",
                    "qg": "Global solar radiation (W/m²)",
                    "n": "Total cloud cover (okta)",
                },
                "time_range": {
                    "start": f"{date.isoformat()}T00:00:00Z",
                    "end": f"{date.isoformat()}T23:59:59Z",
                },
                "expected_obs_per_station": 144,
                "station_count": len(station_records),
                "stations_with_data": stations_with_data,
                "stations_queried": queried,
                "completeness_pct": completeness_pct,
                "failed_stations": failed_stations,
                "no_data_stations": no_data_stations,
                "total_observations": total_obs,
                "version": "1.1",
                # A day is "complete" when ≥ _COMPLETENESS_THRESHOLD of
                # stations returned data. Below the threshold the file
                # is automatically re-ingested on the next run so
                # partial outages don't cause silent data loss.
                "complete": is_complete,
            },
            "network_statistics": network_stats,
            "stations": station_records,
        }

    def ingest_day(
        self,
        date: "datetime_date",  # type: ignore[name-defined]
        collection: str = "10-minute-in-situ-meteorological-observations",
        target: str = "local",
        force: bool = False,
    ) -> dict[str, Any]:
        """Ingest all KNMI meteorological observations for a single calendar day.

        Data is saved under ``data/raw/knmi/<YYYY-MM-DD>/``:

        .. code-block:: text

            data/raw/knmi/
              2024-01-15/
                stations/
                  0-20000-0-06260.json   ← raw CoverageJSON per station
                  0-20000-0-06270.json
                  …
                daily_unified.json        ← merged observations + metadata

        Args:
            date: Calendar date to ingest (UTC).
            collection: EDR collection name (default: 10-min in-situ obs).
            target: Storage target — ``"local"`` (default) or ``"database"``.
            force: Re-download even if the day directory already exists.

        Returns:
            Dict with keys ``date``, ``station_count``, ``total_observations``,
            ``stations_with_data``, ``skipped``, ``unified_file``, and
            ``edr_coverages`` (for DB persistence).
        """
        persist_to_db = target == "database"
        save_local = target == "local"

        date_str = date.isoformat()
        day_dir = self.config.data_dir / date_str
        unified_path = day_dir / "daily_unified.json"

        # Idempotency check — skip if already done and marked complete.
        # A day is "complete" when ≥ 80% of stations returned data (see
        # _COMPLETENESS_THRESHOLD).  Below that or missing the flag the
        # file is re-ingested, merging with the previous run's stations
        # so no data is lost.
        if save_local and not force and unified_path.exists():
            try:
                with open(unified_path, encoding="utf-8") as fh:
                    existing = json.load(fh)
                is_complete = existing.get("metadata", {}).get("complete", False)
            except Exception:
                is_complete = False

            if is_complete:
                logger.info(
                    f"[{date_str}] Already ingested and complete "
                    "(daily_unified.json exists with complete=true). "
                    "Use force=True to re-download."
                )
                return {
                    "date": date_str,
                    "skipped": True,
                    "unified_file": str(unified_path),
                    "station_count": 0,
                    "stations_with_data": 0,
                    "total_observations": 0,
                    "edr_coverages": [],
                }
            else:
                logger.info(
                    f"[{date_str}] Existing file is incomplete (0 stations or "
                    "missing complete flag) — re-ingesting."
                )

        logger.info(f"[{date_str}] Starting ingestion …")
        ingested_at = datetime.now(UTC).isoformat()
        datetime_range = self._day_datetime_range(date)

        # Collect locations (cached per manager instance to avoid re-fetching).
        if not hasattr(self, "_cached_locations"):
            self._cached_locations: list[Location] = self.edr.get_locations(
                EDRCollection.TEN_MIN_OBSERVATIONS
            )
        locations = self._cached_locations

        station_records: list[dict[str, Any]] = []
        edr_coverages: list[dict[str, Any]] = []  # for DB persistence
        failed_stations: list[str] = []  # non-404 HTTP / connection errors
        no_data_stations: list[str] = []  # 404 "no data" responses

        # Load previous incomplete run (if any) so we can merge rather
        # than overwrite.  Keyed by station_id for O(1) lookup.
        prev_by_station: dict[str, dict[str, Any]] = {}
        if save_local and not force and unified_path.exists():
            try:
                with open(unified_path, encoding="utf-8") as fh:
                    prev_unified = json.load(fh)
                for s in prev_unified.get("stations", []):
                    sid = s.get("station_id", "")
                    if sid and s.get("observation_count", 0) > 0:
                        prev_by_station[sid] = s
            except Exception:
                pass  # corrupt file — ignore previous data

        # Create directories
        stations_dir = day_dir / "stations"
        if save_local:
            stations_dir.mkdir(parents=True, exist_ok=True)

        for loc in locations:
            try:
                raw = self.edr.query_location(
                    EDRCollection.TEN_MIN_OBSERVATIONS,
                    loc.id,
                    datetime_range=datetime_range,
                    parameters=self._EDR_PARAMETERS,
                    save_query=False,  # we handle saving ourselves
                )
            except requests.exceptions.HTTPError as e:
                if (
                    e.response is not None
                    and e.response.status_code == 404
                    and "no data" in e.response.text.lower()
                ):
                    logger.debug(f"[{date_str}] No data for {loc.id}")
                    no_data_stations.append(loc.id)
                else:
                    logger.warning(f"[{date_str}] HTTP error for {loc.id}: {e}")
                    failed_stations.append(loc.id)
                continue
            except Exception as e:
                logger.warning(f"[{date_str}] Failed to query {loc.id}: {e}")
                failed_stations.append(loc.id)
                continue
            finally:
                # Pace requests to stay within KNMI's per-minute quota.
                time.sleep(self._REQUEST_DELAY_S)

            coverages_raw = raw.get("coverages", [raw])
            for cov in coverages_raw:
                if save_local:
                    # Save raw CoverageJSON for reproducibility.
                    station_file = stations_dir / f"{loc.id}.json"
                    with open(station_file, "w", encoding="utf-8") as fh:
                        json.dump(cov, fh, indent=2, default=str)

                # Convert to structured station record.
                station_record = self._coverage_to_observations(
                    cov,
                    loc.id,
                    loc.name,
                    loc.elevation,
                )
                station_records.append(station_record)

                # Queue for DB persistence.
                edr_coverages.append(
                    {
                        "location_id": loc.id,
                        "location_name": loc.name,
                        "location_elevation_m": loc.elevation,
                        "coverage": cov,
                    }
                )

        # Merge: keep stations from previous run that we failed to
        # re-fetch this time (prevents data loss on partial outages).
        new_ids = {s["station_id"] for s in station_records}
        for sid, prev_rec in prev_by_station.items():
            if sid not in new_ids:
                station_records.append(prev_rec)
                logger.debug(f"[{date_str}] Retained {sid} from previous run")

        logger.info(
            f"[{date_str}] Fetched {len(station_records)} stations "
            f"({len(failed_stations)} failed, "
            f"{len(no_data_stations)} no-data), "
            f"{sum(s['observation_count'] for s in station_records)} "
            "observations."
        )

        # Build and save the unified daily file.
        unified = self._build_unified_daily_file(
            date=date,
            station_records=station_records,
            ingested_at=ingested_at,
            collection=collection,
            total_queried=len(locations),
            failed_stations=failed_stations,
            no_data_stations=no_data_stations,
        )

        if save_local:
            with open(unified_path, "w", encoding="utf-8") as fh:
                json.dump(unified, fh, indent=2, default=str)
            logger.info(f"[{date_str}] Saved unified file → {unified_path}")

        # Persist to DB if requested.
        if persist_to_db and edr_coverages:
            self._persist_edr_to_db({"edr_coverages": edr_coverages})

        return {
            "date": date_str,
            "skipped": False,
            "unified_file": str(unified_path) if save_local else None,
            "station_count": len(station_records),
            "stations_with_data": unified["metadata"]["stations_with_data"],
            "total_observations": unified["metadata"]["total_observations"],
            "edr_coverages": edr_coverages,
        }

    def ingest_date_range(
        self,
        start_date: "datetime_date",  # type: ignore[name-defined]
        end_date: "datetime_date",  # type: ignore[name-defined]
        collection: str = "10-minute-in-situ-meteorological-observations",
        target: str = "local",
        force: bool = False,
    ) -> dict[str, Any]:
        """Ingest KNMI meteorological observations for a date range, day by day.

        Each calendar day is processed independently so that:
        - Large date ranges (months/years) do not exhaust memory.
        - A partial failure only affects the current day; already-ingested
          days are preserved on disk.
        - Re-runs are idempotent: existing ``daily_unified.json`` files are
          skipped unless ``force=True``.

        Data layout on disk::

            data/raw/knmi/
              YYYY-MM-DD/
                stations/           ← raw per-station CoverageJSON
                daily_unified.json  ← merged observations + metadata
              YYYY-MM-DD/
                …

        Args:
            start_date: First date to ingest (inclusive).
            end_date: Last date to ingest (inclusive).
            collection: EDR collection name.
            target: Storage target — ``"local"`` (default) or ``"database"``.
            force: Re-download days that already have a unified file.

        Returns:
            Dict with ``start_date``, ``end_date``, ``days_total``,
            ``days_ingested``, ``days_skipped``, ``days_failed``,
            ``total_observations``, and a per-day ``results`` list.
        """
        persist_to_db = target == "database"

        if end_date < start_date:
            raise ValueError(
                f"end_date ({end_date}) must be >= start_date ({start_date})"
            )

        total_days = (end_date - start_date).days + 1
        logger.info(
            f"Ingesting {total_days} day(s): {start_date} → {end_date} "
            f"(persist_to_db={persist_to_db}, force={force})"
        )

        per_day_results: list[dict[str, Any]] = []
        days_ingested = days_skipped = days_failed = total_obs = 0

        current = start_date
        while current <= end_date:
            try:
                day_result = self.ingest_day(
                    date=current,
                    collection=collection,
                    target=target,
                    force=force,
                )
                per_day_results.append(day_result)

                if day_result["skipped"]:
                    days_skipped += 1
                else:
                    days_ingested += 1
                    total_obs += day_result["total_observations"]

            except Exception as e:
                logger.error(f"[{current}] Day ingestion failed: {e}", exc_info=True)
                per_day_results.append(
                    {
                        "date": current.isoformat(),
                        "skipped": False,
                        "failed": True,
                        "error": str(e),
                    }
                )
                days_failed += 1

            current += timedelta(days=1)

        summary = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days_total": total_days,
            "days_ingested": days_ingested,
            "days_skipped": days_skipped,
            "days_failed": days_failed,
            "total_observations": total_obs,
            "results": per_day_results,
        }

        logger.info(
            f"Date-range ingestion complete: {days_ingested} ingested, "
            f"{days_skipped} skipped, {days_failed} failed. "
            f"{total_obs:,} total observations."
        )
        return summary


# ============================================================================
# Convenience Functions
# ============================================================================


def create_ingestion_manager(data_dir: str | None = None) -> KNMIIngestionManager:
    """Create an ingestion manager with optional custom data directory."""
    config = KNMIConfig()
    if data_dir:
        config.data_dir = Path(data_dir)
        config.data_dir.mkdir(parents=True, exist_ok=True)
    return KNMIIngestionManager(config)


def download_latest_observations(
    dataset_name: str = "10-minute-in-situ-meteorological-observations", count: int = 10
) -> list[Path]:
    """Quick function to download latest observation files."""
    manager = create_ingestion_manager()
    dataset = Dataset(name=dataset_name, version="1.0")
    return manager.open_data.download_latest_files(dataset, count=count)


def get_recent_earthquakes(
    days: int = 30,
    min_magnitude: float = 0.0,
    save_result: bool = True,
) -> list[SeismicEvent]:
    """Quick function to get recent earthquake data."""
    manager = create_ingestion_manager()
    if save_result:
        return manager.fdsn_event.get_recent_events(
            days=days,
            min_magnitude=min_magnitude,
        )
    return manager.fdsn_event.get_recent_events(
        days=days,
        min_magnitude=min_magnitude,
        save_result=False,
    )


def query_weather_at_location(
    latitude: float,
    longitude: float,
    hours_back: int = 24,
    parameters: list[str] | None = None,
) -> dict[str, Any]:
    """Quick function to query weather data at a specific location."""
    if parameters is None:
        # temp, humidity, wind speed/dir, pressure
        parameters = ["ta", "rh", "ff", "dd", "pp"]

    manager = create_ingestion_manager()

    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(hours=hours_back)
    datetime_range = f"{start_time.isoformat()}/{end_time.isoformat()}"

    return manager.edr.query_position(
        EDRCollection.TEN_MIN_OBSERVATIONS,
        coords=(longitude, latitude),
        datetime_range=datetime_range,
        parameters=parameters,
    )


# ============================================================================
# CLI Entry Point
# ============================================================================


def main():
    """Command-line entry point for KNMI data ingestion."""
    import argparse

    parser = argparse.ArgumentParser(
        description="KNMI Data Ingestion Module",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Ingest a specific date range (daily loop, one directory per day)
    python knmi.py --ingest --start-date 2024-01-01 --end-date 2024-01-31

    # Ingest a single day
    python knmi.py --ingest --start-date 2024-06-15

    # Ingest last N hours (hourly/live mode — no date structure)
    python knmi.py --ingest --hours-back 2 --persist-to-db

    # Re-download a range even if already ingested
    python knmi.py --ingest --start-date 2024-01-01 --end-date 2024-01-07 --force

    # Run full ingestion (all KNMI sources)
    python knmi.py --full

    # Download latest observation files
    python knmi.py --observations --count 20

    # Get recent earthquakes
    python knmi.py --earthquakes --days 60

    # Query weather at a location
    python knmi.py --weather --lat 52.37 --lon 4.89

    # Start notification listener (requires paho-mqtt)
    python knmi.py --notifications --topic "dataplatform/file/v1/radar-reflectivity-composites/2.0/#"
        """,
    )

    # --- Mode flags ---
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Ingest meteorological observations (date-range or hours-back mode)",
    )
    parser.add_argument(
        "--full", action="store_true", help="Run full data ingestion (all KNMI sources)"
    )
    parser.add_argument(
        "--observations",
        action="store_true",
        help="Download latest observation files from Open Data API",
    )
    parser.add_argument(
        "--earthquakes",
        action="store_true",
        help="Get earthquake data from FDSN Event API",
    )
    parser.add_argument(
        "--weather", action="store_true", help="Query weather at a specific coordinate"
    )
    parser.add_argument(
        "--notifications",
        action="store_true",
        help="Start real-time MQTT notification listener",
    )

    # --- Date-range options ---
    parser.add_argument(
        "--start-date",
        "-s",
        type=str,
        metavar="YYYY-MM-DD",
        help=(
            "Start date for ingestion (inclusive). "
            "If provided without --end-date, only this single day is ingested. "
            "If neither --start-date nor --end-date is given, --hours-back is used."
        ),
    )
    parser.add_argument(
        "--end-date",
        "-e",
        type=str,
        metavar="YYYY-MM-DD",
        help=(
            "End date for ingestion (inclusive, default: today). Requires --start-date."
        ),
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Hours of history to ingest when no --start-date is given "
            "(live/hourly Dagster mode). Default: 2."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download days that already have a daily_unified.json file.",
    )

    # --- Shared options ---
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of files to download (--observations mode)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days of history to retrieve (--earthquakes / --full mode)",
    )
    parser.add_argument("--lat", type=float, help="Latitude for --weather query")
    parser.add_argument("--lon", type=float, help="Longitude for --weather query")
    parser.add_argument("--topic", type=str, help="MQTT topic for --notifications mode")
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data/raw/knmi",
        help="Root output data directory",
    )
    parser.add_argument(
        "--target",
        choices=["local", "database"],
        default="local",
        help="Storage target: local (disk only, default) or database (TimescaleDB only)",
    )

    args = parser.parse_args()

    # Build shared config / manager.
    config = KNMIConfig()
    config.data_dir = Path(args.data_dir)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    manager = KNMIIngestionManager(config)

    # ── --ingest mode ────────────────────────────────────────────────────────
    if args.ingest:
        if args.start_date:
            # Date-range (or single-day) mode.
            try:
                start = datetime_date.fromisoformat(args.start_date)
            except ValueError:
                print(
                    f"Error: --start-date '{args.start_date}' is not a valid YYYY-MM-DD date."
                )
                sys.exit(1)

            if args.end_date:
                try:
                    end = datetime_date.fromisoformat(args.end_date)
                except ValueError:
                    print(
                        f"Error: --end-date '{args.end_date}' is not a valid YYYY-MM-DD date."
                    )
                    sys.exit(1)
            else:
                end = start  # single day

            if end < start:
                print(f"Error: --end-date ({end}) must be >= --start-date ({start}).")
                sys.exit(1)

            results = manager.ingest_date_range(
                start_date=start,
                end_date=end,
                target=args.target,
                force=args.force,
            )
            print(
                json.dumps(
                    {k: v for k, v in results.items() if k != "results"},
                    indent=2,
                    default=str,
                )
            )
            print(f"\nPer-day breakdown ({len(results['results'])} days):")
            for day in results["results"]:
                status = (
                    "SKIP "
                    if day.get("skipped")
                    else "FAIL "
                    if day.get("failed")
                    else "OK   "
                )
                obs = day.get("total_observations", 0)
                print(f"  {status} {day['date']}  {obs:>7,} obs")

        else:
            # Live / hourly mode — no date directories, just raw EDR ingest.
            logger.info(f"Live mode: ingesting last {args.hours_back} hours.")
            results = manager.ingest_meteorological_observations(
                hours_back=args.hours_back,
                target=args.target,
            )
            if args.target == "database":
                manager._persist_edr_to_db(results)
            print(
                json.dumps(
                    {k: v for k, v in results.items() if k != "edr_coverages"},
                    indent=2,
                    default=str,
                )
            )

    # ── --full mode ──────────────────────────────────────────────────────────
    elif args.full:
        results = manager.full_ingestion(
            days_back=args.days,
            target=args.target,
        )
        print(json.dumps(results, indent=2, default=str))

    # ── --observations ───────────────────────────────────────────────────────
    elif args.observations:
        files = download_latest_observations(count=args.count)
        print(f"Downloaded {len(files)} observation files:")
        for f in files:
            print(f"  - {f}")

    # ── --earthquakes ────────────────────────────────────────────────────────
    elif args.earthquakes:
        events = get_recent_earthquakes(days=args.days)
        print(f"Found {len(events)} earthquakes in the last {args.days} days:")
        for e in events:
            print(
                f"  - {e.time.strftime('%Y-%m-%d %H:%M')} | M{e.magnitude:.1f} | "
                f"{e.latitude:.2f}, {e.longitude:.2f}"
            )

        # Persist to TimescaleDB
        if args.target == "database" and events:
            try:
                from avian_biosurveillance.db.loaders import upsert_seismic_events
                from avian_biosurveillance.db.models import (
                    SeismicEvent as SeismicEventModel,
                )

                models = [
                    SeismicEventModel.from_api(
                        {
                            "event_id": e.event_id,
                            "time": e.time.isoformat() if e.time else None,
                            "latitude": e.latitude,
                            "longitude": e.longitude,
                            "depth": e.depth,
                            "magnitude": e.magnitude,
                            "magnitude_type": e.magnitude_type,
                            "description": e.description,
                        }
                    )
                    for e in events
                ]
                upsert_seismic_events(models)
                print(f"  DB: upserted {len(models)} seismic events")
            except Exception as exc:
                print(f"  DB: seismic event upsert failed: {exc}")

    # ── --weather ────────────────────────────────────────────────────────────
    elif args.weather:
        if not args.lat or not args.lon:
            print("Error: --lat and --lon are required for --weather query")
            sys.exit(1)
        data = query_weather_at_location(args.lat, args.lon)
        print(json.dumps(data, indent=2, default=str))

    # ── --notifications ──────────────────────────────────────────────────────
    elif args.notifications:
        if not args.topic:
            print("Error: --topic is required for --notifications mode")
            sys.exit(1)
        print(f"Starting notification listener for topic: {args.topic}")
        print("Press Ctrl+C to stop")
        manager.notifications.connect()
        manager.notifications.subscribe(args.topic)
        manager.notifications.start_listening(blocking=True)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
