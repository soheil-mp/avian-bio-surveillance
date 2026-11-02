"""Ensure the 2024-2025 historical ingestion and feature window is complete.

This module is intentionally CLI-driven. It does not depend on Dagster sensors;
it calls the existing ingestion, processing, and refresh entry points in
bounded batches and records resumable progress under ``data/processed/coverage``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg2.extras

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import (
    BUCKET_FEATURES,
    BUCKET_PROCESSED_AUDIO,
    BUCKET_RAW_AUDIO,
    AudioStore,
)
from avian_biosurveillance.ingestion.earth_engine_cadence import (
    ANCHOR_DAYS,
    anchor_dates_in_range,
)


logger = logging.getLogger(__name__)

VALID_SOURCES = frozenset(
    {
        "birdweather",
        "knmi",
        "ebird",
        "earth_engine_grid",
        "cds_drought",
        "sovon",
        "movebank",
        "seismic",
    }
)
DEFAULT_SOURCES = (
    "birdweather",
    "knmi",
    "ebird",
    "earth_engine_grid",
    "cds_drought",
    "sovon",
    "movebank",
    "seismic",
)
VALID_PHASES = frozenset(
    {"audit", "cleanup", "backfill", "audio", "features", "refresh", "verify", "all"}
)
ALL_PHASES = ("audit", "cleanup", "backfill", "audio", "features", "refresh", "verify")

_REPORT_ROOT = Path("data/processed/coverage")
_DEFAULT_STATE_PATH = _REPORT_ROOT / "ensure_historical_window_state.json"
_BIRDWEATHER_CLEANUP_BATCH_SIZE = 5_000
_BIRDWEATHER_STATION_CLEANUP_BATCH_SIZE = 5_000
_AUDIO_SUFFIXES = (".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg")
_FEATURE_FAILURE_FIELDS = (
    "missing_clip",
    "storage_unavailable",
    "decode_failed",
    "artifact_upload_failed",
    "background_failed",
    "embeddings_skipped",
)
_DATE_GRAIN_COLUMNS = frozenset({"observation_date", "observation_month"})


class VerificationFailure(RuntimeError):
    """Raised when the final verification report contains blocking failures."""

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        self.failures = failures
        super().__init__(f"Historical window verification failed: {len(failures)}")


@dataclass(frozen=True)
class Window:
    """Inclusive date window."""

    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True)
class HistoricalWindowConfig:
    """Runtime options for the historical-window operation."""

    start_date: date
    end_date: date
    sources: tuple[str, ...]
    batch_days: int
    workers: int
    delete_off_target: bool
    target: str
    grid_size_km: float
    state_path: Path


class OperationState:
    """Small JSON state file used for resumable CLI batches."""

    def __init__(self, path: Path, payload: dict[str, Any] | None = None) -> None:
        self.path = path
        self.payload = payload or {}
        self.payload.setdefault("completed", {})
        self.payload.setdefault("successful_zero_result_days", {})
        self.payload.setdefault("reports", [])

    @classmethod
    def load(cls, path: Path) -> OperationState:
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
            return cls(path, payload)
        return cls(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        with self.path.open("r+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                existing_text = fh.read().strip()
                existing_payload = json.loads(existing_text) if existing_text else {}
                self.payload = _merge_operation_state_payload(
                    existing_payload,
                    self.payload,
                )
                fh.seek(0)
                fh.truncate()
                json.dump(
                    self.payload,
                    fh,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                )
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def is_completed(self, key: str) -> bool:
        return bool(self.payload["completed"].get(key))

    def mark_completed(self, key: str) -> None:
        self.payload["completed"][key] = _utc_now()
        self.save()

    def record_zero_result_day(self, source: str, day: date) -> None:
        days = set(self.payload["successful_zero_result_days"].get(source, []))
        days.add(day.isoformat())
        self.payload["successful_zero_result_days"][source] = sorted(days)
        self.save()

    def zero_result_days(self, source: str) -> set[str]:
        return set(self.payload["successful_zero_result_days"].get(source, []))

    def record_report(self, report_path: Path) -> None:
        reports = list(self.payload.get("reports", []))
        reports.append(str(report_path))
        self.payload["reports"] = reports[-50:]
        self.save()


def _merge_operation_state_payload(
    existing_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing_payload)

    completed = dict(existing_payload.get("completed", {}))
    completed.update(incoming_payload.get("completed", {}))
    merged["completed"] = completed

    existing_zero = existing_payload.get("successful_zero_result_days", {})
    incoming_zero = incoming_payload.get("successful_zero_result_days", {})
    merged_zero: dict[str, list[str]] = {}
    for source in set(existing_zero) | set(incoming_zero):
        merged_zero[source] = sorted(
            set(existing_zero.get(source, [])) | set(incoming_zero.get(source, []))
        )
    merged["successful_zero_result_days"] = merged_zero

    reports: list[str] = []
    for report in list(existing_payload.get("reports", [])) + list(
        incoming_payload.get("reports", [])
    ):
        if report not in reports:
            reports.append(report)
    merged["reports"] = reports[-50:]

    for key, value in incoming_payload.items():
        if key in {"completed", "successful_zero_result_days", "reports"}:
            continue
        merged[key] = value

    return merged


class HistoricalWindowRunner:
    """Coordinates cleanup, backfill, audio processing, and verification."""

    def __init__(self, config: HistoricalWindowConfig) -> None:
        self.config = config
        self.state = OperationState.load(config.state_path)
        self.pool = ConnectionPool(min_conn=1, max_conn=4)
        self._store: AudioStore | None = None
        self.target_birdweather_ids = list(species_registry.birdweather_ids)
        self.target_ebird_codes = [
            code.strip().lower() for code in species_registry.ebird_codes if code
        ]
        self.target_movebank_taxa = [
            taxon.strip().lower() for taxon in species_registry.movebank_taxa if taxon
        ]

    @property
    def store(self) -> AudioStore:
        if self._store is None:
            self._store = AudioStore()
        return self._store

    def close(self) -> None:
        self.pool.close()

    def run(self, phases: Sequence[str]) -> None:
        for phase in phases:
            logger.info("Starting phase: %s", phase)
            if phase == "audit":
                self.audit()
            elif phase == "cleanup":
                self.cleanup()
            elif phase == "backfill":
                self.backfill()
            elif phase == "audio":
                self.audio()
            elif phase == "features":
                self.features()
            elif phase == "refresh":
                self.refresh()
            elif phase == "verify":
                self.verify()
            else:
                msg = f"Unsupported phase: {phase}"
                raise ValueError(msg)
            logger.info("Finished phase: %s", phase)

    # -- Audit ---------------------------------------------------------

    def audit(self) -> dict[str, Any]:
        report = self._base_report("audit")
        report["off_target"] = self._off_target_counts()
        report["coverage"] = self._coverage_report()
        report["birdweather_audio"] = {
            "mode": "deferred_to_audio_features_verify_phases"
        }
        self._write_report("audit", report)
        return report

    def _off_target_counts(self) -> dict[str, int]:
        self._require_target_config()
        return {
            "birdweather_tbl": self._birdweather_off_target_count(),
            "ebird_tbl": self._count(
                """
                SELECT count(*)
                FROM ebird_tbl
                WHERE observation_date BETWEEN %s AND %s
                  AND lower(species_code) <> ALL(%s)
                """,
                (
                    self.config.start_date,
                    self.config.end_date,
                    self.target_ebird_codes,
                ),
            ),
            "movebank_tbl": self._count(
                """
                SELECT count(*)
                FROM movebank_tbl
                WHERE timestamp::date BETWEEN %s AND %s
                  AND lower(COALESCE(individual_taxon_canonical_name, '')) <> ALL(%s)
                """,
                (
                    self.config.start_date,
                    self.config.end_date,
                    self.target_movebank_taxa,
                ),
            ),
            "species_tbl": self._count(
                """
                SELECT count(*)
                FROM species_tbl
                WHERE species_id <> ALL(%s)
                """,
                (self.target_birdweather_ids,),
            ),
            "species_tracker_tbl": self._count(
                """
                SELECT count(*)
                FROM species_tracker_tbl
                WHERE species_id IS NOT NULL
                  AND species_id <> ALL(%s)
                """,
                (self.target_birdweather_ids,),
            ),
        }

    def _birdweather_off_target_count(self) -> int:
        """Return ``1`` when any off-target BirdWeather row is present.

        Exact counts are emitted by the cleanup delete loop. Audit and verify
        only need a blocking zero/non-zero signal here; exact pre-counts require
        a full scan of the current multi-million-row hypertable.
        """
        return self._count(
            """
            SELECT CASE WHEN EXISTS (
                SELECT 1
                FROM birdweather_tbl
                WHERE species_id <> ALL(%s)
                LIMIT 1
            ) THEN 1 ELSE 0 END
            """,
            (self.target_birdweather_ids,),
        )

    def _coverage_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "expected_days": len(list(_iter_days(self.config.start_date, self.config.end_date))),
            "expected_months": len(
                list(_iter_months(self.config.start_date, self.config.end_date))
            ),
        }
        if "birdweather" in self.config.sources:
            report["birdweather"] = self._daily_source_coverage(
                "birdweather_tbl",
                "observation_date",
            )
        if "knmi" in self.config.sources:
            report["knmi"] = self._daily_source_coverage(
                "knmi_tbl",
                "observation_date",
            )
        if "ebird" in self.config.sources:
            report["ebird"] = self._daily_source_coverage(
                "ebird_tbl",
                "observation_date",
            )
        if "earth_engine_grid" in self.config.sources:
            report["earth_engine_grid"] = self._earth_engine_coverage()
        if "cds_drought" in self.config.sources:
            report["cds_drought"] = self._monthly_source_coverage(
                "cds_drought_tbl",
                "observation_month",
            )
        if "sovon" in self.config.sources:
            report["sovon"] = self._monthly_source_coverage(
                "sovon_tbl",
                "observation_date",
            )
        if "movebank" in self.config.sources:
            report["movebank"] = self._timestamp_source_coverage(
                "movebank_tbl",
                "timestamp",
            )
        if "seismic" in self.config.sources:
            report["seismic"] = self._timestamp_source_coverage(
                "seismic_tbl",
                "event_time",
            )
        return report

    # -- Cleanup -------------------------------------------------------

    def cleanup(self) -> dict[str, Any]:
        if not self.config.delete_off_target:
            logger.info("Cleanup phase skipped; --delete-off-target was not provided.")
            report = self._base_report("cleanup-skipped")
            report["off_target"] = self._off_target_counts()
            self._write_report("cleanup_skipped", report)
            return report

        self._require_target_config()
        report = self._base_report("cleanup")
        deleted: dict[str, Any] = {}
        deleted["birdweather"] = self._cleanup_off_target_birdweather()
        deleted["ebird_tbl"] = self._delete_sql(
            """
            DELETE FROM ebird_tbl
            WHERE observation_date BETWEEN %s AND %s
              AND lower(species_code) <> ALL(%s)
            """,
            (self.config.start_date, self.config.end_date, self.target_ebird_codes),
        )
        deleted["movebank_tbl"] = self._delete_sql(
            """
            DELETE FROM movebank_tbl
            WHERE timestamp::date BETWEEN %s AND %s
              AND lower(COALESCE(individual_taxon_canonical_name, '')) <> ALL(%s)
            """,
            (
                self.config.start_date,
                self.config.end_date,
                self.target_movebank_taxa,
            ),
        )
        deleted["species_tracker_tbl"] = self._delete_sql(
            """
            DELETE FROM species_tracker_tbl
            WHERE species_id IS NOT NULL
              AND species_id <> ALL(%s)
            """,
            (self.target_birdweather_ids,),
        )
        deleted["species_tbl"] = self._delete_sql(
            """
            DELETE FROM species_tbl
            WHERE species_id <> ALL(%s)
            """,
            (self.target_birdweather_ids,),
        )
        logger.info(
            "Skipping orphan BirdWeather station cleanup during off-target purge; run it separately after row cleanup if needed."
        )
        deleted["orphan_birdweather_stations_tbl"] = 0
        report["deleted"] = deleted
        logger.info(
            "Skipping expensive post-cleanup off-target recount during row purge; verify separately after cleanup if needed."
        )
        report["remaining_off_target"] = {}
        self._write_report("cleanup", report)
        return report

    def _cleanup_off_target_birdweather(self) -> dict[str, int]:
        deleted_rows = 0
        deleted_objects = 0
        scanned_rows = 0
        for day in _iter_days(self.config.start_date, self.config.end_date):
            if not self._has_off_target_birdweather_day(day):
                continue
            existing_refs = self._existing_birdweather_refs(day)
            if not any(existing_refs.values()):
                deleted_for_day = self._delete_off_target_birdweather_day_fast(day)
                if not deleted_for_day:
                    continue
                scanned_rows += deleted_for_day
                deleted_rows += deleted_for_day
                logger.info(
                    "BirdWeather cleanup progress: %s: %d day row(s), %d total row(s), %d MinIO object(s).",
                    day.isoformat(),
                    deleted_for_day,
                    deleted_rows,
                    deleted_objects,
                )
                continue

            rows = self._delete_off_target_birdweather_day(day)
            if not rows:
                continue
            scanned_rows += len(rows)
            for row in rows:
                deleted_objects += _delete_birdweather_objects(
                    self.store,
                    row,
                    existing_refs,
                )
            deleted_rows += len(rows)
            logger.info(
                "BirdWeather cleanup progress: %s: %d day row(s), %d total row(s), %d MinIO object(s).",
                day.isoformat(),
                len(rows),
                deleted_rows,
                deleted_objects,
            )

        return {
            "scanned_rows": scanned_rows,
            "deleted_rows": deleted_rows,
            "deleted_minio_objects": deleted_objects,
        }

    def _has_off_target_birdweather_day(self, day: date) -> bool:
        day_sql, day_params = self._birdweather_day_filter(day)
        with self.pool.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1
                FROM birdweather_tbl
                WHERE {day_sql}
                  AND species_id <> ALL(%s)
                LIMIT 1
                """,
                day_params + (self.target_birdweather_ids,),
            )
            return cur.fetchone() is not None

    def _existing_birdweather_refs(self, day: date) -> dict[str, set[str]]:
        date_str = day.isoformat()
        refs = {
            BUCKET_RAW_AUDIO: set(
                self.store.list_objects(
                    prefix=f"{date_str}/audio/",
                    bucket=BUCKET_RAW_AUDIO,
                )
            ),
            BUCKET_PROCESSED_AUDIO: set(
                self.store.list_objects(
                    prefix=f"birdweather/{date_str}/",
                    bucket=BUCKET_PROCESSED_AUDIO,
                )
            ),
            BUCKET_FEATURES: set(
                self.store.list_objects(
                    prefix=f"spectrograms/birdweather/{date_str}/",
                    bucket=BUCKET_FEATURES,
                )
            ),
        }
        refs[BUCKET_FEATURES].update(
            self.store.list_objects(
                prefix=f"waveforms/birdweather/{date_str}/",
                bucket=BUCKET_FEATURES,
            )
        )
        return refs

    def _delete_off_target_birdweather_day_fast(self, day: date) -> int:
        deleted_rows = 0
        while True:
            deleted_batch = self._delete_off_target_birdweather_day_fast_batch(
                day,
                _BIRDWEATHER_CLEANUP_BATCH_SIZE,
            )
            if deleted_batch == 0:
                return deleted_rows
            deleted_rows += deleted_batch

    def _delete_off_target_birdweather_day_fast_batch(
        self,
        day: date,
        batch_size: int,
    ) -> int:
        day_sql, day_params = self._birdweather_day_filter(day)
        with self.pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT detection_id, detected_at
                FROM birdweather_tbl
                WHERE {day_sql}
                  AND species_id <> ALL(%s)
                ORDER BY detected_at, detection_id
                LIMIT %s
                """,
                day_params + (self.target_birdweather_ids, batch_size),
            )
            rows = list(cur.fetchall())
        self._delete_birdweather_rows(rows)
        return len(rows)

    def _delete_off_target_birdweather_day(self, day: date) -> list[dict[str, Any]]:
        deleted_rows: list[dict[str, Any]] = []
        while True:
            batch = self._delete_off_target_birdweather_day_batch(
                day,
                _BIRDWEATHER_CLEANUP_BATCH_SIZE,
            )
            if not batch:
                return deleted_rows
            deleted_rows.extend(batch)

    def _delete_off_target_birdweather_day_batch(
        self,
        day: date,
        batch_size: int,
    ) -> list[dict[str, Any]]:
        day_sql, day_params = self._birdweather_day_filter(day)
        with self.pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    detection_id,
                    detected_at,
                    observation_date,
                    station_id,
                    species_id,
                    audio_filename,
                    audio_url,
                    has_processed_clip,
                    feature_artifacts
                FROM birdweather_tbl
                WHERE {day_sql}
                  AND species_id <> ALL(%s)
                ORDER BY detected_at, detection_id
                LIMIT %s
                """,
                day_params + (self.target_birdweather_ids, batch_size),
            )
            rows = list(cur.fetchall())
        self._delete_birdweather_rows(rows)
        return rows

    def _delete_birdweather_rows(self, rows: Sequence[dict[str, Any]]) -> int:
        keys = [(row["detection_id"], row["detected_at"]) for row in rows]
        if not keys:
            return 0
        with self.pool.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                DELETE FROM birdweather_tbl AS detections
                USING (VALUES %s) AS doomed(detection_id, detected_at)
                WHERE detections.detection_id = doomed.detection_id
                  AND detections.detected_at = doomed.detected_at::timestamptz
                """,
                keys,
                template="(%s, %s)",
            )
            return len(keys)

    def _delete_orphan_birdweather_stations(self) -> int:
        deleted_rows = 0
        while True:
            deleted_batch = self._delete_orphan_birdweather_stations_batch(
                _BIRDWEATHER_STATION_CLEANUP_BATCH_SIZE,
            )
            if deleted_batch == 0:
                return deleted_rows
            deleted_rows += deleted_batch

    def _delete_orphan_birdweather_stations_batch(self, batch_size: int) -> int:
        with self.pool.cursor() as cur:
            cur.execute(
                """
                SELECT stations.station_id
                FROM birdweather_stations_tbl AS stations
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM birdweather_tbl AS detections
                    WHERE detections.station_id = stations.station_id
                )
                ORDER BY stations.station_id
                LIMIT %s
                """,
                (batch_size,),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        keys = [(row[0],) for row in rows]
        with self.pool.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                DELETE FROM birdweather_stations_tbl AS stations
                USING (VALUES %s) AS doomed(station_id)
                WHERE stations.station_id = doomed.station_id
                """,
                keys,
                template="(%s)",
            )
        return len(keys)

    # -- Backfill ------------------------------------------------------

    def backfill(self) -> None:
        for source in self.config.sources:
            if source == "birdweather":
                self._backfill_birdweather()
            elif source == "knmi":
                self._backfill_knmi()
            elif source == "ebird":
                self._backfill_ebird()
            elif source == "earth_engine_grid":
                self._backfill_earth_engine_grid()
            elif source == "cds_drought":
                self._backfill_cds_drought()
            elif source == "sovon":
                self._backfill_sovon()
            elif source == "movebank":
                self._backfill_movebank()
            elif source == "seismic":
                self._backfill_seismic()

    def _backfill_birdweather(self) -> None:
        from avian_biosurveillance.orchestration.birdweather_flow import (
            backfill_birdweather_day,
            deduplicate_birdweather_detections,
        )

        for window in self._windows():
            key = self._completion_key("backfill", "birdweather", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed BirdWeather window %s.", window.key)
                continue
            for day in _iter_days(window.start, window.end):
                backfill_birdweather_day(
                    obs_date=day,
                    species_ids=self.target_birdweather_ids,
                    all_species=False,
                    workers=self.config.workers,
                    download_audio=False,
                    target=self.config.target,
                )
                if self._birdweather_day_count(day) == 0:
                    self.state.record_zero_result_day("birdweather", day)
            deduplicate_birdweather_detections(
                lookback_days=max((date.today() - self.config.start_date).days + 2, 2)
            )
            self.state.mark_completed(key)

    def _backfill_knmi(self) -> None:
        from avian_biosurveillance.orchestration.knmi_flow import run_knmi_repair_flow

        for window in self._windows():
            key = self._completion_key("backfill", "knmi", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed KNMI window %s.", window.key)
                continue
            run_knmi_repair_flow(
                start_date=window.start.isoformat(),
                end_date=window.end.isoformat(),
                target=self.config.target,
                force=True,
            )
            self._record_zero_result_days("knmi", window, self._knmi_day_count)
            self.state.mark_completed(key)

    def _backfill_ebird(self) -> None:
        from avian_biosurveillance.orchestration.ebird_flow import run_ebird_repair_flow

        for window in self._windows():
            key = self._completion_key("backfill", "ebird", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed eBird window %s.", window.key)
                continue
            run_ebird_repair_flow(
                start_date=window.start.isoformat(),
                end_date=window.end.isoformat(),
                region="NL",
                target_species_only=True,
                target=self.config.target,
            )
            self._record_zero_result_days("ebird", window, self._ebird_day_count)
            self.state.mark_completed(key)

    def _backfill_earth_engine_grid(self) -> None:
        from avian_biosurveillance.orchestration.earth_engine_flow import (
            backfill_earth_engine_grid_day,
        )

        for window in self._windows():
            key = self._completion_key("backfill", "earth_engine_grid", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed Earth Engine window %s.", window.key)
                continue
            anchors = anchor_dates_in_range(window.start, window.end)
            for day in anchors:
                backfill_earth_engine_grid_day(
                    obs_date=day,
                    force=True,
                    target=self.config.target,
                    grid_size_km=self.config.grid_size_km,
                    max_workers=max(1, min(self.config.workers, 4)),
                )
                if self._earth_engine_grid_day_count(day) == 0:
                    self.state.record_zero_result_day("earth_engine_grid", day)
            self.state.mark_completed(key)

    def _backfill_cds_drought(self) -> None:
        from avian_biosurveillance.orchestration.cds_drought_flow import (
            run_cds_drought_catchup_flow,
        )

        window = Window(self.config.start_date, self.config.end_date)
        key = self._completion_key("backfill", "cds_drought", window)
        if self.state.is_completed(key):
            logger.info("Skipping completed CDS drought backfill.")
            return
        run_cds_drought_catchup_flow(
            start_month=self.config.start_date.strftime("%Y-%m"),
            end_month=self.config.end_date.strftime("%Y-%m"),
            force=True,
            target=self.config.target,
            include_ensemble_members=True,
        )
        self.state.mark_completed(key)

    def _backfill_sovon(self) -> None:
        from avian_biosurveillance.orchestration.sovon_flow import run_sovon_flow

        window = Window(self.config.start_date, self.config.end_date)
        key = self._completion_key("backfill", "sovon", window)
        if self.state.is_completed(key):
            logger.info("Skipping completed Sovon backfill.")
            return
        run_sovon_flow(target=self.config.target)
        self.state.mark_completed(key)

    def _backfill_movebank(self) -> None:
        from avian_biosurveillance.orchestration.movebank_flow import (
            run_movebank_repair_flow,
        )

        for window in self._windows():
            key = self._completion_key("backfill", "movebank", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed Movebank window %s.", window.key)
                continue
            run_movebank_repair_flow(
                start_date=window.start.isoformat(),
                end_date=window.end.isoformat(),
                all_sensors=True,
                target=self.config.target,
            )
            self.state.mark_completed(key)

    def _backfill_seismic(self) -> None:
        from avian_biosurveillance.orchestration.knmi_flow import (
            run_seismic_repair_flow,
        )

        window = Window(self.config.start_date, self.config.end_date)
        key = self._completion_key("backfill", "seismic", window)
        if self.state.is_completed(key):
            logger.info("Skipping completed seismic backfill.")
            return
        run_seismic_repair_flow(
            start_date=self.config.start_date.isoformat(),
            end_date=self.config.end_date.isoformat(),
            target=self.config.target,
            min_magnitude=0.0,
        )
        self.state.mark_completed(key)

    # -- Audio / features ---------------------------------------------

    def audio(self) -> None:
        if "birdweather" not in self.config.sources:
            logger.info("Audio phase skipped because birdweather is not selected.")
            return

        from avian_biosurveillance.processing.birdweather_audio import (
            process_birdweather_audio_clips,
        )
        from avian_biosurveillance.processing.birdweather_raw_audio import (
            download_birdweather_raw_audio_objects,
        )

        for window in self._windows():
            key = self._completion_key("audio", "birdweather", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed audio window %s.", window.key)
                continue
            pending_days = [
                day
                for day in _iter_days(window.start, window.end)
                if not self._birdweather_audio_day_materialized(day)
            ]
            if not pending_days:
                logger.info(
                    "Skipping audio window %s because raw and processed BirdWeather folders are already materialized.",
                    window.key,
                )
                self.state.mark_completed(key)
                continue
            raw_stats = download_birdweather_raw_audio_objects(
                lookback_days=None,
                limit=None,
                start_date=min(pending_days),
                end_date=max(pending_days),
                workers=self.config.workers,
            )
            raw_upload_failed = int(getattr(raw_stats, "upload_failed", 0) or 0)
            _raise_for_birdweather_raw_audio_failures(
                f"BirdWeather raw audio {window.key}",
                raw_stats,
                ignore_upload_failed=raw_upload_failed > 0,
            )
            if raw_upload_failed:
                logger.warning(
                    "BirdWeather raw-audio window %s had %d upload_failed row(s); leaving window incomplete for retry.",
                    window.key,
                    raw_upload_failed,
                )
                continue
            window_storage_unavailable = 0
            for day in pending_days:
                audio_stats = process_birdweather_audio_clips(
                    lookback_days=None,
                    limit=None,
                    start_date=day,
                    end_date=day,
                    storage_retry_attempts=2,
                    continue_on_storage_unavailable=True,
                )
                window_storage_unavailable += int(
                    getattr(audio_stats, "storage_unavailable", 0) or 0
                )
                _raise_for_birdweather_audio_processing_failures(
                    f"BirdWeather audio processing {day.isoformat()}",
                    audio_stats,
                )
            if window_storage_unavailable:
                logger.warning(
                    "BirdWeather audio window %s had %d storage_unavailable row(s); leaving window incomplete for retry.",
                    window.key,
                    window_storage_unavailable,
                )
                continue
            self.state.mark_completed(key)

    def features(self) -> None:
        if "birdweather" not in self.config.sources:
            logger.info("Feature phase skipped because birdweather is not selected.")
            return

        from avian_biosurveillance.features.birdweather_features import (
            engineer_birdweather_audio_features,
        )

        for day in _iter_days(self.config.start_date, self.config.end_date):
            window = Window(day, day)
            key = self._completion_key("features", "birdweather", window)
            if self.state.is_completed(key):
                logger.info("Skipping completed feature day %s.", day.isoformat())
                continue
            if self._birdweather_feature_day_materialized(day):
                logger.info(
                    "Skipping feature day %s because feature folders are already materialized.",
                    day.isoformat(),
                )
                self.state.mark_completed(key)
                continue
            stats = engineer_birdweather_audio_features(
                lookback_days=None,
                limit=None,
                start_date=day,
                end_date=day,
                storage_retry_attempts=2,
                continue_on_storage_unavailable=False,
            )
            _raise_for_stat_failures(
                f"BirdWeather feature engineering {day.isoformat()}",
                stats,
                _FEATURE_FAILURE_FIELDS,
            )
            self.state.mark_completed(key)

    # -- Refresh -------------------------------------------------------

    def refresh(self) -> None:
        from avian_biosurveillance.orchestration.data_quality_flow import (
            run_quality_audit_task,
        )
        from avian_biosurveillance.orchestration.derived_aggregates_flow import (
            refresh_movebank_tracks_task,
            refresh_species_dim_task,
            refresh_species_tracker_task,
        )
        from avian_biosurveillance.orchestration.station_summary_flow import (
            refresh_station_summary_task,
        )

        window = Window(self.config.start_date, self.config.end_date)
        key = self._completion_key("refresh", "all", window)
        if self.state.is_completed(key):
            logger.info("Skipping completed refresh phase.")
            return

        refresh_station_summary_task()
        refresh_species_dim_task()
        refresh_movebank_tracks_task()
        refresh_species_tracker_task(
            start_date=self.config.start_date,
            end_date=self.config.end_date,
        )
        run_quality_audit_task()
        self.state.mark_completed(key)

    # -- Verify --------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        report = self._base_report("verify")
        failures: list[dict[str, Any]] = []

        off_target = self._off_target_counts()
        report["off_target"] = off_target
        for table, count in off_target.items():
            if count:
                _add_failure(
                    failures,
                    "off_target_rows",
                    count,
                    {"table": table},
                )

        coverage = self._coverage_report()
        report["coverage"] = coverage
        self._verify_daily_coverage(failures)
        self._verify_monthly_coverage(failures)

        bw_audio = self._birdweather_audio_db_report(exact=False)
        report["birdweather_audio"] = bw_audio
        if "birdweather" in self.config.sources:
            for check, count in bw_audio.items():
                if count:
                    _add_failure(
                        failures,
                        f"birdweather_{check}",
                        count,
                        {},
                    )
            object_report = self._verify_birdweather_objects(failures)
            report["birdweather_objects"] = object_report

        report["failures"] = failures
        self._write_report("verify", report)
        if failures:
            raise VerificationFailure(failures)
        logger.info("Verification passed for %s..%s.", self.config.start_date, self.config.end_date)
        return report

    def _verify_daily_coverage(self, failures: list[dict[str, Any]]) -> None:
        checks = {
            "birdweather": ("birdweather_tbl", "observation_date"),
            "knmi": ("knmi_tbl", "observation_date"),
            "ebird": ("ebird_tbl", "observation_date"),
        }
        for source, (table, column) in checks.items():
            if source not in self.config.sources:
                continue
            missing = set(self._missing_daily_dates(table, column))
            unresolved = missing - self.state.zero_result_days(source)
            if unresolved:
                _add_failure(
                    failures,
                    f"{source}_missing_days",
                    len(unresolved),
                    {"sample": sorted(unresolved)[:20]},
                )

        if "earth_engine_grid" not in self.config.sources:
            return
        anchors = self._earth_engine_anchor_dates()
        missing = {
            day.isoformat()
            for day in self._missing_earth_engine_anchor_dates(anchors)
        }
        unresolved = missing - self.state.zero_result_days("earth_engine_grid")
        if unresolved:
            _add_failure(
                failures,
                "earth_engine_grid_missing_days",
                len(unresolved),
                {"sample": sorted(unresolved)[:20]},
            )
        incomplete = self._earth_engine_incomplete_days()
        if incomplete:
            _add_failure(
                failures,
                "earth_engine_grid_incomplete_days",
                len(incomplete),
                {"sample": incomplete[:20]},
            )

    def _verify_monthly_coverage(self, failures: list[dict[str, Any]]) -> None:
        expected = {month.isoformat() for month in _iter_months(self.config.start_date, self.config.end_date)}
        if "cds_drought" in self.config.sources:
            present = set(self._present_months("cds_drought_tbl", "observation_month"))
            missing = expected - present
            if missing:
                _add_failure(
                    failures,
                    "cds_drought_missing_months",
                    len(missing),
                    {"sample": sorted(missing)[:20]},
                )
        if "sovon" in self.config.sources:
            present = set(self._present_months("sovon_tbl", "observation_date"))
            missing = expected - present
            if missing:
                _add_failure(
                    failures,
                    "sovon_missing_months",
                    len(missing),
                    {"sample": sorted(missing)[:20]},
                )

    def _verify_birdweather_objects(
        self,
        failures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        report = {
            "raw_missing": 0,
            "processed_missing": 0,
            "feature_artifact_missing": 0,
            "samples": {
                "raw_missing": [],
                "processed_missing": [],
                "feature_artifact_missing": [],
            },
        }
        for day in _iter_days(self.config.start_date, self.config.end_date):
            raw_keys = set(
                self.store.list_objects(
                    prefix=f"{day.isoformat()}/audio/",
                    bucket=BUCKET_RAW_AUDIO,
                )
            )
            processed_keys = set(
                self.store.list_objects(
                    prefix=f"birdweather/{day.isoformat()}/",
                    bucket=BUCKET_PROCESSED_AUDIO,
                )
            )
            feature_keys = set(
                self.store.list_objects(
                    prefix=f"spectrograms/birdweather/{day.isoformat()}/",
                    bucket=BUCKET_FEATURES,
                )
            )
            feature_keys.update(
                self.store.list_objects(
                    prefix=f"waveforms/birdweather/{day.isoformat()}/",
                    bucket=BUCKET_FEATURES,
                )
            )

            for row in self._birdweather_object_rows(day):
                detection_id = str(row["detection_id"])
                if _birdweather_row_requires_raw_object(row) and not any(
                    key in raw_keys for key in _candidate_raw_keys(row)
                ):
                    _increment_object_failure(report, "raw_missing", detection_id)

                if _is_processable_birdweather_row(row):
                    processed_key = _processed_clip_key(row)
                    if processed_key not in processed_keys:
                        _increment_object_failure(
                            report,
                            "processed_missing",
                            detection_id,
                        )

                refs = _feature_object_refs(row.get("feature_artifacts"))
                feature_refs = [key for bucket, key in refs if bucket == BUCKET_FEATURES]
                if row.get("has_processed_clip") and (
                    not feature_refs or any(key not in feature_keys for key in feature_refs)
                ):
                    _increment_object_failure(
                        report,
                        "feature_artifact_missing",
                        detection_id,
                    )

        for check in (
            "raw_missing",
            "processed_missing",
            "feature_artifact_missing",
        ):
            count = int(report[check])
            if count:
                _add_failure(
                    failures,
                    f"birdweather_object_{check}",
                    count,
                    {"sample": report["samples"][check]},
                )
        return report

    # -- SQL reports and helpers --------------------------------------

    def _birdweather_audio_db_report(self, *, exact: bool) -> dict[str, int]:
        query_func = self._count if exact else self._presence_count
        return {
            "audio_url_missing_raw_filename": query_func(
                """
                SELECT count(*)
                FROM birdweather_tbl
                WHERE observation_date BETWEEN %s AND %s
                  AND audio_url IS NOT NULL
                  AND NULLIF(audio_filename, '') IS NULL
                """,
                (self.config.start_date, self.config.end_date),
            ),
            "processable_missing_processed_clip": query_func(
                """
                SELECT count(*)
                FROM birdweather_tbl
                WHERE observation_date BETWEEN %s AND %s
                  AND audio_url IS NOT NULL
                  AND audio_start_time_s IS NOT NULL
                  AND audio_end_time_s IS NOT NULL
                  AND audio_end_time_s > audio_start_time_s
                  AND NOT has_processed_clip
                """,
                (self.config.start_date, self.config.end_date),
            ),
            "processed_missing_features": query_func(
                """
                SELECT count(*)
                FROM birdweather_tbl
                WHERE observation_date BETWEEN %s AND %s
                  AND has_processed_clip
                  AND (
                    feature_artifacts IS NULL
                    OR feature_artifacts = '{}'::jsonb
                    OR background_sound_clf IS NULL
                    OR COALESCE(background_sound_clf ->> 'status', '') = 'classification_failed'
                    OR birdnet_embedding IS NULL
                    OR perch_v2_embedding IS NULL
                  )
                """,
                (self.config.start_date, self.config.end_date),
            ),
        }

    def _daily_source_coverage(self, table: str, column: str) -> dict[str, Any]:
        if table == "birdweather_tbl":
            return self._birdweather_chunk_coverage()
        missing_days = self._missing_daily_dates(table, column)
        report: dict[str, Any] = {
            "present_days": self._expected_day_count() - len(missing_days),
            "missing_days": missing_days,
            "min_date": self._min_date(table, column),
            "max_date": self._max_date(table, column),
        }
        report["row_count"] = self._table_window_count(table, column)
        return report

    def _birdweather_chunk_coverage(self) -> dict[str, Any]:
        with self.pool.cursor() as cur:
            cur.execute(
                """
                SELECT
                    range_start::date AS chunk_day
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = 'public'
                  AND hypertable_name = 'birdweather_tbl'
                  AND range_start::date <= %s
                  AND (range_end::date - 1) >= %s
                ORDER BY chunk_day
                """,
                (self.config.end_date, self.config.start_date),
            )
            present = {
                max(self.config.start_date, row[0]).isoformat()
                for row in cur.fetchall()
            }
        expected = {day.isoformat() for day in _iter_days(self.config.start_date, self.config.end_date)}
        missing = sorted(expected - present)
        return {
            "present_days": len(expected) - len(missing),
            "missing_days": missing,
            "min_date": min(present) if present else None,
            "max_date": max(present) if present else None,
            "row_count_estimate": self._estimated_table_count("birdweather_tbl"),
            "coverage_source": "timescaledb_chunk_metadata",
        }

    def _earth_engine_coverage(self) -> dict[str, Any]:
        anchors = self._earth_engine_anchor_dates()
        missing = self._missing_earth_engine_anchor_dates(anchors)
        base: dict[str, Any] = {
            "present_days": len(anchors) - len(missing),
            "missing_days": [day.isoformat() for day in missing],
            "min_date": self._min_date(
                "google_earth_engine_tbl", "observation_date"
            ),
            "max_date": self._max_date(
                "google_earth_engine_tbl", "observation_date"
            ),
            "row_count": self._table_window_count(
                "google_earth_engine_tbl", "observation_date"
            ),
            "cadence": "biweekly",
            "anchor_days": list(ANCHOR_DAYS),
            "expected_anchor_days": len(anchors),
        }
        base["grid_system"] = _earth_engine_grid_system(self.config.grid_size_km)
        base["expected_cells_per_day"] = self._earth_engine_expected_cell_count()
        base["incomplete_days"] = self._earth_engine_incomplete_days()
        return base

    def _monthly_source_coverage(self, table: str, column: str) -> dict[str, Any]:
        present = self._present_months(table, column)
        expected = {month.isoformat() for month in _iter_months(self.config.start_date, self.config.end_date)}
        return {
            "present_months": len(present),
            "missing_months": sorted(expected - set(present)),
            "row_count": self._table_window_count(table, column),
            "months": present,
        }

    def _timestamp_source_coverage(self, table: str, column: str) -> dict[str, Any]:
        return {
            "row_count": self._table_window_count(table, column),
            "present_days": self._count_distinct_dates(table, column),
            "min_date": self._min_date(table, column),
            "max_date": self._max_date(table, column),
        }

    def _count(self, sql: str, params: Sequence[Any]) -> int:
        with self.pool.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return int(row[0] if row else 0)

    def _presence_count(self, sql: str, params: Sequence[Any]) -> int:
        inner = sql.replace("SELECT count(*)", "SELECT 1", 1)
        presence_sql = f"SELECT CASE WHEN EXISTS ({inner}) THEN 1 ELSE 0 END"
        return self._count(presence_sql, params)

    def _delete_sql(self, sql: str, params: Sequence[Any]) -> int:
        with self.pool.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.rowcount or 0)

    def _table_window_count(self, table: str, column: str) -> int:
        date_expr = _date_sql_expr(column)
        sql = (
            f"SELECT count(*) FROM {table} "  # noqa: S608
            f"WHERE {date_expr} BETWEEN %s AND %s"
        )
        return self._count(sql, (self.config.start_date, self.config.end_date))

    def _count_distinct_dates(self, table: str, column: str) -> int:
        date_expr = _date_sql_expr(column)
        sql = (
            f"SELECT count(DISTINCT {date_expr}) FROM {table} "  # noqa: S608
            f"WHERE {date_expr} BETWEEN %s AND %s"
        )
        return self._count(sql, (self.config.start_date, self.config.end_date))

    def _min_date(self, table: str, column: str) -> str | None:
        date_expr = _date_sql_expr(column)
        sql = (
            f"SELECT min({date_expr}) FROM {table} "  # noqa: S608
            f"WHERE {date_expr} BETWEEN %s AND %s"
        )
        with self.pool.cursor() as cur:
            cur.execute(sql, (self.config.start_date, self.config.end_date))
            row = cur.fetchone()
        return row[0].isoformat() if row and row[0] else None

    def _max_date(self, table: str, column: str) -> str | None:
        date_expr = _date_sql_expr(column)
        sql = (
            f"SELECT max({date_expr}) FROM {table} "  # noqa: S608
            f"WHERE {date_expr} BETWEEN %s AND %s"
        )
        with self.pool.cursor() as cur:
            cur.execute(sql, (self.config.start_date, self.config.end_date))
            row = cur.fetchone()
        return row[0].isoformat() if row and row[0] else None

    def _missing_daily_dates(self, table: str, column: str) -> list[str]:
        date_expr = _date_sql_expr(column)
        sql = (
            "WITH expected(day) AS ("  # noqa: S608
            "  SELECT generate_series(%s::date, %s::date, interval '1 day')::date"
            ") "
            "SELECT expected.day "
            "FROM expected "
            "WHERE NOT EXISTS ("
            f"  SELECT 1 FROM {table} "
            f"  WHERE {date_expr} = expected.day"
            ") "
            "ORDER BY expected.day"
        )
        with self.pool.cursor() as cur:
            cur.execute(
                sql,
                (
                    self.config.start_date,
                    self.config.end_date,
                ),
            )
            rows = cur.fetchall()
        return [row[0].isoformat() for row in rows]

    def _estimated_table_count(self, table: str) -> int:
        with self.pool.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(sum(cls.reltuples), 0)::bigint
                FROM _timescaledb_catalog.hypertable AS ht
                JOIN _timescaledb_catalog.chunk AS ch
                  ON ch.hypertable_id = ht.id
                JOIN pg_namespace AS ns
                  ON ns.nspname = ch.schema_name
                JOIN pg_class AS cls
                  ON cls.relnamespace = ns.oid
                 AND cls.relname = ch.table_name
                WHERE ht.table_name = %s
                """,
                (table,),
            )
            row = cur.fetchone()
        return int(row[0] if row else 0)

    def _expected_day_count(self) -> int:
        return (self.config.end_date - self.config.start_date).days + 1

    def _present_months(self, table: str, column: str) -> list[str]:
        date_expr = _date_sql_expr(column)
        sql = (
            f"SELECT DISTINCT date_trunc('month', {column})::date AS month "  # noqa: S608
            f"FROM {table} "
            f"WHERE {date_expr} BETWEEN %s AND %s "
            "ORDER BY month"
        )
        with self.pool.cursor() as cur:
            cur.execute(sql, (self.config.start_date, self.config.end_date))
            rows = cur.fetchall()
        return [row[0].isoformat() for row in rows]

    def _earth_engine_expected_cell_count(self) -> int:
        return self._count(
            """
            SELECT count(*)
            FROM grid_cells_tbl
            WHERE grid_system = %s
            """,
            (_earth_engine_grid_system(self.config.grid_size_km),),
        )

    def _earth_engine_anchor_dates(self) -> list[date]:
        """Bi-weekly anchor dates (1st/15th) within the configured window."""
        return anchor_dates_in_range(self.config.start_date, self.config.end_date)

    def _missing_earth_engine_anchor_dates(
        self, anchors: Sequence[date]
    ) -> list[date]:
        """Return anchor dates with no Earth Engine grid rows present."""
        if not anchors:
            return []
        with self.pool.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT observation_date
                FROM google_earth_engine_tbl
                WHERE observation_date = ANY(%s::date[])
                """,
                (list(anchors),),
            )
            present = {row[0] for row in cur.fetchall()}
        return [day for day in anchors if day not in present]

    def _earth_engine_incomplete_days(self) -> list[dict[str, Any]]:
        expected_cells = self._earth_engine_expected_cell_count()
        if expected_cells == 0:
            return []
        with self.pool.cursor() as cur:
            cur.execute(
                """
                SELECT observation_date, count(*) AS rows_present
                FROM google_earth_engine_tbl AS gee
                JOIN grid_cells_tbl AS cells USING (grid_cell_id)
                WHERE gee.observation_date BETWEEN %s AND %s
                  AND cells.grid_system = %s
                GROUP BY observation_date
                HAVING count(*) < %s
                ORDER BY observation_date
                """,
                (
                    self.config.start_date,
                    self.config.end_date,
                    _earth_engine_grid_system(self.config.grid_size_km),
                    expected_cells,
                ),
            )
            rows = cur.fetchall()
        return [
            {
                "date": row[0].isoformat(),
                "rows_present": int(row[1]),
                "expected_rows": expected_cells,
            }
            for row in rows
        ]

    def _birdweather_object_rows(self, day: date) -> list[dict[str, Any]]:
        day_sql, day_params = self._birdweather_day_filter(day)
        with self.pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    detection_id,
                    observation_date,
                    station_id,
                    species_id,
                    audio_filename,
                    audio_url,
                    audio_start_time_s,
                    audio_end_time_s,
                    has_processed_clip,
                    feature_artifacts
                FROM birdweather_tbl
                WHERE {day_sql}
                  AND (
                    audio_url IS NOT NULL
                    OR has_processed_clip
                    OR feature_artifacts IS NOT NULL
                  )
                """,
                day_params,
            )
            return list(cur.fetchall())

    def _birdweather_day_count(self, day: date) -> int:
        day_sql, day_params = self._birdweather_day_filter(day)
        return self._count(
            f"SELECT count(*) FROM birdweather_tbl WHERE {day_sql}",
            day_params,
        )

    def _birdweather_audio_day_status(self, day: date) -> dict[str, int]:
        day_sql, day_params = self._birdweather_day_filter(day)
        with self.pool.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE audio_url IS NOT NULL) AS rows_with_audio_url,
                    count(*) FILTER (
                        WHERE audio_url IS NOT NULL
                          AND NULLIF(audio_filename, '') IS NULL
                    ) AS missing_raw_filename,
                                        count(*) FILTER (
                                                WHERE audio_url IS NOT NULL
                                                    AND NOT has_processed_clip
                                        ) AS raw_retention_required_rows,
                    count(*) FILTER (
                        WHERE audio_url IS NOT NULL
                          AND audio_start_time_s IS NOT NULL
                          AND audio_end_time_s IS NOT NULL
                          AND audio_end_time_s > audio_start_time_s
                    ) AS processable_rows,
                    count(*) FILTER (
                        WHERE audio_url IS NOT NULL
                          AND audio_start_time_s IS NOT NULL
                          AND audio_end_time_s IS NOT NULL
                          AND audio_end_time_s > audio_start_time_s
                          AND NOT has_processed_clip
                    ) AS missing_processed_clip
                FROM birdweather_tbl
                WHERE {day_sql}
                """,
                day_params,
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
        return {
            "rows_with_audio_url": int(row[0] or 0),
            "missing_raw_filename": int(row[1] or 0),
            "raw_retention_required_rows": int(row[2] or 0),
            "processable_rows": int(row[3] or 0),
            "missing_processed_clip": int(row[4] or 0),
        }

    def _birdweather_feature_day_status(self, day: date) -> dict[str, int]:
        day_sql, day_params = self._birdweather_day_filter(day)
        with self.pool.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE has_processed_clip) AS processed_rows,
                    count(*) FILTER (
                        WHERE has_processed_clip
                          AND (
                            feature_artifacts IS NULL
                                                        OR feature_artifacts = '{{}}'::jsonb
                            OR background_sound_clf IS NULL
                            OR COALESCE(background_sound_clf ->> 'status', '') = 'classification_failed'
                            OR birdnet_embedding IS NULL
                            OR perch_v2_embedding IS NULL
                          )
                    ) AS missing_features
                FROM birdweather_tbl
                WHERE {day_sql}
                """,
                day_params,
            )
            row = cur.fetchone() or (0, 0)
        return {
            "processed_rows": int(row[0] or 0),
            "missing_features": int(row[1] or 0),
        }

    def _birdweather_day_filter(self, day: date) -> tuple[str, tuple[datetime, datetime, date]]:
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        return (
            "detected_at >= %s AND detected_at < %s AND observation_date = %s",
            (day_start, day_end, day),
        )

    def _prefix_has_objects(self, prefix: str, *, bucket: str) -> bool:
        return self.store.has_objects(prefix=prefix, bucket=bucket, recursive=True)

    def _birdweather_audio_day_materialized(self, day: date) -> bool:
        status = self._birdweather_audio_day_status(day)
        if status["rows_with_audio_url"] == 0 and status["processable_rows"] == 0:
            return True
        if status["missing_raw_filename"] or status["missing_processed_clip"]:
            return False

        date_str = day.isoformat()
        raw_ready = (
            status["rows_with_audio_url"] == 0
            or status["raw_retention_required_rows"] == 0
            or self._prefix_has_objects(
                f"{date_str}/audio/",
                bucket=BUCKET_RAW_AUDIO,
            )
        )
        processed_ready = status["processable_rows"] == 0 or self._prefix_has_objects(
            f"birdweather/{date_str}/",
            bucket=BUCKET_PROCESSED_AUDIO,
        )
        return raw_ready and processed_ready

    def _birdweather_feature_day_materialized(self, day: date) -> bool:
        status = self._birdweather_feature_day_status(day)
        if status["processed_rows"] == 0:
            return True
        if status["missing_features"]:
            return False

        date_str = day.isoformat()
        return self._prefix_has_objects(
            f"spectrograms/birdweather/{date_str}/",
            bucket=BUCKET_FEATURES,
        ) or self._prefix_has_objects(
            f"waveforms/birdweather/{date_str}/",
            bucket=BUCKET_FEATURES,
        )

    def _knmi_day_count(self, day: date) -> int:
        return self._count(
            "SELECT count(*) FROM knmi_tbl WHERE observation_date = %s",
            (day,),
        )

    def _ebird_day_count(self, day: date) -> int:
        return self._count(
            "SELECT count(*) FROM ebird_tbl WHERE observation_date = %s",
            (day,),
        )

    def _earth_engine_grid_day_count(self, day: date) -> int:
        return self._count(
            """
            SELECT count(*)
            FROM google_earth_engine_tbl
            WHERE observation_date = %s
            """,
            (day,),
        )

    # -- Small helpers -------------------------------------------------

    def _record_zero_result_days(
        self,
        source: str,
        window: Window,
        count_func: Any,
    ) -> None:
        for day in _iter_days(window.start, window.end):
            if count_func(day) == 0:
                self.state.record_zero_result_day(source, day)

    def _windows(self) -> Iterable[Window]:
        return _date_windows(
            self.config.start_date,
            self.config.end_date,
            self.config.batch_days,
        )

    def _completion_key(self, phase: str, source: str, window: Window) -> str:
        return f"{phase}:{source}:{window.key}"

    def _base_report(self, phase: str) -> dict[str, Any]:
        return {
            "phase": phase,
            "generated_at": _utc_now(),
            "start_date": self.config.start_date.isoformat(),
            "end_date": self.config.end_date.isoformat(),
            "sources": list(self.config.sources),
            "target_species": {
                "birdweather_ids": self.target_birdweather_ids,
                "ebird_codes": self.target_ebird_codes,
                "movebank_taxa": self.target_movebank_taxa,
            },
        }

    def _write_report(self, phase: str, report: dict[str, Any]) -> Path:
        report_root = self.config.state_path.parent
        report_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = report_root / f"ensure_historical_window_{phase}_{stamp}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True, default=_json_default)
        self.state.record_report(path)
        logger.info("Wrote %s report to %s.", phase, path)
        return path

    def _require_target_config(self) -> None:
        if not self.target_birdweather_ids:
            msg = "No target BirdWeather species IDs configured."
            raise RuntimeError(msg)
        if not self.target_ebird_codes:
            msg = "No target eBird species codes configured."
            raise RuntimeError(msg)
        if not self.target_movebank_taxa:
            msg = "No target Movebank taxa configured."
            raise RuntimeError(msg)


def parse_sources(raw: str) -> tuple[str, ...]:
    """Parse and validate a comma-separated source list."""
    sources = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not sources:
        msg = "At least one source is required."
        raise ValueError(msg)
    unknown = sorted(set(sources) - VALID_SOURCES)
    if unknown:
        msg = f"Unknown source(s): {', '.join(unknown)}"
        raise ValueError(msg)
    return sources


def expand_phases(phase: str) -> tuple[str, ...]:
    """Expand the CLI phase selector into concrete phases."""
    if phase not in VALID_PHASES:
        msg = f"Unknown phase: {phase}"
        raise ValueError(msg)
    if phase == "all":
        return ALL_PHASES
    return (phase,)


def _date_windows(start: date, end: date, batch_days: int) -> Iterable[Window]:
    if batch_days < 1:
        msg = "batch_days must be >= 1"
        raise ValueError(msg)
    current = start
    while current <= end:
        window_end = min(end, current + timedelta(days=batch_days - 1))
        yield Window(current, window_end)
        current = window_end + timedelta(days=1)


def _iter_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _iter_months(start: date, end: date) -> Iterable[date]:
    current = start.replace(day=1)
    end_month = end.replace(day=1)
    while current <= end_month:
        yield current
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def _delete_birdweather_objects(
    store: AudioStore,
    row: dict[str, Any],
    existing_refs: dict[str, set[str]] | None = None,
) -> int:
    deleted = 0
    for bucket, key in _birdweather_object_refs(row):
        if existing_refs is not None and key not in existing_refs.get(bucket, set()):
            continue
        if store.delete_object(key, bucket=bucket, missing_ok=True):
            deleted += 1
            if existing_refs is not None:
                existing_refs.get(bucket, set()).discard(key)
    return deleted


def _birdweather_object_refs(row: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if row.get("audio_filename"):
        refs.extend((BUCKET_RAW_AUDIO, key) for key in _candidate_raw_keys(row))
    if row.get("has_processed_clip"):
        refs.append((BUCKET_PROCESSED_AUDIO, _processed_clip_key(row)))
    if row.get("feature_artifacts"):
        refs.extend(_canonical_feature_refs(row))
        refs.extend(_feature_object_refs(row.get("feature_artifacts")))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for bucket, key in refs:
        ref = (bucket, key)
        if key and ref not in seen:
            seen.add(ref)
            deduped.append(ref)
    return deduped


def _candidate_raw_keys(row: dict[str, Any]) -> list[str]:
    observation_date = _row_date_str(row)
    detection_id = str(row.get("detection_id") or "")
    species_id = str(row.get("species_id") or "")
    names: list[str] = []
    audio_filename = row.get("audio_filename")
    if audio_filename:
        names.append(PurePosixPath(str(audio_filename)).name)
    suffix = _audio_extension(str(row.get("audio_url") or ""))
    if detection_id and species_id:
        names.append(f"{detection_id}_{species_id}{suffix}")
    if detection_id:
        names.extend(f"{detection_id}_{species_id}{item}" for item in _AUDIO_SUFFIXES)
        names.extend(f"{detection_id}{item}" for item in _AUDIO_SUFFIXES)

    keys: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        keys.append(str(PurePosixPath(observation_date, "audio", name)))
    return keys


def _birdweather_row_requires_raw_object(row: dict[str, Any]) -> bool:
    return bool(row.get("audio_url")) and not bool(row.get("has_processed_clip"))


def _processed_clip_key(row: dict[str, Any]) -> str:
    return str(
        PurePosixPath(
            "birdweather",
            _row_date_str(row),
            str(row.get("station_id") or "unknown_station"),
            f"{row.get('detection_id')}.flac",
        )
    )


def _canonical_feature_refs(row: dict[str, Any]) -> list[tuple[str, str]]:
    base = PurePosixPath(
        "birdweather",
        _row_date_str(row),
        str(row.get("station_id") or "unknown_station"),
    )
    detection_id = str(row.get("detection_id"))
    return [
        (BUCKET_FEATURES, str(PurePosixPath("spectrograms", base, f"{detection_id}.png"))),
        (BUCKET_FEATURES, str(PurePosixPath("waveforms", base, f"{detection_id}.png"))),
        (
            BUCKET_FEATURES,
            str(PurePosixPath("waveforms", base, f"{detection_id}_smooth.png")),
        ),
    ]


def _feature_object_refs(feature_artifacts: Any) -> list[tuple[str, str]]:
    """Extract MinIO object refs from a BirdWeather feature manifest."""
    if isinstance(feature_artifacts, str):
        try:
            feature_artifacts = json.loads(feature_artifacts)
        except json.JSONDecodeError:
            return []
    if not isinstance(feature_artifacts, dict):
        return []

    refs: list[tuple[str, str]] = []
    for value in _walk_json_values(feature_artifacts):
        if not isinstance(value, str):
            continue
        ref = _object_ref_from_manifest_value(value)
        if ref:
            refs.append(ref)
    return refs


def _object_ref_from_manifest_value(value: str) -> tuple[str, str] | None:
    normalized = value.strip()
    for bucket in (BUCKET_RAW_AUDIO, BUCKET_PROCESSED_AUDIO, BUCKET_FEATURES):
        prefix = f"{bucket}/"
        if normalized.startswith(prefix):
            return bucket, normalized[len(prefix) :]
    if normalized.startswith(("spectrograms/", "waveforms/")):
        return BUCKET_FEATURES, normalized
    if normalized.startswith("birdweather/"):
        return BUCKET_PROCESSED_AUDIO, normalized
    return None


def _walk_json_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_values(item)
    else:
        yield value


def _row_date_str(row: dict[str, Any]) -> str:
    value = row.get("observation_date")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _audio_extension(url: str) -> str:
    lowered = url.lower()
    for suffix in _AUDIO_SUFFIXES:
        if suffix in lowered:
            return suffix
    return ".flac"


def _is_processable_birdweather_row(row: dict[str, Any]) -> bool:
    start = row.get("audio_start_time_s")
    end = row.get("audio_end_time_s")
    if not row.get("audio_url") or start is None or end is None:
        return False
    try:
        return float(end) > float(start)
    except (TypeError, ValueError):
        return False


def _increment_object_failure(
    report: dict[str, Any],
    check: str,
    detection_id: str,
) -> None:
    report[check] += 1
    samples = report["samples"][check]
    if len(samples) < 20:
        samples.append(detection_id)


def _raise_for_stat_failures(
    label: str,
    stats: object | None,
    fields: tuple[str, ...],
) -> None:
    failures: dict[str, int] = {}
    for field in fields:
        count = int(getattr(stats, field, 0) or 0)
        if count:
            failures[field] = count
    if failures:
        msg = f"{label} reported blocking failure counters: {failures}"
        raise RuntimeError(msg)


def _raise_for_birdweather_raw_audio_failures(
    label: str,
    stats: object | None,
    *,
    ignore_upload_failed: bool = False,
) -> None:
    """Raise only for blocking BirdWeather raw-audio historical failures.

    Older BirdWeather CDN objects are often archived and can no longer be
    downloaded. Audit those rows without aborting the rest of the historical
    repair window. Treat `missing_file`-only downloader outcomes the same way.
    Callers can ignore `upload_failed` when transient storage unavailability
    should leave the current window incomplete for retry instead of aborting the
    entire historical slice.
    """
    archived = int(getattr(stats, "archived", 0) or 0)
    if archived:
        logger.warning(
            "%s reported %d archived raw-audio row(s); continuing historical repair.",
            label,
            archived,
        )

    failures: dict[str, int] = {}
    fields = [
        "failed",
        "not_found",
        "forbidden",
        "server_error",
        "timeout",
        "download_error",
        "max_retries",
    ]
    if not ignore_upload_failed:
        fields.insert(1, "upload_failed")

    for field in fields:
        count = int(getattr(stats, field, 0) or 0)
        if count:
            failures[field] = count

    missing_file = int(getattr(stats, "missing_file", 0) or 0)
    if missing_file and "failed" in failures:
        remaining_failed = max(0, failures["failed"] - missing_file)
        if remaining_failed == 0:
            logger.warning(
                "%s reported %d missing_file raw-audio result(s); continuing historical repair.",
                label,
                missing_file,
            )
            failures.pop("failed", None)
        else:
            failures["failed"] = remaining_failed

    if failures:
        msg = f"{label} reported blocking failure counters: {failures}"
        raise RuntimeError(msg)


def _raise_for_birdweather_audio_processing_failures(
    label: str,
    stats: object | None,
) -> None:
    """Raise only for blocking BirdWeather processed-audio historical failures."""
    failures: dict[str, int] = {}

    for field in ("missing_raw", "invalid_window", "decode_failed"):
        count = int(getattr(stats, field, 0) or 0)
        if count:
            failures[field] = count

    storage_unavailable = int(getattr(stats, "storage_unavailable", 0) or 0)
    if storage_unavailable:
        failures["storage_unavailable"] = storage_unavailable

    if failures:
        logger.warning(
            "%s reported non-blocking failure counters: %s; continuing historical repair.",
            label,
            failures,
        )

    upload_failed = int(getattr(stats, "upload_failed", 0) or 0)
    if upload_failed:
        msg = f"{label} reported blocking failure counters: {{'upload_failed': {upload_failed}}}"
        raise RuntimeError(msg)


def _add_failure(
    failures: list[dict[str, Any]],
    check: str,
    count: int,
    details: dict[str, Any],
) -> None:
    failures.append({"check": check, "count": int(count), "details": details})


def _earth_engine_grid_system(grid_size_km: float) -> str:
    token = f"{grid_size_km:g}".replace(".", "p")
    return f"nl_geodesic_{token}km"


def _date_sql_expr(column: str) -> str:
    if column in _DATE_GRAIN_COLUMNS:
        return column
    return f"{column}::date"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ensure a historical data and feature window is complete.",
    )
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="Comma-separated source list.",
    )
    parser.add_argument("--batch-days", type=int, default=7)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--delete-off-target", action="store_true")
    parser.add_argument(
        "--phase",
        choices=sorted(VALID_PHASES),
        default="audit",
    )
    parser.add_argument("--target", default="database", choices=("database", "local"))
    parser.add_argument("--grid-size-km", type=float, default=5.0)
    parser.add_argument(
        "--state-path",
        type=Path,
        default=_DEFAULT_STATE_PATH,
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> HistoricalWindowConfig:
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    if end < start:
        msg = f"end-date ({end}) must be >= start-date ({start})"
        raise ValueError(msg)
    if args.batch_days < 1:
        msg = "--batch-days must be >= 1"
        raise ValueError(msg)
    if args.workers < 1:
        msg = "--workers must be >= 1"
        raise ValueError(msg)
    return HistoricalWindowConfig(
        start_date=start,
        end_date=end,
        sources=parse_sources(args.sources),
        batch_days=args.batch_days,
        workers=args.workers,
        delete_off_target=args.delete_off_target,
        target=args.target,
        grid_size_km=args.grid_size_km,
        state_path=args.state_path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        phases = expand_phases(args.phase)
        runner = HistoricalWindowRunner(config)
        try:
            runner.run(phases)
        finally:
            runner.close()
    except VerificationFailure as exc:
        logger.error("Verification failed: %s", exc.failures)
        return 1
    except Exception:
        logger.exception("Historical-window operation failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
