"""BirdWeather station extraction and profiling.

Scans BirdWeather ``detections.jsonl`` files, extracts unique station
metadata, and computes per-station trustworthiness / activity metrics
so downstream consumers (e.g. NASA AppEEARS, analytics dashboards) can
assess data coverage and sensor reliability.

Output schema (JSON array, one object per station)::

    {
        "id": "7935",
        "name": "PUC-7935-RdG",
        "type": "puc",
        "country": "Netherlands",
        "state": "Noord-Brabant",
        "coords": {"lat": 51.50494, "lon": 5.639},
        "stats": {
            "total_detections": 1234,
            "unique_species": 42,
            "unique_days": 14,
            "first_detection": "2026-01-01",
            "last_detection": "2026-01-14",
            "avg_confidence": 0.72,
            "monthly_detections": {"2026-01": 1234}
        }
    }

Python API
----------
::

    from avian_biosurveillance.data.stations import generate_stations_file

    path = generate_stations_file(Path("data/raw/birdweather"))

CLI
---
The module can also be invoked directly from the command line::

    # Generate stations.json in the default location (data/raw/birdweather/)
    poetry run python -m avian_biosurveillance.data.stations

    # Use a custom input directory
    poetry run python -m avian_biosurveillance.data.stations --input path/to/birdweather

    # Write output to a specific file
    poetry run python -m avian_biosurveillance.data.stations --output results/stations.json

    # Print JSON to stdout (useful for piping into other tools)
    poetry run python -m avian_biosurveillance.data.stations --print

    # Enable verbose (DEBUG-level) logging
    poetry run python -m avian_biosurveillance.data.stations -v

Run ``poetry run python -m avian_biosurveillance.data.stations --help`` for the
full list of options.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class StationStats:
    """Per-station activity and trustworthiness metrics.

    Attributes:
        total_detections: Total number of detection records referencing
            this station across all ingested data.
        unique_species: Count of distinct species IDs detected.
        unique_days: Number of calendar days with at least one detection.
        first_detection: ISO-format date of the earliest detection.
        last_detection: ISO-format date of the most recent detection.
        avg_confidence: Mean detection confidence score (0–1).
        monthly_detections: ``"YYYY-MM"`` → detection count mapping,
            useful for tracking seasonal data availability.
        top_species: Top 5 species by detection count, each a dict with
            ``name``, ``count``, and ``thumbnail_url``.
        high_confidence_detections: Count of detections with
            confidence >= 0.85.
        detections_per_day: Average detections per active day.
        coverage_span_days: Calendar days between first and last
            detection (inclusive).
        lifetime_availability_pct: Percent of active days across the
            station's observed lifespan from first to last detection.
        availability_90d_pct: Percent of active days in the trailing
            90-day window.
    """

    total_detections: int = 0
    unique_species: int = 0
    unique_days: int = 0
    first_detection: str | None = None
    last_detection: str | None = None
    avg_confidence: float | None = None
    monthly_detections: dict[str, int] = field(default_factory=dict)
    top_species: list[dict[str, Any]] = field(default_factory=list)
    high_confidence_detections: int = 0
    detections_per_day: float | None = None
    coverage_span_days: int | None = None
    lifetime_availability_pct: float | None = None
    availability_90d_pct: float | None = None


@dataclass
class StationProfile:
    """Enriched BirdWeather station profile with location and activity stats.

    Attributes:
        id: BirdWeather station identifier.
        name: Human-readable station name.
        type: Device type (e.g. ``"puc"``, ``"birdnetpi"``).
        country: Country where the station is located.
        state: Province / state / region name.
        coords: ``{"lat": float, "lon": float}`` WGS-84 position.
        stats: Aggregated :class:`StationStats`.
        in_database: ``True`` when the station's detections have been
            imported into the TimescaleDB instance.  Set by
            :func:`tag_database_availability`; defaults to ``False``.
    """

    id: str
    name: str
    type: str | None = None
    country: str | None = None
    state: str | None = None
    coords: dict[str, float] = field(default_factory=dict)
    stats: StationStats = field(default_factory=StationStats)
    in_database: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return asdict(self)


# ============================================================================
# Internal Accumulator
# ============================================================================


class _StationAccumulator:
    """Accumulates raw counters while scanning detection files.

    This is an internal helper used by :func:`extract_station_profiles`;
    it is **not** part of the public API.
    """

    __slots__ = (
        "meta",
        "detection_count",
        "species_ids",
        "days",
        "monthly_counts",
        "confidence_sum",
        "confidence_n",
        "species_counts",
        "species_thumbnails",
        "high_conf_count",
    )

    def __init__(self, meta: dict[str, Any]) -> None:
        self.meta = meta
        self.detection_count: int = 0
        self.species_ids: set[str] = set()
        self.days: set[str] = set()
        self.monthly_counts: dict[str, int] = defaultdict(int)
        self.confidence_sum: float = 0.0
        self.confidence_n: int = 0
        self.species_counts: dict[str, int] = defaultdict(int)
        self.species_thumbnails: dict[str, str] = {}
        self.high_conf_count: int = 0

    def record(self, record: dict[str, Any]) -> None:
        """Incorporate one detection record into the accumulators."""
        self.detection_count += 1

        # Species
        species = record.get("species", {})
        sid = species.get("id") or record.get("speciesId")
        if sid:
            self.species_ids.add(str(sid))
        common_name = species.get("commonName", "")
        if common_name:
            self.species_counts[common_name] += 1
            thumb = species.get("thumbnailUrl")
            if thumb and common_name not in self.species_thumbnails:
                self.species_thumbnails[common_name] = thumb

        # Temporal
        ts_str = record.get("timestamp", "")
        if ts_str:
            day = ts_str[:10]  # "YYYY-MM-DD" prefix
            self.days.add(day)
            month = ts_str[:7]  # "YYYY-MM" prefix
            self.monthly_counts[month] += 1

        # Confidence
        conf = record.get("confidence")
        if conf is not None:
            try:
                conf_f = float(conf)
                self.confidence_sum += conf_f
                self.confidence_n += 1
                if conf_f >= 0.85:
                    self.high_conf_count += 1
            except (TypeError, ValueError):
                pass

    def build_profile(self) -> StationProfile:
        """Compile the accumulated data into a :class:`StationProfile`."""
        sorted_days = sorted(self.days) if self.days else []
        avg_conf = (
            round(self.confidence_sum / self.confidence_n, 4)
            if self.confidence_n > 0
            else None
        )

        # Top 5 species by count
        top = sorted(
            self.species_counts.items(), key=lambda x: x[1], reverse=True
        )[:5]
        top_species = [
            {
                "name": name,
                "count": count,
                "thumbnail_url": self.species_thumbnails.get(name),
            }
            for name, count in top
        ]

        # Coverage span
        coverage_span = None
        if len(sorted_days) >= 2:
            d0 = date.fromisoformat(sorted_days[0])
            d1 = date.fromisoformat(sorted_days[-1])
            coverage_span = (d1 - d0).days + 1
        elif len(sorted_days) == 1:
            coverage_span = 1

        # Detection rate
        dpd = (
            round(self.detection_count / len(sorted_days), 1)
            if sorted_days
            else None
        )

        return StationProfile(
            id=str(self.meta.get("id", "")),
            name=self.meta.get("name", str(self.meta.get("id", ""))),
            type=self.meta.get("type"),
            country=self.meta.get("country"),
            state=self.meta.get("state"),
            coords=self.meta.get("coords", {}),
            stats=StationStats(
                total_detections=self.detection_count,
                unique_species=len(self.species_ids),
                unique_days=len(sorted_days),
                first_detection=sorted_days[0] if sorted_days else None,
                last_detection=sorted_days[-1] if sorted_days else None,
                avg_confidence=avg_conf,
                monthly_detections=dict(
                    sorted(self.monthly_counts.items())
                ),
                top_species=top_species,
                high_confidence_detections=self.high_conf_count,
                detections_per_day=dpd,
                coverage_span_days=coverage_span,
            ),
        )


# ============================================================================
# Public API
# ============================================================================


def extract_station_profiles(
    birdweather_dir: Path,
) -> list[StationProfile]:
    """Scan BirdWeather detection files and build enriched station profiles.

    For every unique station found in the ``detections.jsonl`` files the
    function computes:

    * **total_detections** — how many records reference this station.
    * **unique_species** — distinct species IDs detected.
    * **unique_days** — calendar days with ≥ 1 detection.
    * **first_detection / last_detection** — temporal coverage bounds.
    * **avg_confidence** — mean of the ``confidence`` field.
    * **monthly_detections** — ``"YYYY-MM"`` → count breakdown.

    Args:
        birdweather_dir: Root directory containing date-organized
            sub-folders with ``detections.jsonl`` files.

    Returns:
        List of :class:`StationProfile` sorted by station ``id``.

    Raises:
        FileNotFoundError: If *birdweather_dir* does not exist.
        ValueError: If no stations with valid coordinates are found.
    """
    if not birdweather_dir.exists():
        msg = f"BirdWeather directory not found: {birdweather_dir}"
        raise FileNotFoundError(msg)

    accumulators: dict[str, _StationAccumulator] = {}

    for jsonl_path in sorted(birdweather_dir.rglob("detections.jsonl")):
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                station = record.get("station", {})
                sid = station.get("id")
                if not sid:
                    continue

                # Ensure coordinates are available
                sc = station.get("coords", {})
                if sc.get("lat") is None or sc.get("lon") is None:
                    continue

                if sid not in accumulators:
                    accumulators[sid] = _StationAccumulator(meta=station)

                accumulators[sid].record(record)

    if not accumulators:
        msg = (
            f"No stations with coordinates found in "
            f"{birdweather_dir}. Ensure detections.jsonl files "
            "exist and contain station data."
        )
        raise ValueError(msg)

    profiles = [acc.build_profile() for acc in accumulators.values()]
    profiles.sort(key=lambda p: p.id)

    logger.info(
        "Extracted %d station profiles from %s.",
        len(profiles),
        birdweather_dir,
    )
    return profiles


def tag_database_availability(
    profiles: list[StationProfile],
    db_url: str,
) -> int:
    """Mark stations present in the database with ``in_database=True``.

    Connects to the TimescaleDB instance, fetches the set of distinct
    ``station_name`` values, and flips each matching profile's flag.

    Args:
        profiles: Station profiles to tag (modified in-place).
        db_url: PostgreSQL connection string
            (e.g. ``"host=localhost port=5432 dbname=... user=... password=..."``).

    Returns:
        Number of profiles that matched a DB station name.
    """
    try:
        import psycopg2  # noqa: PLC0415 — optional dependency
    except ImportError as exc:
        logger.warning(
            "psycopg2 not installed — skipping DB availability tagging. "
            "Install it with: pip install psycopg2-binary"
        )
        raise SystemExit(1) from exc

    conn = psycopg2.connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT station_name FROM birdweather_stations_tbl")
        db_names: set[str] = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    matched = 0
    for p in profiles:
        if p.name in db_names:
            p.in_database = True
            matched += 1

    logger.info(
        "DB availability: %d/%d stations matched (%d not in DB).",
        matched,
        len(profiles),
        len(profiles) - matched,
    )
    return matched


def detect_date_range(
    source: list[StationProfile] | list[dict[str, Any]] | Path,
    *,
    buffer_days: int = 15,
) -> tuple[str, str]:
    """Detect the active date range from station data.

    Scans ``first_detection`` / ``last_detection`` across all stations
    and returns a ``(start_date, end_date)`` tuple in **MM-DD-YYYY**
    format (the convention used by the AppEEARS API) with a
    configurable buffer on both sides.

    The buffer ensures satellite imagery is available slightly before
    the first bird detection and slightly after the last, capturing
    environmental context around the monitoring window.

    Args:
        source: Station data in one of three forms:

            * A list of :class:`StationProfile` objects.
            * A list of raw dicts (as loaded from ``stations.json``).
            * A :class:`~pathlib.Path` to a ``stations.json`` file.

        buffer_days: Days to pad before the earliest and after the
            latest detection (default: 15).

    Returns:
        ``(start_date, end_date)`` in ``MM-DD-YYYY`` format.

    Raises:
        ValueError: If *source* is empty or no valid detection dates
            are found.
        FileNotFoundError: If *source* is a ``Path`` that does not
            exist.

    Example::

        start, end = detect_date_range(Path("data/raw/birdweather/stations.json"))
        # e.g. ("12-17-2025", "01-29-2026") for 15-day buffer around Jan 1-14
    """
    # --- resolve source to a flat list of dicts ---
    if isinstance(source, Path):
        if not source.exists():
            msg = f"Stations file not found: {source}"
            raise FileNotFoundError(msg)
        with open(source, encoding="utf-8") as fh:
            raw: list[dict[str, Any]] = json.load(fh)
        stations = raw
    elif source and isinstance(source[0], StationProfile):
        stations = [p.to_dict() for p in source]  # type: ignore[union-attr]
    else:
        stations = list(source)  # type: ignore[arg-type]

    if not stations:
        msg = "Cannot detect date range: no station data provided."
        raise ValueError(msg)

    # --- collect all first/last detection dates ---
    all_dates: list[date] = []
    for station in stations:
        stats = station.get("stats", {})
        for key in ("first_detection", "last_detection"):
            value = stats.get(key)
            if value:
                try:
                    all_dates.append(date.fromisoformat(value))
                except (TypeError, ValueError):
                    logger.debug(
                        "Skipping unparseable date '%s' for station %s.",
                        value,
                        station.get("id", "?"),
                    )

    if not all_dates:
        msg = (
            "No valid detection dates found across "
            f"{len(stations)} station(s). Ensure station stats "
            "contain first_detection / last_detection fields."
        )
        raise ValueError(msg)

    earliest = min(all_dates) - timedelta(days=buffer_days)
    latest = max(all_dates) + timedelta(days=buffer_days)

    start_mm_dd_yyyy = earliest.strftime("%m-%d-%Y")
    end_mm_dd_yyyy = latest.strftime("%m-%d-%Y")

    logger.info(
        "Detected date range: %s → %s "
        "(buffer=%d days, from %d station date(s)).",
        start_mm_dd_yyyy,
        end_mm_dd_yyyy,
        buffer_days,
        len(all_dates),
    )
    return start_mm_dd_yyyy, end_mm_dd_yyyy


def generate_stations_file(
    birdweather_dir: Path,
    output_path: Path | None = None,
) -> Path:
    """Generate a stations JSON file from BirdWeather detection data.

    Scans all ``detections.jsonl`` files under *birdweather_dir*,
    extracts unique station metadata (id, name, type, country, state,
    coords) together with trustworthiness / activity statistics, and
    writes a consolidated JSON array to disk.

    This pre-processing step should run **before** submitting an
    AppEEARS task so the pipeline has a clean stations file to read.

    Args:
        birdweather_dir: Root directory with BirdWeather data
            (e.g. ``data/raw/birdweather``).
        output_path: Where to write the stations JSON.  Defaults to
            ``<birdweather_dir>/stations.json``.

    Returns:
        Path to the written stations JSON file.

    Raises:
        FileNotFoundError: If *birdweather_dir* does not exist.
        ValueError: If no stations could be extracted.
    """
    profiles = extract_station_profiles(birdweather_dir)

    stations = [p.to_dict() for p in profiles]

    dest = output_path or (birdweather_dir / "stations.json")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(stations, fh, indent=2)

    logger.info(
        "Generated stations file: %d stations → %s",
        len(stations),
        dest,
    )
    return dest


# ============================================================================
# CLI
# ============================================================================


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for station extraction and profiling.

    Parses command-line arguments and generates a consolidated
    ``stations.json`` from BirdWeather detection data.
    """
    parser = argparse.ArgumentParser(
        description="Extract and profile BirdWeather stations from detection data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Generate stations.json in the default location
  python -m avian_biosurveillance.data.stations

  # Specify a custom BirdWeather data directory
  python -m avian_biosurveillance.data.stations --input data/raw/birdweather

  # Write the output to a custom path
  python -m avian_biosurveillance.data.stations --output results/stations.json

  # Print extracted profiles to stdout instead of writing a file
  python -m avian_biosurveillance.data.stations --print
""",
    )

    parser.add_argument(
        "--input",
        type=str,
        default="data/raw/birdweather",
        help="Root directory containing BirdWeather date-organized "
        "sub-folders with detections.jsonl files "
        "(default: data/raw/birdweather)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path for the output stations JSON file "
        "(default: <input>/stations.json)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_stdout",
        help="Print station profiles to stdout as JSON instead of "
        "writing to a file",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose (DEBUG-level) logging",
    )
    parser.add_argument(
        "--db-check",
        type=str,
        default=None,
        metavar="DSN",
        help="PostgreSQL connection string to tag stations present in "
        "the database (e.g. 'host=localhost port=5432 "
        "dbname=avian_biosurveillance user=main password=...')",
    )

    args = parser.parse_args(argv)

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    birdweather_dir = Path(args.input)

    try:
        if args.print_stdout:
            profiles = extract_station_profiles(birdweather_dir)
            if args.db_check:
                tag_database_availability(profiles, args.db_check)
            json.dump(
                [p.to_dict() for p in profiles],
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
        else:
            profiles = extract_station_profiles(birdweather_dir)
            if args.db_check:
                tag_database_availability(profiles, args.db_check)
            stations = [p.to_dict() for p in profiles]
            output_path = Path(args.output) if args.output else None
            dest = output_path or (birdweather_dir / "stations.json")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(stations, fh, indent=2)
            logger.info(
                "Generated stations file: %d stations → %s",
                len(stations),
                dest,
            )
            print(f"Stations file written to {dest}")  # noqa: T201
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
