#!/usr/bin/env python3
"""Replay the BirdWeather audio-to-embedding path for a narrow date window.

This utility is intended for operations and post-incident recovery checks.
It runs the same raw-audio download, audio processing, and feature-engineering
functions used by Dagster, then reports before/after BirdWeather embedding
coverage for the selected date window.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.config.runtime import load_runtime_env  # noqa: E402
from avian_biosurveillance.db.database import ConnectionPool  # noqa: E402
from avian_biosurveillance.db.minio_client import AudioStore  # noqa: E402
from avian_biosurveillance.features.birdweather_features import (  # noqa: E402
    engineer_birdweather_audio_features,
)
from avian_biosurveillance.modeling.pipeline import (
    run_model_anomaly_scoring,
)
from avian_biosurveillance.processing.birdweather_audio import (  # noqa: E402
    process_birdweather_audio_clips,
)
from avian_biosurveillance.processing.birdweather_raw_audio import (  # noqa: E402
    download_birdweather_raw_audio_objects,
)


load_runtime_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingWindowSnapshot:
    """Aggregated BirdWeather embedding coverage for a date window."""

    rows_total: int = 0
    rows_with_audio_url: int = 0
    rows_with_audio_filename: int = 0
    rows_with_processed_clip: int = 0
    rows_with_feature_artifacts: int = 0
    rows_with_birdnet_embedding: int = 0
    rows_with_perch_v2_embedding: int = 0
    rows_fully_embedded: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay the BirdWeather raw-audio -> embedding pipeline for a narrow date window.",
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        help="First observation date to replay (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        help="Last observation date to replay (YYYY-MM-DD). Defaults to start-date.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=1,
        help="If no explicit dates are provided, replay the most recent N full UTC day(s).",
    )
    parser.add_argument(
        "--raw-limit",
        type=int,
        default=1_000,
        help="Max BirdWeather rows to consider for raw-audio replay.",
    )
    parser.add_argument(
        "--processing-limit",
        type=int,
        default=1_000,
        help="Max BirdWeather rows to consider for processed-clip replay.",
    )
    parser.add_argument(
        "--feature-limit",
        type=int,
        default=500,
        help="Max BirdWeather rows to consider for feature replay.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Concurrent BirdWeather raw-audio download workers.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run steps even when downstream objects already exist.",
    )
    parser.add_argument(
        "--background-device",
        default="",
        help="Optional device override for background-sound classification.",
    )
    parser.add_argument(
        "--background-top-k",
        type=int,
        default=5,
        help="Top-K background classes to retain.",
    )
    parser.add_argument(
        "--background-min-confidence",
        type=float,
        default=0.10,
        help="Minimum background-classification confidence.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Report BirdWeather window health without replaying any work.",
    )
    parser.add_argument(
        "--include-model-scoring",
        action="store_true",
        help="Run model anomaly scoring after BirdWeather features finish.",
    )
    parser.add_argument(
        "--expect-embeddings",
        action="store_true",
        help="Fail if the selected window ends with processed clips but no full BirdNET+Perch2 embeddings.",
    )
    parser.add_argument(
        "--skip-raw-audio",
        action="store_true",
        help="Skip the raw-audio replay step.",
    )
    parser.add_argument(
        "--skip-audio-processing",
        action="store_true",
        help="Skip the audio-processing replay step.",
    )
    parser.add_argument(
        "--skip-feature-engineering",
        action="store_true",
        help="Skip the feature-engineering replay step.",
    )
    parser.add_argument(
        "--snapshot-limit",
        type=int,
        help=(
            "Limit before/after verification to the most recent N rows in the selected window. "
            "Use 0 to snapshot the full window. Defaults to the replay slice size."
        ),
    )
    return parser.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    if args.start_date is not None:
        end_date = args.end_date or args.start_date
        if end_date < args.start_date:
            raise ValueError("--end-date must be on or after --start-date.")
        return args.start_date, end_date

    if args.end_date is not None:
        raise ValueError("--end-date requires --start-date.")

    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero.")

    end_date = _utc_today() - timedelta(days=1)
    start_date = end_date - timedelta(days=args.lookback_days - 1)
    return start_date, end_date


def _snapshot_window(
    pool: ConnectionPool,
    *,
    start_date: date,
    end_date: date,
    limit: int | None,
) -> EmbeddingWindowSnapshot:
    window_sql = (
        "SELECT audio_url, audio_filename, has_processed_clip, feature_artifacts, "
        "birdnet_embedding, perch_v2_embedding "
        "FROM birdweather_tbl "
        "WHERE observation_date >= %s "
        "  AND observation_date <= %s"
    )
    params: list[object] = [start_date, end_date]
    if limit is not None:
        window_sql += " ORDER BY detected_at DESC LIMIT %s"
        params.append(limit)

    with pool.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS rows_total,
                COUNT(*) FILTER (WHERE audio_url IS NOT NULL) AS rows_with_audio_url,
                COUNT(*) FILTER (WHERE audio_filename IS NOT NULL) AS rows_with_audio_filename,
                COUNT(*) FILTER (WHERE has_processed_clip) AS rows_with_processed_clip,
                COUNT(*) FILTER (WHERE feature_artifacts IS NOT NULL) AS rows_with_feature_artifacts,
                COUNT(*) FILTER (WHERE birdnet_embedding IS NOT NULL) AS rows_with_birdnet_embedding,
                COUNT(*) FILTER (WHERE perch_v2_embedding IS NOT NULL) AS rows_with_perch_v2_embedding,
                COUNT(*) FILTER (
                    WHERE birdnet_embedding IS NOT NULL
                      AND perch_v2_embedding IS NOT NULL
                ) AS rows_fully_embedded
                        FROM ({window_sql}) AS window_rows
            """,
            params,
        )
        row = cur.fetchone()

    if row is None:
        return EmbeddingWindowSnapshot()

    return EmbeddingWindowSnapshot(
        rows_total=int(row[0] or 0),
        rows_with_audio_url=int(row[1] or 0),
        rows_with_audio_filename=int(row[2] or 0),
        rows_with_processed_clip=int(row[3] or 0),
        rows_with_feature_artifacts=int(row[4] or 0),
        rows_with_birdnet_embedding=int(row[5] or 0),
        rows_with_perch_v2_embedding=int(row[6] or 0),
        rows_fully_embedded=int(row[7] or 0),
    )


