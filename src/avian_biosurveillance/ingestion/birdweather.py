#!/usr/bin/env python3
"""BirdWeather Data Ingestion Module.

This module provides a comprehensive data ingestion pipeline for the BirdWeather API.
It includes full API coverage, bug fixes, and support for all available data endpoints
including real-time subscriptions, station management, and detection analytics.

Key Features:
    - **Comprehensive API Coverage**: Support for all BirdWeather GraphQL endpoints.
    - **Robust Error Handling**: Automatic retries, rate limiting, and backoff strategies.
    - **Real-time Subscriptions**: WebSocket support for live detection events.
    - **Data Ingestion**: Tools for bulk downloading stations, species, and detections.
    - **Audio/Image Download**: Parallel downloading of media files with resume capability.
    - **Analytics**: Methods for fetching aggregated counts and time-series data.

Notes:
    - Fixes period parameter format (from/to instead of start/end).
    - Fixes species query response parsing and BirdNET sightings.
    - Adds missing API endpoints (timeOfDayDetectionCounts, topSpecies, etc.).

Example:
    To collect data for the last 24 hours:

    .. code-block:: bash

        python -m avian_biosurveillance.ingestion.birdweather --hours 24
"""

import argparse
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from queue import Queue
from typing import Any

import requests

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.config.species import registry
from avian_biosurveillance.utils.phylopic import resolve_phylopic
from avian_biosurveillance.utils.progress import tqdm
from avian_biosurveillance.utils.runtime_paths import preferred_temp_dir


load_runtime_env()

_DEFAULT_PAGE_SLEEP_SECONDS = 0.1

# Smallest size (in bytes) for a usable downloaded audio body. A 0-byte body is
# an empty/dropped CDN response (a 200 with no payload) rather than a genuine
# clip, and is rejected for retry instead of persisted as an undecodable file.
_MIN_VALID_AUDIO_BYTES = 1


def _page_sleep() -> None:
    """Throttle BirdWeather pagination with an env-tunable delay."""
    raw_value = os.getenv("BIRDWEATHER_PAGE_SLEEP_SECONDS")
    if raw_value is None:
        delay = _DEFAULT_PAGE_SLEEP_SECONDS
    else:
        try:
            delay = max(0.0, float(raw_value))
        except ValueError:
            delay = _DEFAULT_PAGE_SLEEP_SECONDS
    if delay > 0:
        time.sleep(delay)


# --- Configuration & Constants ---

API_ENDPOINT = "https://app.birdweather.com/graphql"

# Default Bounding Box: Netherlands
DEFAULT_NE = {"lat": 53.7, "lon": 7.22}
DEFAULT_SW = {"lat": 50.75, "lon": 3.33}

HEADERS = {
    "User-Agent": "AvianBiosurveillance/2.0 (Research Project; contact@example.com)",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# --- Comprehensive GraphQL Queries ---

# ============================================================================
# STATIONS QUERIES
# ============================================================================

QUERY_STATIONS_COMPREHENSIVE = """
query Stations($ne: InputLocation, $sw: InputLocation, $cursor: String, $query: String, $period: InputDuration) {
  stations(
    ne: $ne
    sw: $sw
    query: $query
    period: $period
    first: 50
    after: $cursor
  ) {
    pageInfo { hasNextPage endCursor }
    totalCount
    nodes {
      id
      name
      type
      location
      timezone
      continent
      country
      state
      notes
      audioUrl
      videoUrl
      source
      
      coords { lat lon }
      
      # Station settings
      locationPrivacy
      portableDetections
      hasProbabilities
      minConfidence
      minProbability
      minScore
      eclipse
      
      # Timestamps
      earliestDetectionAt
      latestDetectionAt
      
      # Environmental Data
      airPollution {
        aqi
        co
        nh3
        no
        no2
        o3
        pm10
        pm2_5
        so2
        timestamp
        coords { lat lon }
      }
      
      weather {
        temp
        tempMin
        tempMax
        feelsLike
        humidity
        pressure
        seaLevel
        groundLevel
        cloudiness
        visibility
        windSpeed
        windDir
        windGust
        rain1h
        rain3h
        snow1h
        snow3h
        sunrise
        sunset
        description
        timestamp
        coords { lat lon }
      }
      
      # Sensor Data
      sensors {
        environment {
          temperature
          humidity
          barometricPressure
          aqi
          voc
          eco2
          soundPressureLevel
          timestamp
        }
        environmentHistory(first: 48) { 
          nodes { 
            temperature 
            humidity 
            barometricPressure
            aqi
            voc
            eco2
            soundPressureLevel
            timestamp 
          } 
        }
        light {
          clear
          nir
          f1 f2 f3 f4 f5 f6 f7 f8
          timestamp
        }
        lightHistory(first: 48) {
          nodes { 
            clear nir f1 f2 f3 f4 f5 f6 f7 f8 timestamp 
          }
        }
        accel {
          x y z
          timestamp
        }
        location {
          lat lon altitude satellites
          timestamp
        }
        locationHistory(first: 48) {
          nodes { lat lon altitude satellites timestamp }
        }
        mag {
          x y z
          timestamp
        }
        system {
          batteryVoltage
          wifiRssi
          firmware
          powerSource
          sdAvailable
          sdCapacity
          uploadingCompleted
          uploadingTotal
          timestamp
        }
        systemHistory(first: 48) {
          nodes {
            batteryVoltage wifiRssi firmware powerSource
            sdAvailable sdCapacity uploadingCompleted uploadingTotal timestamp
          }
        }
      }
      
      # Aggregates
      counts {
        species
        detections
      }
    }
  }
}
"""

QUERY_STATION_SINGLE = """
query Station($id: ID!, $period: InputDuration, $topSpeciesLimit: Int, $sensorHistoryLimit: Int, $detectionsFirst: Int) {
  station(id: $id) {
    id
    name
    type
    location
    timezone
    continent
    country
    state
    notes
    audioUrl
    videoUrl
    source
    
    coords { lat lon }
    
    locationPrivacy
    portableDetections
    hasProbabilities
    minConfidence
    minProbability
    minScore
    eclipse
    
    earliestDetectionAt
    latestDetectionAt
    
    airPollution {
      aqi co nh3 no no2 o3 pm10 pm2_5 so2 timestamp
      coords { lat lon }
    }
    
    weather {
      temp tempMin tempMax feelsLike humidity pressure
      seaLevel groundLevel cloudiness visibility
      windSpeed windDir windGust
      rain1h rain3h snow1h snow3h
      sunrise sunset description timestamp
      coords { lat lon }
    }
    
    counts(period: $period) {
      species
      detections
    }
    
    # Detection counts binned by time (NEW)
    detectionCounts(period: $period) {
      speciesId
      count
      species {
        id
        commonName
        scientificName
      }
      bins {
        key
        count
      }
    }
    
    # Time-of-day detection counts (NEW)
    timeOfDayDetectionCounts(period: $period) {
      speciesId
      count
      species {
        id
        commonName
        scientificName
      }
      bins {
        key
        count
      }
    }
    
    # Recent detections (NEW)
    detections(first: $detectionsFirst) {
      totalCount
      nodes {
        id
        timestamp
        score
        confidence
        probability
        certainty
        speciesId
        species {
          id
          commonName
          scientificName
        }
        soundscape {
          id
          url
          duration
        }
      }
    }
    
    # Weekly probability predictions per species
    probabilities {
      speciesId
      species {
        id
        commonName
        scientificName
      }
      weeks
    }
    
    # Top detected species at this station
    topSpecies(limit: $topSpeciesLimit, period: $period) {
      speciesId
      count
      averageProbability
      species {
        id
        commonName
        scientificName
        imageUrl
        thumbnailUrl
      }
      breakdown {
        almostCertain
        veryLikely
        uncertain
        unlikely
      }
    }
    
    # Full sensor data with configurable history limit
    sensors {
      environment { temperature humidity barometricPressure aqi voc eco2 soundPressureLevel timestamp }
      environmentHistory(first: $sensorHistoryLimit) {
        totalCount
        nodes { temperature humidity barometricPressure aqi voc eco2 soundPressureLevel timestamp }
      }
      light { clear nir f1 f2 f3 f4 f5 f6 f7 f8 timestamp }
      lightHistory(first: $sensorHistoryLimit) {
        totalCount
        nodes { clear nir f1 f2 f3 f4 f5 f6 f7 f8 timestamp }
      }
      accel { x y z timestamp }
      accelHistory(first: $sensorHistoryLimit) {
        totalCount
        nodes { x y z timestamp }
      }
      location { lat lon altitude satellites timestamp }
      locationHistory(first: $sensorHistoryLimit) {
        totalCount
        nodes { lat lon altitude satellites timestamp }
      }
      mag { x y z timestamp }
      magHistory(first: $sensorHistoryLimit) {
        totalCount
        nodes { x y z timestamp }
      }
      system { batteryVoltage wifiRssi firmware powerSource sdAvailable sdCapacity uploadingCompleted uploadingTotal timestamp }
      systemHistory(first: $sensorHistoryLimit) {
        totalCount
        nodes { batteryVoltage wifiRssi firmware powerSource sdAvailable sdCapacity uploadingCompleted uploadingTotal timestamp }
      }
    }
  }
}
"""

QUERY_STATION_METADATA = """
query StationMetadata($id: ID!) {
    station(id: $id) {
        id
        name
        type
        location
        timezone
        continent
        country
        state
        audioUrl
        videoUrl
        source
        coords { lat lon }
        locationPrivacy
    }
}
"""

# ============================================================================
# STATION WEATHER QUERY (lightweight — for enriching detections)
# ============================================================================

QUERY_STATION_WEATHER = """
query StationWeather($id: ID!) {
  station(id: $id) {
    id
    name
    coords { lat lon }

    weather {
      temp
      tempMin
      tempMax
      feelsLike
      humidity
      pressure
      seaLevel
      groundLevel
      cloudiness
      visibility
      windSpeed
      windDir
      windGust
      rain1h
      rain3h
      snow1h
      snow3h
      sunrise
      sunset
      description
      timestamp
      coords { lat lon }
    }

    airPollution {
      aqi
      co
      nh3
      no
      no2
      o3
      pm10
      pm2_5
      so2
      timestamp
      coords { lat lon }
    }

    sensors {
      environment {
        temperature
        humidity
        barometricPressure
        aqi
        voc
        eco2
        soundPressureLevel
        timestamp
      }
    }
  }
}
"""

# ============================================================================
# DETECTIONS QUERIES
# ============================================================================

QUERY_DETECTIONS_COMPREHENSIVE = """
query Detections(
  $stationIds: [ID!], 
  $period: InputDuration,
  $cursor: String,
  # Score filters (all variants)
  $scoreGt: Float,
  $scoreLt: Float,
  $scoreGte: Float,
  $scoreLte: Float,
  # Confidence filters (all variants)
  $confidenceGt: Float,
  $confidenceLt: Float,
  $confidenceGte: Float,
  $confidenceLte: Float,
  # Probability filters (all variants)
  $probabilityGt: Float,
  $probabilityLt: Float,
  $probabilityGte: Float,
  $probabilityLte: Float,
  # Other filters
  $validSoundscape: Boolean,
  $recordingModes: [String!],
  $eclipse: Boolean,
  $timeOfDayGte: Int,
  $timeOfDayLte: Int,
  $countries: [String!],
  $continents: [String!],
  $speciesId: ID,
  $speciesIds: [ID!],
  $classifications: [String!],
  $stationTypes: [String!],
  $vote: Int,
  $sortBy: String,
  $uniqueStations: Boolean,
  $overrideStationFilters: Boolean,
  $ne: InputLocation,
  $sw: InputLocation
) {
  detections(
    stationIds: $stationIds
    period: $period
    scoreGt: $scoreGt
    scoreLt: $scoreLt
    scoreGte: $scoreGte
    scoreLte: $scoreLte
    confidenceGt: $confidenceGt
    confidenceLt: $confidenceLt
    confidenceGte: $confidenceGte
    confidenceLte: $confidenceLte
    probabilityGt: $probabilityGt
    probabilityLt: $probabilityLt
    probabilityGte: $probabilityGte
    probabilityLte: $probabilityLte
    validSoundscape: $validSoundscape
    recordingModes: $recordingModes
    eclipse: $eclipse
    timeOfDayGte: $timeOfDayGte
    timeOfDayLte: $timeOfDayLte
    countries: $countries
    continents: $continents
    speciesId: $speciesId
    speciesIds: $speciesIds
    classifications: $classifications
    stationTypes: $stationTypes
    vote: $vote
    sortBy: $sortBy
    uniqueStations: $uniqueStations
    overrideStationFilters: $overrideStationFilters
    ne: $ne
    sw: $sw
    first: 1000
    after: $cursor
  ) {
    pageInfo { hasNextPage endCursor }
    totalCount
    speciesCount
    nodes {
      id
      timestamp
      
      # Classification Metrics
      confidence
      score
      probability
      certainty
      eclipse
      
      # Taxonomy
      speciesId
      species {
        id
        commonName
        scientificName
        alpha
        alpha6
        ebirdCode
        color
        imageUrl
        thumbnailUrl
      }
      
      # Links & flags
      favoriteUrl
      flagUrl
      voteUrl
      
      # Geolocation
      coords { lat lon }
      
      # Audio / Soundscape
      soundscape {
        id
        url
        downloadFilename
        duration
        filesize
        startTime
        endTime
        timestamp
      }
      
      # Station Context
      station {
        id
        name
        type
        country
        state
        coords { lat lon }
      }
    }
  }
}
"""

# ============================================================================
# SPECIES QUERIES
# ============================================================================

QUERY_SPECIES_SEARCH = """
query SearchSpecies($cursor: String, $query: String, $searchLocale: String, $limit: Int) {
  searchSpecies(query: $query, first: 100, after: $cursor, searchLocale: $searchLocale, limit: $limit) {
    pageInfo { hasNextPage endCursor }
    totalCount
    nodes {
      id
      commonName
      scientificName
      alpha
      alpha6
      ebirdCode
      color
      birdweatherUrl
      ebirdUrl
      mlUrl
      wikipediaUrl
      wikipediaSummary
      imageUrl
      thumbnailUrl
      imageCredit
      imageLicense
      imageLicenseUrl
      predictionArea
      range
    }
  }
}
"""

QUERY_SPECIES_SINGLE = """
query Species($id: ID, $scientificName: String, $period: InputDuration, $stationTypes: [String!]) {
  species(id: $id, scientificName: $scientificName) {
    id
    commonName
    scientificName
    alpha
    alpha6
    ebirdCode
    color
    birdweatherUrl
    ebirdUrl
    mlUrl
    wikipediaUrl
    wikipediaSummary
    imageUrl
    thumbnailUrl
    imageCredit
    imageLicense
    imageLicenseUrl
    predictionArea
    range
    
    # Translations for localized names
    translations {
      locale
      commonName
      wikipediaSummary
      wikipediaUrl
    }
    
    # Detection counts over time (binned)
    detectionCounts(period: $period) {
      speciesId
      count
      bins {
        key
        count
      }
    }
    
    # Which stations detected this species
    stations(period: $period, stationTypes: $stationTypes) {
      count
      station {
        id
        name
        country
        coords { lat lon }
      }
    }
    
    # Top quality detections for this species
    topDetections(period: $period, first: 10) {
      totalCount
      nodes {
        id
        timestamp
        score
        confidence
        probability
        soundscape {
          id
          url
          duration
        }
        station {
          id
          name
        }
      }
    }
  }
}
"""

QUERY_ALL_SPECIES = """
query AllSpecies($ids: [ID!]!, $cursor: String) {
  allSpecies(ids: $ids, first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    totalCount
    nodes {
      id
      commonName
      scientificName
      alpha
      alpha6
      ebirdCode
      color
      imageUrl
      thumbnailUrl
      birdweatherUrl
      ebirdUrl
      wikipediaUrl
    }
  }
}
"""

# ============================================================================
# BIRDNET SIGHTINGS QUERY (Fixed)
# ============================================================================

QUERY_BIRDNET_SIGHTINGS = """
query BirdnetSightings(
  $cursor: String,
  $period: InputDuration,
  $ne: InputLocation,
  $sw: InputLocation,
  $speciesId: ID,
  $limit: Int,
  $offset: Int
) {
  birdnetSightings(
    first: 100
    after: $cursor
    period: $period
    ne: $ne
    sw: $sw
    speciesId: $speciesId
    limit: $limit
    offset: $offset
  ) {
    pageInfo { hasNextPage endCursor }
    totalCount
    nodes {
      id
      timestamp
      score
      coords { lat lon }
      speciesId
      species {
        id
        commonName
        scientificName
        alpha
        ebirdCode
      }
    }
  }
}
"""

# ============================================================================
# AGGREGATE / ANALYTICS QUERIES
# ============================================================================

QUERY_COUNTS = """
query Counts($period: InputDuration, $speciesId: ID, $stationIds: [ID!], $stationTypes: [String!], $ne: InputLocation, $sw: InputLocation) {
  counts(
    period: $period
    speciesId: $speciesId
    stationIds: $stationIds
    stationTypes: $stationTypes
    ne: $ne
    sw: $sw
  ) {
    detections
    species
    stations
    birdnet
    breakdown {
      stations {
        type
        count
      }
    }
  }
}
"""

QUERY_DAILY_DETECTION_COUNTS = """
query DailyDetectionCounts($period: InputDuration, $speciesIds: [ID!], $stationIds: [ID!]) {
  dailyDetectionCounts(period: $period, speciesIds: $speciesIds, stationIds: $stationIds) {
    date
    dayOfYear
    total
    counts {
      speciesId
      count
      species {
        id
        commonName
        scientificName
      }
    }
  }
}
"""

QUERY_TIME_OF_DAY_DETECTION_COUNTS = """
query TimeOfDayDetectionCounts(
  $period: InputDuration,
  $speciesId: ID,
  $stationIds: [ID!],
  $scoreGte: Float,
  $confidenceGte: Float,
  $probabilityGte: Float,
  $timeOfDayGte: Int,
  $timeOfDayLte: Int,
  $ne: InputLocation,
  $sw: InputLocation
) {
  timeOfDayDetectionCounts(
    period: $period
    speciesId: $speciesId
    stationIds: $stationIds
    scoreGte: $scoreGte
    confidenceGte: $confidenceGte
    probabilityGte: $probabilityGte
    timeOfDayGte: $timeOfDayGte
    timeOfDayLte: $timeOfDayLte
    ne: $ne
    sw: $sw
  ) {
    speciesId
    count
    species {
      id
      commonName
      scientificName
    }
    bins {
      key
      count
    }
  }
}
"""

QUERY_TOP_SPECIES = """
query TopSpecies(
  $limit: Int,
  $offset: Int,
  $period: InputDuration,
  $speciesId: ID,
  $stationTypes: [String!],
  $stationIds: [ID!],
  $ne: InputLocation,
  $sw: InputLocation
) {
  topSpecies(
    limit: $limit
    offset: $offset
    period: $period
    speciesId: $speciesId
    stationTypes: $stationTypes
    stationIds: $stationIds
    ne: $ne
    sw: $sw
  ) {
    speciesId
    count
    averageProbability
    species {
      id
      commonName
      scientificName
      imageUrl
      thumbnailUrl
      ebirdCode
    }
    breakdown {
      almostCertain
      veryLikely
      uncertain
      unlikely
    }
  }
}
"""

QUERY_TOP_BIRDNET_SPECIES = """
query TopBirdnetSpecies(
  $limit: Int,
  $offset: Int,
  $period: InputDuration,
  $speciesId: ID,
  $ne: InputLocation,
  $sw: InputLocation
) {
  topBirdnetSpecies(
    limit: $limit
    offset: $offset
    period: $period
    speciesId: $speciesId
    ne: $ne
    sw: $sw
  ) {
    speciesId
    count
    averageProbability
    species {
      id
      commonName
      scientificName
      imageUrl
      thumbnailUrl
    }
    breakdown {
      almostCertain
      veryLikely
      uncertain
      unlikely
    }
  }
}
"""

# --- Logging ---


class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[94m",  # Blue
        "INFO": "\033[92m",  # Green
        "WARNING": "\033[93m",  # Yellow
        "ERROR": "\033[91m",  # Red
        "CRITICAL": "\033[1;91m",  # Bold Red
    }
    RESET = "\033[0m"

    def format(self, record):
        log_fmt = f"{self.COLORS.get(record.levelname, self.RESET)}%(asctime)s - %(levelname)s - %(message)s{self.RESET}"
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter())

logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)


# --- Download Statistics ---


@dataclass
class DownloadStats:
    """Track statistics for file download operations.

    Attributes:
        total (int): Total number of files processed.
        downloaded (int): Number of files successfully downloaded.
        already_exists (int): Number of files skipped because they already exist.
        failed (int): Number of files that failed to download.
        no_url (int): Number of items skipped because no download URL was provided.
        errors (Dict[str, int]): Dictionary mapping error types to their occurrence count.
    """

    total: int = 0
    downloaded: int = 0
    already_exists: int = 0
    failed: int = 0
    no_url: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def add_error(self, error_type: str) -> None:
        """Record an error of the given type.

        Args:
            error_type (str): The category/name of the error to record.
        """
        self.errors[error_type] = self.errors.get(error_type, 0) + 1

    def summary(self) -> str:
        """Return a human-readable summary of download stats.

        Returns:
            str: Comma-separated summary string (e.g., "10 downloaded, 5 cached").
        """
        parts = []
        if self.downloaded:
            parts.append(f"{self.downloaded} downloaded")
        if self.already_exists:
            parts.append(f"{self.already_exists} cached")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.no_url:
            parts.append(f"{self.no_url} no URL")
        return ", ".join(parts) if parts else "no files"

    def error_summary(self) -> str:
        """Return a summary of error types.

        Returns:
            str: Formatted string of error counts, or empty string if no errors.
        """
        if not self.errors:
            return ""
        return " | Errors: " + ", ".join(f"{k}: {v}" for k, v in self.errors.items())


# --- Client ---


