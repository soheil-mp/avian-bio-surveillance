#!/usr/bin/env python3
"""Pre-compute static assets for the /explore frontend page.

Generates:
  1. ``frontend/public/data/explore_options.json``
     Dropdown options (species, stations, locations) so the frontend
     avoids three slow DISTINCT queries on every page load.

  2. ``data/processed/clips/``
     Trimmed ~3-second audio clips (detection window only) stored
     alongside the originals, ready for MinIO upload.

Usage::

    # From repo root (with containers / .env loaded)
    python scripts/ops/precompute_explore.py

    # Only regenerate dropdown options
    python scripts/ops/precompute_explore.py --options-only

    # Only crop audio clips
    python scripts/ops/precompute_explore.py --clips-only

    # Custom clip output directory
    python scripts/ops/precompute_explore.py --clip-dir data/processed/clips
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path


# Ensure repo root ``src/`` is importable
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

from avian_biosurveillance.config.runtime import load_runtime_env  # noqa: E402
from avian_biosurveillance.db.database import ConnectionPool  # noqa: E402


load_runtime_env()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

PUBLIC_DATA = _REPO / "frontend" / "public" / "data"
DEFAULT_CLIP_DIR = _REPO / "data" / "processed" / "clips"
AUDIO_RAW_DIR = _REPO / "data" / "raw" / "birdweather"

# Padding around the detection window when cropping (seconds)
CLIP_PAD = 0.25


def _build_options(pool: ConnectionPool) -> dict:
    """Query the database for dropdown option lists."""
    logger.info("Fetching species options …")
    with pool.cursor() as cur:
        # common_name lives on species_tbl now; only species *actually
        # detected* should appear in the explore dropdown.
        cur.execute(
            "SELECT DISTINCT sp.common_name "
            "FROM birdweather_tbl b "
            "JOIN species_tbl sp ON sp.species_id = b.species_id "
            "WHERE sp.common_name IS NOT NULL "
            "ORDER BY sp.common_name"
        )
        species = [row[0] for row in cur.fetchall()]

    logger.info("Fetching station options …")
    with pool.cursor() as cur:
        cur.execute(
            "SELECT station_name FROM birdweather_stations_tbl ORDER BY station_name"
        )
        stations = [row[0] for row in cur.fetchall()]

    logger.info("Fetching location options …")
    with pool.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT station_state, station_country "
            "FROM birdweather_stations_tbl "
            "WHERE station_state IS NOT NULL OR station_country IS NOT NULL "
            "ORDER BY station_state, station_country"
        )
        locations = sorted(
            {", ".join(filter(None, (row[0], row[1]))) for row in cur.fetchall()}
        )

    logger.info("Fetching background-noise label palette …")
    with pool.cursor() as cur:
        # Tags now live inside the ``background_sound_clf`` JSONB envelope:
        # ``{"status": ..., "tags": [{"label": ..., "score": ...}, ...]}``.
        cur.execute(
            """
            SELECT DISTINCT label
            FROM birdweather_tbl,
                 LATERAL jsonb_array_elements(background_sound_clf -> 'tags') AS t(elem),
                 LATERAL jsonb_extract_path_text(elem, 'label') AS label
            WHERE background_sound_clf IS NOT NULL
              AND jsonb_typeof(background_sound_clf -> 'tags') = 'array'
              AND jsonb_array_length(background_sound_clf -> 'tags') > 0
            ORDER BY label
            """
        )
        bg_labels = [row[0] for row in cur.fetchall()]

    return {
        "species": species,
        "stations": stations,
        "locations": locations,
        "bgLabels": bg_labels,
        "generatedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }


def _save_options(opts: dict) -> None:
    """Write explore_options.json for the frontend."""
    PUBLIC_DATA.mkdir(parents=True, exist_ok=True)
    out = PUBLIC_DATA / "explore_options.json"
    out.write_text(json.dumps(opts, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "Wrote %s  (%d species, %d stations, %d locations, %d bg labels)",
        out,
        len(opts["species"]),
        len(opts["stations"]),
        len(opts["locations"]),
        len(opts["bgLabels"]),
    )


def _crop_clips(pool: ConnectionPool, clip_dir: Path) -> None:
    """Trim raw audio files to the detection window using ffmpeg."""
    clip_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Querying detections with audio timing …")
    with pool.cursor() as cur:
        cur.execute(
            """
            SELECT detection_id, observation_date, species_id,
                   audio_filename, audio_start_time_s, audio_end_time_s
            FROM birdweather_tbl
            WHERE audio_start_time_s IS NOT NULL
              AND audio_end_time_s   IS NOT NULL
              AND audio_filename     IS NOT NULL
            ORDER BY detected_at DESC
            """
        )
        rows = cur.fetchall()

    logger.info("Found %d detections with timing info", len(rows))

    created = skipped = failed = 0
    for det_id, obs_date, species_id, audio_fname, start_s, end_s in rows:
        out_path = clip_dir / f"{det_id}.flac"
        if out_path.exists():
            skipped += 1
            continue

        date_str = (
            obs_date.strftime("%Y-%m-%d")
            if hasattr(obs_date, "strftime")
            else str(obs_date).split("T")[0]
        )
        ext = audio_fname.rsplit(".", 1)[-1] if "." in audio_fname else "flac"
        local_name = f"{det_id}_{species_id}.{ext}"
        src_path = AUDIO_RAW_DIR / date_str / "audio" / local_name

        if not src_path.exists():
            src_path = AUDIO_RAW_DIR / date_str / "audio" / audio_fname
            if not src_path.exists():
                failed += 1
                continue

        ss = max(0, float(start_s) - CLIP_PAD)
        duration = float(end_s) - float(start_s) + 2 * CLIP_PAD

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(src_path),
                    "-ss",
                    f"{ss:.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-c:a",
                    "flac",
                    "-ar",
                    "48000",
                    str(out_path),
                ],
                check=True,
                timeout=30,
            )
            created += 1
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("ffmpeg failed for %s: %s", det_id, exc)
            failed += 1

    logger.info(
        "Clips: %d created, %d skipped (exist), %d failed",
        created,
        skipped,
        failed,
    )


def _upload_clips_to_minio(clip_dir: Path) -> None:
    """Upload trimmed clips to the 'processed-audio' MinIO bucket."""
    try:
        from avian_biosurveillance.db.minio_client import AudioStore
    except ImportError:
        logger.warning("minio SDK not installed — skipping upload")
        return

    store = AudioStore()
    bucket = "processed-audio"
    uploaded = 0

    for clip in sorted(clip_dir.glob("*.flac")):
        det_id = clip.stem
        object_name = f"clips/{det_id}.flac"
        try:
            store._client.fput_object(
                bucket,
                object_name,
                str(clip),
                content_type="audio/flac",
            )
            uploaded += 1
        except Exception as exc:
            logger.warning("Upload failed for %s: %s", clip.name, exc)

    logger.info("Uploaded %d clips to MinIO bucket '%s'", uploaded, bucket)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Pre-compute explore page assets")
    parser.add_argument(
        "--options-only",
        action="store_true",
        help="Generate explore_options.json only (no audio cropping)",
    )
    parser.add_argument(
        "--clips-only",
        action="store_true",
        help="Crop audio clips only (no JSON generation)",
    )
    parser.add_argument(
        "--clip-dir",
        type=Path,
        default=DEFAULT_CLIP_DIR,
        help="Output directory for trimmed clips",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload trimmed clips to MinIO after cropping",
    )
    args = parser.parse_args()

    pool = ConnectionPool(min_conn=1, max_conn=4)

    try:
        if not args.clips_only:
            opts = _build_options(pool)
            _save_options(opts)

        if not args.options_only:
            _crop_clips(pool, args.clip_dir)
            if args.upload:
                _upload_clips_to_minio(args.clip_dir)
    finally:
        pool.close()

    logger.info("Done.")


if __name__ == "__main__":
    main()
