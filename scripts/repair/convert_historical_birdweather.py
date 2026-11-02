#!/usr/bin/env python3
"""Convert manually downloaded BirdWeather historical CSVs to the pipeline format.

Reads CSV exports from the BirdWeather website (data/raw/birdweather_historical_manual/)
and produces the same directory structure as the automated pipeline:

    data/raw/birdweather/{YYYY-MM-DD}/
        detections.jsonl
        species_list.json
        metadata.json
        audio/{soundscape_hash}.flac

The CSV soundscape URLs (hash-based) are not directly downloadable (S3 403).
Audio download uses the BirdWeather GraphQL API to resolve proper media URLs
by matching detections on timestamp, species, and station coordinates.

Usage:
    # Convert all CSVs and download audio
    poetry run python scripts/repair/convert_historical_birdweather.py

    # Convert without audio
    poetry run python scripts/repair/convert_historical_birdweather.py --no-audio

    # Only process specific years
    poetry run python scripts/repair/convert_historical_birdweather.py --years 2024 2025

    # Limit workers for downloads
    poetry run python scripts/repair/convert_historical_birdweather.py --workers 5

    # Custom input/output directories
    poetry run python scripts/repair/convert_historical_birdweather.py \
        --input-dir data/raw/birdweather_historical_manual \
        --output-dir data/raw/birdweather
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from tqdm import tqdm


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.ingestion.birdweather import BirdWeatherClient  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

SOUNDSCAPE_HASH_RE = re.compile(r"/soundscapes/normalize/([a-f0-9]+)$")


def parse_timestamp(ts_str: str) -> datetime | None:
    try:
        return datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S %z")
    except (ValueError, AttributeError):
        return None


def csv_row_to_detection(row: dict[str, str]) -> dict | None:
    ts = parse_timestamp(row.get("Timestamp", ""))
    if ts is None:
        return None

    score_raw = row.get("Score", "")
    conf_raw = row.get("Confidence", "")
    prob_raw = row.get("Probability", "")

    score = float(score_raw) if score_raw else None
    confidence = float(conf_raw) if conf_raw else None
    probability = float(prob_raw) if prob_raw else None

    lat_raw = row.get("Latitude", "")
    lon_raw = row.get("Longitude", "")
    lat = float(lat_raw) if lat_raw else None
    lon = float(lon_raw) if lon_raw else None

    soundscape_url = (row.get("Soundscape") or "").strip()
    soundscape_hash = None
    soundscape = None
    if soundscape_url:
        match = SOUNDSCAPE_HASH_RE.search(soundscape_url)
        soundscape_hash = match.group(1) if match else None
        start_time_raw = row.get("Soundscape start", "")
        end_time_raw = row.get("Soundscape end", "")
        soundscape = {
            "url": soundscape_url,
            "hash": soundscape_hash,
            "startTime": float(start_time_raw) if start_time_raw else None,
            "endTime": float(end_time_raw) if end_time_raw else None,
        }

    id_seed = f"{ts.isoformat()}_{lat}_{lon}_{score}"
    det_id = hashlib.md5(id_seed.encode()).hexdigest()[:16]  # noqa: S324

    upvotes_raw = row.get("Upvotes", "0")
    downvotes_raw = row.get("Downvotes", "0")

    return {
        "id": det_id,
        "timestamp": ts.isoformat(),
        "confidence": confidence,
        "score": score,
        "probability": probability,
        "species": {
            "commonName": row.get("Common Name", ""),
            "scientificName": row.get("Scientific Name", ""),
        },
        "coords": {"lat": lat, "lon": lon} if lat and lon else None,
        "soundscape": soundscape,
        "station": {"name": row.get("Station", "")},
        "votes": {
            "up": int(upvotes_raw) if upvotes_raw else 0,
            "down": int(downvotes_raw) if downvotes_raw else 0,
        },
        "_source": "historical_csv",
        "_collected_at": datetime.now().isoformat(),
    }


def check_archive_status(url: str, timeout: int = 10) -> bool:
    try:
        response = requests.head(url, timeout=timeout)
        archive_status = response.headers.get("x-amz-archive-status", "")
        return "ARCHIVE" in archive_status.upper()
    except requests.RequestException:
        return False


def download_audio_file(
    client: BirdWeatherClient, url: str, path: Path
) -> tuple[bool, str]:
    if path.exists() and path.stat().st_size > 0:
        return True, "already_exists"

    return client.download_file(url, path)


def resolve_audio_urls_for_day(
    client: BirdWeatherClient,
    day_str: str,
    csv_detections: list[dict],
    ne: dict | None = None,
    sw: dict | None = None,
) -> dict[str, dict]:
    csv_lookup: dict[tuple[str, str], list[str]] = defaultdict(list)
    for detection in csv_detections:
        ts_str = detection.get("timestamp", "")
        sci_name = detection.get("species", {}).get("scientificName", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            key = (ts.strftime("%Y-%m-%dT%H:%M"), sci_name)
            csv_lookup[key].append(detection["id"])
        except ValueError:
            continue

    if not csv_lookup:
        return {}

    day_start = datetime.strptime(day_str, "%Y-%m-%d")
    day_end = day_start.replace(hour=23, minute=59, second=59)

    resolved: dict[str, dict] = {}
    try:
        kwargs: dict = {
            "start": day_start,
            "end": day_end,
            "valid_soundscape": True,
        }
        if ne and sw:
            kwargs["ne"] = ne
            kwargs["sw"] = sw

        for api_det in client.fetch_detections(**kwargs):
            api_ts = api_det.get("timestamp", "")
            soundscape = api_det.get("soundscape")
            api_species = api_det.get("species", {}).get("scientificName", "")
            if not api_ts or not soundscape or not soundscape.get("url"):
                continue

            try:
                ts = datetime.fromisoformat(api_ts)
                key = (ts.strftime("%Y-%m-%dT%H:%M"), api_species)
            except ValueError:
                continue

            if key in csv_lookup:
                download_url = soundscape.get("url", "")
                dl_filename = soundscape.get("downloadFilename", "")
                if not download_url and dl_filename:
                    parts = dl_filename.split("-", 1)
                    if len(parts) == 2:
                        station_id = parts[0]
                        download_url = (
                            f"https://media.birdweather.com/soundscapes/"
                            f"{station_id}/{dl_filename}"
                        )

                if not download_url:
                    continue

                for det_id in csv_lookup[key]:
                    if det_id not in resolved:
                        resolved[det_id] = {
                            "url": download_url,
                            "soundscape_id": soundscape.get("id"),
                            "filename": dl_filename,
                            "filesize": soundscape.get("filesize"),
                        }

            if len(resolved) >= sum(len(value) for value in csv_lookup.values()):
                break

    except Exception as exc:
        logger.warning("  API query failed for %s: %s", day_str, exc)

    return resolved


def process_csv_file(
    csv_path: Path,
    output_dir: Path,
    download_audio_flag: bool = True,
    workers: int = 10,
) -> dict[str, int]:
    logger.info("Processing: %s", csv_path.name)

    days: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    total = 0

    with open(csv_path, encoding="utf-8") as file_handle:
        reader = csv.DictReader(file_handle, skipinitialspace=True)
        for row in reader:
            total += 1
            row = {
                key.strip(): (value.strip() if value else "")
                for key, value in row.items()
                if key
            }
            detection = csv_row_to_detection(row)
            if detection is None:
                skipped += 1
                continue

            ts = parse_timestamp(row["Timestamp"])
            day_str = ts.strftime("%Y-%m-%d")
            days[day_str].append(detection)

    logger.info(
        "  Parsed %s rows -> %s detections across %s days (%s skipped)",
        total,
        total - skipped,
        len(days),
        skipped,
    )

    stats = {
        "total_rows": total,
        "detections": total - skipped,
        "skipped": skipped,
        "days": len(days),
        "audio_downloaded": 0,
        "audio_cached": 0,
        "audio_failed": 0,
        "audio_not_available": 0,
        "audio_unresolved": 0,
        "audio_archived": 0,
    }

    all_lats, all_lons = [], []
    for day_detections in days.values():
        for detection in day_detections:
            coords = detection.get("coords")
            if coords and coords.get("lat") and coords.get("lon"):
                all_lats.append(coords["lat"])
                all_lons.append(coords["lon"])

    ne, sw = None, None
    if all_lats and all_lons:
        margin = 0.5
        ne = {"lat": max(all_lats) + margin, "lon": max(all_lons) + margin}
        sw = {"lat": min(all_lats) - margin, "lon": min(all_lons) - margin}

    client = BirdWeatherClient() if download_audio_flag else None

    for day_str in sorted(days.keys()):
        day_detections = days[day_str]
        day_dir = output_dir / day_str
        day_dir.mkdir(parents=True, exist_ok=True)

        detections_file = day_dir / "detections.jsonl"
        existing_ids: set[str] = set()
        if detections_file.exists():
            with open(detections_file, encoding="utf-8") as file_handle:
                for line in file_handle:
                    try:
                        existing_ids.add(json.loads(line).get("id", ""))
                    except json.JSONDecodeError:
                        continue

        new_detections = [
            detection
            for detection in day_detections
            if detection["id"] not in existing_ids
        ]

        if new_detections:
            with open(detections_file, "a", encoding="utf-8") as file_handle:
                for detection in new_detections:
                    file_handle.write(json.dumps(detection) + "\n")

        species_file = day_dir / "species_list.json"
        species_seen: dict[str, str] = {}
        if species_file.exists():
            with open(species_file, encoding="utf-8") as file_handle:
                for species in json.load(file_handle):
                    species_seen[
                        str(species.get("id", species.get("commonName", "")))
                    ] = species.get("commonName", "")

        for detection in new_detections:
            species = detection.get("species", {})
            name = species.get("commonName", "")
            if name:
                species_seen[name] = name

        species_data = [
            {"id": key, "commonName": value} for key, value in species_seen.items()
        ]
        with open(species_file, "w", encoding="utf-8") as file_handle:
            json.dump(species_data, file_handle, indent=2)

        metadata_file = day_dir / "metadata.json"
        existing_meta = {}
        if metadata_file.exists():
            with open(metadata_file, encoding="utf-8") as file_handle:
                existing_meta = json.load(file_handle)

        historical_count = len(new_detections)
        prev_historical = existing_meta.get("historical_detections_added", 0)

        metadata = {
            **existing_meta,
            "historical_csv_processed": True,
            "historical_source": csv_path.name,
            "historical_detections_added": prev_historical + historical_count,
            "historical_processed_at": datetime.now().isoformat(),
        }
        with open(metadata_file, "w", encoding="utf-8") as file_handle:
            json.dump(metadata, file_handle, indent=2)

        if download_audio_flag and client and new_detections:
            audio_candidates = [
                detection
                for detection in new_detections
                if detection.get("soundscape") and detection["soundscape"].get("hash")
            ]

            if not audio_candidates:
                stats["audio_not_available"] += len(new_detections)
                continue

            resolved = resolve_audio_urls_for_day(
                client, day_str, audio_candidates, ne=ne, sw=sw
            )
            logger.info(
                "  %s: resolved %s/%s audio URLs via API",
                day_str,
                len(resolved),
                len(audio_candidates),
            )

            first_url = next(iter(resolved.values()), {}).get("url", "")
            if first_url and check_archive_status(first_url):
                logger.info(
                    "  %s: audio archived in S3 Intelligent Tiering (not downloadable) — skipping %s files",
                    day_str,
                    len(audio_candidates),
                )
                stats["audio_archived"] += len(audio_candidates)
                continue

            audio_dir = day_dir / "audio"
            audio_dir.mkdir(exist_ok=True)

            download_tasks = []
            for detection in audio_candidates:
                det_id = detection["id"]
                if det_id in resolved:
                    info = resolved[det_id]
                    ext = Path(info.get("filename", "audio.flac")).suffix or ".flac"
                    audio_path = audio_dir / f"{detection['soundscape']['hash']}{ext}"
                    download_tasks.append(
                        {
                            "url": info["url"],
                            "path": audio_path,
                            "expected_size": info.get("filesize"),
                        }
                    )
                else:
                    stats["audio_unresolved"] += 1

            if download_tasks:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(
                            download_audio_file, client, task["url"], task["path"]
                        ): task
                        for task in download_tasks
                    }

                    with tqdm(
                        total=len(download_tasks),
                        desc=f"  Audio {day_str}",
                        unit="file",
                        leave=False,
                    ) as pbar:
                        for future in as_completed(futures):
                            success, status = future.result()
                            if success:
                                if status == "already_exists":
                                    stats["audio_cached"] += 1
                                else:
                                    stats["audio_downloaded"] += 1
                            else:
                                stats["audio_failed"] += 1
                            pbar.update(1)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert historical BirdWeather CSV exports to pipeline format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert all CSVs and download audio
  poetry run python scripts/repair/convert_historical_birdweather.py

  # Skip audio download
  poetry run python scripts/repair/convert_historical_birdweather.py --no-audio

  # Only process 2024 and 2025
  poetry run python scripts/repair/convert_historical_birdweather.py --years 2024 2025

  # More parallel workers
  poetry run python scripts/repair/convert_historical_birdweather.py --workers 20
        """,
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default="data/raw/birdweather_historical_manual",
        help="Directory containing CSV files (default: data/raw/birdweather_historical_manual)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/raw/birdweather",
        help="Output directory matching pipeline format (default: data/raw/birdweather)",
    )
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        help="Only process specific years (e.g., --years 2024 2025)",
    )
    parser.add_argument(
        "--no-audio", action="store_true", help="Skip audio file downloads"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of parallel audio download workers (default: 10)",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists():
        logger.error("Input directory not found: %s", input_dir)
        return

    csv_files = sorted(input_dir.glob("*.csv"))
    if args.years:
        year_strs = {str(year) for year in args.years}
        csv_files = [
            file_path
            for file_path in csv_files
            if any(year in file_path.name for year in year_strs)
        ]

    if not csv_files:
        logger.error("No CSV files found in %s", input_dir)
        return

    logger.info("=" * 60)
    logger.info("HISTORICAL BIRDWEATHER CSV CONVERSION")
    logger.info("=" * 60)
    logger.info("Input:  %s", input_dir)
    logger.info("Output: %s", output_dir)
    logger.info("Files:  %s", len(csv_files))
    logger.info("Audio:  %s", "yes" if not args.no_audio else "no")
    logger.info("Workers: %s", args.workers)
    logger.info("=" * 60)

    grand_stats = {
        "total_rows": 0,
        "detections": 0,
        "days": 0,
        "audio_downloaded": 0,
        "audio_cached": 0,
        "audio_failed": 0,
        "audio_not_available": 0,
        "audio_unresolved": 0,
        "audio_archived": 0,
    }

    for csv_file in csv_files:
        stats = process_csv_file(
            csv_path=csv_file,
            output_dir=output_dir,
            download_audio_flag=not args.no_audio,
            workers=args.workers,
        )
        for key in grand_stats:
            grand_stats[key] += stats.get(key, 0)

    logger.info("\n" + "=" * 60)
    logger.info("CONVERSION COMPLETE")
    logger.info("=" * 60)
    logger.info("Total rows processed: %s", f"{grand_stats['total_rows']:,}")
    logger.info("Detections written:   %s", f"{grand_stats['detections']:,}")
    logger.info("Days covered:         %s", grand_stats["days"])
    if not args.no_audio:
        logger.info("Audio downloaded:     %s", f"{grand_stats['audio_downloaded']:,}")
        logger.info("Audio cached:         %s", f"{grand_stats['audio_cached']:,}")
        logger.info("Audio failed:         %s", f"{grand_stats['audio_failed']:,}")
        logger.info(
            "Audio not available:  %s", f"{grand_stats['audio_not_available']:,}"
        )
        logger.info("Audio unresolved:     %s", f"{grand_stats['audio_unresolved']:,}")
        logger.info("Audio archived (S3):  %s", f"{grand_stats['audio_archived']:,}")


if __name__ == "__main__":
    main()