def _print_snapshot(label: str, snapshot: EmbeddingWindowSnapshot) -> None:
    logger.info(
        "%s snapshot: %s", label, json.dumps(snapshot.to_dict(), sort_keys=True)
    )


def _print_stats(label: str, stats: Any) -> None:
    payload = stats.to_metadata() if hasattr(stats, "to_metadata") else stats
    logger.info("%s: %s", label, json.dumps(payload, sort_keys=True, default=str))


def _behavior_failures(
    before: EmbeddingWindowSnapshot,
    after: EmbeddingWindowSnapshot,
    *,
    replay_executed: bool,
    expect_embeddings: bool,
) -> list[str]:
    failures: list[str] = []

    if after.rows_with_audio_url == 0:
        return failures

    if after.rows_with_processed_clip == 0:
        failures.append(
            "BirdWeather window has audio detections but no processed clips."
        )

    if (
        expect_embeddings
        and after.rows_with_processed_clip > 0
        and after.rows_fully_embedded == 0
    ):
        failures.append(
            "BirdWeather window has processed clips but no rows with both BirdNET and Perch2 embeddings."
        )

    if replay_executed and after.rows_fully_embedded < before.rows_fully_embedded:
        failures.append("BirdWeather replay reduced the number of fully embedded rows.")

    return failures


