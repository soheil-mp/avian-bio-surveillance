"""Run the BirdWeather 2024-2025 backlog with local raw audio staging.

This CLI walks day-by-day, optionally ensures detections are present in the
database, downloads only still-unprocessed raw audio to local disk, crops and
uploads processed clips to MinIO, then prunes verified local raw files.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import AudioStore
from avian_biosurveillance.ops.ensure_historical_window import OperationState
from avian_biosurveillance.ops.prune_birdweather_raw_audio import (
    prune_birdweather_local_raw_audio_files,
)
from avian_biosurveillance.orchestration.birdweather_flow import (
    _raise_for_audio_processing_followup_failures,
    _raise_for_raw_audio_backfill_failures,
    backfill_birdweather_gaps,
)
from avian_biosurveillance.processing.birdweather_audio import (
    process_birdweather_audio_clips,
)
from avian_biosurveillance.processing.birdweather_raw_audio import (
    download_birdweather_raw_audio_objects,
)
from avian_biosurveillance.processing.birdweather_raw_storage import (
    DEFAULT_LOCAL_BIRDWEATHER_RAW_ROOT,
)


logger = logging.getLogger(__name__)

_REPORT_ROOT = Path("data/processed/coverage")
_DEFAULT_STATE_PATH = Path("data/raw/_state/birdweather_local_backlog_state.json")


@dataclass(frozen=True)
class BirdWeatherLocalBacklogConfig:
    """Runtime options for the local BirdWeather backlog runner."""

    start_date: date
    end_date: date
    state_path: Path
    raw_root: Path
    ensure_detections: bool
    workers: int
    raw_workers: int
    storage_retry_attempts: int
    force: bool = False
    prune_trust_processed: bool = False


class BirdWeatherLocalBacklogRunner:
    """Drive the local-raw BirdWeather backlog one day at a time."""

    def __init__(self, config: BirdWeatherLocalBacklogConfig) -> None:
        self.config = config
        self.state = OperationState.load(config.state_path)
        self.pool = ConnectionPool(min_conn=1, max_conn=4)
        self.store = AudioStore()

    def close(self) -> None:
        self.pool.close()

    def run(self) -> None:
        for day in _iter_days(self.config.start_date, self.config.end_date):
            self._run_day(day)

    def _run_day(self, day: date) -> None:
        complete_key = self._phase_key("complete", day)
        if self.state.is_completed(complete_key):
            logger.info("Skipping completed backlog day %s.", day.isoformat())
            return

        if self.config.ensure_detections:
            detections_key = self._phase_key("detections", day)
            if not self.state.is_completed(detections_key):
                logger.info("Ensuring BirdWeather detections for %s.", day.isoformat())
                backfill_birdweather_gaps(
                    start_date=day,
                    end_date=day + timedelta(days=1),
                    workers=self.config.workers,
                    download_audio=False,
                    target="database",
                    parallel_days=1,
                )
                self.state.mark_completed(detections_key)

        status = self._day_status(day)
        if status["rows_with_audio_url"] == 0 and status["processable_rows"] == 0:
            logger.info("No BirdWeather audio work remains for %s.", day.isoformat())
            self.state.mark_completed(complete_key)
            return

        download_key = self._phase_key("download", day)
        if not self.state.is_completed(download_key):
            logger.info("Downloading local BirdWeather raw audio for %s.", day.isoformat())
            raw_stats = download_birdweather_raw_audio_objects(
                lookback_days=None,
                limit=None,
                start_date=day,
                end_date=day,
                workers=self.config.raw_workers,
                skip_processed=True,
                local_audio_root=self.config.raw_root,
                pool=self.pool,
            )
            _raise_for_raw_audio_backfill_failures(
                f"BirdWeather local raw audio {day.isoformat()}",
                raw_stats,
            )
            self.state.mark_completed(download_key)

        process_key = self._phase_key("process", day)
        if not self.state.is_completed(process_key):
            logger.info("Processing BirdWeather audio for %s.", day.isoformat())
            audio_stats = process_birdweather_audio_clips(
                lookback_days=None,
                limit=None,
                force=self.config.force,
                start_date=day,
                end_date=day,
                local_audio_root=self.config.raw_root,
                workers=self.config.raw_workers,
                storage_retry_attempts=self.config.storage_retry_attempts,
                continue_on_storage_unavailable=True,
                store=self.store,
                pool=self.pool,
            )
            _raise_for_audio_processing_followup_failures(
                f"BirdWeather local audio processing {day.isoformat()}",
                audio_stats,
            )
            if int(getattr(audio_stats, "storage_unavailable", 0) or 0):
                msg = (
                    "BirdWeather local audio processing had storage_unavailable "
                    f"rows for {day.isoformat()}; rerun after MinIO recovers."
                )
                raise RuntimeError(msg)
            if any(
                int(getattr(audio_stats, field, 0) or 0)
                for field in ("missing_raw", "invalid_window", "decode_failed")
            ):
                logger.warning(
                    "BirdWeather local audio %s completed with non-blocking counters: "
                    "missing_raw=%d invalid_window=%d decode_failed=%d.",
                    day.isoformat(),
                    int(getattr(audio_stats, "missing_raw", 0) or 0),
                    int(getattr(audio_stats, "invalid_window", 0) or 0),
                    int(getattr(audio_stats, "decode_failed", 0) or 0),
                )
            self.state.mark_completed(process_key)

        prune_key = self._phase_key("prune", day)
        if not self.state.is_completed(prune_key):
            logger.info("Pruning verified local BirdWeather raw audio for %s.", day.isoformat())
            prune_stats, _verified, missing_processed = prune_birdweather_local_raw_audio_files(
                start_date=day,
                end_date=day,
                raw_root=str(self.config.raw_root),
                pool=self.pool,
                store=self.store,
                apply=True,
                verify_processed=not self.config.prune_trust_processed,
            )
            if prune_stats.skipped_storage_unavailable or prune_stats.skipped_delete_unavailable:
                msg = (
                    "BirdWeather local prune could not verify or delete some local raw "
                    f"files for {day.isoformat()}; rerun after storage/filesystem recovery."
                )
                raise RuntimeError(msg)
            if missing_processed:
                logger.warning(
                    "BirdWeather local prune skipped %d raw file(s) on %s because processed clips were still missing.",
                    len(missing_processed),
                    day.isoformat(),
                )
            logger.info(
                "BirdWeather local prune %s: deleted=%d verified=%d missing_raw=%d missing_processed=%d.",
                day.isoformat(),
                prune_stats.deleted_objects,
                prune_stats.verified_objects,
                prune_stats.skipped_missing_raw,
                prune_stats.skipped_missing_processed,
            )
            self.state.mark_completed(prune_key)

        self.state.mark_completed(complete_key)

    def _phase_key(self, phase: str, day: date) -> str:
        return f"birdweather-local-backlog:{phase}:{day.isoformat()}"

    def _day_status(self, day: date) -> dict[str, int]:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        with self.pool.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE audio_url IS NOT NULL) AS rows_with_audio_url,
                    count(*) FILTER (
                        WHERE audio_url IS NOT NULL
                          AND audio_start_time_s IS NOT NULL
                          AND audio_end_time_s IS NOT NULL
                          AND audio_end_time_s > audio_start_time_s
                    ) AS processable_rows
                FROM birdweather_tbl
                WHERE detected_at >= %s
                  AND detected_at < %s
                  AND observation_date = %s
                """,
                (day_start, day_end, day),
            )
            row = cur.fetchone() or (0, 0)
        return {
            "rows_with_audio_url": int(row[0] or 0),
            "processable_rows": int(row[1] or 0),
        }