@dataclass
class BirdWeatherClient:
    """A robust HTTP client for the BirdWeather GraphQL API.

    Handles authentication, error handling, retries, and rate limiting for
    both the GraphQL API and file downloads.

    Attributes:
        session (requests.Session): Session for API requests.
        max_retries (int): Maximum number of retries for failed requests.
        base_backoff (float): Base seconds for exponential backoff.
        download_session (requests.Session): Session for file downloads.
    """

    session: requests.Session = field(default_factory=requests.Session)
    max_retries: int = 5
    base_backoff: float = 2.0
    download_session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        """Initialize headers and sessions."""
        import requests.adapters

        self.session.headers.update(HEADERS)
        self.download_session.headers.update({"User-Agent": HEADERS["User-Agent"]})

        # Increase connection pool size to handle 20+ parallel audio downloaders
        adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
        self.download_session.mount("http://", adapter)
        self.download_session.mount("https://", adapter)

    def _execute_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query with retry logic and error handling.

        Args:
            query (str): The GraphQL query string.
            variables (Optional[Dict[str, Any]]): Variables for the query.

        Returns:
            Dict[str, Any]: The JSON response data from the server.

        Raises:
            Exception: If max retries are exceeded or if the server returns GraphQL errors.
            requests.RequestException: For underlying network issues after retries.
        """
        payload = {"query": query, "variables": variables or {}}

        for attempt in range(self.max_retries):
            try:
                response = self.session.post(API_ENDPOINT, json=payload, timeout=60)

                if response.status_code == 429:
                    sleep_time = self.base_backoff * (2**attempt)
                    logger.warning(f"Rate limited (429). Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue

                if response.status_code >= 500:
                    sleep_time = self.base_backoff * (2**attempt)
                    logger.warning(
                        f"Server error ({response.status_code}). Retrying in {sleep_time}s..."
                    )
                    time.sleep(sleep_time)
                    continue

                try:
                    data = response.json()
                except json.JSONDecodeError:
                    if attempt == self.max_retries - 1:
                        raise RuntimeError("Invalid JSON response from server")
                    continue

                if "errors" in data:
                    logger.error(f"GraphQL Errors: {data['errors']}")
                    if not data.get("data"):
                        raise RuntimeError(f"GraphQL Error: {data['errors']}")

                return data

            except requests.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise RuntimeError(str(e)) from e
                logger.warning(f"Network error: {e}. Retrying...")
                time.sleep(self.base_backoff * (2**attempt))

        raise RuntimeError("Max retries exceeded")

    def download_file(
        self,
        url: str,
        path: Path,
        expected_size: int | None = None,
        max_retries: int = 3,
    ) -> tuple[bool, str]:
        """Download a file with retry logic and detailed status reporting.

        Args:
            url (str): URL to download from.
            path (Path): Local filesystem path to save the file.
            expected_size (Optional[int]): Expected file size in bytes for validation.
            max_retries (int): Maximum number of retry attempts.

        Returns:
            Tuple[bool, str]: A tuple containing (success, status).

            Status codes:
                - "downloaded": Successfully downloaded.
                - "already_exists": File already exists with correct size.
                - "not_found": 404 error.
                - "forbidden": 403 error.
                - "server_error": 5xx error.
                - "timeout": Request timed out.
                - "size_mismatch": Downloaded size doesn't match expected.
                - "empty_body": Response had an empty/truncated body (retryable).
                - "error": Other generic error.
        """
        try:
            # Check if already downloaded
            if path.exists():
                actual_size = path.stat().st_size
                if actual_size > 0:
                    return True, "already_exists"

            path.parent.mkdir(parents=True, exist_ok=True)

            for attempt in range(max_retries):
                try:
                    # We intentionally drop `stream=True` here and ingest the entire file into memory instantly (usually only ~200kb).
                    # BirdWeather's CDN drops connections brutally under parallel load during `iter_content` chunking.
                    with self.download_session.get(url, timeout=60) as r:
                        if r.status_code == 404:
                            logger.warning(f"Audio not found (404): {url}")
                            return False, "not_found"
                        elif r.status_code == 403:
                            body = r.text
                            if "InvalidObjectState" in body or "ARCHIVE_ACCESS" in body:
                                logger.debug(
                                    "Audio archived and unavailable via public BirdWeather media URL: %s",
                                    url,
                                )
                                return False, "archived"
                            logger.debug(
                                f"Access forbidden (403) - older file likely expired: {url}"
                            )
                            return False, "forbidden"
                        elif r.status_code >= 500:
                            if attempt < max_retries - 1:
                                wait_time = 2**attempt
                                logger.warning(
                                    f"Server error ({r.status_code}), retrying in {wait_time}s..."
                                )
                                time.sleep(wait_time)
                                continue
                            logger.error(
                                f"Server error ({r.status_code}) after {max_retries} attempts: {url}"
                            )
                            return False, "server_error"

                        r.raise_for_status()

                        # Write the payload in one gulp immediately
                        with open(path, "wb") as f:
                            f.write(r.content)

                        # We intentionally DO NOT validate exact byte sizes here anymore.
                        # BirdWeather's backend dynamically transcodes/compresses audio meaning
                        # the physical .m4a file size frequently differs from their database's listed `expected_size`.
                        # If the file successfully streams without network disconnects, we keep it!
                        #
                        # We DO, however, reject an empty/near-empty body: under parallel
                        # SSL/timeout churn the CDN sometimes returns a 200 with a dropped
                        # (zero-length) payload. Persisting that yields a 0-byte file that no
                        # decoder can open. Treat it as a transient failure so it is retried.
                        written = path.stat().st_size if path.exists() else 0
                        if written < _MIN_VALID_AUDIO_BYTES:
                            logger.warning(
                                "Empty/truncated audio body (%d byte(s)) for %s; discarding for retry.",
                                written,
                                url,
                            )
                            if path.exists():
                                path.unlink()
                            if attempt < max_retries - 1:
                                time.sleep(2**attempt)
                                continue
                            return False, "empty_body"

                        return True, "downloaded"

                except requests.exceptions.Timeout:
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(f"Timeout, retrying in {wait_time}s: {url}")
                        time.sleep(wait_time)
                        continue
                    logger.error(f"Timeout after {max_retries} attempts: {url}")
                    return False, "timeout"

                except requests.exceptions.RequestException as e:
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"Request error ({e}), retrying in {wait_time}s..."
                        )
                        time.sleep(wait_time)
                        continue
                    logger.error(
                        f"Request failed after {max_retries} attempts: {url}: {e}"
                    )
                    if path.exists():
                        path.unlink()
                    return False, "error"

            return False, "max_retries"

        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            if path.exists():
                path.unlink()
            return False, "error"

    # ========================================================================
    # STATION METHODS
    # ========================================================================

    def fetch_all_stations(
        self,
        ne: dict[str, Any] | None = None,
        sw: dict[str, Any] | None = None,
        query: str | None = None,
        period: dict[str, Any] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Fetch all stations within a bounding box with full field coverage.

        Args:
            ne (Dict, optional): Northeast corner coordinates (lat, lon).
            sw (Dict, optional): Southwest corner coordinates (lat, lon).
            query (str, optional): Search query string for filtering stations.
            period (Dict, optional): Time period for station metrics.

        Yields:
            Dict: A dictionary representing a station node.
        """
        cursor = None
        current_count = 0
        while True:
            vars = {
                "ne": ne,
                "sw": sw,
                "cursor": cursor,
                "query": query,
                "period": period,
            }
            data = self._execute_query(QUERY_STATIONS_COMPREHENSIVE, vars)

            connection = data.get("data", {}).get("stations", {})
            nodes = connection.get("nodes", [])
            page_info = connection.get("pageInfo", {})

            for node in nodes:
                yield node
                current_count += 1

            new_cursor = page_info.get("endCursor")
            if not new_cursor or new_cursor == cursor:
                logger.info(f"Finished fetching stations. Total: {current_count}")
                break

            cursor = new_cursor
            time.sleep(0.2)

    def fetch_station(
        self,
        station_id: str,
        period: dict = None,
        top_species_limit: int = 20,
        sensor_history_limit: int = 48,
        detections_first: int = 50,
    ) -> dict | None:
        """Fetch a single station with detailed data.

        Includes probabilities, top species, nested detection counts, and sensor history.

        Args:
            station_id (str): The unique identifier of the station.
            period (Dict, optional): Time period for counts and detections.
            top_species_limit (int): Max number of top species to return (default: 20).
            sensor_history_limit (int): Max history readings per sensor type (default: 48).
            detections_first (int): Max number of recent detections to return (default: 50).

        Returns:
            Optional[Dict]: Station data dictionary, or None if not found.
        """
        vars = {
            "id": station_id,
            "period": period,
            "topSpeciesLimit": top_species_limit,
            "sensorHistoryLimit": sensor_history_limit,
            "detectionsFirst": detections_first,
        }
        data = self._execute_query(QUERY_STATION_SINGLE, vars)
        return data.get("data", {}).get("station")

    def fetch_station_metadata(self, station_id: str) -> dict | None:
        """Fetch a single station's reference metadata without nested analytics."""
        data = self._execute_query(QUERY_STATION_METADATA, {"id": station_id})
        return data.get("data", {}).get("station")

    def fetch_station_weather(self, station_id: str) -> dict | None:
        """Fetch weather, air-pollution and sensor data for a station.

        Uses a lightweight query (no species/detection counts) to minimise
        API load when enriching detection records.

        Args:
            station_id: BirdWeather station ID.

        Returns:
            Dict with ``weather``, ``airPollution`` and ``sensors`` keys,
            or ``None`` when the station is not found.
        """
        data = self._execute_query(QUERY_STATION_WEATHER, {"id": station_id})
        return data.get("data", {}).get("station")

    # ========================================================================
    # DETECTION METHODS
    # ========================================================================

    def fetch_detections(
        self,
        station_ids: list[str] = None,
        start: datetime = None,
        end: datetime = None,
        # Score filters (all variants)
        score_gt: float = None,
        score_lt: float = None,
        score_gte: float = None,
        score_lte: float = None,
        # Confidence filters (all variants)
        confidence_gt: float = None,
        confidence_lt: float = None,
        confidence_gte: float = None,
        confidence_lte: float = None,
        # Probability filters (all variants)
        probability_gt: float = None,
        probability_lt: float = None,
        probability_gte: float = None,
        probability_lte: float = None,
        # Other filters
        valid_soundscape: bool = None,
        recording_modes: list[str] = None,
        eclipse: bool = None,
        time_of_day_gte: int = None,
        time_of_day_lte: int = None,
        countries: list[str] = None,
        continents: list[str] = None,
        species_id: str = None,
        species_ids: list[str] = None,
        classifications: list[str] = None,
        station_types: list[str] = None,
        vote: int = None,
        sort_by: str = None,
        unique_stations: bool = None,
        override_station_filters: bool = None,
        ne: dict = None,
        sw: dict = None,
        # Legacy aliases for backwards compatibility
        min_score: float = None,
        min_confidence: float = None,
        min_probability: float = None,
    ) -> Generator[dict, None, None]:
        """Fetch detections with comprehensive filtering options.

        All score, confidence, and probability filters support four variants:

        *   `_gt`: Greater than (exclusive)
        *   `_lt`: Less than (exclusive)
        *   `_gte`: Greater than or equal (inclusive)
        *   `_lte`: Less than or equal (inclusive)

        Legacy parameters (`min_score`, `min_confidence`, `min_probability`) map to `_gte` variants.

        Args:
            station_ids (List[str], optional): List of station IDs to filter by.
            start (datetime, optional): Start of the time range.
            end (datetime, optional): End of the time range.
            score_gt (float, optional): Score greater than value.
            score_lt (float, optional): Score less than value.
            score_gte (float, optional): Score greater than or equal value.
            score_lte (float, optional): Score less than or equal value.
            confidence_gt (float, optional): Confidence greater than value.
            confidence_lt (float, optional): Confidence less than value.
            confidence_gte (float, optional): Confidence greater than or equal value.
            confidence_lte (float, optional): Confidence less than or equal value.
            probability_gt (float, optional): Probability greater than value.
            probability_lt (float, optional): Probability less than value.
            probability_gte (float, optional): Probability greater than or equal value.
            probability_lte (float, optional): Probability less than or equal value.
            valid_soundscape (bool, optional): Filter for detections with valid audio.
            recording_modes (List[str], optional): Filter by recording mode.
            eclipse (bool, optional): Filter by eclipse status.
            time_of_day_gte (int, optional): Start minute of day (0-1440).
            time_of_day_lte (int, optional): End minute of day (0-1440).
            countries (List[str], optional): Filter by country names.
            continents (List[str], optional): Filter by continent names.
            species_id (str, optional): Filter by single species ID.
            species_ids (List[str], optional): Filter by multiple species IDs.
            classifications (List[str], optional): Filter by classification string.
            station_types (List[str], optional): Filter by station types.
            vote (int, optional): Filter by vote count.
            sort_by (str, optional): Sort order string.
            unique_stations (bool, optional): Return one detection per station.
            override_station_filters (bool, optional): Override station-level filters.
            ne (Dict, optional): Northest corner for spatial filtering.
            sw (Dict, optional): Southwest corner for spatial filtering.
            min_score (float, optional): Alias for score_gte.
            min_confidence (float, optional): Alias for confidence_gte.
            min_probability (float, optional): Alias for probability_gte.

        Yields:
            Dict: A dictionary representing a detection.
        """
        cursor = None

        # Handle legacy parameters (map to _gte variants)
        if min_score is not None and score_gte is None:
            score_gte = min_score
        if min_confidence is not None and confidence_gte is None:
            confidence_gte = min_confidence
        if min_probability is not None and probability_gte is None:
            probability_gte = min_probability

        # Build period dict with correct field names
        period = None
        if start and end:
            period = {"from": start.isoformat(), "to": end.isoformat()}

        # BirdWeather API bug: querying >10 species breaks pagination backend
        # Chunk species IDs if necessary and yield sequentially
        species_chunks = []
        if species_ids and len(species_ids) > 10:
            species_chunks = [
                species_ids[i : i + 10] for i in range(0, len(species_ids), 10)
            ]
        else:
            species_chunks = [species_ids]

        for chunk_species_ids in species_chunks:
            # BirdWeather API bug: unindexed queries (like bounding boxes without specific stations)
            # forcibly terminate after returning ~7,000 records regardless of the paginator depth.
            # To extract 10k+ records, we dynamically time-slice the request into 4-hour chunks.
            if start and end:
                import base64

                current_end_time = end
                target_start_time = start
                seen_ids = set()

                while current_end_time > target_start_time:
                    current_start_time = max(
                        current_end_time - timedelta(hours=4), target_start_time
                    )
                    slice_period = {
                        "from": current_start_time.isoformat(),
                        "to": current_end_time.isoformat(),
                    }

                    # Forge starting cursor at current_end_time to bypass BirdWeather's period filtering bug
                    cursor_ts = int(current_end_time.timestamp()) + 1
                    cursor = base64.b64encode(str(cursor_ts).encode("utf-8")).decode(
                        "utf-8"
                    )

                    prev_last_seen_ts = None
                    while True:
                        vars = {
                            "stationIds": station_ids,
                            "period": slice_period,
                            "cursor": cursor,
                            # Score filters
                            "scoreGt": score_gt,
                            "scoreLt": score_lt,
                            "scoreGte": score_gte,
                            "scoreLte": score_lte,
                            # Confidence filters
                            "confidenceGt": confidence_gt,
                            "confidenceLt": confidence_lt,
                            "confidenceGte": confidence_gte,
                            "confidenceLte": confidence_lte,
                            # Probability filters
                            "probabilityGt": probability_gt,
                            "probabilityLt": probability_lt,
                            "probabilityGte": probability_gte,
                            "probabilityLte": probability_lte,
                            # Other filters
                            "validSoundscape": valid_soundscape,
                            "recordingModes": recording_modes,
                            "eclipse": eclipse,
                            "timeOfDayGte": time_of_day_gte,
                            "timeOfDayLte": time_of_day_lte,
                            "countries": countries,
                            "continents": continents,
                            "speciesId": species_id,
                            "speciesIds": chunk_species_ids,
                            "classifications": classifications,
                            "stationTypes": station_types,
                            "vote": vote,
                            "sortBy": sort_by,
                            "uniqueStations": unique_stations,
                            "overrideStationFilters": override_station_filters,
                            "ne": ne,
                            "sw": sw,
                        }

                        # Remove None values
                        vars = {k: v for k, v in vars.items() if v is not None}

                        data = self._execute_query(QUERY_DETECTIONS_COMPREHENSIVE, vars)

                        connection = data.get("data", {}).get("detections", {})
                        nodes = connection.get("nodes", [])
                        page_info = connection.get("pageInfo", {})

                        if not nodes and not cursor:
                            break

                        out_of_bounds = False
                        last_seen_ts = None

                        for node in nodes:
                            try:
                                dt = datetime.fromisoformat(node["timestamp"])
                                node_ts = dt.timestamp()

                                # If node is older than our slice start time, break slice loop
                                if node_ts <= current_start_time.timestamp():
                                    out_of_bounds = True
                                    continue
                                # If node is in the future relative to our chunk, skip it
                                if node_ts > current_end_time.timestamp():
                                    continue
                            except Exception:
                                pass

                            if not out_of_bounds:
                                node_id = node.get("id")
                                if node_id and node_id in seen_ids:
                                    continue
                                if node_id:
                                    seen_ids.add(node_id)
                                last_seen_ts = node_ts
                                yield node

                        if out_of_bounds:
                            break

                        new_cursor = page_info.get("endCursor")
                        if not new_cursor or new_cursor == cursor:
                            # BirdWeather's global unindexed search engine frequently times out mid-slice and silently returns
                            # hasNextPage: false even if there are hundreds of items left. We must forcefully resume extraction!
                            if (
                                last_seen_ts
                                and last_seen_ts > current_start_time.timestamp()
                                and last_seen_ts != prev_last_seen_ts
                            ):
                                import base64

                                cursor_ts = int(last_seen_ts)
                                cursor = base64.b64encode(
                                    str(cursor_ts).encode("utf-8")
                                ).decode("utf-8")
                                prev_last_seen_ts = last_seen_ts
                                _page_sleep()
                                continue
                            break

                        cursor = new_cursor
                        _page_sleep()

                    current_end_time = current_start_time

            else:
                # Fallback for queries without start/end times
                cursor = None  # Reset pagination for each chunk
                while True:
                    vars = {
                        "stationIds": station_ids,
                        "period": None,
                        "cursor": cursor,
                        # Score filters
                        "scoreGt": score_gt,
                        "scoreLt": score_lt,
                        "scoreGte": score_gte,
                        "scoreLte": score_lte,
                        # Confidence filters
                        "confidenceGt": confidence_gt,
                        "confidenceLt": confidence_lt,
                        "confidenceGte": confidence_gte,
                        "confidenceLte": confidence_lte,
                        # Probability filters
                        "probabilityGt": probability_gt,
                        "probabilityLt": probability_lt,
                        "probabilityGte": probability_gte,
                        "probabilityLte": probability_lte,
                        # Other filters
                        "validSoundscape": valid_soundscape,
                        "recordingModes": recording_modes,
                        "eclipse": eclipse,
                        "timeOfDayGte": time_of_day_gte,
                        "timeOfDayLte": time_of_day_lte,
                        "countries": countries,
                        "continents": continents,
                        "speciesId": species_id,
                        "speciesIds": chunk_species_ids,
                        "classifications": classifications,
                        "stationTypes": station_types,
                        "vote": vote,
                        "sortBy": sort_by,
                        "uniqueStations": unique_stations,
                        "overrideStationFilters": override_station_filters,
                        "ne": ne,
                        "sw": sw,
                    }

                    # Remove None values
                    vars = {k: v for k, v in vars.items() if v is not None}

                    data = self._execute_query(QUERY_DETECTIONS_COMPREHENSIVE, vars)

                    connection = data.get("data", {}).get("detections", {})
                    nodes = connection.get("nodes", [])
                    page_info = connection.get("pageInfo", {})

                    if not nodes and not cursor:
                        break

                    for node in nodes:
                        yield node

                    new_cursor = page_info.get("endCursor")
                    if not new_cursor or new_cursor == cursor:
                        break

                    cursor = new_cursor
                    _page_sleep()

    def get_detections_count(
        self,
        station_ids: list[str] = None,
        start: datetime = None,
        end: datetime = None,
        confidence_gte: float = None,
        valid_soundscape: bool = None,
        continents: list[str] = None,
        countries: list[str] = None,
        species_ids: list[str] = None,
        ne: dict = None,
        sw: dict = None,
    ) -> int:
        """Get total count of detections matching the given filters.

        This is a lightweight query that retrieves only the total count without
        fetching all detection data. Useful for progress tracking.

        Args:
            station_ids: Filter by station IDs.
            start: Start of time range.
            end: End of time range.
            confidence_gte: Minimum confidence threshold.
            valid_soundscape: Filter for detections with valid audio.
            continents: Filter by continent names.
            countries: Filter by country names.
            species_ids: Filter by species IDs.
            ne: Northeast corner for spatial filtering.
            sw: Southwest corner for spatial filtering.
        Returns:
            int: Total number of detections matching the filters.
        """
        # Validate / configure bounding box
        # Assuming DEFAULT_NE and DEFAULT_SW are defined elsewhere, e.g., at the top of the file
        # For the purpose of this edit, I'll assume they exist.
        # If they don't, this would cause a NameError.
        ne = ne or DEFAULT_NE
        sw = sw or DEFAULT_SW
        if not continents and not countries:
            ne_box, sw_box = (ne, sw)
        else:
            ne_box, sw_box = (None, None)

        # Build period
        period = None
        if start and end:
            period = {"from": start.isoformat(), "to": end.isoformat()}

        # BirdWeather API bug: querying >10 species breaks counts query
        # Sum counts across species chunks
        species_chunks = []
        if species_ids and len(species_ids) > 10:
            species_chunks = [
                species_ids[i : i + 10] for i in range(0, len(species_ids), 10)
            ]
        else:
            species_chunks = [species_ids]

        total_accumulated = 0
        for chunk_species_ids in species_chunks:
            vars = {
                "stationIds": station_ids,
                "period": period,
                "confidenceGte": confidence_gte,
                "validSoundscape": valid_soundscape,
                "continents": continents,
                "countries": countries,
                "speciesIds": chunk_species_ids,
                "ne": ne_box,
                "sw": sw_box,
            }
            vars = {k: v for k, v in vars.items() if v is not None}

            query = """
            query DetectionsCount($stationIds: [ID!], $period: InputDuration, $confidenceGte: Float, $validSoundscape: Boolean, $continents: [String!], $countries: [String!], $speciesIds: [ID!], $ne: InputLocation, $sw: InputLocation) {
              detections(stationIds: $stationIds, period: $period, confidenceGte: $confidenceGte, validSoundscape: $validSoundscape, continents: $continents, countries: $countries, speciesIds: $speciesIds, ne: $ne, sw: $sw, first: 1) {
                totalCount
              }
            }
            """

            data = self._execute_query(query, vars)
            total_accumulated += (
                data.get("data", {}).get("detections", {}).get("totalCount", 0)
            )

        return total_accumulated

    # ========================================================================
    # SPECIES METHODS
    # ========================================================================

    def fetch_all_species(
        self, query: str = "", search_locale: str = None
    ) -> Generator[dict, None, None]:
        """Fetch all species via search with pagination.

        Args:
            query (str): Search query string.
            search_locale (str, optional): Locale for search results.

        Yields:
            Dict: A dictionary representing a species node.
        """
        cursor = None
        while True:
            vars = {"cursor": cursor, "query": query, "searchLocale": search_locale}
            data = self._execute_query(QUERY_SPECIES_SEARCH, vars)

            # FIXED: Use correct path "searchSpecies" not "species"
            connection = data.get("data", {}).get("searchSpecies", {})
            nodes = connection.get("nodes", [])
            page_info = connection.get("pageInfo", {})

            for node in nodes:
                yield node

            new_cursor = page_info.get("endCursor")
            if not new_cursor or new_cursor == cursor:
                break

            cursor = new_cursor
            _page_sleep()

    def fetch_species(
        self,
        species_id: str = None,
        scientific_name: str = None,
        period: dict = None,
        station_types: list[str] = None,
    ) -> dict | None:
        """Fetch a single species with detailed data.

        Args:
            species_id (str, optional): The ID of the species to fetch.
            scientific_name (str, optional): The scientific name to search for.
            period (Dict, optional): Time period for metrics.
            station_types (List[str], optional): Filter stats by station types.

        Returns:
            Optional[Dict]: Species data dictionary including detection counts and stations, or None.
        """
        vars = {
            "id": species_id,
            "scientificName": scientific_name,
            "period": period,
            "stationTypes": station_types,
        }
        data = self._execute_query(QUERY_SPECIES_SINGLE, vars)
        return data.get("data", {}).get("species")

    def fetch_species_by_ids(self, ids: list[str]) -> Generator[dict, None, None]:
        """Fetch multiple species by their IDs.

        Args:
            ids (List[str]): List of species IDs to fetch.

        Yields:
            Dict: Species data dictionary.
        """
        cursor = None
        while True:
            vars = {"ids": ids, "cursor": cursor}
            data = self._execute_query(QUERY_ALL_SPECIES, vars)

            connection = data.get("data", {}).get("allSpecies", {})
            nodes = connection.get("nodes", [])
            page_info = connection.get("pageInfo", {})

            for node in nodes:
                yield node

            new_cursor = page_info.get("endCursor")
            if not new_cursor or new_cursor == cursor:
                break

            cursor = new_cursor
            _page_sleep()

    # ========================================================================
    # BIRDNET SIGHTINGS (FIXED)
    # ========================================================================

    def fetch_birdnet_sightings(
        self, ne: dict, sw: dict, start: datetime, end: datetime, species_id: str = None
    ) -> Generator[dict, None, None]:
        """Fetch BirdNET mobile app sightings.

        Args:
            ne (Dict): Northeast corner coordinates.
            sw (Dict): Southwest corner coordinates.
            start (datetime): Start time for search.
            end (datetime): End time for search.
            species_id (str, optional): Filter by species ID.

        Yields:
            Dict: A dictionary representing a BirdNET sighting.
        """
        cursor = None

        # FIXED: Use from/to instead of start/end
        period = {"from": start.isoformat(), "to": end.isoformat()}

        while True:
            vars = {
                "ne": ne,
                "sw": sw,
                "period": period,
                "cursor": cursor,
                "speciesId": species_id,
            }
            data = self._execute_query(QUERY_BIRDNET_SIGHTINGS, vars)

            connection = data.get("data", {}).get("birdnetSightings", {})
            nodes = connection.get("nodes", [])
            page_info = connection.get("pageInfo", {})

            for node in nodes:
                yield node

            new_cursor = page_info.get("endCursor")
            if not new_cursor or new_cursor == cursor:
                break

            cursor = new_cursor
            _page_sleep()

    # ========================================================================
    # AGGREGATE / ANALYTICS METHODS
    # ========================================================================

    def fetch_counts(
        self,
        period: dict = None,
        species_id: str = None,
        station_ids: list[str] = None,
        station_types: list[str] = None,
        ne: dict = None,
        sw: dict = None,
    ) -> dict:
        """Fetch aggregate counts for detections, species, and stations.

        Args:
            period (Dict, optional): Time period for counts.
            species_id (str, optional): Filter by species ID.
            station_ids (List[str], optional): Filter by station IDs.
            station_types (List[str], optional): Filter by station types.
            ne (Dict, optional): Northeast corner.
            sw (Dict, optional): Southwest corner.

        Returns:
            Dict: Dictionary containing count metrics.
        """
        vars = {
            "period": period,
            "speciesId": species_id,
            "stationIds": station_ids,
            "stationTypes": station_types,
            "ne": ne,
            "sw": sw,
        }
        vars = {k: v for k, v in vars.items() if v is not None}
        data = self._execute_query(QUERY_COUNTS, vars)
        return data.get("data", {}).get("counts", {})

    def fetch_daily_detection_counts(
        self,
        period: dict = None,
        species_ids: list[str] = None,
        station_ids: list[str] = None,
    ) -> list[dict]:
        """Fetch daily aggregated detection counts by species.

        Args:
            period (Dict, optional): Time period for counts.
            species_ids (List[str], optional): Filter by species IDs.
            station_ids (List[str], optional): Filter by station IDs.

        Returns:
            List[Dict]: List of daily count records.
        """
        vars = {"period": period, "speciesIds": species_ids, "stationIds": station_ids}
        vars = {k: v for k, v in vars.items() if v is not None}
        data = self._execute_query(QUERY_DAILY_DETECTION_COUNTS, vars)
        return data.get("data", {}).get("dailyDetectionCounts", [])

    def fetch_time_of_day_detection_counts(
        self,
        period: dict = None,
        species_id: str = None,
        station_ids: list[str] = None,
        score_gte: float = None,
        confidence_gte: float = None,
        probability_gte: float = None,
        time_of_day_gte: int = None,
        time_of_day_lte: int = None,
        ne: dict = None,
        sw: dict = None,
    ) -> list[dict]:
        """Fetch detection counts binned by time of day.

        Args:
            period (Dict, optional): Time period.
            species_id (str, optional): Species ID filter.
            station_ids (List[str], optional): Station IDs filter.
            score_gte, confidence_gte, probability_gte (float, optional): Quality filters.
            time_of_day_gte, time_of_day_lte (int, optional): Minute of day range.
            ne, sw (Dict, optional): Bounding box filters.

        Returns:
            List[Dict]: List of time-binned count records.
        """
        vars = {
            "period": period,
            "speciesId": species_id,
            "stationIds": station_ids,
            "scoreGte": score_gte,
            "confidenceGte": confidence_gte,
            "probabilityGte": probability_gte,
            "timeOfDayGte": time_of_day_gte,
            "timeOfDayLte": time_of_day_lte,
            "ne": ne,
            "sw": sw,
        }
        vars = {k: v for k, v in vars.items() if v is not None}
        data = self._execute_query(QUERY_TIME_OF_DAY_DETECTION_COUNTS, vars)
        return data.get("data", {}).get("timeOfDayDetectionCounts", [])

    def fetch_top_species(
        self,
        limit: int = 50,
        offset: int = None,
        period: dict = None,
        species_id: str = None,
        station_types: list[str] = None,
        station_ids: list[str] = None,
        ne: dict = None,
        sw: dict = None,
    ) -> list[dict]:
        """Fetch top detected species with counts and breakdowns.

        Args:
            limit (int): Max number of species to return.
            offset (int, optional): Pagination offset.
            period (Dict, optional): Time period.
            species_id (str, optional): Filter by species ID.
            station_types (List[str], optional): Filter by station types.
            station_ids (List[str], optional): Filter by station IDs.
            ne, sw (Dict, optional): Bounding box filters.

        Returns:
            List[Dict]: List of top species records.
        """
        vars = {
            "limit": limit,
            "offset": offset,
            "period": period,
            "speciesId": species_id,
            "stationTypes": station_types,
            "stationIds": station_ids,
            "ne": ne,
            "sw": sw,
        }
        vars = {k: v for k, v in vars.items() if v is not None}
        data = self._execute_query(QUERY_TOP_SPECIES, vars)
        return data.get("data", {}).get("topSpecies", [])

    def fetch_top_birdnet_species(
        self,
        limit: int = 50,
        offset: int = None,
        period: dict = None,
        species_id: str = None,
        ne: dict = None,
        sw: dict = None,
    ) -> list[dict]:
        """Fetch top BirdNET-detected species with counts and breakdowns.

        Args:
            limit (int): Max number of species to return.
            offset (int, optional): Pagination offset.
            period (Dict, optional): Time period.
            species_id (str, optional): Filter by species ID.
            ne, sw (Dict, optional): Bounding box filters.

        Returns:
            List[Dict]: List of top BirdNET species records.
        """
        vars = {
            "limit": limit,
            "offset": offset,
            "period": period,
            "speciesId": species_id,
            "ne": ne,
            "sw": sw,
        }
        vars = {k: v for k, v in vars.items() if v is not None}
        data = self._execute_query(QUERY_TOP_BIRDNET_SPECIES, vars)
        return data.get("data", {}).get("topBirdnetSpecies", [])


# --- Real-Time Subscription Client ---


