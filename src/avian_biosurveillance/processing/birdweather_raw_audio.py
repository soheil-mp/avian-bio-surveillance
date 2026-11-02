"""BirdWeather raw-audio queue and downloader.

This module keeps raw audio ingestion separate from detection-table ingestion
so Dagster can model the slow download/upload step as its own asset.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import (
    BUCKET_RAW_AUDIO,
    AudioStore,
    AudioStoreUnavailableError,
)
from avian_biosurveillance.ingestion.birdweather import AudioCollector
from avian_biosurveillance.processing.birdweather_raw_storage import (
    MIN_VALID_RAW_AUDIO_BYTES,
    local_raw_audio_is_present,
    local_raw_audio_path_from_key,
)
from avian_biosurveillance.utils.runtime_paths import preferred_temp_dir


logger = logging.getLogger(__name__)

_DEFAULT_RAW_AUDIO_DOWNLOAD_WORKERS = 4
_MAX_RAW_AUDIO_DOWNLOAD_WORKERS_ENV = "BIRDWEATHER_RAW_AUDIO_MAX_WORKERS"
_TRANSIENT_DOWNLOAD_STATUSES = {
    "server_error",
    "timeout",
    "error",
    "max_retries",
    "empty_body",
    "missing_file",
}
_RAW_AUDIO_RETRY_PASSES = 2
_RAW_AUDIO_UPLOAD_RETRY_ATTEMPTS = 3
_RAW_AUDIO_UPLOAD_RETRY_BASE_DELAY_SECONDS = 1.0
_RAW_AUDIO_DB_UPDATE_BATCH_SIZE = 100

RawAudioRowUpdate = tuple[str, datetime, date, str]

_AUDIO_CONTENT_TYPES = {
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
}


@dataclass(frozen=True, slots=True)
class RawAudioCandidate:
    """Database row eligible for raw BirdWeather audio download."""

    detection_id: str
    detected_at: datetime
    observation_date: date
    station_id: str
    species_id: str
    audio_url: str
    audio_filename: str | None
    audio_filesize_bytes: int | None

    @property
    def date_str(self) -> str:
        return self.observation_date.isoformat()


@dataclass(frozen=True, slots=True)
class RawAudioDownloadJob:
    """One unique raw-audio object download plus all rows that reference it."""

    row: RawAudioCandidate
    audio_path: Path
    grouped_rows: tuple[RawAudioCandidate, ...]

    @property
    def object_key(self) -> str:
        return raw_audio_object_key(self.row)


@dataclass(slots=True)
class RawAudioQueueStats:
    """Summary of queued raw-audio work."""

    candidates: int = 0
    already_present: int = 0
    queued: int = 0

    def to_metadata(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class RawAudioDownloadStats:
    """Summary emitted by the raw-audio download asset."""

    candidates: int = 0
    already_present: int = 0
    attempted: int = 0
    downloaded: int = 0
    failed: int = 0
    archived: int = 0
    not_found: int = 0
    forbidden: int = 0
    server_error: int = 0
    timeout: int = 0
    download_error: int = 0
    max_retries: int = 0
    missing_file: int = 0
    retry_attempted: int = 0
    retry_recovered: int = 0
    uploaded: int = 0
    upload_failed: int = 0
    bytes_uploaded: int = 0
    db_updated: int = 0

    def to_metadata(self) -> dict[str, int]:
        return asdict(self)


def _max_raw_audio_download_workers() -> int:
    """Return the configured upper bound for parallel BirdWeather downloads."""
    raw_value = os.getenv(_MAX_RAW_AUDIO_DOWNLOAD_WORKERS_ENV, "").strip()
    if not raw_value:
        return _DEFAULT_RAW_AUDIO_DOWNLOAD_WORKERS

    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r; expected a positive integer.",
            _MAX_RAW_AUDIO_DOWNLOAD_WORKERS_ENV,
            raw_value,
        )
        return _DEFAULT_RAW_AUDIO_DOWNLOAD_WORKERS

    if parsed < 1:
        logger.warning(
            "Ignoring %s=%r; expected a positive integer.",
            _MAX_RAW_AUDIO_DOWNLOAD_WORKERS_ENV,
            raw_value,
        )
        return _DEFAULT_RAW_AUDIO_DOWNLOAD_WORKERS

    return parsed


def resolve_raw_audio_download_workers(requested_workers: int) -> int:
    """Clamp requested BirdWeather download parallelism to a safe local ceiling."""
    requested = max(1, requested_workers)
    effective = min(requested, _max_raw_audio_download_workers())
    if effective < requested:
        logger.info(
            "Clamping BirdWeather raw-audio download workers from %d to %d to reduce CDN SSL/timeout churn.",
            requested,
            effective,
        )
    return effective


def inspect_birdweather_raw_audio_queue(
    *,
    lookback_days: int | None = 2,
    limit: int | None = 5_000,
    force: bool = False,
    store: AudioStore | None = None,
    pool: ConnectionPool | None = None,
) -> RawAudioQueueStats:
    """Count BirdWeather detections that still need raw audio objects."""
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=2)
    store = store or AudioStore()
    stats = RawAudioQueueStats()

    try:
        rows = _load_raw_audio_candidates(
            pool,
            lookback_days=lookback_days,
            limit=limit,
        )
        stats.candidates = len(rows)
        if force:
            stats.queued = stats.candidates
            return stats

        existing_keys = _list_existing_raw_audio_keys(store, rows)
        for row in rows:
            if raw_audio_object_key(row) in existing_keys:
                stats.already_present += 1
            else:
                stats.queued += 1
        return stats
    finally:
        if own_pool:
            pool.close()


def download_birdweather_raw_audio_objects(
    *,
    lookback_days: int | None = 2,
    limit: int | None = 1_000,
    force: bool = False,
    workers: int = _DEFAULT_RAW_AUDIO_DOWNLOAD_WORKERS,
    start_date: date | None = None,
    end_date: date | None = None,
    skip_processed: bool = False,
    local_audio_root: str | os.PathLike[str] | None = None,
    store: AudioStore | None = None,
    pool: ConnectionPool | None = None,
) -> RawAudioDownloadStats:
    """Download missing BirdWeather raw audio and persist it to storage."""
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=4)
    use_local_audio_root = local_audio_root is not None
    if not use_local_audio_root:
        store = store or AudioStore()
    collector = AudioCollector()
    stats = RawAudioDownloadStats()

    try:
        rows = _load_raw_audio_candidates(
            pool,
            lookback_days=lookback_days,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
            skip_processed=skip_processed,
        )
        stats.candidates = len(rows)
        if force:
            existing_keys = set[str]()
        elif use_local_audio_root:
            existing_keys = _list_existing_local_raw_audio_keys(
                rows,
                local_audio_root=local_audio_root,
            )
        else:
            existing_keys = _list_existing_raw_audio_keys(store, rows)
        existing_rows: list[RawAudioRowUpdate] = []
        pending: list[RawAudioCandidate] = []
        for row in rows:
            object_key = raw_audio_object_key(row)
            if force or object_key not in existing_keys:
                pending.append(row)
            else:
                existing_rows.append(
                    (
                        row.detection_id,
                        row.detected_at,
                        row.observation_date,
                        object_key,
                    )
                )

        stats.already_present = len(existing_rows)
        stats.db_updated += _mark_audio_rows_in_batches(pool, existing_rows)
        if not pending:
            return stats

        effective_workers = resolve_raw_audio_download_workers(workers)

        with tempfile.TemporaryDirectory(
            prefix="birdweather_raw_audio_",
            dir=preferred_temp_dir(),
        ) as temp_dir:
            temp_root = Path(temp_dir)
            uploaded_rows: list[RawAudioRowUpdate] = []

            def _handle_downloaded(job: RawAudioDownloadJob) -> None:
                if use_local_audio_root:
                    uploaded_key = _persist_downloaded_local_raw_audio(
                        job.row,
                        job.audio_path,
                        stats,
                        local_audio_root=local_audio_root,
                    )
                else:
                    uploaded_key = _upload_downloaded_raw_audio(
                        store,
                        job.row,
                        job.audio_path,
                        stats,
                    )
                if uploaded_key is None:
                    return

                uploaded_rows.extend(
                    (
                        row.detection_id,
                        row.detected_at,
                        row.observation_date,
                        uploaded_key,
                    )
                    for row in job.grouped_rows
                )
                if len(uploaded_rows) >= _RAW_AUDIO_DB_UPDATE_BATCH_SIZE:
                    stats.db_updated += _flush_uploaded_audio_rows(pool, uploaded_rows)

                if stats.uploaded % 100 == 0 or stats.uploaded == stats.attempted:
                    logger.info(
                        "BirdWeather raw-audio progress: uploaded %d/%d object(s).",
                        stats.uploaded,
                        stats.attempted,
                    )

            download_jobs = _build_download_jobs(pending, temp_root)
            stats.attempted = len(download_jobs)

            failed_downloads = _download_raw_audio_batch(
                collector,
                download_jobs,
                workers=effective_workers,
                on_downloaded=_handle_downloaded,
            )
            failed_downloads = _retry_transient_raw_audio_downloads(
                collector,
                failed_downloads,
                stats=stats,
                on_downloaded=_handle_downloaded,
            )

            _log_download_failures(failed_downloads)
            for job, status in failed_downloads:
                _record_download_failure(
                    stats,
                    status,
                    affected_rows=len(job.grouped_rows),
                )

            stats.db_updated += _flush_uploaded_audio_rows(pool, uploaded_rows)
        if stats.failed:
            logger.error("BirdWeather raw-audio failures by final status: %s", _download_failure_summary(stats))
        if stats.archived:
            logger.warning(
                "BirdWeather raw-audio source objects archived/unavailable: %d.",
                stats.archived,
            )
        return stats
    finally:
        if own_pool:
            pool.close()


def _staged_temp_audio_is_usable(path: Path) -> bool:
    """Return True when a freshly downloaded temp file holds a non-empty body."""
    try:
        return path.is_file() and path.stat().st_size >= MIN_VALID_RAW_AUDIO_BYTES
    except OSError:
        return False


def _classify_unusable_download(path: Path) -> str:
    """Drop an empty/missing staged file and return its retry status."""
    if path.exists():
        try:
            path.unlink()
        except OSError:
            logger.debug("Could not remove empty raw-audio temp file %s", path)
        return "empty_body"
    return "missing_file"


def _download_raw_audio_batch(
    collector: AudioCollector,
    download_jobs: list[RawAudioDownloadJob],
    *,
    workers: int,
    on_downloaded: Callable[[RawAudioDownloadJob], None],
) -> list[tuple[RawAudioDownloadJob, str]]:
    failed_downloads: list[tuple[RawAudioDownloadJob, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                collector.client.download_file,
                job.row.audio_url,
                job.audio_path,
                job.row.audio_filesize_bytes,
            ): job
            for job in download_jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            status = "error"
            try:
                success, status = future.result()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Raw audio download raised for %s", job.row.detection_id)
                success = False

            if success and _staged_temp_audio_is_usable(job.audio_path):
                on_downloaded(job)
            elif success:
                failed_downloads.append(
                    (job, _classify_unusable_download(job.audio_path))
                )
            else:
                failed_downloads.append((job, str(status or "error")))

    return failed_downloads


def _retry_transient_raw_audio_downloads(
    collector: AudioCollector,
    failed_downloads: list[tuple[RawAudioDownloadJob, str]],
    *,
    stats: RawAudioDownloadStats,
    on_downloaded: Callable[[RawAudioDownloadJob], None],
) -> list[tuple[RawAudioDownloadJob, str]]:
    remaining = failed_downloads
    for retry_pass in range(1, _RAW_AUDIO_RETRY_PASSES + 1):
        retryable = [
            job
            for job, status in remaining
            if status in _TRANSIENT_DOWNLOAD_STATUSES
        ]
        if not retryable:
            break

        non_retryable = [
            item for item in remaining if item[1] not in _TRANSIENT_DOWNLOAD_STATUSES
        ]
        stats.retry_attempted += sum(len(job.grouped_rows) for job in retryable)
        logger.info(
            "Retrying %d transient BirdWeather raw-audio download failure(s), pass %d/%d.",
            len(retryable),
            retry_pass,
            _RAW_AUDIO_RETRY_PASSES,
        )

        next_remaining = non_retryable
        for job in retryable:
            if job.audio_path.exists():
                try:
                    job.audio_path.unlink()
                except OSError:
                    logger.debug("Could not remove partial raw-audio temp file %s", job.audio_path)

            status = "error"
            try:
                success, status = collector.client.download_file(
                    job.row.audio_url,
                    job.audio_path,
                    job.row.audio_filesize_bytes,
                    5,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Raw audio retry raised for %s", job.row.detection_id)
                success = False

            if success and _staged_temp_audio_is_usable(job.audio_path):
                stats.retry_recovered += len(job.grouped_rows)
                on_downloaded(job)
            elif success:
                next_remaining.append(
                    (job, _classify_unusable_download(job.audio_path))
                )
            else:
                next_remaining.append((job, str(status or "error")))

        remaining = next_remaining

    return remaining


def _record_download_failure(
    stats: RawAudioDownloadStats,
    status: str,
    *,
    affected_rows: int = 1,
) -> None:
    if status == "archived":
        stats.archived += affected_rows
        return

    stats.failed += affected_rows
    if status == "not_found":
        stats.not_found += affected_rows
    elif status == "forbidden":
        stats.forbidden += affected_rows
    elif status == "server_error":
        stats.server_error += affected_rows
    elif status == "timeout":
        stats.timeout += affected_rows
    elif status == "max_retries":
        stats.max_retries += affected_rows
    elif status == "missing_file":
        stats.missing_file += affected_rows
    else:
        stats.download_error += affected_rows


def _upload_downloaded_raw_audio(
    store: AudioStore,
    row: RawAudioCandidate,
    audio_path: Path,
    stats: RawAudioDownloadStats,
) -> str | None:
    object_key = raw_audio_object_key(row)
    data: bytes | None = None
    try:
        try:
            data = audio_path.read_bytes()
        except OSError:  # pylint: disable=broad-exception-caught
            logger.exception("Raw audio temp-file read failed for %s", object_key)
            stats.upload_failed += 1
            return None

        try:
            for attempt in range(1, _RAW_AUDIO_UPLOAD_RETRY_ATTEMPTS + 1):
                try:
                    store.upload_audio(
                        object_key,
                        data,
                        bucket=BUCKET_RAW_AUDIO,
                        content_type=_content_type(object_key),
                    )
                    break
                except AudioStoreUnavailableError:
                    if attempt == _RAW_AUDIO_UPLOAD_RETRY_ATTEMPTS:
                        raise

                    delay = _RAW_AUDIO_UPLOAD_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Raw audio storage unavailable while uploading %s; retrying in %.1f s (%d/%d)",
                        object_key,
                        delay,
                        attempt,
                        _RAW_AUDIO_UPLOAD_RETRY_ATTEMPTS - 1,
                    )
                    time.sleep(delay)
        except AudioStoreUnavailableError:
            logger.exception("Raw audio storage unavailable for %s", object_key)
            stats.upload_failed += 1
            return None
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Raw audio upload failed for %s", object_key)
            stats.upload_failed += 1
            return None

        stats.downloaded += 1
        stats.uploaded += 1
        stats.bytes_uploaded += len(data)
        return object_key
    finally:
        if audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                logger.debug(
                    "Could not remove raw-audio temp file %s after upload handling.",
                    audio_path,
                )


def _persist_downloaded_local_raw_audio(
    row: RawAudioCandidate,
    audio_path: Path,
    stats: RawAudioDownloadStats,
    *,
    local_audio_root: str | os.PathLike[str] | None,
) -> str | None:
    object_key = raw_audio_object_key(row)
    destination = local_raw_audio_path_from_key(
        object_key,
        raw_root=local_audio_root,
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = audio_path.read_bytes()
        if len(payload) < MIN_VALID_RAW_AUDIO_BYTES:
            logger.warning(
                "Refusing to persist empty/truncated raw audio (%d byte(s)) for %s.",
                len(payload),
                object_key,
            )
            stats.upload_failed += 1
            return None
        destination.write_bytes(payload)
    except OSError:
        logger.exception("Local raw audio write failed for %s", destination)
        stats.upload_failed += 1
        return None
    finally:
        if audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                logger.debug(
                    "Could not remove raw-audio temp file %s after local persistence.",
                    audio_path,
                )

    stats.downloaded += 1
    stats.uploaded += 1
    stats.bytes_uploaded += len(payload)
    return object_key


def _flush_uploaded_audio_rows(
    pool: ConnectionPool,
    uploaded_rows: list[RawAudioRowUpdate],
) -> int:
    if not uploaded_rows:
        return 0

    updated = _mark_audio_rows_in_batches(pool, uploaded_rows)
    uploaded_rows.clear()
    return updated


def _mark_audio_rows_in_batches(
    pool: ConnectionPool,
    rows: list[RawAudioRowUpdate],
) -> int:
    if not rows:
        return 0

    rows_by_day: dict[date, list[RawAudioRowUpdate]] = {}
    for row in rows:
        rows_by_day.setdefault(row[2], []).append(row)

    updated = 0
    for _, day_rows in sorted(rows_by_day.items()):
        for start in range(0, len(day_rows), _RAW_AUDIO_DB_UPDATE_BATCH_SIZE):
            updated += _mark_audio_filename(
                pool,
                day_rows[start : start + _RAW_AUDIO_DB_UPDATE_BATCH_SIZE],
            )
    return updated


def _download_failure_summary(stats: RawAudioDownloadStats) -> dict[str, int]:
    return {
        key: value
        for key, value in {
            "not_found": stats.not_found,
            "forbidden": stats.forbidden,
            "server_error": stats.server_error,
            "timeout": stats.timeout,
            "max_retries": stats.max_retries,
            "missing_file": stats.missing_file,
            "download_error": stats.download_error,
            "upload_failed": stats.upload_failed,
        }.items()
        if value
    }


def _log_download_failures(
    failed_downloads: list[tuple[RawAudioDownloadJob, str]],
) -> None:
    if not failed_downloads:
        return

    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for job, status in failed_downloads:
        counts[status] = counts.get(status, 0) + len(job.grouped_rows)
        status_samples = samples.setdefault(status, [])
        for row in job.grouped_rows:
            if len(status_samples) >= 5:
                break
            status_samples.append(row.detection_id)

    for status, count in sorted(counts.items()):
        logger.warning(
            "Raw audio downloads ended with status=%s for %d row(s); sample detection_ids=%s.",
            status,
            count,
            samples.get(status, []),
        )


def _build_download_jobs(
    rows: list[RawAudioCandidate],
    temp_root: Path,
) -> list[RawAudioDownloadJob]:
    grouped_rows: dict[str, list[RawAudioCandidate]] = {}
    for row in rows:
        grouped_rows.setdefault(raw_audio_object_key(row), []).append(row)

    jobs: list[RawAudioDownloadJob] = []
    for object_key, matching_rows in grouped_rows.items():
        audio_path = temp_root / PurePosixPath(object_key)
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        jobs.append(
            RawAudioDownloadJob(
                row=matching_rows[0],
                audio_path=audio_path,
                grouped_rows=tuple(matching_rows),
            )
        )
    return jobs


def raw_audio_object_key(row: RawAudioCandidate) -> str:
    """Return the canonical raw-audio object key for a detection."""
    filename = _resolved_audio_filename(row)
    return str(PurePosixPath(row.date_str, "audio", filename))


def _resolved_audio_filename(row: RawAudioCandidate) -> str:
    if row.audio_filename:
        name = PurePosixPath(row.audio_filename).name
        if PurePosixPath(name).suffix:
            return name
        return f"{name}{_audio_extension(row.audio_url)}"
    return f"{row.detection_id}_{row.species_id}{_audio_extension(row.audio_url)}"


def _audio_extension(url: str) -> str:
    lowered = url.lower()
    for suffix in _AUDIO_CONTENT_TYPES:
        if suffix in lowered:
            return suffix
    return ".flac"


def _content_type(object_key: str) -> str:
    suffix = PurePosixPath(object_key).suffix.lower()
    return _AUDIO_CONTENT_TYPES.get(suffix, "application/octet-stream")


def _list_existing_raw_audio_keys(
    store: AudioStore,
    rows: list[RawAudioCandidate],
) -> set[str]:
    """Load existing raw-audio object keys with one MinIO listing per date prefix."""
    existing_keys: set[str] = set()
    prefixes = sorted({f"{row.date_str}/audio/" for row in rows})
    for prefix in prefixes:
        existing_keys.update(
            store.list_objects(
                prefix=prefix,
                bucket=BUCKET_RAW_AUDIO,
                recursive=True,
            )
        )
    return existing_keys


def _list_existing_local_raw_audio_keys(
    rows: list[RawAudioCandidate],
    *,
    local_audio_root: str | os.PathLike[str] | None,
) -> set[str]:
    existing_keys: set[str] = set()
    for row in rows:
        object_key = raw_audio_object_key(row)
        if local_raw_audio_is_present(
            object_key,
            raw_root=local_audio_root,
        ):
            existing_keys.add(object_key)
    return existing_keys


def _load_raw_audio_candidates(
    pool: ConnectionPool,
    *,
    lookback_days: int | None,
    limit: int | None,
    start_date: date | None = None,
    end_date: date | None = None,
    skip_processed: bool = False,
) -> list[RawAudioCandidate]:
    filters = ["audio_url IS NOT NULL"]
    params: list[Any] = []

    if skip_processed:
        filters.append("NOT has_processed_clip")

    if start_date is not None:
        filters.append("detected_at >= %s")
        params.append(_day_start(start_date))
    if end_date is not None:
        filters.append("detected_at < %s")
        params.append(_day_start(end_date + timedelta(days=1)))
    if start_date is None and end_date is None and lookback_days is not None:
        filters.append("detected_at >= (CURRENT_DATE - %s::int)::timestamptz")
        params.append(lookback_days)

    sql_template = """
        SELECT
            detection_id,
            detected_at,
            observation_date,
            station_id,
            species_id,
            audio_url,
            audio_filename,
            audio_filesize_bytes
        FROM birdweather_tbl
        WHERE {filters}
        ORDER BY detected_at DESC
    """
    sql = sql_template.format(filters=" AND ".join(filters))
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        records = cur.fetchall()

    return [
        RawAudioCandidate(
            detection_id=str(record["detection_id"]),
            detected_at=record["detected_at"],
            observation_date=record["observation_date"],
            station_id=str(record.get("station_id") or "unknown_station"),
            species_id=str(record.get("species_id") or "unknown_species"),
            audio_url=str(record["audio_url"]),
            audio_filename=record.get("audio_filename"),
            audio_filesize_bytes=record.get("audio_filesize_bytes"),
        )
        for record in records
    ]


def _day_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _detected_at_day(value: datetime) -> date:
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(UTC).date()


def _mark_audio_filename(
    pool: ConnectionPool,
    rows: list[RawAudioRowUpdate],
) -> int:
    if not rows:
        return 0

    updated_rows = 0
    for detection_id, detected_at, observation_date, object_key in rows:
        day_start = _day_start(observation_date)
        day_end = day_start + timedelta(days=1)
        audio_filename = PurePosixPath(object_key).name

        with pool.cursor() as cur:
            cur.execute(
                """
                SELECT tableoid::regclass::text AS chunk_name, ctid::text AS chunk_tid
                FROM birdweather_tbl
                WHERE detected_at >= %s
                  AND detected_at < %s
                                    AND observation_date = %s
                  AND detection_id = %s
                  AND detected_at = %s
                  AND audio_filename IS NULL
                LIMIT 1
                """,
                (
                    day_start,
                    day_end,
                                        observation_date,
                    detection_id,
                    detected_at,
                ),
            )
            target_row = cur.fetchone()
            if target_row is None:
                continue

            if isinstance(target_row, dict):
                chunk_name = target_row["chunk_name"]
                chunk_tid = target_row["chunk_tid"]
            else:
                chunk_name, chunk_tid = target_row

            cur.execute(
                f"""
                UPDATE {_quote_qualified_identifier(str(chunk_name))} AS target
                SET audio_filename = %s
                WHERE ctid = %s::tid
                                    AND target.observation_date = %s
                  AND target.detection_id = %s
                  AND target.detected_at = %s
                  AND target.audio_filename IS NULL
                """,  # noqa: S608
                (
                    audio_filename,
                    chunk_tid,
                                        observation_date,
                    detection_id,
                    detected_at,
                ),
            )
            updated_rows += int(cur.rowcount or 0)
    return updated_rows


def _quote_qualified_identifier(value: str) -> str:
    parts = value.split(".")
    if not parts or any(not part or '"' in part for part in parts):
        msg = f"unsafe SQL identifier: {value!r}"
        raise ValueError(msg)
    return ".".join(f'"{part}"' for part in parts)


__all__ = [
    "RawAudioCandidate",
    "RawAudioDownloadStats",
    "RawAudioQueueStats",
    "download_birdweather_raw_audio_objects",
    "inspect_birdweather_raw_audio_queue",
    "raw_audio_object_key",
]