def _check_minio(store: AudioStore) -> None:
    health = store.healthcheck()
    if not health["ok"]:
        missing = ", ".join(sorted(str(name) for name in health["buckets_missing"]))
        raise RuntimeError(f"MinIO healthcheck failed; missing buckets: {missing}")
    logger.info(
        "MinIO healthy with buckets: %s",
        ", ".join(sorted(str(name) for name in health["buckets_found"])),
    )


def _resolve_snapshot_limit(args: argparse.Namespace) -> int | None:
    if args.snapshot_limit is not None:
        return None if args.snapshot_limit <= 0 else args.snapshot_limit

    if args.check_only:
        return 500

    return max(args.raw_limit, args.processing_limit, args.feature_limit)


def main() -> int:
    args = _parse_args()
    start_date, end_date = _resolve_window(args)
    snapshot_limit = _resolve_snapshot_limit(args)
    logger.info("BirdWeather replay window: %s .. %s", start_date, end_date)
    if snapshot_limit is None:
        logger.info("Snapshot scope: full window")
    else:
        logger.info("Snapshot scope: most recent %d row(s)", snapshot_limit)

    store = AudioStore()
    pool = ConnectionPool(min_conn=1, max_conn=4)

    try:
        _check_minio(store)
        logger.info("Collecting pre-replay snapshot.")
        before = _snapshot_window(
            pool,
            start_date=start_date,
            end_date=end_date,
            limit=snapshot_limit,
        )
        _print_snapshot("Before", before)

        replay_executed = not args.check_only
        if replay_executed:
            if args.skip_raw_audio:
                logger.info("Skipping raw-audio replay.")
            else:
                logger.info("Starting raw-audio replay.")
                raw_stats = download_birdweather_raw_audio_objects(
                    lookback_days=None,
                    limit=args.raw_limit,
                    force=args.force,
                    workers=args.workers,
                    start_date=start_date,
                    end_date=end_date,
                    store=store,
                    pool=pool,
                )
                _print_stats("Raw audio replay", raw_stats)

            if args.skip_audio_processing:
                logger.info("Skipping audio-processing replay.")
            else:
                logger.info("Starting audio-processing replay.")
                processing_stats = process_birdweather_audio_clips(
                    lookback_days=None,
                    limit=args.processing_limit,
                    force=args.force,
                    start_date=start_date,
                    end_date=end_date,
                    store=store,
                    pool=pool,
                )
                _print_stats("Audio processing replay", processing_stats)

            if args.skip_feature_engineering:
                logger.info("Skipping feature-engineering replay.")
            else:
                logger.info("Starting feature-engineering replay.")
                feature_stats = engineer_birdweather_audio_features(
                    lookback_days=None,
                    limit=args.feature_limit,
                    force=args.force,
                    start_date=start_date,
                    end_date=end_date,
                    background_device=args.background_device or None,
                    background_top_k=args.background_top_k,
                    background_min_confidence=args.background_min_confidence,
                    continue_on_storage_unavailable=True,
                    storage_retry_attempts=3,
                    storage_retry_backoff_seconds=1.0,
                    store=store,
                    pool=pool,
                )
                _print_stats("Feature engineering replay", feature_stats)

            if args.include_model_scoring:
                logger.info("Starting model anomaly scoring.")
                model_stats = run_model_anomaly_scoring()
                _print_stats("Model anomaly scoring", model_stats)

        logger.info("Collecting post-replay snapshot.")
        after = _snapshot_window(
            pool,
            start_date=start_date,
            end_date=end_date,
            limit=snapshot_limit,
        )
        _print_snapshot("After", after)

        failures = _behavior_failures(
            before,
            after,
            replay_executed=replay_executed,
            expect_embeddings=args.expect_embeddings,
        )
        if failures:
            for failure in failures:
                logger.error("Expectation failed: %s", failure)
            return 1

        if after.rows_with_audio_url == 0:
            logger.info("No BirdWeather audio detections found in the selected window.")
        else:
            logger.info(
                "BirdWeather replay checks passed for %s .. %s.", start_date, end_date
            )
        return 0
    except Exception:
        logger.exception("BirdWeather replay/check failed.")
        return 1
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
