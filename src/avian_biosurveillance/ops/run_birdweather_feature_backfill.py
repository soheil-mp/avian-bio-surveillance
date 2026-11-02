"""One-off backfill of BirdWeather feature columns for processed clips.

This CLI is intentionally operational rather than orchestration-facing. It
walks the pending BirdWeather feature backlog day by day, runs feature
engineering only for rows that already have processed clips, and reports any
days that still have missing embeddings or background classification after the
pass.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import time

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import AudioStore
from avian_biosurveillance.features.birdweather_features import (
    FeatureEngineeringStats,
    engineer_birdweather_audio_features,
)


logger = logging.getLogger(__name__)

_FEATURE_STAT_FIELDS = (
    "candidates",
    "featured",
    "missing_clip",
    "storage_retries",
    "storage_unavailable",
    "decode_failed",
    "artifact_upload_failed",
    "artifacts_uploaded",
    "background_classified",
    "background_failed",
    "embeddings_written",
    "embeddings_skipped",
    "db_updated",
)

_EMBEDDING_MODEL_CHOICES: dict[str, tuple[str, ...]] = {
    "birdnet": ("birdnet",),
    "perch": ("perch_v2",),
    "both": ("birdnet", "perch_v2"),
}


@dataclass(frozen=True)
class BirdWeatherFeatureBackfillConfig:
    """Runtime options for the one-off BirdWeather feature backfill."""

    start_date: date | None
    end_date: date | None
    status_only: bool
    embeddings_only: bool
    embedding_models: tuple[str, ...]
    window_days: int
    progress_every: int
    shard_count: int | None
    shard_index: int | None
    background_device: str | None
    background_top_k: int | None
    background_min_confidence: float
    continue_on_storage_unavailable: bool
    storage_retry_attempts: int
    storage_retry_backoff_seconds: float


@dataclass(frozen=True)
class PendingFeatureWindow:
    """Aggregate status for processed BirdWeather rows missing features."""

    pending_rows: int
    min_date: date | None
    max_date: date | None
    missing_background_rows: int
    missing_birdnet_rows: int
    missing_perch_rows: int

    def to_payload(self) -> dict[str, int | str | None]:
        return {
            "pending_rows": self.pending_rows,
            "min_date": self.min_date.isoformat() if self.min_date else None,
            "max_date": self.max_date.isoformat() if self.max_date else None,
            "missing_background_rows": self.missing_background_rows,
            "missing_birdnet_rows": self.missing_birdnet_rows,
            "missing_perch_rows": self.missing_perch_rows,
        }


@dataclass(frozen=True)
class PendingDayStatus:
    """Per-day pending status used to drive the backfill loop."""

    day: date
    pending_rows: int
    missing_background_rows: int
    missing_birdnet_rows: int
    missing_perch_rows: int

    def to_payload(self) -> dict[str, int | str]:
        return {
            "day": self.day.isoformat(),
            "pending_rows": self.pending_rows,
            "missing_background_rows": self.missing_background_rows,
            "missing_birdnet_rows": self.missing_birdnet_rows,
            "missing_perch_rows": self.missing_perch_rows,
        }


class BirdWeatherFeatureBackfillRunner:
    """Execute the one-off BirdWeather feature backfill."""

    def __init__(self, config: BirdWeatherFeatureBackfillConfig) -> None:
        self.config = config
        self.pool = ConnectionPool(min_conn=1, max_conn=4)
        self.store = AudioStore()

    def close(self) -> None:
        self.pool.close()

    def run(self) -> int:
        scan_start, scan_end = self._scan_date_window()
        if scan_start is None or scan_end is None:
            logger.info("No BirdWeather processed-clip window was found for feature backfill.")
            return 0

        if self.config.status_only:
            initial_days = self._load_pending_days(scan_start, scan_end)
            initial = self._pending_window(initial_days)
            logger.info(
                "Initial BirdWeather feature backlog: %s",
                json.dumps(initial.to_payload(), sort_keys=True),
            )
            if initial.pending_rows == 0:
                logger.info("No processed BirdWeather rows are missing features.")
            return 0

        logger.info(
            "Running BirdWeather feature backfill in %d-day windows from %s to %s.",
            self.config.window_days,
            scan_start.isoformat(),
            scan_end.isoformat(),
        )
        if self.config.shard_count is not None and self.config.shard_index is not None:
            logger.info(
                "Shard selection: %d/%d.",
                self.config.shard_index + 1,
                self.config.shard_count,
            )
        logger.info(
            "Row progress logging interval: every %d row(s).",
            self.config.progress_every,
        )

        windows = list(
            _iter_date_windows(
                scan_start,
                scan_end,
                window_days=self.config.window_days,
            )
        )
        total_windows = len(windows)
        run_started_at = time.perf_counter()
        aggregate_stats = FeatureEngineeringStats()
        failed_windows: list[dict[str, object]] = []
        for window_index, (window_start, window_end) in enumerate(windows, start=1):
            window_label = _window_label(
                window_index,
                total_windows,
                shard_count=self.config.shard_count,
                shard_index=self.config.shard_index,
            )
            pending_days = self._load_pending_days(window_start, window_end)
            pending_window = self._pending_window(pending_days)
            if pending_window.pending_rows == 0:
                logger.info(
                    "%s %s .. %s: no pending rows.",
                    window_label,
                    window_start.isoformat(),
                    window_end.isoformat(),
                )
                continue

            logger.info(
                "%s %s .. %s: pending_rows=%d missing_background=%d missing_birdnet=%d missing_perch=%d embeddings_only=%s.",
                window_label,
                window_start.isoformat(),
                window_end.isoformat(),
                pending_window.pending_rows,
                pending_window.missing_background_rows,
                pending_window.missing_birdnet_rows,
                pending_window.missing_perch_rows,
                self.config.embeddings_only,
            )
            window_started_at = time.perf_counter()
            stats = engineer_birdweather_audio_features(
                lookback_days=None,
                limit=None,
                include_background_classification=not self.config.embeddings_only,
                include_feature_artifacts=not self.config.embeddings_only,
                embedding_models=self.config.embedding_models,
                progress_every=self.config.progress_every,
                shard_count=self.config.shard_count,
                shard_index=self.config.shard_index,
                progress_label=(
                    f"{window_label} {window_start.isoformat()}"
                    if window_start == window_end
                    else f"{window_label} {window_start.isoformat()} .. {window_end.isoformat()}"
                ),
                start_date=window_start,
                end_date=window_end,
                background_device=self.config.background_device,
                background_top_k=self.config.background_top_k,
                background_min_confidence=self.config.background_min_confidence,
                continue_on_storage_unavailable=self.config.continue_on_storage_unavailable,
                storage_retry_attempts=self.config.storage_retry_attempts,
                storage_retry_backoff_seconds=self.config.storage_retry_backoff_seconds,
                store=self.store,
                pool=self.pool,
            )
            _accumulate_feature_stats(aggregate_stats, stats)
            window_elapsed_s = time.perf_counter() - window_started_at
            total_elapsed_s = time.perf_counter() - run_started_at

            if stats.candidates == 0:
                logger.info(
                    "%s %s .. %s completed in %.1fs (elapsed %.1fs): candidates=0.",
                    window_label,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    window_elapsed_s,
                    total_elapsed_s,
                )
                continue

            remaining_days = self._load_pending_days(window_start, window_end)
            if not remaining_days:
                logger.info(
                    "%s %s .. %s completed in %.1fs (elapsed %.1fs): candidates=%d featured=%d db_updated=%d embeddings_written=%d background_classified=%d missing_clip=%d decode_failed=%d storage_unavailable=%d.",
                    window_label,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    window_elapsed_s,
                    total_elapsed_s,
                    stats.candidates,
                    stats.featured,
                    stats.db_updated,
                    stats.embeddings_written,
                    stats.background_classified,
                    stats.missing_clip,
                    stats.decode_failed,
                    stats.storage_unavailable,
                )
                continue

            remaining_window = self._pending_window(remaining_days)

            failed_windows.append(
                {
                    "start_date": window_start.isoformat(),
                    "end_date": window_end.isoformat(),
                    "remaining": [status.to_payload() for status in remaining_days],
                    "stats": stats.to_metadata(),
                }
            )
            logger.warning(
                "%s %s .. %s incomplete after %.1fs (elapsed %.1fs): remaining_rows=%d remaining_days=%d.",
                window_label,
                window_start.isoformat(),
                window_end.isoformat(),
                window_elapsed_s,
                total_elapsed_s,
                remaining_window.pending_rows,
                len(remaining_days),
            )

        total_elapsed_s = time.perf_counter() - run_started_at
        logger.info(
            "BirdWeather feature backfill totals after %.1fs: windows=%d candidates=%d featured=%d db_updated=%d embeddings_written=%d background_classified=%d missing_clip=%d decode_failed=%d storage_unavailable=%d artifact_upload_failed=%d.",
            total_elapsed_s,
            total_windows,
            aggregate_stats.candidates,
            aggregate_stats.featured,
            aggregate_stats.db_updated,
            aggregate_stats.embeddings_written,
            aggregate_stats.background_classified,
            aggregate_stats.missing_clip,
            aggregate_stats.decode_failed,
            aggregate_stats.storage_unavailable,
            aggregate_stats.artifact_upload_failed,
        )
        if failed_windows:
            logger.error(
                "BirdWeather feature backfill completed with %d incomplete window(s).",
                len(failed_windows),
            )
            for failure in failed_windows:
                logger.error(
                    "Incomplete window summary: %s",
                    json.dumps(failure, sort_keys=True),
                )
            return 1
        return 0

    def _pending_window(
        self,
        pending_days: Sequence[PendingDayStatus],
    ) -> PendingFeatureWindow:
        if not pending_days:
            return PendingFeatureWindow(
                pending_rows=0,
                min_date=None,
                max_date=None,
                missing_background_rows=0,
                missing_birdnet_rows=0,
                missing_perch_rows=0,
            )

        return PendingFeatureWindow(
            pending_rows=sum(status.pending_rows for status in pending_days),
            min_date=pending_days[0].day,
            max_date=pending_days[-1].day,
            missing_background_rows=sum(
                status.missing_background_rows for status in pending_days
            ),
            missing_birdnet_rows=sum(status.missing_birdnet_rows for status in pending_days),
            missing_perch_rows=sum(status.missing_perch_rows for status in pending_days),
        )

    def _load_pending_days(
        self,
        start_date: date,
        end_date: date,
    ) -> list[PendingDayStatus]:
        logger.debug(
            "Loading BirdWeather pending feature days from %s to %s.",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        pending_predicate = _pending_feature_predicate(
            embeddings_only=self.config.embeddings_only,
            embedding_models=self.config.embedding_models,
        )
        shard_filter, shard_params = _pending_shard_filter(
            shard_count=self.config.shard_count,
            shard_index=self.config.shard_index,
        )
        with self.pool.cursor() as cur:
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            cur.execute(
                f"""
                SELECT
                    observation_date,
                    count(*)::int AS pending_rows,
                    count(*) FILTER (
                        WHERE background_sound_clf IS NULL
                           OR COALESCE(background_sound_clf ->> 'status', '') = 'classification_failed'
                    )::int AS missing_background_rows,
                    count(*) FILTER (WHERE birdnet_embedding IS NULL)::int AS missing_birdnet_rows,
                    count(*) FILTER (WHERE perch_v2_embedding IS NULL)::int AS missing_perch_rows
                FROM birdweather_tbl
                WHERE has_processed_clip
                                    AND ({pending_predicate})
                                    {shard_filter}
                  AND detected_at >= %s
                  AND detected_at < %s
                  AND observation_date BETWEEN %s AND %s
                GROUP BY observation_date
                ORDER BY observation_date
                """,
                (
                    *shard_params,
                    _day_start(start_date),
                    _day_start(end_date + timedelta(days=1)),
                    start_date,
                    end_date,
                ),
            )
            rows = cur.fetchall()
        return [
            PendingDayStatus(
                day=row[0],
                pending_rows=int(row[1] or 0),
                missing_background_rows=int(row[2] or 0),
                missing_birdnet_rows=int(row[3] or 0),
                missing_perch_rows=int(row[4] or 0),
            )
            for row in rows
        ]

    def _pending_day(self, day: date) -> PendingDayStatus:
        day_start = _day_start(day)
        day_end = _day_start(day + timedelta(days=1))
        pending_predicate = _pending_feature_predicate(
            embeddings_only=self.config.embeddings_only,
            embedding_models=self.config.embedding_models,
        )
        shard_filter, shard_params = _pending_shard_filter(
            shard_count=self.config.shard_count,
            shard_index=self.config.shard_index,
        )
        with self.pool.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    count(*)::int AS pending_rows,
                    count(*) FILTER (
                        WHERE background_sound_clf IS NULL
                           OR COALESCE(background_sound_clf ->> 'status', '') = 'classification_failed'
                    )::int AS missing_background_rows,
                    count(*) FILTER (WHERE birdnet_embedding IS NULL)::int AS missing_birdnet_rows,
                    count(*) FILTER (WHERE perch_v2_embedding IS NULL)::int AS missing_perch_rows
                FROM birdweather_tbl
                WHERE has_processed_clip
                                    AND ({pending_predicate})
                                    {shard_filter}
                  AND detected_at >= %s
                  AND detected_at < %s
                  AND observation_date = %s
                """,
                                (*shard_params, day_start, day_end, day),
            )
            row = cur.fetchone() or (0, 0, 0, 0)
        return PendingDayStatus(
            day=day,
            pending_rows=int(row[0] or 0),
            missing_background_rows=int(row[1] or 0),
            missing_birdnet_rows=int(row[2] or 0),
            missing_perch_rows=int(row[3] or 0),
        )

    def _scan_date_window(self) -> tuple[date | None, date | None]:
        start_date = self.config.start_date
        end_date = self.config.end_date

        if start_date is None:
            start_date = self._processed_boundary_date(descending=False, end_date=end_date)
        if end_date is None:
            end_date = self._processed_boundary_date(descending=True, start_date=start_date)
        if start_date is None or end_date is None or end_date < start_date:
            return None, None
        return start_date, end_date

    def _processed_boundary_date(
        self,
        *,
        descending: bool,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> date | None:
        params: list[object] = []
        filters = ["has_processed_clip"]
        if start_date is not None:
            filters.append("detected_at >= %s")
            params.append(_day_start(start_date))
            filters.append("observation_date >= %s")
            params.append(start_date)
        if end_date is not None:
            filters.append("detected_at < %s")
            params.append(_day_start(end_date + timedelta(days=1)))
            filters.append("observation_date <= %s")
            params.append(end_date)
        if self.config.shard_count is not None and self.config.shard_index is not None:
            filters.append(
                "mod((hashtext(detection_id)::bigint & 2147483647), %s) = %s"
            )
            params.extend((self.config.shard_count, self.config.shard_index))

        direction = "DESC" if descending else "ASC"
        with self.pool.cursor() as cur:
            cur.execute(
                f"""
                SELECT observation_date
                FROM birdweather_tbl
                WHERE {' AND '.join(filters)}
                ORDER BY detected_at {direction}
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
        return None if row is None else row[0]

def _day_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _accumulate_feature_stats(
    aggregate: FeatureEngineeringStats,
    stats: FeatureEngineeringStats,
) -> None:
    for field_name in _FEATURE_STAT_FIELDS:
        setattr(
            aggregate,
            field_name,
            int(getattr(aggregate, field_name)) + int(getattr(stats, field_name)),
        )


def _iter_date_windows(
    start_date: date,
    end_date: date,
    *,
    window_days: int,
) -> Sequence[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = start_date
    step = max(1, window_days)
    while cursor <= end_date:
        window_end = min(end_date, cursor + timedelta(days=step - 1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _pending_feature_predicate(
    *,
    embeddings_only: bool,
    embedding_models: tuple[str, ...],
) -> str:
    embed_clauses: list[str] = []
    if "birdnet" in embedding_models:
        embed_clauses.append("birdnet_embedding IS NULL")
    if "perch_v2" in embedding_models:
        embed_clauses.append("perch_v2_embedding IS NULL")
    embed_predicate = " OR ".join(embed_clauses)
    if embeddings_only:
        return embed_predicate
    return (
        "feature_artifacts IS NULL OR "
        "background_sound_clf IS NULL OR "
        "COALESCE(background_sound_clf ->> 'status', '') = 'classification_failed' OR "
        f"{embed_predicate}"
    )


def _pending_shard_filter(
    *,
    shard_count: int | None,
    shard_index: int | None,
) -> tuple[str, tuple[int, int] | tuple[()]]:
    if shard_count is None or shard_index is None:
        return "", ()
    return (
        "AND mod((hashtext(detection_id)::bigint & 2147483647), %s) = %s",
        (shard_count, shard_index),
    )


def _window_label(
    window_index: int,
    total_windows: int,
    *,
    shard_count: int | None,
    shard_index: int | None,
) -> str:
    label = f"[{window_index}/{total_windows}]"
    if shard_count is None or shard_index is None:
        return label
    return f"{label} [shard {shard_index + 1}/{shard_count}]"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill BirdWeather background classification and embedding columns "
            "for rows that already have processed clips."
        )
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        help="Optional first observation date to process (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        help="Optional last observation date to process (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Report the current pending-feature backlog without processing rows.",
    )
    parser.add_argument(
        "--embeddings-only",
        action="store_true",
        help="Only backfill BirdNET and Perch embeddings; skip background classification.",
    )
    parser.add_argument(
        "--embedding-models",
        choices=tuple(_EMBEDDING_MODEL_CHOICES),
        default="both",
        help=(
            "Which embedding model(s) to compute and backfill: 'birdnet', 'perch', "
            "or 'both' (default). Only rows missing a selected model are loaded, and "
            "only the selected model(s) are computed and written, so a model that is "
            "already populated is never recomputed."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=31,
        help="Date-window size for one-off backfill execution (default: 31 days).",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Emit a progress log after every N candidate rows within a window (default: 100). Use 0 to disable.",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        help="Optional number of deterministic hash shards to split the backfill into.",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        help="Zero-based shard index to process when --shard-count is set.",
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
        "--storage-retry-attempts",
        type=int,
        default=3,
        help="Retries for processed-audio storage checks and downloads.",
    )
    parser.add_argument(
        "--storage-retry-backoff-seconds",
        type=float,
        default=1.0,
        help="Base backoff between processed-audio storage retries.",
    )
    parser.add_argument(
        "--continue-on-storage-unavailable",
        action="store_true",
        help="Skip rows with transient processed-audio storage failures instead of aborting.",
    )
    args = parser.parse_args(argv)
    if args.start_date is not None and args.end_date is not None and args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date.")
    if args.background_top_k is not None and args.background_top_k < 1:
        parser.error("--background-top-k must be at least 1.")
    if args.storage_retry_attempts < 0:
        parser.error("--storage-retry-attempts must be non-negative.")
    if args.storage_retry_backoff_seconds < 0:
        parser.error("--storage-retry-backoff-seconds must be non-negative.")
    if args.window_days < 1:
        parser.error("--window-days must be at least 1.")
    if args.progress_every < 0:
        parser.error("--progress-every must be non-negative.")
    if (args.shard_count is None) != (args.shard_index is None):
        parser.error("--shard-count and --shard-index must be provided together.")
    if args.shard_count is not None and args.shard_count < 1:
        parser.error("--shard-count must be at least 1.")
    if (
        args.shard_count is not None
        and (args.shard_index < 0 or args.shard_index >= args.shard_count)
    ):
        parser.error("--shard-index must be in [0, --shard-count).")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    config = BirdWeatherFeatureBackfillConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        status_only=args.status_only,
        embeddings_only=args.embeddings_only,
        embedding_models=_EMBEDDING_MODEL_CHOICES[args.embedding_models],
        window_days=args.window_days,
        progress_every=args.progress_every,
        shard_count=args.shard_count,
        shard_index=args.shard_index,
        background_device=args.background_device or None,
        background_top_k=args.background_top_k,
        background_min_confidence=args.background_min_confidence,
        continue_on_storage_unavailable=args.continue_on_storage_unavailable,
        storage_retry_attempts=args.storage_retry_attempts,
        storage_retry_backoff_seconds=args.storage_retry_backoff_seconds,
    )
    runner = BirdWeatherFeatureBackfillRunner(config)
    try:
        return runner.run()
    except Exception:
        logger.exception("BirdWeather feature backfill failed.")
        return 1
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())