@dataclass
class BirdWeatherSubscription:
    """Real-time WebSocket subscription client for BirdWeather detections.

    Uses ActionCable protocol (Rails GraphQL subscriptions) to receive live detection events.

    Example:
        .. code-block:: python

            def on_detection(detection):
                print(f"New detection: {detection['species']['commonName']}")


            sub = BirdWeatherSubscription()
            sub.subscribe(on_detection, station_ids=["12345"], score_gte=0.8)
            # Runs until stopped or error
            sub.stop()

    Attributes:
        ws_url (str): WebSocket URL (default: wss://app.birdweather.com/cable).
        on_detection (Callable): Callback for new detections.
        on_error (Callable): Callback for errors.
        on_connect (Callable): Callback for successful connection.
        on_disconnect (Callable): Callback for disconnection.
    """

    ws_url: str = "wss://app.birdweather.com/cable"
    on_detection: Callable[[dict], None] | None = None
    on_error: Callable[[Exception], None] | None = None
    on_connect: Callable[[], None] | None = None
    on_disconnect: Callable[[], None] | None = None

    _ws: Any = None
    _running: bool = False
    _thread: threading.Thread | None = None
    _message_queue: Queue = field(default_factory=Queue)

    # Subscription GraphQL query (matches API documentation)
    SUBSCRIPTION_QUERY = """
    subscription NewDetection(
      $speciesIds: [ID!],
      $classifications: [String!],
      $stationIds: [ID!],
      $stationTypes: [String!],
      $continents: [String!],
      $countries: [String!],
      $recordingModes: [String!],
      $scoreGt: Float,
      $scoreLt: Float,
      $scoreGte: Float,
      $scoreLte: Float,
      $confidenceGt: Float,
      $confidenceLt: Float,
      $confidenceGte: Float,
      $confidenceLte: Float,
      $probabilityGt: Float,
      $probabilityLt: Float,
      $probabilityGte: Float,
      $probabilityLte: Float,
      $overrideStationFilters: Boolean,
      $timeOfDayGte: Int,
      $timeOfDayLte: Int
    ) {
      newDetection(
        speciesIds: $speciesIds,
        classifications: $classifications,
        stationIds: $stationIds,
        stationTypes: $stationTypes,
        continents: $continents,
        countries: $countries,
        recordingModes: $recordingModes,
        scoreGt: $scoreGt,
        scoreLt: $scoreLt,
        scoreGte: $scoreGte,
        scoreLte: $scoreLte,
        confidenceGt: $confidenceGt,
        confidenceLt: $confidenceLt,
        confidenceGte: $confidenceGte,
        confidenceLte: $confidenceLte,
        probabilityGt: $probabilityGt,
        probabilityLt: $probabilityLt,
        probabilityGte: $probabilityGte,
        probabilityLte: $probabilityLte,
        overrideStationFilters: $overrideStationFilters,
        timeOfDayGte: $timeOfDayGte,
        timeOfDayLte: $timeOfDayLte
      ) {
        detection {
          id
          timestamp
          confidence
          score
          probability
          certainty
          eclipse
          speciesId
          species {
            id
            commonName
            scientificName
            alpha
            ebirdCode
            imageUrl
            thumbnailUrl
          }
          coords { lat lon }
          soundscape {
            id
            url
            duration
            mode
          }
          station {
            id
            name
            type
            country
            state
            timezone
            coords { lat lon }
          }
        }
      }
    }
    """

    def subscribe(
        self,
        callback: Callable[[dict], None],
        # Filter parameters (all optional)
        species_ids: list[str] = None,
        classifications: list[str] = None,
        station_ids: list[str] = None,
        station_types: list[str] = None,
        continents: list[str] = None,
        countries: list[str] = None,
        recording_modes: list[str] = None,
        # Score filters
        score_gt: float = None,
        score_lt: float = None,
        score_gte: float = None,
        score_lte: float = None,
        # Confidence filters
        confidence_gt: float = None,
        confidence_lt: float = None,
        confidence_gte: float = None,
        confidence_lte: float = None,
        # Probability filters
        probability_gt: float = None,
        probability_lt: float = None,
        probability_gte: float = None,
        probability_lte: float = None,
        # Other
        override_station_filters: bool = None,
        time_of_day_gte: int = None,
        time_of_day_lte: int = None,
        blocking: bool = True,
    ) -> None:
        """Subscribe to real-time detection events.

        Args:
            callback (Callable[[Dict], None]): Function to call for each detection received.
            species_ids (List[str], optional): Filter by species IDs.
            classifications (List[str], optional): Filter by classification.
            station_ids (List[str], optional): Filter by station IDs.
            station_types (List[str], optional): Filter by station types.
            continents (List[str], optional): Filter by continents.
            countries (List[str], optional): Filter by countries.
            recording_modes (List[str], optional): Filter by recording modes.
            score_gt, score_lt, score_gte, score_lte (float, optional): Score filters.
            confidence_gt, confidence_lt, confidence_gte, confidence_lte (float, optional): Confidence filters.
            probability_gt, probability_lt, probability_gte, probability_lte (float, optional): Probability filters.
            override_station_filters (bool, optional): Override station settings.
            time_of_day_gte, time_of_day_lte (int, optional): Time of day filters.
            blocking (bool): If True (default), blocks until stopped. If False, runs in background.

        Raises:
            ImportError: If websocket-client is not installed.
        """
        try:
            import websocket
        except ImportError:
            raise ImportError(
                "websocket-client package required for real-time subscriptions. "
                "Install with: pip install websocket-client"
            )

        self.on_detection = callback

        # Build subscription variables
        variables = {
            "speciesIds": species_ids,
            "classifications": classifications,
            "stationIds": station_ids,
            "stationTypes": station_types,
            "continents": continents,
            "countries": countries,
            "recordingModes": recording_modes,
            "scoreGt": score_gt,
            "scoreLt": score_lt,
            "scoreGte": score_gte,
            "scoreLte": score_lte,
            "confidenceGt": confidence_gt,
            "confidenceLt": confidence_lt,
            "confidenceGte": confidence_gte,
            "confidenceLte": confidence_lte,
            "probabilityGt": probability_gt,
            "probabilityLt": probability_lt,
            "probabilityGte": probability_gte,
            "probabilityLte": probability_lte,
            "overrideStationFilters": override_station_filters,
            "timeOfDayGte": time_of_day_gte,
            "timeOfDayLte": time_of_day_lte,
        }
        # Remove None values
        variables = {k: v for k, v in variables.items() if v is not None}

        def on_message(ws, message):
            try:
                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "welcome":
                    # Connection established, subscribe to channel
                    subscribe_msg = {
                        "command": "subscribe",
                        "identifier": json.dumps(
                            {
                                "channel": "GraphqlChannel",
                                "query": self.SUBSCRIPTION_QUERY,
                                "variables": variables,
                            }
                        ),
                    }
                    ws.send(json.dumps(subscribe_msg))
                    logger.info("Sent subscription request")

                elif msg_type == "confirm_subscription":
                    logger.info("Subscription confirmed")
                    if self.on_connect:
                        self.on_connect()

                elif msg_type == "ping":
                    # Keep-alive ping, no action needed
                    pass

                elif data.get("message"):
                    # Detection event received
                    result = data["message"].get("result", {})
                    detection = (
                        result.get("data", {}).get("newDetection", {}).get("detection")
                    )
                    if detection and self.on_detection:
                        self.on_detection(detection)

            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON received: {e}")
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                if self.on_error:
                    self.on_error(e)

        def on_error(ws, error):
            logger.error(f"WebSocket error: {error}")
            if self.on_error:
                self.on_error(error)

        def on_close(ws, close_status_code, close_msg):
            logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
            self._running = False
            if self.on_disconnect:
                self.on_disconnect()

        def on_open(ws):
            logger.info("WebSocket connection opened")

        # Create WebSocket connection
        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
            header={"User-Agent": HEADERS["User-Agent"]},
        )

        self._running = True

        if blocking:
            self._ws.run_forever()
        else:
            self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
            self._thread.start()
            logger.info("Subscription started in background thread")

    def stop(self) -> None:
        """Stop the subscription and close the WebSocket connection.

        This method will terminate the background thread if running in non-blocking mode.
        """
        self._running = False
        if self._ws:
            self._ws.close()
            logger.info("Subscription stopped")

    def is_running(self) -> bool:
        """Check if the subscription is currently active.

        Returns:
            bool: True if the subscription is running, False otherwise.
        """
        return self._running


# --- Data Ingestor ---


