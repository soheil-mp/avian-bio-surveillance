#!/usr/bin/env python3
"""One-time backfill of existing local data into TimescaleDB.

Scans the ``data/`` directory tree for locally-ingested raw files and
upserts them into the running TimescaleDB instance.

Supported sources: BirdWeather, KNMI, eBird, Movebank, Earth Engine, Sovon.

Usage::

    # From repo root (with containers running)
    python scripts/ops/seed_db.py

    # Seed only specific sources
    python scripts/ops/seed_db.py --birdweather
    python scripts/ops/seed_db.py --knmi
    python scripts/ops/seed_db.py --ebird
    python scripts/ops/seed_db.py --movebank
    python scripts/ops/seed_db.py --earth-engine
    python scripts/ops/seed_db.py --sovon

    # Dry-run: count records without writing
    python scripts/ops/seed_db.py --dry-run
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path
from string import ascii_lowercase

import psycopg2


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.loaders.birdweather import (
    upsert_birdweather_detections,
    upsert_birdweather_species,
    upsert_birdweather_stations,
)
from avian_biosurveillance.db.loaders.earth_engine import upsert_earth_engine_profiles
from avian_biosurveillance.db.loaders.ebird import upsert_ebird_observations
from avian_biosurveillance.db.loaders.knmi import upsert_knmi_observations
from avian_biosurveillance.db.loaders.movebank import upsert_movebank_events
from avian_biosurveillance.db.loaders.sovon import upsert_sovon_mortality
from avian_biosurveillance.db.models.birdweather import BirdWeatherDetection
from avian_biosurveillance.db.models.earth_engine import EarthEngineProfile
from avian_biosurveillance.db.models.ebird import EBirdObservation
from avian_biosurveillance.db.models.knmi import KNMIWeatherObservation
from avian_biosurveillance.db.models.movebank import MovebankEvent
from avian_biosurveillance.ingestion.birdweather import BirdWeatherClient


load_runtime_env()

logger = logging.getLogger(__name__)

_BW_DIR = _REPO_ROOT / "data" / "raw" / "birdweather"
_KNMI_DIR = _REPO_ROOT / "data" / "raw" / "knmi"
_EBIRD_DIR = _REPO_ROOT / "data" / "raw" / "ebird"
_MOVEBANK_DIR = _REPO_ROOT / "data" / "raw" / "movebank"
_EARTH_ENGINE_DIR = _REPO_ROOT / "data" / "raw" / "earth_engine"
_SOVON_DIR = _REPO_ROOT / "data" / "processed" / "sovon"

_NL_LAT_MIN, _NL_LAT_MAX = 50.75, 53.70
_NL_LON_MIN, _NL_LON_MAX = 3.33, 7.22
_BIRDWEATHER_SPECIES_MIN_EMPTY_QUERY = 5_000


def _in_nl_bbox(raw: dict[str, object]) -> bool:
    station = raw.get("station")
    if not isinstance(station, dict):
        return True

    coords = station.get("coords")
    if not isinstance(coords, dict):
        return True

    lat = coords.get("lat")
    lon = coords.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return True

    return _NL_LAT_MIN <= lat <= _NL_LAT_MAX and _NL_LON_MIN <= lon <= _NL_LON_MAX


def _birdweather_species_record(node: dict[str, object]) -> dict[str, object] | None:
    species_id = str(node.get("id") or "").strip()
    if not species_id:
        return None

    common_name = str(
        node.get("commonName") or node.get("scientificName") or species_id
    ).strip()
    if not common_name:
        return None

    return {
        "species_id": species_id,
        "common_name": common_name,
        "scientific_name": node.get("scientificName"),
        "alpha_code": node.get("alpha"),
        "alpha6_code": node.get("alpha6"),
        "ebird_code": node.get("ebirdCode"),
        "species_color": node.get("color"),
        "species_image_url": node.get("imageUrl"),
        "species_thumbnail_url": node.get("thumbnailUrl"),
        "birdweather_url": node.get("birdweatherUrl"),
        "ebird_url": node.get("ebirdUrl"),
        "ml_url": node.get("mlUrl"),
        "wikipedia_url": node.get("wikipediaUrl"),
        "wikipedia_summary": node.get("wikipediaSummary"),
        "prediction_area": node.get("predictionArea"),
        "range_map": node.get("range"),
    }


def _birdweather_station_record(
    node: dict[str, object],
    *,
    synced_at: datetime,
) -> dict[str, object] | None:
    station_id = str(node.get("id") or "").strip()
    if not station_id:
        return None

    coords = node.get("coords")
    if not isinstance(coords, dict):
        coords = {}

    return {
        "station_id": station_id,
        "station_name": node.get("name"),
        "station_type": node.get("type"),
        "station_country": node.get("country"),
        "station_state": node.get("state"),
        "station_location": node.get("location"),
        "station_timezone": node.get("timezone"),
        "station_continent": node.get("continent"),
        "station_audio_url": node.get("audioUrl"),
        "station_video_url": node.get("videoUrl"),
        "metadata_source": "birdweather_api",
        "is_public": True,
        "metadata_synced_at": synced_at,
        "lat": coords.get("lat"),
        "lon": coords.get("lon"),
    }


def _collect_birdweather_species(client: BirdWeatherClient) -> list[dict[str, object]]:
    species_by_id: dict[str, dict[str, object]] = {}

    def collect_for_query(query: str) -> int:
        added = 0
        for node in client.fetch_all_species(query=query):
            record = _birdweather_species_record(node)
            if record is None:
                continue
            species_id = str(record["species_id"])
            if species_id in species_by_id:
                continue
            species_by_id[species_id] = record
            added += 1
        return added

    empty_added = collect_for_query("")
    logger.info("BirdWeather species: empty query yielded %d unique rows", empty_added)

    if len(species_by_id) < _BIRDWEATHER_SPECIES_MIN_EMPTY_QUERY:
        logger.warning(
            "BirdWeather species empty query returned only %d unique rows; expanding with alphabet shards",
            len(species_by_id),
        )
        for shard in ascii_lowercase:
            added = collect_for_query(shard)
            logger.debug(
                "BirdWeather species shard %r added %d unique rows", shard, added
            )

    return list(species_by_id.values())


def _collect_birdweather_stations(client: BirdWeatherClient) -> list[dict[str, object]]:
    synced_at = datetime.now(UTC)
    records: list[dict[str, object]] = []

    for node in client.fetch_all_stations():
        record = _birdweather_station_record(node, synced_at=synced_at)
        if record is not None:
            records.append(record)

    logger.info("BirdWeather stations: fetched %d public stations", len(records))
    return records


def _close_birdweather_client(client: BirdWeatherClient) -> None:
    client.session.close()
    client.download_session.close()


def _seed_birdweather_reference(dry_run: bool = False) -> int:
    client = BirdWeatherClient()
    try:
        species_records = _collect_birdweather_species(client)
        station_records = _collect_birdweather_stations(client)
    except (RuntimeError, OSError) as exc:
        logger.warning("BirdWeather reference seeding failed", exc_info=exc)
        print(f"  WARNING: BirdWeather reference seeding failed: {exc}")
        return 0
    finally:
        _close_birdweather_client(client)

    if dry_run:
        print(f"  species: {len(species_records):,} rows (dry-run)")
        print(f"  stations: {len(station_records):,} rows (dry-run)")
    else:
        species_upserted = upsert_birdweather_species(species_records)
        station_upserted = upsert_birdweather_stations(station_records)
        print(
            f"  species: {len(species_records):,} rows → upserted {species_upserted:,}"
        )
        print(
            f"  stations: {len(station_records):,} rows → upserted {station_upserted:,}"
        )

    return len(species_records) + len(station_records)


def _refresh_birdweather_tables(
    *, refresh_species: bool, refresh_stations: bool
) -> None:
    if not (refresh_species or refresh_stations):
        return

    pool = None
    try:
        from avian_biosurveillance.db.database import ConnectionPool

        pool = ConnectionPool(min_conn=1, max_conn=2)
        with pool.cursor() as cur:
            if refresh_species:
                print("\n▸ Refreshing species reference …")
                cur.execute("SELECT refresh_species_dim()")
                print("  species reference refreshed.")
            if refresh_stations:
                print("\n▸ Refreshing station_summary …")
                cur.execute("SELECT refresh_station_summary()")
                print("  station_summary refreshed.")
    except (psycopg2.Error, KeyError, OSError) as exc:
        if refresh_species:
            print(f"  WARNING: species reference refresh failed: {exc}")
        if refresh_stations:
            print(f"  WARNING: station_summary refresh failed: {exc}")
    finally:
        if pool is not None:
            pool.close()


def _seed_birdweather(dry_run: bool = False) -> int:
    jsonl_files = sorted(_BW_DIR.rglob("detections.jsonl"))
    if not jsonl_files:
        print(f"  No detections.jsonl files found under {_BW_DIR}")
        return 0

    total = 0
    for path in jsonl_files:
        records: list[BirdWeatherDetection] = []
        skipped = 0
        with open(path, encoding="utf-8") as file_handle:
            for line in file_handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if not _in_nl_bbox(raw):
                    skipped += 1
                    continue
                records.append(BirdWeatherDetection.from_jsonl(raw))

        date_dir = path.parent.name
        message = f"  {date_dir}: {len(records):,} detections"
        if skipped:
            message += f" ({skipped:,} outside NL skipped)"

        if dry_run:
            print(message + " (dry-run)")
        else:
            upserted = upsert_birdweather_detections(records)
            print(message + f" → upserted {upserted:,}")

        total += len(records)

    return total


def _seed_sovon(dry_run: bool = False) -> int:
    csv_files = sorted(_SOVON_DIR.glob("*.csv"))
    if not csv_files:
        print(f"  No CSV files found under {_SOVON_DIR}")
        return 0

    total = 0
    for path in csv_files:
        records: list[dict[str, object]] = []
        with open(path, encoding="utf-8", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            for row in reader:
                records.append(
                    {
                        "species_english": row.get("species_english", ""),
                        "species_dutch": row.get("species_dutch"),
                        "sovon_species_id": None,
                        "year": int(row["year"]),
                        "month": int(row["month"]),
                        "dead_bird_count": int(row.get("count", 0)),
                        "n_total": None,
                        "source_image": None,
                    }
                )

        print(f"  {path.name}: {len(records):,} rows", end="")

        if dry_run:
            print(" (dry-run)")
        else:
            upserted = upsert_sovon_mortality(records)
            print(f" → upserted {upserted:,}")

        total += len(records)

    return total


def _seed_knmi(dry_run: bool = False) -> int:
    if not _KNMI_DIR.exists():
        print(f"  No KNMI directory found at {_KNMI_DIR}")
        return 0

    date_dirs = sorted(
        directory
        for directory in _KNMI_DIR.iterdir()
        if directory.is_dir() and (directory / "stations").is_dir()
    )
    if not date_dirs:
        print(f"  No date directories with stations/ found under {_KNMI_DIR}")
        return 0

    total = 0
    for date_dir in date_dirs:
        station_files = sorted((date_dir / "stations").glob("*.json"))
        records: list[KNMIWeatherObservation] = []

        for station_file in station_files:
            station_id = (
                station_file.stem.split("-")[-1]
                if "-" in station_file.stem
                else station_file.stem
            )
            with open(station_file, encoding="utf-8") as file_handle:
                coverage = json.load(file_handle)
            observations = KNMIWeatherObservation.from_edr_coverage(
                coverage,
                location_id=station_id,
            )
            records.extend(observations)

        print(
            f"  {date_dir.name}: {len(records):,} observations from {len(station_files)} stations",
            end="",
        )

        if dry_run:
            print(" (dry-run)")
        else:
            upserted = upsert_knmi_observations(records)
            print(f" → upserted {upserted:,}")

        total += len(records)

    return total


def _seed_ebird(dry_run: bool = False) -> int:
    if not _EBIRD_DIR.exists():
        print(f"  No eBird directory found at {_EBIRD_DIR}")
        return 0

    jsonl_files = sorted(_EBIRD_DIR.rglob("observations.jsonl"))
    if not jsonl_files:
        print(f"  No observations.jsonl files found under {_EBIRD_DIR}")
        return 0

    total = 0
    for path in jsonl_files:
        records: list[EBirdObservation] = []
        with open(path, encoding="utf-8") as file_handle:
            for line in file_handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                records.append(EBirdObservation.from_api(raw))

        label = "/".join(path.parts[-3:-1])
        print(f"  {label}: {len(records):,} observations", end="")

        if dry_run:
            print(" (dry-run)")
        else:
            upserted = upsert_ebird_observations(records)
            print(f" → upserted {upserted:,}")

        total += len(records)

    return total


def _seed_movebank(dry_run: bool = False) -> int:
    if not _MOVEBANK_DIR.exists():
        print(f"  No Movebank directory found at {_MOVEBANK_DIR}")
        return 0

    jsonl_files = sorted(_MOVEBANK_DIR.rglob("events.jsonl"))
    if not jsonl_files:
        print(f"  No events.jsonl files found under {_MOVEBANK_DIR}")
        return 0

    total = 0
    for path in jsonl_files:
        study_id_str = path.parts[-3]
        try:
            study_id = int(study_id_str)
        except ValueError:
            study_id = 0

        records: list[MovebankEvent] = []
        with open(path, encoding="utf-8") as file_handle:
            for line in file_handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                records.append(MovebankEvent.from_csv_row(raw, study_id=study_id))

        date_dir = path.parent.name
        print(f"  study {study_id_str}/{date_dir}: {len(records):,} events", end="")

        if dry_run:
            print(" (dry-run)")
        else:
            upserted = upsert_movebank_events(records)
            print(f" → upserted {upserted:,}")

        total += len(records)

    return total


def _seed_earth_engine(dry_run: bool = False) -> int:
    if not _EARTH_ENGINE_DIR.exists():
        print(f"  No Earth Engine directory found at {_EARTH_ENGINE_DIR}")
        return 0

    profile_files = sorted(_EARTH_ENGINE_DIR.rglob("profiles.json"))
    if not profile_files:
        print(f"  No profiles.json files found under {_EARTH_ENGINE_DIR}")
        return 0

    total = 0
    for path in profile_files:
        with open(path, encoding="utf-8") as file_handle:
            profiles = json.load(file_handle)

        if not isinstance(profiles, list):
            profiles = [profiles]

        records: list[EarthEngineProfile] = []
        for profile in profiles:
            station_id = str(profile.get("station_id", ""))
            date_str = profile.get("date", path.parent.name)
            try:
                obs_date = date_cls.fromisoformat(date_str)
            except (ValueError, TypeError):
                obs_date = date_cls.fromisoformat(path.parent.name)

            source = profile.get("source")
            records.append(
                EarthEngineProfile.from_profile_dict(
                    profile,
                    station_id=station_id,
                    observation_date=obs_date,
                    source=source,
                )
            )

        date_dir = path.parent.name
        print(f"  {date_dir}: {len(records):,} profiles", end="")

        if dry_run:
            print(" (dry-run)")
        else:
            upserted = upsert_earth_engine_profiles(records)
            print(f" → upserted {upserted:,}")

        total += len(records)

    return total


def main() -> int:
    """Seed the database from local data files."""
    parser = argparse.ArgumentParser(
        description="Backfill TimescaleDB from local data files and BirdWeather API metadata.",
    )
    parser.add_argument(
        "--birdweather",
        action="store_true",
        help="Seed BirdWeather reference metadata and detections",
    )
    parser.add_argument(
        "--birdweather-reference",
        action="store_true",
        help="Seed BirdWeather species/station metadata from the public API",
    )
    parser.add_argument(
        "--knmi", action="store_true", help="Seed KNMI weather observations"
    )
    parser.add_argument("--ebird", action="store_true", help="Seed eBird observations")
    parser.add_argument(
        "--movebank", action="store_true", help="Seed Movebank tracking events"
    )
    parser.add_argument(
        "--earth-engine",
        action="store_true",
        help="Seed Earth Engine environmental profiles",
    )
    parser.add_argument(
        "--sovon", action="store_true", help="Seed Sovon mortality data"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Count records without writing"
    )
    parser.add_argument(
        "--refresh-species",
        action="store_true",
        help="Run refresh_species_dim() after seeding",
    )
    parser.add_argument(
        "--refresh-stations",
        action="store_true",
        help="Run refresh_station_summary() after seeding",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    seed_all = not (
        args.birdweather
        or args.birdweather_reference
        or args.knmi
        or args.ebird
        or args.movebank
        or args.earth_engine
        or args.sovon
    )

    print("═" * 60)
    print("Seeding TimescaleDB from local data and BirdWeather metadata")
    print("═" * 60)
    if args.dry_run:
        print("(DRY RUN — no data will be written)\n")

    grand_total = 0

    if seed_all or args.birdweather or args.birdweather_reference:
        print("\n▸ BirdWeather reference metadata")
        count = _seed_birdweather_reference(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if seed_all or args.birdweather:
        print("\n▸ BirdWeather detections")
        count = _seed_birdweather(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if seed_all or args.knmi:
        print("\n▸ KNMI weather observations")
        count = _seed_knmi(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if seed_all or args.ebird:
        print("\n▸ eBird observations")
        count = _seed_ebird(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if seed_all or args.movebank:
        print("\n▸ Movebank tracking events")
        count = _seed_movebank(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if seed_all or args.earth_engine:
        print("\n▸ Earth Engine environmental profiles")
        count = _seed_earth_engine(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if seed_all or args.sovon:
        print("\n▸ Sovon mortality")
        count = _seed_sovon(dry_run=args.dry_run)
        grand_total += count
        print(f"  Subtotal: {count:,}\n")

    if not args.dry_run:
        _refresh_birdweather_tables(
            refresh_species=args.refresh_species or seed_all or args.birdweather,
            refresh_stations=args.refresh_stations or seed_all or args.birdweather,
        )

    print("═" * 60)
    print(
        f"Grand total: {grand_total:,} records {'(dry-run)' if args.dry_run else 'upserted'}"
    )
    print("═" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