def _iter_days(start_date: date, end_date: date) -> Iterable[date]:
    cursor = start_date
    while cursor <= end_date:
        yield cursor
        cursor += timedelta(days=1)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the BirdWeather backlog with local raw-audio staging and "
            "processed-clip uploads to MinIO."
        )
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=date(2024, 1, 1),
        help="First BirdWeather observation date to process (default: 2024-01-01).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=date(2025, 12, 31),
        help="Last BirdWeather observation date to process (default: 2025-12-31).",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=_DEFAULT_STATE_PATH,
        help="JSON state file used for resumable day checkpoints.",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_LOCAL_BIRDWEATHER_RAW_ROOT,
        help="Local root for BirdWeather raw audio staging.",
    )
    parser.add_argument(
        "--ensure-detections",
        action="store_true",
        help="Backfill each day into birdweather_tbl before audio work.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Worker count for the per-day detection backfill pre-pass.",
    )
    parser.add_argument(
        "--raw-workers",
        type=int,
        default=4,
        help="Worker count for raw-audio downloads.",
    )
    parser.add_argument(
        "--storage-retry-attempts",
        type=int,
        default=2,
        help="Retry count for processed-audio MinIO operations.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Skip the per-day existing-clip listing and reprocess/overwrite "
            "every clip. Avoids the slow recursive MinIO list that is "
            "pathological on spinning-disk storage; day-level resume via "
            "--state-path is unaffected."
        ),
    )
    parser.add_argument(
        "--prune-trust-processed",
        action="store_true",
        help=(
            "During pruning, trust that processed clips were uploaded (the "
            "process phase confirms this and aborts otherwise) instead of "
            "re-listing MinIO to verify. Confirms the local raw file exists, "
            "then deletes it. Use when MinIO is on slow storage where the "
            "verification list times out."
        ),
    )
    args = parser.parse_args(argv)
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date.")
    if args.workers <= 0:
        parser.error("--workers must be greater than zero.")
    if args.raw_workers <= 0:
        parser.error("--raw-workers must be greater than zero.")
    if args.storage_retry_attempts < 0:
        parser.error("--storage-retry-attempts cannot be negative.")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    from avian_biosurveillance.config.runtime import load_runtime_env

    load_runtime_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = BirdWeatherLocalBacklogConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        state_path=args.state_path,
        raw_root=args.raw_root,
        ensure_detections=bool(args.ensure_detections),
        workers=int(args.workers),
        raw_workers=int(args.raw_workers),
        storage_retry_attempts=int(args.storage_retry_attempts),
        force=bool(args.force),
        prune_trust_processed=bool(args.prune_trust_processed),
    )
    runner = BirdWeatherLocalBacklogRunner(config)
    try:
        runner.run()
    finally:
        runner.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())