class DataIngestor:
    """Orchestrates the complete BirdWeather data ingestion pipeline.

    Attributes:
        output_dir (Path): The root directory for storing ingested data.
        client (BirdWeatherClient): The client used for API requests.
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = BirdWeatherClient()

    def _write_jsonl(self, filepath: Path, records: list[dict]) -> int:
        """Write records to a JSONL file with ingestion timestamp.

        Args:
            filepath (Path): Path to the output JSONL file.
            records (List[Dict]): List of records to write.

        Returns:
            int: Number of records written.
        """
        count = 0
        with open(filepath, "w", encoding="utf-8") as f:
            for record in records:
                record["_ingested_at"] = datetime.utcnow().isoformat()
                f.write(json.dumps(record) + "\n")
                count += 1
        return count

    def _append_jsonl(self, filepath: Path, record: dict) -> None:
        """Append a single record to a JSONL file.

        Args:
            filepath (Path): Path to the output JSONL file.
            record (Dict): Record to append.
        """
        record["_ingested_at"] = datetime.utcnow().isoformat()
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    # ========================================================================
    # INGESTION METHODS
    # ========================================================================

    def ingest_species_metadata(self, search_locale: str = None) -> int:
        """Ingest the complete species taxonomy metadata.

        Args:
            search_locale (str, optional): Locale for search results.

        Returns:
            int: Number of species records ingested.
        """
        logger.info("Ingesting Species Metadata...")
        filename = self.output_dir / "species_metadata.jsonl"
        count = 0
        with open(filename, "w", encoding="utf-8") as f:
            for sp in self.client.fetch_all_species(search_locale=search_locale):
                sp["_ingested_at"] = datetime.utcnow().isoformat()
                f.write(json.dumps(sp) + "\n")
                count += 1
        logger.info(f"Ingested {count} species records.")
        return count

    def ingest_detailed_species(
        self, species_ids: list[str], period: dict = None
    ) -> int:
        """Ingest detailed species data including detection counts and station lists.

        Args:
            species_ids (List[str]): List of species IDs to fetch.
            period (Dict, optional): Time period for metrics.

        Returns:
            int: Number of detailed records ingested.
        """
        logger.info(f"Ingesting detailed data for {len(species_ids)} species...")
        filename = self.output_dir / f"species_detailed_{date.today()}.jsonl"
        count = 0
        with open(filename, "w", encoding="utf-8") as f:
            for sid in species_ids:
                sp = self.client.fetch_species(species_id=sid, period=period)
                if sp:
                    sp["_ingested_at"] = datetime.utcnow().isoformat()
                    f.write(json.dumps(sp) + "\n")
                    count += 1
                time.sleep(0.2)
        logger.info(f"Ingested {count} detailed species records.")
        return count

    def ingest_stations(
        self, ne: dict, sw: dict, query: str = None, period: dict = None
    ) -> list[dict]:
        """Ingest all stations within a bounding box.

        Args:
            ne (Dict): Northeast corner.
            sw (Dict): Southwest corner.
            query (str, optional): Search query.
            period (Dict, optional): Time period.

        Returns:
            List[Dict]: List of ingested station records.
        """
        logger.info("Ingesting Stations...")
        filename = self.output_dir / f"stations_{date.today()}.jsonl"
        stations = []
        with open(filename, "w", encoding="utf-8") as f:
            for st in self.client.fetch_all_stations(
                ne, sw, query=query, period=period
            ):
                st["_ingested_at"] = datetime.utcnow().isoformat()
                stations.append(st)
                f.write(json.dumps(st) + "\n")
        logger.info(f"Ingested {len(stations)} stations.")
        return stations

    def ingest_detailed_stations(
        self, station_ids: list[str], period: dict = None, top_species_limit: int = 20
    ) -> int:
        """Ingest detailed station data including probabilities and top species.

        Args:
            station_ids (List[str]): List of station IDs to fetch.
            period (Dict, optional): Time period for metrics.
            top_species_limit (int, optional): Max top species.

        Returns:
            int: Number of detailed station records ingested.
        """
        logger.info(f"Ingesting detailed data for {len(station_ids)} stations...")
        filename = self.output_dir / f"stations_detailed_{date.today()}.jsonl"
        count = 0
        with open(filename, "w", encoding="utf-8") as f:
            for sid in station_ids:
                st = self.client.fetch_station(
                    sid, period=period, top_species_limit=top_species_limit
                )
                if st:
                    st["_ingested_at"] = datetime.utcnow().isoformat()
                    f.write(json.dumps(st) + "\n")
                    count += 1
                time.sleep(0.2)
        logger.info(f"Ingested {count} detailed station records.")
        return count

    def ingest_detections(
        self,
        station_ids: list[str],
        start: datetime,
        end: datetime,
        download_audio: bool = False,
        **filter_kwargs,
    ) -> int:
        """Ingest detections for specified stations with optional audio download.

        Args:
            station_ids (List[str]): List of station IDs.
            start (datetime): Start time.
            end (datetime): End time.
            download_audio (bool): Whether to download audio files.
            **filter_kwargs: Additional filters passed to `fetch_detections`.

        Returns:
            int: Total number of detections ingested.
        """
        logger.info(f"Ingesting detections from {start.date()} to {end.date()}...")

        total_count = 0
        for station_id in station_ids:
            day_count = 0
            curr_d = start

            while curr_d < end:
                next_d = curr_d + timedelta(days=1)
                day_str = curr_d.strftime("%Y-%m-%d")

                out_file = self.output_dir / f"detections_{station_id}_{day_str}.jsonl"

                if out_file.exists() and out_file.stat().st_size > 0:
                    logger.debug(f"Skipping {station_id} {day_str} (already exists)")
                    curr_d = next_d
                    continue

                logger.info(
                    f"Fetching detections for station {station_id} on {day_str}"
                )

                detections = []
                for det in self.client.fetch_detections(
                    station_ids=[station_id], start=curr_d, end=next_d, **filter_kwargs
                ):
                    det["station_id"] = station_id
                    det["_ingested_at"] = datetime.utcnow().isoformat()
                    detections.append(det)

                if detections:
                    with open(out_file, "w", encoding="utf-8") as f:
                        for det in detections:
                            f.write(json.dumps(det) + "\n")

                    logger.info(f"-> Saved {len(detections)} detections")
                    day_count += len(detections)

                    # Optional audio download
                    if download_audio:
                        self._download_detection_audio(station_id, day_str, detections)
                else:
                    out_file.touch()
                    logger.info("-> No detections")

                curr_d = next_d

            total_count += day_count

        logger.info(f"Total detections ingested: {total_count}")
        return total_count

    def _download_detection_audio(
        self,
        station_id: str,
        day_str: str,
        detections: list[dict],
        show_progress: bool = True,
    ) -> DownloadStats:
        """Download audio files for detections with detailed progress tracking.

        Args:
            station_id (str): Station ID for directory organization.
            day_str (str): Date string for directory organization.
            detections (List[Dict]): List of detection dictionaries with soundscape data.
            show_progress (bool): Whether to log progress during download (default: True).

        Returns:
            DownloadStats: Object containing download counts and error breakdown.
        """
        audio_dir = self.output_dir / "audio" / str(station_id) / day_str
        stats = DownloadStats(total=len(detections))

        logger.info(f"  Processing {len(detections)} detections for audio download...")

        for i, det in enumerate(detections):
            soundscape = det.get("soundscape")

            if not soundscape:
                stats.no_url += 1
                continue

            url = soundscape.get("url")
            if not url:
                stats.no_url += 1
                continue

            # Use default extension based on URL or fallback to .flac
            filename = soundscape.get("downloadFilename")
            if not filename:
                ext = ".mp3" if ".mp3" in url else ".flac"
                filename = f"{det['id']}{ext}"

            path = audio_dir / filename
            expected_size = soundscape.get("filesize")

            success, status = self.client.download_file(url, path, expected_size)

            if success:
                if status == "already_exists":
                    stats.already_exists += 1
                else:
                    stats.downloaded += 1
            else:
                stats.failed += 1
                stats.add_error(status)

            # Progress logging every 10 files or at completion
            if show_progress and ((i + 1) % 10 == 0 or i == len(detections) - 1):
                logger.info(
                    f"    Progress: {i + 1}/{len(detections)} | "
                    f"{stats.downloaded} new, {stats.already_exists} cached, {stats.failed} failed"
                )

        # Final summary
        logger.info(f"  -> Audio: {stats.summary()}{stats.error_summary()}")

        return stats

    def _download_species_images(self, species_list: list[dict]) -> dict[str, int]:
        """Download images for all species in the list.

        Downloads both full-size images (400x400) and thumbnails (100x100).

        Args:
            species_list (List[Dict]): List of species dictionaries with imageUrl/thumbnailUrl.

        Returns:
            Dict[str, int]: Counts with keys "images" and "thumbnails".
        """
        images_dir = self.output_dir / "species_images"
        images_dir.mkdir(exist_ok=True)

        thumbnails_dir = self.output_dir / "species_thumbnails"
        thumbnails_dir.mkdir(exist_ok=True)

        downloaded = {"images": 0, "thumbnails": 0}

        for species in species_list:
            species_id = species.get("id")
            if not species_id:
                continue

            # Download full image
            image_url = species.get("imageUrl")
            if image_url:
                # Extract extension from URL or default to .jpg
                ext = ".jpg"
                if "." in image_url.split("/")[-1]:
                    ext = "." + image_url.split(".")[-1].split("?")[0]

                image_path = images_dir / f"{species_id}{ext}"
                if not image_path.exists():
                    success, _ = self.client.download_file(image_url, image_path)
                    if success:
                        downloaded["images"] += 1

            # Download thumbnail
            thumb_url = species.get("thumbnailUrl")
            if thumb_url:
                ext = ".jpg"
                if "." in thumb_url.split("/")[-1]:
                    ext = "." + thumb_url.split(".")[-1].split("?")[0]

                thumb_path = thumbnails_dir / f"{species_id}_thumb{ext}"
                if not thumb_path.exists():
                    success, _ = self.client.download_file(thumb_url, thumb_path)
                    if success:
                        downloaded["thumbnails"] += 1

        if downloaded["images"] or downloaded["thumbnails"]:
            logger.info(
                f"-> Downloaded {downloaded['images']} images, {downloaded['thumbnails']} thumbnails"
            )

        return downloaded

    def _download_station_media(self, stations: list[dict]) -> dict[str, int]:
        """Download audio and video files for stations.

        Downloads audioUrl and videoUrl from station records.

        Args:
            stations (List[Dict]): List of station dictionaries with audioUrl/videoUrl.

        Returns:
            Dict[str, int]: Counts with keys "audio" and "video".
        """
        audio_dir = self.output_dir / "station_media" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        video_dir = self.output_dir / "station_media" / "video"
        video_dir.mkdir(parents=True, exist_ok=True)

        downloaded = {"audio": 0, "video": 0}

        for station in stations:
            station_id = station.get("id")
            if not station_id:
                continue

            # Download audio
            audio_url = station.get("audioUrl")
            if audio_url:
                ext = ".mp3"
                if "." in audio_url.split("/")[-1]:
                    ext = "." + audio_url.split(".")[-1].split("?")[0]

                audio_path = audio_dir / f"{station_id}{ext}"
                if not audio_path.exists():
                    success, _ = self.client.download_file(audio_url, audio_path)
                    if success:
                        downloaded["audio"] += 1

            # Download video
            video_url = station.get("videoUrl")
            if video_url:
                ext = ".mp4"
                if "." in video_url.split("/")[-1]:
                    ext = "." + video_url.split(".")[-1].split("?")[0]

                video_path = video_dir / f"{station_id}{ext}"
                if not video_path.exists():
                    success, _ = self.client.download_file(video_url, video_path)
                    if success:
                        downloaded["video"] += 1

        if downloaded["audio"] or downloaded["video"]:
            logger.info(
                f"-> Downloaded {downloaded['audio']} station audio, {downloaded['video']} station video files"
            )

        return downloaded

    def ingest_birdnet_sightings(
        self, ne: dict, sw: dict, start: datetime, end: datetime
    ) -> int:
        """Ingest BirdNET mobile app sightings.

        Args:
            ne (Dict): Northeast corner.
            sw (Dict): Southwest corner.
            start (datetime): Start time.
            end (datetime): End time.

        Returns:
            int: Number of sightings ingested.
        """
        logger.info("Ingesting BirdNET Sightings...")
        filename = self.output_dir / f"birdnet_sightings_{date.today()}.jsonl"
        count = 0

        with open(filename, "w", encoding="utf-8") as f:
            for sighting in self.client.fetch_birdnet_sightings(ne, sw, start, end):
                sighting["_ingested_at"] = datetime.utcnow().isoformat()
                f.write(json.dumps(sighting) + "\n")
                count += 1

        logger.info(f"Ingested {count} BirdNET sightings.")
        return count

    def ingest_detections_by_species(
        self,
        species_ids: list[str],
        ne: dict,
        sw: dict,
        start: datetime,
        end: datetime,
        download_audio: bool = False,
        min_score: float = None,
        min_confidence: float = None,
    ) -> dict[str, int]:
        """Ingest detections by species ID (bypasses stations endpoint).

        This is a workaround for when the stations endpoint is failing.
        Queries detections directly filtered by species.

        Args:
            species_ids (List[str]): List of species IDs to fetch detections for.
            ne (Dict): Northeast corner.
            sw (Dict): Southwest corner.
            start (datetime): Start time.
            end (datetime): End time.
            download_audio (bool): Whether to download audio files.
            min_score (float, optional): Minimum detection score filter.
            min_confidence (float, optional): Minimum confidence filter.

        Returns:
            Dict[str, int]: Dictionary with detection and audio counts per species.
        """
        logger.info(f"Ingesting detections for {len(species_ids)} species...")

        results = {}
        total_detections = 0
        total_audio = 0

        period = {"from": start.isoformat(), "to": end.isoformat()}

        for species_id in species_ids:
            logger.info(f"Fetching detections for species {species_id}...")

            species_detections = []

            # Fetch detections for this species
            for det in self.client.fetch_detections(
                species_ids=[species_id],
                start=start,
                end=end,
                ne=ne,
                sw=sw,
                min_score=min_score,
                min_confidence=min_confidence,
            ):
                # Client-side bbox filter — API country/bbox filters are unreliable
                if ne and sw:
                    sta = det.get("station") or {}
                    sta_coords = sta.get("coords") or {}
                    s_lat = sta_coords.get("lat")
                    s_lon = sta_coords.get("lon")
                    if s_lat is not None and s_lon is not None:
                        if not (
                            sw["lat"] <= s_lat <= ne["lat"]
                            and sw["lon"] <= s_lon <= ne["lon"]
                        ):
                            continue

                det["_ingested_at"] = datetime.utcnow().isoformat()
                species_detections.append(det)

            if species_detections:
                # Get species name for filename
                species_name = (
                    species_detections[0]
                    .get("species", {})
                    .get("commonName", species_id)
                )
                species_name_safe = species_name.replace(" ", "_").replace("/", "-")

                # Save detections
                out_file = (
                    self.output_dir
                    / f"detections_species_{species_name_safe}_{date.today()}.jsonl"
                )
                with open(out_file, "w", encoding="utf-8") as f:
                    for det in species_detections:
                        f.write(json.dumps(det) + "\n")

                logger.info(
                    f"  -> Saved {len(species_detections)} detections for {species_name}"
                )
                total_detections += len(species_detections)

                # Download audio if requested
                audio_count = 0
                if download_audio:
                    audio_dir = self.output_dir / "audio" / species_name_safe
                    audio_dir.mkdir(parents=True, exist_ok=True)

                    stats = DownloadStats(total=len(species_detections))
                    logger.info(
                        f"  Processing {len(species_detections)} detections for audio download..."
                    )

                    for i, det in enumerate(species_detections):
                        soundscape = det.get("soundscape")
                        if not soundscape or not soundscape.get("url"):
                            stats.no_url += 1
                            continue

                        audio_url = soundscape["url"]
                        detection_id = det.get("id", "unknown")

                        # Determine file extension from URL
                        ext = (
                            ".mp3"
                            if ".mp3" in audio_url
                            else (".wav" if ".wav" in audio_url else ".flac")
                        )
                        filename = soundscape.get(
                            "downloadFilename", f"{detection_id}{ext}"
                        )
                        audio_path = audio_dir / filename
                        expected_size = soundscape.get("filesize")

                        success, status = self.client.download_file(
                            audio_url, audio_path, expected_size
                        )

                        if success:
                            if status == "already_exists":
                                stats.already_exists += 1
                            else:
                                stats.downloaded += 1
                        else:
                            stats.failed += 1
                            stats.add_error(status)

                        # Progress logging every 10 files
                        if (i + 1) % 10 == 0 or i == len(species_detections) - 1:
                            logger.info(
                                f"    Progress: {i + 1}/{len(species_detections)} | "
                                f"{stats.downloaded} new, {stats.already_exists} cached, {stats.failed} failed"
                            )

                    audio_count = stats.downloaded
                    logger.info(f"  -> Audio: {stats.summary()}{stats.error_summary()}")
                    total_audio += stats.downloaded

                results[species_id] = {
                    "species_name": species_name,
                    "detections": len(species_detections),
                    "audio_files": audio_count,
                }

        logger.info(f"Total: {total_detections} detections, {total_audio} audio files")
        return results

    def ingest_aggregates(self, period: dict, ne: dict = None, sw: dict = None) -> None:
        """Ingest all aggregate statistics.

        Args:
            period (Dict): Time period for aggregates.
            ne (Dict, optional): Northeast corner.
            sw (Dict, optional): Southwest corner.
        """
        logger.info("Ingesting Aggregate Data...")

        # Global counts
        counts = self.client.fetch_counts(period=period, ne=ne, sw=sw)
        counts["_ingested_at"] = datetime.utcnow().isoformat()
        counts["period"] = period

        with open(self.output_dir / f"counts_{date.today()}.json", "w") as f:
            json.dump(counts, f, indent=2)

        # Daily detection counts
        daily = self.client.fetch_daily_detection_counts(period=period)
        self._write_jsonl(self.output_dir / f"daily_counts_{date.today()}.jsonl", daily)

        # Time of day counts
        tod = self.client.fetch_time_of_day_detection_counts(
            period=period, ne=ne, sw=sw
        )
        self._write_jsonl(
            self.output_dir / f"time_of_day_counts_{date.today()}.jsonl", tod
        )

        # Top species
        top_sp = self.client.fetch_top_species(limit=100, period=period, ne=ne, sw=sw)
        self._write_jsonl(self.output_dir / f"top_species_{date.today()}.jsonl", top_sp)

        # Top BirdNET species
        top_bn = self.client.fetch_top_birdnet_species(
            limit=100, period=period, ne=ne, sw=sw
        )
        self._write_jsonl(
            self.output_dir / f"top_birdnet_species_{date.today()}.jsonl", top_bn
        )

        logger.info("Finished Aggregate Data.")

    def run(
        self,
        ne: dict = DEFAULT_NE,
        sw: dict = DEFAULT_SW,
        days: int = 7,
        start_date: str = None,
        end_date: str = None,
        min_score: float = None,
        min_confidence: float = None,
        min_probability: float = None,
        download_audio: bool = False,
        download_species_images: bool = False,
        download_station_media: bool = False,
        ingest_species: bool = False,
        ingest_birdnet: bool = False,
        ingest_aggregates: bool = False,
        ingest_detailed_stations: bool = False,
        ingest_detailed_species: bool = False,
        skip_stations: bool = False,
        station_query: str = None,
        valid_soundscape: bool = None,
        recording_modes: list[str] = None,
        eclipse: bool = None,
        time_of_day_gte: int = None,
        time_of_day_lte: int = None,
        countries: list[str] = None,
        continents: list[str] = None,
        station_types: list[str] = None,
        species_ids: list[str] = None,
        vote: int = None,
        sort_by: str = None,
        unique_stations: bool = None,
    ) -> None:
        """Execute the complete data ingestion pipeline.

        Args:
            ne (Dict): Northeast corner (default: Netherlands).
            sw (Dict): Southwest corner (default: Netherlands).
            days (int): Number of days to look back (default: 7).
            start_date (str, optional): Start date (YYYY-MM-DD).
            end_date (str, optional): End date (YYYY-MM-DD).
            min_score (float, optional): Minimum score filter.
            min_confidence (float, optional): Minimum confidence filter.
            min_probability (float, optional): Minimum probability filter.
            download_audio (bool): If True, download detection audio.
            download_species_images (bool): If True, download species images.
            download_station_media (bool): If True, download station audio/video.
            ingest_species (bool): If True, ingest species metadata.
            ingest_birdnet (bool): If True, ingest BirdNET sightings.
            ingest_aggregates (bool): If True, ingest aggregate stats.
            ingest_detailed_stations (bool): If True, ingest detailed station data.
            ingest_detailed_species (bool): If True, ingest detailed species data.
            skip_stations (bool): If True, skip station ingestion (load from file).
            station_query (str, optional): Station search query.
            valid_soundscape (bool, optional): Filter for valid soundscape.
            recording_modes (List[str], optional): Filter by recording modes.
            eclipse (bool, optional): Filter by eclipse status.
            time_of_day_gte (int, optional): Minute of day start.
            time_of_day_lte (int, optional): Minute of day end.
            countries (List[str], optional): Filter by countries.
            continents (List[str], optional): Filter by continents.
            station_types (List[str], optional): Filter by station types.
            species_ids (List[str], optional): Filter by species IDs.
            vote (int, optional): Filter by vote.
            sort_by (str, optional): Sort order.
            unique_stations (bool, optional): Unique stations only.
        """

        # Calculate date range
        if start_date and end_date:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            end = datetime.now()
            start = end - timedelta(days=days)

        period = {"from": start.isoformat(), "to": end.isoformat()}

        # 1. Species metadata
        if ingest_species:
            self.ingest_species_metadata()

        # 2. Aggregates
        if ingest_aggregates:
            self.ingest_aggregates(period, ne, sw)

        # 3. BirdNET sightings
        if ingest_birdnet:
            self.ingest_birdnet_sightings(ne, sw, start, end)

        # 4. Stations
        stations = []
        if not skip_stations:
            stations = self.ingest_stations(ne, sw, query=station_query, period=period)
        else:
            stat_file = self.output_dir / f"stations_{date.today()}.jsonl"
            if stat_file.exists():
                with open(stat_file) as f:
                    stations = [json.loads(line) for line in f]
                logger.info(f"Loaded {len(stations)} stations from file")
            else:
                logger.error("Cannot skip: no station file found")
                return

        # 5. Detailed stations (optional)
        if ingest_detailed_stations and stations:
            station_ids = [s["id"] for s in stations]
            self.ingest_detailed_stations(station_ids, period=period)

        # 6. Detailed species (optional)
        if ingest_detailed_species and species_ids:
            self.ingest_detailed_species(species_ids, period=period)

        # 7. Detections
        if stations:
            station_ids = [s["id"] for s in stations]
            self.ingest_detections(
                station_ids=station_ids,
                start=start,
                end=end,
                download_audio=download_audio,
                min_score=min_score,
                min_confidence=min_confidence,
                min_probability=min_probability,
                valid_soundscape=valid_soundscape,
                recording_modes=recording_modes,
                eclipse=eclipse,
                time_of_day_gte=time_of_day_gte,
                time_of_day_lte=time_of_day_lte,
                countries=countries,
                continents=continents,
                station_types=station_types,
                species_ids=species_ids,
                vote=vote,
                sort_by=sort_by,
                unique_stations=unique_stations,
            )

        # 8. Download species images (optional)
        if download_species_images:
            logger.info("Downloading species images...")
            # Load species from file if we ingested them
            species_file = self.output_dir / "species_metadata.jsonl"
            if species_file.exists():
                species_list = []
                with open(species_file) as f:
                    for line in f:
                        species_list.append(json.loads(line))
                self._download_species_images(species_list)
            else:
                logger.warning(
                    "No species file found. Run with --ingest-species first."
                )

        # 9. Download station media (optional)
        if download_station_media and stations:
            logger.info("Downloading station media (audio/video)...")
            self._download_station_media(stations)


@dataclass
class CollectionStats:
    """Statistics for a collection run.

    Attributes:
        start_time (datetime): When the collection started.
        end_time (Optional[datetime]): When the collection ended.
        species_queried (int): Number of species queried.
        detections_found (int): Total detections found.
        detections_with_audio (int): Detections that have audio URLs.
        audio_downloaded (int): Number of audio files downloaded.
        audio_cached (int): Number of audio files skipped (cached).
        audio_failed (int): Number of audio downloads failed.
        errors (List[str]): List of error messages.
    """

    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None
    species_queried: int = 0
    detections_found: int = 0
    detections_scanned: int = 0
    detections_with_audio: int = 0
    audio_downloaded: int = 0
    audio_cached: int = 0
    audio_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert stats to dictionary for serialization.

        Returns:
            Dict with 'results' (counts) and 'run' (execution info).
        """
        duration = (
            (self.end_time - self.start_time).total_seconds() if self.end_time else None
        )
        return {
            "results": {
                "species_queried": self.species_queried,
                "detections_found": self.detections_found,
                "detections_scanned": self.detections_scanned,
                "detections_with_audio": self.detections_with_audio,
                "audio_downloaded": self.audio_downloaded,
                "audio_cached": self.audio_cached,
                "audio_failed": self.audio_failed,
            },
            "run": {
                "started_at": self.start_time.isoformat(),
                "finished_at": (self.end_time.isoformat() if self.end_time else None),
                "duration_seconds": (round(duration, 2) if duration else None),
                "errors": self.errors,
            },
        }


@dataclass
class BundleSyncStats:
    """Summary for syncing local BirdWeather date bundles into DB/MinIO."""

    bundles_discovered: int = 0
    bundles_attempted: int = 0
    bundles_synced: int = 0
    bundles_skipped: int = 0
    bundles_failed: int = 0
    directories_deleted: int = 0
    species_upserted: int = 0
    stations_upserted: int = 0
    detections_upserted: int = 0
    audio_uploaded: int = 0
    audio_already_present: int = 0
    audio_upload_failed: int = 0

    def absorb(self, other: "BundleSyncStats") -> None:
        """Accumulate counts from another sync summary."""
        for field_name in (
            "bundles_discovered",
            "bundles_attempted",
            "bundles_synced",
            "bundles_skipped",
            "bundles_failed",
            "directories_deleted",
            "species_upserted",
            "stations_upserted",
            "detections_upserted",
            "audio_uploaded",
            "audio_already_present",
            "audio_upload_failed",
        ):
            setattr(
                self,
                field_name,
                getattr(self, field_name) + getattr(other, field_name),
            )

    def to_metadata(self) -> dict[str, int]:
        """Return a Dagster-friendly flat metadata payload."""
        return {
            "bundles_discovered": self.bundles_discovered,
            "bundles_attempted": self.bundles_attempted,
            "bundles_synced": self.bundles_synced,
            "bundles_skipped": self.bundles_skipped,
            "bundles_failed": self.bundles_failed,
            "directories_deleted": self.directories_deleted,
            "species_upserted": self.species_upserted,
            "stations_upserted": self.stations_upserted,
            "detections_upserted": self.detections_upserted,
            "audio_uploaded": self.audio_uploaded,
            "audio_already_present": self.audio_already_present,
            "audio_upload_failed": self.audio_upload_failed,
        }


