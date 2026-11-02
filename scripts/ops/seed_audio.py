#!/usr/bin/env python3
"""One-time backfill of existing local audio files into MinIO.

Scans the ``data/raw/birdweather/`` directory tree for FLAC, MP3, and WAV files
and uploads them to the MinIO AudioDB container.
"""

# ruff: noqa: E402

from __future__ import annotations

import logging
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.minio_client import BUCKET_RAW_AUDIO, AudioStore


load_runtime_env()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


def main() -> int:
    """Upload locally stored BirdWeather audio files into MinIO."""
    store = AudioStore()
    store.ensure_buckets()

    base_dir = _REPO_ROOT / "data" / "raw" / "birdweather"
    if not base_dir.exists():
        logging.error("Directory %s does not exist.", base_dir)
        return 1

    audio_files = (
        list(base_dir.rglob("*.mp3"))
        + list(base_dir.rglob("*.flac"))
        + list(base_dir.rglob("*.wav"))
    )
    logging.info("Found %d audio files to upload to MinIO...", len(audio_files))

    uploaded = 0
    skipped = 0
    for path in audio_files:
        try:
            date_str = path.parent.parent.name
            filename = path.name
            object_name = f"{date_str}/{filename}"

            if store.exists(object_name, bucket=BUCKET_RAW_AUDIO):
                skipped += 1
                continue

            content_type = (
                "audio/flac"
                if path.suffix.lower() == ".flac"
                else "audio/mpeg"
                if path.suffix.lower() == ".mp3"
                else "audio/wav"
            )

            with open(path, "rb") as file_handle:
                store.upload_audio(
                    object_name=object_name,
                    data=file_handle,
                    bucket=BUCKET_RAW_AUDIO,
                    content_type=content_type,
                    length=path.stat().st_size,
                )
            uploaded += 1
            if uploaded % 25 == 0:
                logging.info(
                    "Progress: Uploaded %d / %d files...",
                    uploaded,
                    len(audio_files),
                )

        except Exception as exc:
            logging.error("Failed to upload %s: %s", path, exc)

    logging.info(
        "Finished! Uploaded: %d, Skipped (already existed): %d",
        uploaded,
        skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