class AudioCollector:
    """Automated audio collection that bypasses the stations endpoint.

    Attributes:
        output_dir (Path): Root directory for output.
        client (BirdWeatherClient): API client.
    """

    def __init__(self, output_dir: Path = Path("data/raw/birdweather")):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = BirdWeatherClient()
        self._species_cache: list[dict] | None = None

    def get_species_list(self, force_refresh: bool = False) -> list[dict]:
        """Get the full species list, using cache if available.

        Args:
            force_refresh (bool): If True, ignore cache and fetch fresh list.

        Returns:
            List[Dict]: List of species dictionaries with id, commonName, scientificName.
        """
        cache_file = self.output_dir / "species_cache.json"

        # Try to load from cache
        if not force_refresh and cache_file.exists():
            cache_age = datetime.now().timestamp() - cache_file.stat().st_mtime
            if cache_age < 86400:  # Cache valid for 24 hours
                logger.info("Loading species from cache...")
                with open(cache_file) as f:
                    self._species_cache = json.load(f)
                logger.info(f"Loaded {len(self._species_cache)} species from cache")
                return self._species_cache

        # Fetch fresh species list
        logger.info("Fetching species list from API...")
        species_list = []
        for species in self.client.fetch_all_species():
            species_list.append(
                {
                    "id": species["id"],
                    "commonName": species.get("commonName", "Unknown"),
                    "scientificName": species.get("scientificName", ""),
                }
            )

        # Cache for future use
        with open(cache_file, "w") as f:
            json.dump(species_list, f, indent=2)

        logger.info(f"Fetched and cached {len(species_list)} species")
        self._species_cache = species_list
        return species_list

    def _download_single_audio(self, task: dict) -> tuple[bool, str]:
        """Download a single audio file (for parallel processing).

        Args:
            task (Dict): Dictionary containing 'url', 'audio_path', and optional 'expected_size'.

        Returns:
            Tuple[bool, str]: Tuple of (success, status).
        """
        url = task["url"]
        audio_path = Path(task["audio_path"])
        expected_size = task.get("expected_size")

        # Skip if already exists
        if audio_path.exists():
            if expected_size and audio_path.stat().st_size == expected_size:
                return True, "already_exists"

        return self.client.download_file(url, audio_path, expected_size)

    def _enrich_weather_data(
        self,
        detections: list[dict],
        detections_file: Path | None = None,
        sleep_between: float = 0.3,
    ) -> int:
        """Enrich detection records with station weather data.

        Deduplicates stations, fetches weather/airPollution/sensors once per
        station, then merges into every matching detection *in-place*.
        If ``detections_file`` is provided the enriched data is rewritten
        atomically.

        Args:
            detections: Mutable list of detection dicts (modified in-place).
            detections_file: Path to ``detections.jsonl`` — if given, the
                file is rewritten with enriched records.
            sleep_between: Seconds to sleep between station API calls to
                avoid rate-limiting (default 0.3 s).

        Returns:
            Number of stations successfully enriched.
        """
        # Collect unique station IDs
        station_ids: dict[str, list[int]] = {}
        for idx, det in enumerate(detections):
            sid = (det.get("station") or {}).get("id")
            if sid:
                station_ids.setdefault(str(sid), []).append(idx)

        if not station_ids:
            logger.info("Weather enrichment: no station IDs found in detections")
            return 0

        logger.info(
            f"Enriching weather data for {len(station_ids)} unique stations "
            f"across {len(detections)} detections..."
        )

        # Fetch weather for each unique station (with cache + rate limit)
        weather_cache: dict[str, dict] = {}
        enriched_count = 0

        with tqdm(
            total=len(station_ids),
            desc="Fetching station weather",
            unit="stn",
            colour="cyan",
        ) as pbar:
            for sid in station_ids:
                try:
                    station_data = self.client.fetch_station_weather(sid)
                    if station_data:
                        weather_cache[sid] = {
                            k: station_data[k]
                            for k in ("weather", "airPollution", "sensors")
                            if station_data.get(k)
                        }
                        enriched_count += 1
                except Exception as e:
                    logger.debug(f"Weather fetch failed for station {sid}: {e}")

                pbar.update(1)
                time.sleep(sleep_between)

        logger.info(
            f"Weather data retrieved for {enriched_count}/{len(station_ids)} stations"
        )

        # Merge weather into detections
        for sid, indices in station_ids.items():
            env_data = weather_cache.get(sid)
            if not env_data:
                continue
            for idx in indices:
                detections[idx]["station_weather"] = env_data.get("weather")
                detections[idx]["station_air_pollution"] = env_data.get("airPollution")
                detections[idx]["station_sensors"] = env_data.get("sensors")

        # Rewrite detections.jsonl with enriched data
        if detections_file and detections_file.exists():
            tmp = detections_file.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for det in detections:
                    f.write(json.dumps(det) + "\n")
            tmp.replace(detections_file)
            logger.info(f"Rewrote {detections_file} with weather data")

        return enriched_count

    def _sync_stations(
        self,
        detections: list[dict],
        date_dir: Path | None = None,
    ) -> int:
        """Extract unique stations from detections and save to stations.jsonl.

        Deduplicates station records from the detection stream and writes
        one JSONL line per unique station. If ``date_dir`` is provided,
        the file is written atomically via a ``.tmp`` rename.

        Args:
            detections: List of detection dicts (each with a ``station`` key).
            date_dir: Directory to write ``stations.jsonl`` into.

        Returns:
            Number of unique stations found.
        """
        stations: dict[str, dict] = {}
        for det in detections:
            st = det.get("station") or {}
            sid = st.get("id")
            if sid and sid not in stations:
                stations[sid] = {
                    "id": sid,
                    "name": st.get("name"),
                    "type": st.get("type"),
                    "country": st.get("country"),
                    "state": st.get("state"),
                    "coords": st.get("coords"),
                    "_synced_at": datetime.now().isoformat(),
                }

        if not stations:
            return 0

        if date_dir:
            stations_file = date_dir / "stations.jsonl"
            tmp = stations_file.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for st in stations.values():
                    f.write(json.dumps(st) + "\n")
            tmp.replace(stations_file)
            logger.info(f"Synced {len(stations)} stations to {stations_file}")

        return len(stations)

    def _station_metadata_record_from_detection(
        self,
        detection: dict[str, Any],
        *,
        synced_at: datetime,
    ) -> dict[str, Any] | None:
        """Build a station metadata row from a detection payload."""
        station = detection.get("station") or {}
        station_id = station.get("id")
        if not station_id:
            return None

        coords = station.get("coords") or {}
        return {
            "station_id": str(station_id),
            "station_name": station.get("name"),
            "station_type": station.get("type"),
            "station_country": station.get("country"),
            "station_state": station.get("state"),
            "station_location": station.get("location"),
            "station_timezone": station.get("timezone"),
            "station_continent": station.get("continent"),
            "lat": coords.get("lat"),
            "lon": coords.get("lon"),
        }

    def _species_metadata_record_from_detection(
        self,
        detection: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Build a species metadata row from a detection payload."""
        species = detection.get("species") or {}
        species_id = str(species.get("id") or detection.get("speciesId") or "").strip()
        if not species_id:
            return None

        common_name = str(
            species.get("commonName") or species.get("scientificName") or species_id
        ).strip()
        if not common_name:
            return None

        return {
            "species_id": species_id,
            "common_name": common_name,
            "scientific_name": species.get("scientificName"),
            "alpha_code": species.get("alpha"),
            "alpha6_code": species.get("alpha6"),
            "ebird_code": species.get("ebirdCode"),
            "species_color": species.get("color"),
            "species_image_url": species.get("imageUrl"),
            "species_thumbnail_url": species.get("thumbnailUrl"),
            "birdweather_url": species.get("birdweatherUrl"),
            "ebird_url": species.get("ebirdUrl"),
            "ml_url": species.get("mlUrl"),
            "wikipedia_url": species.get("wikipediaUrl"),
            "wikipedia_summary": species.get("wikipediaSummary"),
            "prediction_area": species.get("predictionArea"),
            "range_map": species.get("range"),
        }

    def _persist_species_metadata_to_db(
        self,
        detections: list[dict],
    ) -> int:
        """Upsert BirdWeather species metadata discovered during collection."""
        if not detections:
            return 0

        from avian_biosurveillance.db.loaders import (
            upsert_birdweather_species,
        )

        species_records: dict[str, dict[str, Any]] = {}

        for detection in detections:
            record = self._species_metadata_record_from_detection(detection)
            if record and record["species_id"] not in species_records:
                lookup_name = record.get("scientific_name") or record.get("common_name")
                phylopic = resolve_phylopic(str(lookup_name) if lookup_name else None)
                if phylopic.svg_url or phylopic.thumb_url or phylopic.uuid:
                    record["phylopic_svg_url"] = phylopic.svg_url
                    record["phylopic_thumb_url"] = phylopic.thumb_url
                    record["phylopic_attribution"] = phylopic.attribution
                    record["phylopic_uuid"] = phylopic.uuid
                    record["phylopic_fetched_at"] = datetime.now(UTC)
                species_records[record["species_id"]] = record

        if not species_records:
            logger.warning("No BirdWeather species metadata available for DB upsert.")
            return 0

        total = upsert_birdweather_species(list(species_records.values()))
        logger.info("DB: upserted %d birdweather species", total)
        return total

    def _station_metadata_record_from_api(
        self,
        station: dict[str, Any],
        *,
        synced_at: datetime,
    ) -> dict[str, Any] | None:
        """Build a station metadata row from the single-station API payload."""
        if not station:
            return None

        station_id = station.get("id")
        if not station_id:
            return None

        coords = station.get("coords") or {}
        location_privacy = str(station.get("locationPrivacy") or "").lower()
        return {
            "station_id": str(station_id),
            "station_name": station.get("name"),
            "station_type": station.get("type"),
            "station_country": station.get("country"),
            "station_state": station.get("state"),
            "station_location": station.get("location"),
            "station_timezone": station.get("timezone"),
            "station_continent": station.get("continent"),
            "lat": coords.get("lat"),
            "lon": coords.get("lon"),
        }

    def _persist_station_metadata_to_db(
        self,
        detections: list[dict],
    ) -> int:
        """Upsert BirdWeather station metadata discovered during collection."""
        if not detections:
            return 0

        from avian_biosurveillance.db.loaders import (
            upsert_birdweather_stations,
        )

        synced_at = datetime.now(UTC)
        station_records: dict[str, dict[str, Any]] = {}

        for detection in detections:
            record = self._station_metadata_record_from_detection(
                detection,
                synced_at=synced_at,
            )
            if record and record["station_id"] not in station_records:
                station_records[record["station_id"]] = record

        if not station_records:
            logger.warning("No BirdWeather station metadata available for DB upsert.")
            return 0

        for station_id, base_record in list(station_records.items()):
            try:
                station_detail = self.client.fetch_station_metadata(station_id)
            except Exception as e:
                logger.debug(
                    "Station metadata fetch failed for %s: %s",
                    station_id,
                    e,
                )
                continue

            api_record = self._station_metadata_record_from_api(
                station_detail or {},
                synced_at=synced_at,
            )
            if not api_record:
                continue

            station_records[station_id] = {
                **base_record,
                **{
                    key: value for key, value in api_record.items() if value is not None
                },
            }

        total = upsert_birdweather_stations(list(station_records.values()))
        logger.info("DB: upserted %d birdweather stations", total)
        return total

    def _persist_detections_to_db(
        self,
        detections: list[dict],
    ) -> int:
        """Convert BirdWeather detections to DB rows and upsert them.

        Malformed detections are skipped individually so a single bad API
        record does not abort the entire ingestion batch.
        """
        if not detections:
            return 0

        db_records = self._prepare_detection_db_records(detections)
        if not db_records:
            logger.warning("No valid BirdWeather detections available for DB upsert.")
            return 0

        from avian_biosurveillance.db.loaders import (
            upsert_birdweather_detections,
        )

        n = upsert_birdweather_detections(db_records)
        logger.info(f"DB: upserted {n} birdweather detections")
        return n

    def _prepare_detection_db_records(
        self,
        detections: list[dict],
    ) -> list[Any]:
        """Convert detection payloads to DB model rows, skipping malformed items."""
        if not detections:
            return []

        from avian_biosurveillance.db.models import BirdWeatherDetection

        db_records = []
        skipped = 0

        for det in detections:
            try:
                db_records.append(BirdWeatherDetection.from_jsonl(det))
            except Exception as e:
                skipped += 1
                logger.debug(
                    "Skipping malformed BirdWeather detection %s during DB preparation: %s",
                    det.get("id", "unknown"),
                    e,
                )

        if skipped:
            logger.warning(
                "Skipped %d malformed BirdWeather detections during DB preparation.",
                skipped,
            )

        return db_records

    def _load_local_detections(self, date_dir: Path) -> list[dict[str, Any]]:
        """Load one local BirdWeather bundle into memory for sync."""
        detections_file = date_dir / "detections.jsonl"
        detections: list[dict[str, Any]] = []
        if not detections_file.exists():
            return detections

        with detections_file.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "Skipping malformed local detection JSON in %s",
                        detections_file,
                    )
                    continue
                if isinstance(payload, dict):
                    detections.append(payload)
        return detections

    def _upload_local_audio_bundle(
        self,
        date_dir: Path,
        detections: list[dict[str, Any]],
    ) -> tuple[int, int, int]:
        """Upload locally staged BirdWeather audio into MinIO raw-audio."""
        audio_dir = date_dir / "audio"
        if not audio_dir.exists():
            return 0, 0, 0

        from avian_biosurveillance.db.minio_client import (
            BUCKET_RAW_AUDIO,
            AudioStore,
        )

        store = AudioStore()
        existing_keys = set(
            store.list_objects(
                prefix=f"{date_dir.name}/audio/",
                bucket=BUCKET_RAW_AUDIO,
                recursive=True,
            )
        )

        uploaded = 0
        already_present = 0
        failed = 0
        seen_keys: set[str] = set()

        for detection in detections:
            filename = _local_audio_filename_for_detection(detection)
            if filename is None:
                continue

            object_key = str(PurePosixPath(date_dir.name, "audio", filename))
            if object_key in seen_keys:
                continue
            seen_keys.add(object_key)

            if object_key in existing_keys:
                already_present += 1
                continue

            audio_path = audio_dir / filename
            if not audio_path.exists():
                continue

            try:
                store.upload_audio(
                    object_key,
                    audio_path.read_bytes(),
                    bucket=BUCKET_RAW_AUDIO,
                    content_type=_audio_content_type(audio_path),
                )
                uploaded += 1
            except Exception:
                failed += 1
                logger.exception(
                    "BirdWeather local audio upload failed for %s",
                    object_key,
                )

        return uploaded, already_present, failed

    def sync_local_bundle_to_database(
        self,
        date_dir: Path,
        *,
        delete_on_success: bool = True,
    ) -> BundleSyncStats:
        """Sync one locally staged BirdWeather date bundle into DB/MinIO."""
        stats = BundleSyncStats(bundles_discovered=1)
        manifest = _read_json_file(date_dir / "manifest.json")
        if not _collection_manifest_is_syncable(manifest):
            stats.bundles_skipped = 1
            return stats

        detections = self._load_local_detections(date_dir)
        if not detections:
            stats.bundles_skipped = 1
            return stats

        stats.bundles_attempted = 1
        _update_collection_load_status(date_dir=date_dir, status="in_progress")

        try:
            stats.species_upserted = self._persist_species_metadata_to_db(detections)
            stats.stations_upserted = self._persist_station_metadata_to_db(detections)
            stats.detections_upserted = self._persist_detections_to_db(detections)
            (
                stats.audio_uploaded,
                stats.audio_already_present,
                stats.audio_upload_failed,
            ) = self._upload_local_audio_bundle(date_dir, detections)
            if stats.audio_upload_failed:
                msg = (
                    "Failed to upload "
                    f"{stats.audio_upload_failed} BirdWeather audio object(s)"
                )
                raise RuntimeError(msg)

            _update_collection_load_status(
                date_dir=date_dir,
                status="success",
                results=stats.to_metadata(),
            )
            stats.bundles_synced = 1

            if delete_on_success and date_dir.exists():
                shutil.rmtree(date_dir)
                stats.directories_deleted = 1

            return stats
        except Exception as exc:
            stats.bundles_failed = 1
            _update_collection_load_status(
                date_dir=date_dir,
                status="failed",
                error=str(exc),
                results=stats.to_metadata(),
            )
            raise

    def sync_local_bundles_to_database(
        self,
        *,
        limit: int | None = None,
        bundle_dates: list[str] | None = None,
        delete_on_success: bool = True,
    ) -> BundleSyncStats:
        """Sync pending local BirdWeather bundles into DB/MinIO oldest-first."""
        selected_dates = set(bundle_dates) if bundle_dates else None
        bundle_dirs = find_pending_birdweather_bundle_dirs(
            self.output_dir,
            limit=limit,
            bundle_dates=selected_dates,
        )
        aggregate = BundleSyncStats(bundles_discovered=len(bundle_dirs))
        failed_dates: list[str] = []

        for date_dir in bundle_dirs:
            try:
                aggregate.absorb(
                    self.sync_local_bundle_to_database(
                        date_dir,
                        delete_on_success=delete_on_success,
                    )
                )
            except Exception:
                aggregate.bundles_attempted += 1
                aggregate.bundles_failed += 1
                failed_dates.append(date_dir.name)
                logger.exception(
                    "BirdWeather bundle sync failed for %s",
                    date_dir.name,
                )

        if failed_dates:
            msg = "BirdWeather bundle sync failed for: " + ", ".join(failed_dates)
            raise RuntimeError(msg)

        return aggregate

    def collect(
        self,
        hours: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        min_confidence: float = 0.9,
        continent: str | None = None,
        country: str | None = None,
        species_ids: list[str] | None = None,
        download_audio: bool = True,
        max_detections: int | None = None,
        workers: int = 10,
        target: str = "local",
        enrich_weather: bool = True,
    ) -> CollectionStats:
        """Collect all detections and audio.

        This method fetches detections directly (without per-species queries),
        which is more efficient and bypasses the failing stations endpoint.

        BirdWeather always stages one local date bundle under ``output_dir``.
        When ``target='database'``, the collector then attempts to sync that
        finished bundle into TimescaleDB and MinIO. If the sync fails, the
        local bundle is retained for retry.

        Args:
            hours (Optional[int]): Number of hours to look back (ignored if start_time/end_time set).
            start_time (Optional[datetime]): Start of time range.
            end_time (Optional[datetime]): End of time range.
            min_confidence (float): Minimum detection confidence (default: 0.9).
            continent (Optional[str]): Continent filter (e.g., "Europe").
            country (Optional[str]): Country filter (e.g., "Netherlands").
            species_ids (Optional[List[str]]): List of species IDs to filter by.
            download_audio (bool): If True, download audio files.
            max_detections (Optional[int]): Limit number of detections (for testing).
            workers (int): Number of parallel download workers (default: 10).
            target (str): Storage target — ``"local"`` keeps the staged bundle
                on disk (default), ``"database"`` stages locally first and
                then attempts a DB/MinIO sync.
            enrich_weather (bool): If True, fetch weather/air-pollution/sensor
                data for each unique station and merge into detection records.

        Returns:
            CollectionStats: Summary statistics of what was collected.
        """
        if target not in {"local", "database"}:
            msg = f"Unsupported BirdWeather target: {target}"
            raise ValueError(msg)

        save_local = True

        stats = CollectionStats()

        # Calculate time range
        if start_time and end_time:
            pass  # Use provided times
        elif hours:
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=hours)
        else:
            hours = 24  # Default
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=hours)

        today = end_time.strftime("%Y-%m-%d")
        date_dir = self.output_dir / today
        audio_dir: Path | None = None

        logger.info(f"Collecting data from {start_time} to {end_time}")
        logger.info(f"Output directory: {date_dir}")
        if target == "database":
            logger.info(
                "Database target selected: bundle will be staged locally before DB/MinIO sync"
            )

        date_dir.mkdir(exist_ok=True)

        if download_audio:
            audio_dir = date_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)

        # Build filters
        continents = [continent] if continent else None
        countries = [country] if country else None

        filter_desc = []
        if continent:
            filter_desc.append(f"continent={continent}")
        if country:
            filter_desc.append(f"country={country}")
        if species_ids:
            filter_desc.append(f"species={','.join(species_ids)}")
        filter_str = ", ".join(filter_desc) if filter_desc else "no filter"

        logger.info(f"Fetching detections ({filter_str})...")

        # Map Country Bounding Box
        # GraphQL requires station IDs or bounding boxes to bypass global post-pagination limits
        ne_target = None
        sw_target = None

        # Hardcoding known coordinates for popular countries to bypass BirdWeather GraphQL truncation bug
        if country and country.lower() == "netherlands":
            ne_target = {"lat": 53.7, "lon": 7.22}
            sw_target = {"lat": 50.75, "lon": 3.33}

        # Get total count first for progress tracking
        # Always pass bounding box targets if available to optimize unindexed database queries
        total_count = self.client.get_detections_count(
            start=start_time,
            end=end_time,
            species_ids=species_ids,
            continents=continents,
            countries=countries,
            ne=ne_target,
            sw=sw_target,
            confidence_gte=min_confidence,
            valid_soundscape=None,
        )

        # Apply max_detections limit to expected total
        expected_total = (
            min(total_count, max_detections) if max_detections else total_count
        )
        logger.info(
            f"Total detections available: {total_count:,}"
            + (f" (limited to {max_detections:,})" if max_detections else "")
        )

        all_detections = []
        species_seen: dict[str, str] = {}  # id -> name

        # Load previously collected data for resume support.
        metadata_file = date_dir / "metadata.json"
        detections_file = date_dir / "detections.jsonl"

        if metadata_file.exists():
            try:
                with open(metadata_file, encoding="utf-8") as f:
                    old_meta = json.load(f)
                    # Support both new and legacy metadata layouts
                    prev_species = old_meta.get("results", {}).get(
                        "species"
                    ) or old_meta.get("species_list", [])
                    for s in prev_species:
                        species_seen[str(s["id"])] = s.get("commonName", "Unknown")
                logger.info(
                    f"Loaded {len(species_seen)} previously collected species for this day"
                )
            except Exception as e:
                logger.warning(
                    f"Could not load previous species list from metadata: {e}"
                )

        # Load existing detection IDs to skip duplicates on resume
        existing_detection_ids: set = set()
        if detections_file.exists():
            with open(detections_file, encoding="utf-8") as f:
                for line in f:
                    try:
                        det_id = json.loads(line).get("id")
                        if det_id:
                            existing_detection_ids.add(str(det_id))
                    except json.JSONDecodeError:
                        continue
            if existing_detection_ids:
                logger.info(
                    f"Found {len(existing_detection_ids)} existing detections — will skip duplicates"
                )

        # Stream detections (optionally to disk for crash-resilience)
        fetch_interrupted = False
        det_file = open(detections_file, "a", encoding="utf-8")
        try:
            with tqdm(
                total=expected_total,
                desc="Fetching detections",
                unit="det",
                colour="green",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            ) as pbar:
                for det in self.client.fetch_detections(
                    start=start_time,
                    end=end_time,
                    species_ids=species_ids,
                    continents=continents,
                    countries=countries,
                    ne=ne_target,
                    sw=sw_target,
                    confidence_gte=min_confidence,
                    valid_soundscape=None,
                ):
                    processed_detections = pbar.n + 1
                    stats.detections_scanned = processed_detections

                    # Skip already-saved detections (resume support)
                    det_id = str(det.get("id", ""))
                    should_skip = bool(det_id and det_id in existing_detection_ids)

                    # Client-side bbox filter — BirdWeather API country/bbox filters are unreliable
                    if not should_skip and ne_target and sw_target:
                        sta = det.get("station") or {}
                        sta_coords = sta.get("coords") or {}
                        s_lat = sta_coords.get("lat")
                        s_lon = sta_coords.get("lon")
                        if s_lat is not None and s_lon is not None:
                            if not (
                                sw_target["lat"] <= s_lat <= ne_target["lat"]
                                and sw_target["lon"] <= s_lon <= ne_target["lon"]
                            ):
                                stats.detections_skipped = (
                                    getattr(stats, "detections_skipped", 0) + 1
                                )
                                should_skip = True

                    if not should_skip:
                        det["_collected_at"] = datetime.now().isoformat()

                        # Track species
                        species_id = det.get(
                            "speciesId", (det.get("species") or {}).get("id")
                        )
                        species_name = (det.get("species") or {}).get(
                            "commonName", "Unknown"
                        )
                        if species_id:
                            species_seen[str(species_id)] = species_name

                        # Write to disk immediately (local mode)
                        if det_file is not None:
                            det_file.write(json.dumps(det) + "\n")
                            det_file.flush()

                        all_detections.append(det)
                        stats.detections_found += 1

                        # Count those with audio
                        if (det.get("soundscape") or {}).get("url"):
                            stats.detections_with_audio += 1

                    # Update progress bar
                    pbar.update(1)
                    pbar.set_postfix(
                        species=len(species_seen),
                        with_audio=stats.detections_with_audio,
                    )

                    # Limit for testing: cap total detections scanned from the API,
                    # including duplicates skipped during resume runs.
                    if max_detections and processed_detections >= max_detections:
                        logger.info(
                            f"Reached limit of {max_detections} scanned detections"
                        )
                        break
        except Exception as e:
            fetch_interrupted = True
            logger.error(
                f"Fetch interrupted after {stats.detections_found} detections: {e}"
            )
            logger.info("Progress saved — re-run to resume from where it stopped")
        finally:
            if det_file is not None:
                det_file.close()

        logger.info(
            f"\nTotal: {stats.detections_found} new detections from {len(species_seen)} species"
            + (" (interrupted)" if fetch_interrupted else "")
        )
        stats.species_queried = len(species_seen)

        # Prepare comprehensive species data from the full detections file
        # (Needed to accurately count distribution across resumed batches)
        species_counts: dict[str, dict[str, Any]] = {}
        total_duration_sec = 0.0

        if save_local and detections_file is not None and detections_file.exists():
            with open(detections_file, encoding="utf-8") as f:
                for line in f:
                    try:
                        det = json.loads(line)
                        sp_id = str(
                            det.get(
                                "speciesId", (det.get("species") or {}).get("id", "")
                            )
                        )
                        sp_name = (det.get("species") or {}).get(
                            "commonName", "Unknown"
                        )

                        if sp_id:
                            if sp_id not in species_counts:
                                species_counts[sp_id] = {
                                    "id": sp_id,
                                    "commonName": sp_name,
                                    "count": 0,
                                    "audio_count": 0,
                                }
                            species_counts[sp_id]["count"] += 1

                        soundscape = det.get("soundscape") or {}
                        if soundscape and "url" in soundscape:
                            species_counts[sp_id]["audio_count"] += 1

                        duration = soundscape.get("duration") if soundscape else None
                        if duration is not None:
                            total_duration_sec += float(duration)
                    except json.JSONDecodeError:
                        continue
        else:
            for det in all_detections:
                sp_id = str(
                    det.get("speciesId", (det.get("species") or {}).get("id", ""))
                )
                sp_name = (det.get("species") or {}).get("commonName", "Unknown")

                if sp_id:
                    if sp_id not in species_counts:
                        species_counts[sp_id] = {
                            "id": sp_id,
                            "commonName": sp_name,
                            "count": 0,
                            "audio_count": 0,
                        }
                    species_counts[sp_id]["count"] += 1

                soundscape = det.get("soundscape") or {}
                if soundscape and "url" in soundscape and sp_id:
                    species_counts[sp_id]["audio_count"] += 1

                duration = soundscape.get("duration") if soundscape else None
                if duration is not None:
                    total_duration_sec += float(duration)

        # Sort species list by count (highest first)
        species_data = sorted(
            list(species_counts.values()), key=lambda x: x["count"], reverse=True
        )

        # Enrich detections with station weather/air-pollution/sensor data
        if enrich_weather and all_detections:
            logger.info(
                "Enriching detections with station weather, air-pollution, and sensor data..."
            )
            stations_enriched = self._enrich_weather_data(
                detections=all_detections,
                detections_file=detections_file,
            )
            logger.info(f"Weather enrichment complete: {stations_enriched} stations")

        # Download audio with parallel workers (local mode only)
        if audio_dir is not None and download_audio and stats.detections_with_audio > 0:
            logger.info(
                f"\nDownloading audio for {stats.detections_with_audio} detections with {workers} parallel workers..."
            )

            # Prepare download tasks
            download_tasks = []
            for det in all_detections:
                soundscape = det.get("soundscape")
                if not soundscape or not soundscape.get("url"):
                    continue

                url = soundscape["url"]
                detection_id = det.get("id", "unknown")
                species_id = det.get(
                    "speciesId", (det.get("species") or {}).get("id", "unknown")
                )
                ext = (
                    ".mp3" if ".mp3" in url else (".wav" if ".wav" in url else ".flac")
                )
                filename = f"{detection_id}_{species_id}{ext}"

                download_tasks.append(
                    {
                        "url": url,
                        "audio_path": str(audio_dir / filename),
                        "expected_size": soundscape.get("filesize"),
                    }
                )

            # Execute parallel downloads with tqdm progress bar
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._download_single_audio, task): task
                    for task in download_tasks
                }

                with tqdm(
                    total=len(download_tasks),
                    desc="Downloading audio",
                    unit="file",
                    colour="green",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                ) as pbar:
                    for future in as_completed(futures):
                        try:
                            success, status = future.result()
                            if success:
                                if status == "already_exists":
                                    stats.audio_cached += 1
                                else:
                                    stats.audio_downloaded += 1
                            else:
                                stats.audio_failed += 1
                        except Exception as e:
                            stats.audio_failed += 1
                            logger.debug(f"Download failed: {e}")

                        pbar.update(1)
                        pbar.set_postfix(
                            new=stats.audio_downloaded,
                            cached=stats.audio_cached,
                            failed=stats.audio_failed,
                        )

            logger.info(
                f"Audio complete: {stats.audio_downloaded} downloaded, "
                f"{stats.audio_cached} cached, {stats.audio_failed} failed"
            )

        # Save extended metadata.
        stats.end_time = datetime.now()
        stats_dict = stats.to_dict()
        metadata = {
            "data_range": {
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
                "hours": hours,
            },
            "filters": {
                "min_confidence": min_confidence,
                "continent": continent,
                "country": country,
                "species_ids": species_ids,
                "download_audio": download_audio,
            },
            "results": {
                **stats_dict["results"],
                "audio_duration_hours": round(total_duration_sec / 3600.0, 4),
                "species": species_data,
            },
            "run": {
                "completed": not fetch_interrupted,
                **stats_dict["run"],
            },
        }

        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        logger.info(f"Saved extended metadata to {metadata_file}")

        stations_synced = 0
        if all_detections:
            stations_synced = self._sync_stations(all_detections, date_dir)

        collection_status = (
            "collection_failed"
            if fetch_interrupted or (download_audio and stats.audio_failed > 0)
            else "collected"
        )
        _write_collection_manifest(
            date_dir=date_dir,
            status=collection_status,
            start_time=start_time,
            end_time=end_time,
            hours=hours,
            min_confidence=min_confidence,
            continent=continent,
            country=country,
            species_ids=species_ids,
            download_audio=download_audio,
            workers=workers,
            max_detections=max_detections,
            stats=stats,
            fetch_interrupted=fetch_interrupted,
            stations_synced=stations_synced,
        )

        if target == "database":
            try:
                sync_stats = self.sync_local_bundle_to_database(
                    date_dir,
                    delete_on_success=True,
                )
                logger.info(
                    "BirdWeather bundle sync complete: %d detection(s) upserted, %d audio uploaded, %d already present.",
                    sync_stats.detections_upserted,
                    sync_stats.audio_uploaded,
                    sync_stats.audio_already_present,
                )
            except Exception as exc:
                stats.errors.append(f"bundle sync failed: {exc}")
                logger.exception(
                    "BirdWeather bundle sync failed for %s; local bundle retained for retry.",
                    date_dir,
                )

        # Final summary
        logger.info("\n" + "=" * 60)
        if fetch_interrupted:
            logger.info("COLLECTION INTERRUPTED — re-run to resume")
        else:
            logger.info("COLLECTION COMPLETE")
        logger.info("=" * 60)
        logger.info(
            f"Duration: {(stats.end_time - stats.start_time).total_seconds():.1f} seconds"
        )
        logger.info(f"Species found: {len(species_seen)}")
        logger.info(f"Detections found: {stats.detections_found}")
        logger.info(f"Detections scanned: {stats.detections_scanned}")
        logger.info(
            f"Audio files: {stats.audio_downloaded} new, {stats.audio_cached} cached, {stats.audio_failed} failed"
        )
        if date_dir.exists():
            logger.info(f"Output: {date_dir}")

        return stats

    def retry_failed_downloads(
        self, directory: Path, workers: int = 10
    ) -> CollectionStats:
        """Retry failed audio downloads from an existing collection directory.

        Args:
            directory (Path): Path to the collection directory (e.g., 'data/raw/birdweather/2026-01-02').
            workers (int): Number of parallel download workers.

        Returns:
            CollectionStats: Summary statistics of the retry operation.
        """
        if not directory.exists():
            logger.error(f"Directory not found: {directory}")
            return CollectionStats()

        detections_file = directory / "detections.jsonl"
        if not detections_file.exists():
            logger.error(f"Detections file not found: {detections_file}")
            return CollectionStats()

        logger.info(f"Retrying audio downloads for {directory}")

        # Load detections
        detections = []
        with open(detections_file, encoding="utf-8") as f:
            for line in f:
                try:
                    detections.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        logger.info(f"Loaded {len(detections)} detections from file")

        stats = CollectionStats()
        stats.detections_found = len(detections)

        audio_dir = directory / "audio"
        audio_dir.mkdir(exist_ok=True)

        # Prepare tasks
        download_tasks = []
        for det in detections:
            soundscape = det.get("soundscape")
            if not soundscape or not soundscape.get("url"):
                continue

            stats.detections_with_audio += 1

            url = soundscape["url"]
            detection_id = det.get("id", "unknown")
            species_id = det.get(
                "speciesId", det.get("species", {}).get("id", "unknown")
            )
            ext = ".mp3" if ".mp3" in url else (".wav" if ".wav" in url else ".flac")
            filename = f"{detection_id}_{species_id}{ext}"

            download_tasks.append(
                {
                    "url": url,
                    "audio_path": str(audio_dir / filename),
                    "expected_size": soundscape.get("filesize"),
                }
            )

        if not download_tasks:
            logger.info("No audio URLs found in detections.")
            return stats

        logger.info(
            f"Checking {len(download_tasks)} audio files with {workers} workers..."
        )

        # Execute parallel downloads
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._download_single_audio, task): task
                for task in download_tasks
            }

            with tqdm(
                total=len(download_tasks),
                desc="Retrying downloads",
                unit="file",
                colour="green",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            ) as pbar:
                for future in as_completed(futures):
                    try:
                        success, status = future.result()
                        if success:
                            if status == "already_exists":
                                stats.audio_cached += 1
                            else:
                                stats.audio_downloaded += 1
                        else:
                            stats.audio_failed += 1
                    except Exception as e:
                        stats.audio_failed += 1
                        logger.debug(f"Download failed: {e}")

                    pbar.update(1)
                    pbar.set_postfix(
                        new=stats.audio_downloaded,
                        cached=stats.audio_cached,
                        failed=stats.audio_failed,
                    )

        # Update metadata if it exists
        metadata_file = directory / "metadata.json"
        if metadata_file.exists():
            try:
                with open(metadata_file, encoding="utf-8") as f:
                    metadata = json.load(f)

                retry_dict = stats.to_dict()

                # Update cumulative audio counts in results
                results = metadata.get("results", {})
                results["audio_downloaded"] = (
                    results.get("audio_downloaded", 0) + stats.audio_downloaded
                )
                results["audio_present_on_disk"] = (
                    stats.audio_downloaded + stats.audio_cached
                )
                results["audio_failed"] = stats.audio_failed
                metadata["results"] = results

                # Append retry run info
                metadata["last_retry"] = {
                    **retry_dict["run"],
                    **retry_dict["results"],
                }

                with open(metadata_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)
                logger.info("Updated metadata.json with retry stats")
            except Exception as e:
                logger.error(f"Failed to update metadata: {e}")

        logger.info("\n" + "=" * 60)
        logger.info("RETRY COMPLETE")
        logger.info("=" * 60)
        logger.info(
            f"Audio files: {stats.audio_downloaded} recovered, {stats.audio_cached} verified, {stats.audio_failed} still failing"
        )

        return stats


def _run_background_classification(
    date_dir: Path,
    device: str | None = None,
    batch_size: int = 8,
    force: bool = False,
) -> None:
    """Run AST background sound classification on a date directory.

    Gracefully handles missing dependencies or classification failures
    without interrupting the overall ingestion pipeline.
    """
    try:
        from avian_biosurveillance.ingestion.background_sound_classification import (
            enrich_detections,
        )

        logger.info(f"Running background sound classification on {date_dir.name}...")
        stats = enrich_detections(
            date_dir,
            device=device,
            batch_size=batch_size,
            force=force,
        )
        logger.info(
            f"Background classification: {stats.classified} classified, "
            f"{stats.skipped_existing} pre-tagged"
        )
    except FileNotFoundError as e:
        logger.warning(f"Background classification skipped: {e}")
    except ImportError:
        logger.warning(
            "Background classification skipped: transformers not installed. "
            "Run: pip install transformers"
        )
    except Exception:
        logger.exception("Background classification failed (non-fatal)")


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.debug("Could not read JSON file %s", path)
        return {}


def _write_json_file_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def _write_collection_manifest(
    *,
    date_dir: Path,
    status: str,
    start_time: datetime,
    end_time: datetime,
    hours: int | None,
    min_confidence: float,
    continent: str | None,
    country: str | None,
    species_ids: list[str] | None,
    download_audio: bool,
    workers: int,
    max_detections: int | None,
    stats: CollectionStats | None = None,
    fetch_interrupted: bool = False,
    stations_synced: int = 0,
) -> None:
    """Write the local outbox manifest for one BirdWeather date bundle."""
    manifest_file = date_dir / "manifest.json"
    existing = _read_json_file(manifest_file)
    now = datetime.now(UTC).isoformat()
    detections_file = date_dir / "detections.jsonl"
    stations_file = date_dir / "stations.jsonl"
    audio_dir = date_dir / "audio"

    payload: dict[str, Any] = {
        "schema_version": 1,
        "source": "birdweather",
        "status": status,
        "bundle_date": date_dir.name,
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "data_range": {
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
            "hours": hours,
        },
        "filters": {
            "min_confidence": min_confidence,
            "continent": continent,
            "country": country,
            "species_ids": species_ids or [],
            "download_audio": download_audio,
            "workers": workers,
            "max_detections": max_detections,
        },
        "artifacts": {
            "detections_file": detections_file.name,
            "stations_file": stations_file.name if stations_file.exists() else None,
            "audio_dir": audio_dir.name if audio_dir.exists() else None,
        },
        "collection": {
            "fetch_interrupted": fetch_interrupted,
            "stations_synced": stations_synced,
            "results": stats.to_dict()["results"] if stats else {},
        },
    }
    if existing.get("load"):
        payload["load"] = existing["load"]

    _write_json_file_atomic(manifest_file, payload)
    logger.info(
        "Updated BirdWeather local manifest %s with status=%s",
        manifest_file,
        status,
    )


_SYNC_AUDIO_CONTENT_TYPES = {
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
}


def _audio_extension_from_url(url: str) -> str:
    lowered = url.lower()
    for suffix in _SYNC_AUDIO_CONTENT_TYPES:
        if suffix in lowered:
            return suffix
    return ".flac"


def _local_audio_filename_for_detection(detection: dict[str, Any]) -> str | None:
    soundscape = detection.get("soundscape") or {}
    url = str(soundscape.get("url") or "").strip()
    detection_id = str(detection.get("id") or "").strip()
    species_id = str(
        detection.get("speciesId")
        or (detection.get("species") or {}).get("id")
        or ""
    ).strip()
    if not url or not detection_id or not species_id:
        return None
    return f"{detection_id}_{species_id}{_audio_extension_from_url(url)}"


def _audio_content_type(path: Path) -> str:
    return _SYNC_AUDIO_CONTENT_TYPES.get(
        path.suffix.lower(),
        "application/octet-stream",
    )


def _collection_manifest_is_syncable(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    if payload.get("source") != "birdweather":
        return False
    if ((payload.get("load") or {}).get("status") or "").lower() == "success":
        return False
    collection = payload.get("collection") or {}
    return not bool(collection.get("fetch_interrupted"))


def find_pending_birdweather_bundle_dirs(
    output_dir: Path,
    *,
    limit: int | None = None,
    bundle_dates: set[str] | None = None,
) -> list[Path]:
    """Return syncable BirdWeather date directories ordered oldest-first."""
    if not output_dir.exists():
        return []

    pending: list[Path] = []
    for child in sorted(output_dir.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if bundle_dates and child.name not in bundle_dates:
            continue

        manifest = _read_json_file(child / "manifest.json")
        detections_file = child / "detections.jsonl"
        if not detections_file.exists() or not _collection_manifest_is_syncable(manifest):
            continue

        pending.append(child)
        if limit is not None and len(pending) >= limit:
            break

    return pending


def _update_collection_load_status(
    *,
    date_dir: Path,
    status: str,
    error: str | None = None,
    results: dict[str, int] | None = None,
) -> None:
    """Persist bundle-load progress to the local manifest."""
    manifest_file = date_dir / "manifest.json"
    payload = _read_json_file(manifest_file)
    if not payload:
        return

    now = datetime.now(UTC).isoformat()
    load = dict(payload.get("load") or {})
    if status == "in_progress":
        load["attempts"] = int(load.get("attempts") or 0) + 1
    else:
        load["attempts"] = int(load.get("attempts") or 1)
    load["status"] = status
    load["last_attempt_at"] = now
    if results:
        load["results"] = results
    if error:
        load["error"] = error
    elif "error" in load:
        load.pop("error", None)

    payload["load"] = load
    payload["updated_at"] = now
    _write_json_file_atomic(manifest_file, payload)


def _resolve_cli_species_ids(
    species_ids: list[str] | None,
) -> list[str] | None:
    """Resolve CLI species filters to configured BirdWeather targets.

    When the user does not pass ``--species-id``, BirdWeather ingestion should
    default to the project target species from ``config/target_species.json``
    instead of fetching all species.

    Args:
        species_ids: Explicit CLI species IDs, if provided.

    Returns:
        The explicit species list, the configured BirdWeather target species,
        or ``None`` when no target species are configured.
    """
    if species_ids is not None:
        return species_ids

    default_species_ids = list(registry.birdweather_ids)
    if default_species_ids:
        logger.info(
            "No --species-id provided; defaulting to %d target species from config/target_species.json",
            len(default_species_ids),
        )
        return default_species_ids

    logger.warning(
        "No BirdWeather target species configured; falling back to all species"
    )
    return None


def main() -> None:
    """CLI entrypoint for BirdWeather data ingestion.

    By default, uses AudioCollector which bypasses the failing stations endpoint.

    Parses command line arguments and invokes the appropriate collector.
    """
    parser = argparse.ArgumentParser(
        description="BirdWeather Data Collection (Automated Pipeline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Collect last 24 hours with audio (DEFAULT - bypasses stations endpoint)
  python -m avian_biosurveillance.ingestion.birdweather --hours 24
  
  # Collect last 48 hours from Europe only
  python -m avian_biosurveillance.ingestion.birdweather --hours 48 --continent Europe
  
  # Collect from a specific country
  python -m avian_biosurveillance.ingestion.birdweather --hours 24 --country Netherlands
  
  # Collect specific date range
  python -m avian_biosurveillance.ingestion.birdweather --start-date 2026-01-01 --end-date 2026-01-15
  
  # Faster downloads with more workers
  python -m avian_biosurveillance.ingestion.birdweather --hours 24 --workers 20
  
  # Collect without audio download
  python -m avian_biosurveillance.ingestion.birdweather --hours 24 --no-audio
  
  # Resume/run background classification only (no collection)
  python -m avian_biosurveillance.ingestion.birdweather --classify-only 2026-03-16
  
  # Use legacy mode (requires stations - may fail with 500 errors)
  python -m avian_biosurveillance.ingestion.birdweather --legacy --days 7

Data Structure:
  data/raw/birdweather/
    2026-01-30/                # Date-based folder
      detections.jsonl         # All detections
      species_list.json        # Species observed
      metadata.json            # Collection stats
      audio/
        {detection_id}_{species_id}.flac
        """,
    )

    # Time range options
    parser.add_argument(
        "--hours",
        type=int,
        help="Number of hours to look back (default: 24, ignored if --start-date set)",
    )
    parser.add_argument(
        "--start-date", type=str, help="Start date (YYYY-MM-DD), use with --end-date"
    )
    parser.add_argument(
        "--end-date", type=str, help="End date (YYYY-MM-DD), use with --start-date"
    )

    # Geo filters
    parser.add_argument(
        "--continent", type=str, help="Filter by continent (e.g., Europe, NorthAmerica)"
    )
    parser.add_argument(
        "--country", type=str, help="Filter by country (e.g., Netherlands, Germany)"
    )
    parser.add_argument(
        "--species-id",
        type=str,
        action="append",
        help="Filter by species ID (can use multiple times, e.g., --species-id 11 --species-id 6)",
    )

    # Quality and output options
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.9,
        help="Minimum detection confidence (default: 0.9)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/raw/birdweather",
        help="Output directory",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Skip audio download, only collect detections",
    )
    parser.add_argument(
        "--max-detections", type=int, help="Limit number of detections (for testing)"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of parallel download workers (default: 10)",
    )
    parser.add_argument(
        "--target",
        choices=["local", "database"],
        default="local",
        help="Storage target: local (disk only, default) or database (TimescaleDB + MinIO, no local detection files)",
    )
    parser.add_argument(
        "--no-enrich-weather",
        action="store_true",
        dest="no_enrich_weather",
        help="Skip weather/air-pollution/sensor enrichment (enabled by default)",
    )
    parser.add_argument(
        "--retry",
        type=str,
        help="Retry failed downloads for a specific date (YYYY-MM-DD) or directory path",
    )

    # Background sound classification (AST)
    bg_group = parser.add_argument_group(
        "Background sound classification (post-download enrichment)"
    )
    bg_group.add_argument(
        "--classify-background",
        action="store_true",
        help="Run AST background sound classification on downloaded audio",
    )
    bg_group.add_argument(
        "--classify-only",
        type=str,
        default=None,
        metavar="DATE_OR_DIR",
        help="Run/resume background classification only (skip collection). "
        "Accepts a date (YYYY-MM-DD) or path to a date directory.",
    )
    bg_group.add_argument(
        "--bg-device",
        type=str,
        default=None,
        choices=["cuda", "cpu"],
        help="Device for AST inference (default: auto-detect)",
    )
    bg_group.add_argument(
        "--bg-batch-size",
        type=int,
        default=8,
        help="Batch size for AST inference (default: 8)",
    )
    bg_group.add_argument(
        "--bg-force",
        action="store_true",
        help="Re-classify even if ambient tags already exist",
    )

    # Legacy mode (uses stations endpoint - may fail)
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Use legacy DataIngestor mode (requires stations endpoint)",
    )

    # Legacy mode options (only used with --legacy)
    legacy_group = parser.add_argument_group("Legacy mode options (use with --legacy)")
    legacy_group.add_argument(
        "--days", type=int, default=7, help="Number of days to look back"
    )
    legacy_group.add_argument("--ingest-species", action="store_true")
    legacy_group.add_argument("--ingest-aggregates", action="store_true")
    legacy_group.add_argument("--ingest-birdnet", action="store_true")
    legacy_group.add_argument("--download-all-media", action="store_true")
    legacy_group.add_argument("--skip-stations", action="store_true")

    args = parser.parse_args()

    # CLASSIFY-ONLY MODE: run/resume background classification without collection
    if args.classify_only:
        logger.info("=" * 60)
        logger.info("BIRDWEATHER BACKGROUND CLASSIFICATION (RESUME/RUN)")
        logger.info("=" * 60)

        target = Path(args.classify_only)
        if not target.exists():
            # Try as date string in default output dir
            target = Path(args.output_dir) / args.classify_only
        if not target.exists():
            logger.error(f"Could not find directory for '{args.classify_only}'")
            logger.error(
                f"Tried: {Path(args.classify_only)} and {Path(args.output_dir) / args.classify_only}"
            )
            return

        # Support parent directory (iterate all date subdirs)
        from avian_biosurveillance.ingestion.background_sound_classification import (
            _discover_date_dirs,
        )

        date_dirs = _discover_date_dirs(target)
        if not date_dirs:
            logger.error(f"No date directories with detections.jsonl found in {target}")
            return

        for dd in date_dirs:
            logger.info(f"-- Classifying: {dd.name} --")
            _run_background_classification(
                date_dir=dd,
                device=args.bg_device,
                batch_size=args.bg_batch_size,
                force=args.bg_force,
            )
        return

    # RETRY MODE
    if args.retry:
        logger.info("=" * 60)
        logger.info("BIRDWEATHER DOWNLOAD RETRY")
        logger.info("=" * 60)

        # Determine directory
        path = Path(args.retry)
        if not path.exists():
            # Try as date in default output dir
            default_dir = Path(args.output_dir) / args.retry
            if default_dir.exists():
                path = default_dir
            else:
                logger.error(f"Could not find directory for '{args.retry}'")
                logger.error(f"Tried: {path} and {default_dir}")
                return

        collector = AudioCollector(Path(args.output_dir))
        collector.retry_failed_downloads(path, workers=args.workers)
        return

    # gs = parser.parse_args()

    # DEFAULT MODE: Use AudioCollector (bypasses stations endpoint)
    if not args.legacy:
        logger.info("=" * 60)
        logger.info("BIRDWEATHER AUTOMATED COLLECTION")
        logger.info("=" * 60)
        logger.info("Using direct detection fetching (bypasses stations endpoint)")

        collector = AudioCollector(Path(args.output_dir))
        requested_species_ids = _resolve_cli_species_ids(args.species_id)

        # Date range mode: Day-by-day collection
        if args.start_date and args.end_date:
            start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
            logger.info(f"Date range: {args.start_date} to {args.end_date}")

            curr_date = start_date
            while curr_date <= end_date:
                day_start = curr_date
                day_end = curr_date.replace(hour=23, minute=59, second=59)
                day_str = day_start.strftime("%Y-%m-%d")

                # Smart skip: cross-reference requested species with already-collected species
                day_dir = Path(args.output_dir) / day_str
                metadata_file = day_dir / "metadata.json"
                day_species_ids = requested_species_ids

                if metadata_file.exists() and requested_species_ids:
                    # Species-filtered run: check which are already collected
                    with open(metadata_file, encoding="utf-8") as f:
                        meta = json.load(f)
                    existing_species = meta.get("results", {}).get(
                        "species", []
                    ) or meta.get("species_list", [])
                    existing_ids = {str(s["id"]) for s in existing_species}
                    requested_ids = {str(sid) for sid in requested_species_ids}
                    missing_ids = requested_ids - existing_ids
                    is_completed = meta.get("run", {}).get(
                        "completed", False
                    ) or meta.get("completed", False)

                    if not missing_ids:
                        if is_completed and "species_distribution" not in meta:
                            # Ensure stations.jsonl exists even on skip
                            stations_file = day_dir / "stations.jsonl"
                            if not stations_file.exists():
                                detections_file = day_dir / "detections.jsonl"
                                if detections_file.exists():
                                    dets = []
                                    with open(detections_file, encoding="utf-8") as df:
                                        for line in df:
                                            line = line.strip()
                                            if line:
                                                dets.append(json.loads(line))
                                    if dets:
                                        collector._sync_stations(dets, day_dir)
                            logger.info(
                                f"--- Skipping Day: {day_str} (all {len(requested_ids)} requested species already collected) ---"
                            )
                            # Still run classification if requested (supports resume)
                            if args.classify_background:
                                _run_background_classification(
                                    date_dir=day_dir,
                                    device=args.bg_device,
                                    batch_size=args.bg_batch_size,
                                    force=args.bg_force,
                                )
                            curr_date += timedelta(days=1)
                            continue
                        else:
                            logger.info(
                                f"--- Day: {day_str} - Rewriting legacy metadata schema or resuming ---"
                            )
                    else:
                        logger.info(
                            f"--- Day: {day_str} - {len(missing_ids)}/{len(requested_ids)} species still needed: {missing_ids} ---"
                        )
                        day_species_ids = list(missing_ids)
                elif metadata_file.exists() and not requested_species_ids:
                    # No species filter: fall back to metadata check for full-collection skip
                    with open(metadata_file, encoding="utf-8") as f:
                        meta = json.load(f)
                    is_completed = meta.get("run", {}).get(
                        "completed", False
                    ) or meta.get("completed", False)

                    # Ensure legacy format metadata falls through to be rewritten
                    if is_completed and "species_distribution" not in meta:
                        # Ensure stations.jsonl exists even on skip
                        stations_file = day_dir / "stations.jsonl"
                        if not stations_file.exists():
                            detections_file = day_dir / "detections.jsonl"
                            if detections_file.exists():
                                dets = []
                                with open(detections_file, encoding="utf-8") as df:
                                    for line in df:
                                        line = line.strip()
                                        if line:
                                            dets.append(json.loads(line))
                                if dets:
                                    collector._sync_stations(dets, day_dir)
                        logger.info(
                            f"--- Skipping Day: {day_str} (already completed) ---"
                        )
                        # Still run classification if requested (supports resume)
                        if args.classify_background:
                            _run_background_classification(
                                date_dir=day_dir,
                                device=args.bg_device,
                                batch_size=args.bg_batch_size,
                                force=args.bg_force,
                            )
                        curr_date += timedelta(days=1)
                        continue
                    else:
                        logger.info(
                            f"--- Day: {day_str} - Rewriting legacy metadata schema or resuming ---"
                        )

                logger.info(f"--- Processing Day: {day_start.date()} ---")

                collector.collect(
                    start_time=day_start,
                    end_time=day_end,
                    min_confidence=args.min_confidence,
                    continent=args.continent,
                    country=args.country,
                    species_ids=day_species_ids,
                    download_audio=not args.no_audio,
                    max_detections=args.max_detections,
                    workers=args.workers,
                    target=args.target,
                    enrich_weather=not args.no_enrich_weather,
                )

                # Post-download: background sound classification
                if args.classify_background:
                    _run_background_classification(
                        date_dir=Path(args.output_dir) / day_str,
                        device=args.bg_device,
                        batch_size=args.bg_batch_size,
                        force=args.bg_force,
                    )

                curr_date += timedelta(days=1)
            return

        # Default mode: Last N hours (or single run)
        collector.collect(
            hours=args.hours,
            min_confidence=args.min_confidence,
            continent=args.continent,
            country=args.country,
            species_ids=requested_species_ids,
            download_audio=not args.no_audio,
            max_detections=args.max_detections,
            workers=args.workers,
            target=args.target,
            enrich_weather=not args.no_enrich_weather,
        )

        # Post-download: background sound classification
        if args.classify_background:
            # Determine the date directory that was just written.
            # Use the most-recently-modified date dir so we classify the
            # actual output, not just today's date (which may differ when
            # collecting historical data or across midnight).
            bw_root = Path(args.output_dir)
            date_dirs = sorted(
                (
                    d
                    for d in bw_root.iterdir()
                    if d.is_dir() and (d / "detections.jsonl").exists()
                ),
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
            if date_dirs:
                _run_background_classification(
                    date_dir=date_dirs[0],
                    device=args.bg_device,
                    batch_size=args.bg_batch_size,
                    force=args.bg_force,
                )
            else:
                logger.warning(
                    "No date directories with detections.jsonl found - "
                    "skipping background classification"
                )

        return

    # LEGACY MODE: Use DataIngestor (requires stations endpoint)
    logger.info("Using LEGACY mode with DataIngestor")
    logger.warning("Note: This may fail if stations endpoint returns 500 errors")

    ingestor = DataIngestor(Path(args.output_dir))

    download_audio = args.download_all_media
    download_species_images = args.download_all_media
    download_station_media = args.download_all_media

    ingestor.run(
        days=args.days,
        download_audio=download_audio,
        download_species_images=download_species_images,
        download_station_media=download_station_media,
        ingest_species=args.ingest_species,
        ingest_birdnet=args.ingest_birdnet,
        ingest_aggregates=args.ingest_aggregates,
        skip_stations=args.skip_stations,
    )


if __name__ == "__main__":
    main()
