"""BirdWeather audio processing pipeline.

Crops raw BirdWeather soundscapes to their detection windows and stores the
resulting mono FLAC clips in MinIO's ``processed-audio`` bucket.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Callable

import numpy as np
import psycopg2.extras
import soundfile as sf
from numpy.typing import NDArray

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import (
    BUCKET_PROCESSED_AUDIO,
    BUCKET_RAW_AUDIO,
    AudioStore,
    AudioStoreUnavailableError,
)
from avian_biosurveillance.processing.birdweather_raw_storage import (
    MIN_VALID_RAW_AUDIO_BYTES,
    local_raw_audio_path_from_key,
)


logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = (".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg")
_SUFFIX_PRIORITY = {suffix: index for index, suffix in enumerate(_AUDIO_SUFFIXES)}
_PROCESSED_MARK_BATCH_SIZE = 500
_DEFAULT_AUDIO_PROCESSING_WORKERS = 4
_MAX_AUDIO_PROCESSING_WORKERS_ENV = "BIRDWEATHER_AUDIO_MAX_WORKERS"
_AUDIO_PROGRESS_LOG_INTERVAL = 100


@dataclass(frozen=True, slots=True)
class BirdWeatherAudioCandidate:
    """Database row that can produce one processed BirdWeather clip."""

    detection_id: str
    detected_at: datetime
    observation_date: date
    station_id: str
    species_id: str
    audio_filename: str | None
    audio_start_time_s: float
    audio_end_time_s: float
    chunk_name: str | None = None
    chunk_tid: str | None = None
    birdnet_embedding_present: bool = False
    perch_v2_embedding_present: bool = False

    @property
    def date_str(self) -> str:
        return self.observation_date.isoformat()


@dataclass(slots=True)
class AudioProcessingStats:
    """Summary emitted by the BirdWeather audio processing pipeline."""

    candidates: int = 0
    processed: int = 0
    skipped_existing: int = 0
    missing_raw: int = 0
    invalid_window: int = 0
    decode_failed: int = 0
    upload_failed: int = 0
    storage_unavailable: int = 0
    storage_retries: int = 0
    db_updated: int = 0

    def to_metadata(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class _AudioProcessingWorkerResult:
    """One candidate-processing outcome returned from a worker thread."""

    stats: AudioProcessingStats
    processed_row: tuple[str, datetime, date] | None = None
    fatal_error: Exception | None = None


def processed_clip_key(row: BirdWeatherAudioCandidate) -> str:
    """Return the canonical MinIO key for a processed BirdWeather clip."""
    return str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}.flac",
        )
    )


def process_birdweather_audio_clips(
    *,
    lookback_days: int | None = 2,
    limit: int | None = 1_000,
    force: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
    local_audio_root: str | os.PathLike[str] | None = None,
    workers: int | None = None,
    storage_retry_attempts: int = 0,
    storage_retry_backoff_seconds: float = 1.0,
    continue_on_storage_unavailable: bool = False,
    store: AudioStore | None = None,
    pool: ConnectionPool | None = None,
) -> AudioProcessingStats:
    """Crop raw BirdWeather audio and persist processed clips.

    Args:
        lookback_days: Restrict processing to recent observation dates. ``None``
            scans the full table.
        limit: Maximum candidate rows for one materialization. ``None`` means
            no SQL limit.
        force: Recreate clips even when the processed object already exists.
        start_date: Optional first observation date to process.
        end_date: Optional last observation date to process.
        storage_retry_attempts: Retry transient MinIO failures this many times.
        storage_retry_backoff_seconds: Base exponential backoff between retries.
        continue_on_storage_unavailable: Skip affected rows instead of raising
            when MinIO remains unavailable after retries.
        store: Optional MinIO client for tests or dependency injection.
        pool: Optional PostgreSQL pool for tests or dependency injection.

    Returns:
        Counts for processed, skipped, and failed rows.
    """
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=4)
    store = store or AudioStore()
    stats = AudioProcessingStats()

    try:
        rows = _load_candidates(
            pool,
            lookback_days=lookback_days,
            limit=limit,
            force=force,
            start_date=start_date,
            end_date=end_date,
        )
        stats.candidates = len(rows)
        if not rows:
            return stats

        existing_output_keys = (
            set[str]()
            if force
            else _list_existing_processed_clip_keys(store, rows)
        )
        effective_workers = min(
            resolve_audio_processing_workers(workers),
            len(rows),
        )
        processed_rows: list[tuple[str, datetime, date]] = []
        completed_rows = 0

        with ThreadPoolExecutor(
            max_workers=effective_workers,
            thread_name_prefix="bw-audio",
        ) as executor:
            futures = {
                executor.submit(
                    _process_candidate_row,
                    row,
                    force=force,
                    existing_output_keys=existing_output_keys,
                    local_audio_root=local_audio_root,
                    storage_retry_attempts=storage_retry_attempts,
                    storage_retry_backoff_seconds=storage_retry_backoff_seconds,
                    continue_on_storage_unavailable=continue_on_storage_unavailable,
                    store=store,
                ): row
                for row in rows
            }

            for future in as_completed(futures):
                result = future.result()
                _merge_audio_processing_stats(stats, result.stats)

                if result.processed_row is not None:
                    processed_rows.append(result.processed_row)
                    stats.db_updated += _flush_processed_rows(pool, processed_rows)

                completed_rows += 1
                if result.fatal_error is not None:
                    raise result.fatal_error

                if (
                    completed_rows % _AUDIO_PROGRESS_LOG_INTERVAL == 0
                    or completed_rows == stats.candidates
                ):
                    logger.info(
                        "BirdWeather audio progress: handled %d/%d row(s); processed=%d skipped_existing=%d missing_raw=%d invalid_window=%d decode_failed=%d upload_failed=%d storage_unavailable=%d.",
                        completed_rows,
                        stats.candidates,
                        stats.processed,
                        stats.skipped_existing,
                        stats.missing_raw,
                        stats.invalid_window,
                        stats.decode_failed,
                        stats.upload_failed,
                        stats.storage_unavailable,
                    )

        stats.db_updated += _flush_processed_rows(pool, processed_rows, force=True)
        return stats
    finally:
        if own_pool:
            pool.close()


def _max_audio_processing_workers() -> int:
    raw_value = os.getenv(_MAX_AUDIO_PROCESSING_WORKERS_ENV, "").strip()
    if not raw_value:
        return _DEFAULT_AUDIO_PROCESSING_WORKERS

    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r; expected a positive integer.",
            _MAX_AUDIO_PROCESSING_WORKERS_ENV,
            raw_value,
        )
        return _DEFAULT_AUDIO_PROCESSING_WORKERS

    if parsed < 1:
        logger.warning(
            "Ignoring %s=%r; expected a positive integer.",
            _MAX_AUDIO_PROCESSING_WORKERS_ENV,
            raw_value,
        )
        return _DEFAULT_AUDIO_PROCESSING_WORKERS

    return parsed


def resolve_audio_processing_workers(requested_workers: int | None) -> int:
    requested = _max_audio_processing_workers() if requested_workers is None else max(1, requested_workers)
    effective = min(requested, _max_audio_processing_workers())
    if effective < requested:
        logger.info(
            "Clamping BirdWeather audio-processing workers from %d to %d.",
            requested,
            effective,
        )
    return effective


def _list_existing_processed_clip_keys(
    store: AudioStore,
    rows: list[BirdWeatherAudioCandidate],
) -> set[str]:
    """Return the processed-clip keys that already exist for ``rows``.

    Existence is resolved with bounded, parallel point lookups
    (``AudioStore.exists``) over the candidate keys rather than a recursive
    prefix listing. MinIO's single-drive backend can take minutes to — or fail
    to — list a busy day prefix (every object is a directory with an inlined
    ``xl.meta``), which previously stalled the whole processing pass on the
    120 s read timeout; per-object ``stat`` calls stay fast even when the
    listing path is degraded, and the work now scales with the candidate set
    (already filtered to ``NOT has_processed_clip``) instead of the day's total
    object count. A probe that fails is treated as "not present" so the row is
    processed rather than skipped — clip uploads are idempotent.
    """
    exists = getattr(store, "exists", None)
    if exists is None:
        return set()

    keys = {processed_clip_key(row) for row in rows}
    if not keys:
        return set()

    def _probe(key: str) -> tuple[str, bool]:
        try:
            return key, bool(exists(key, bucket=BUCKET_PROCESSED_AUDIO))
        except AudioStoreUnavailableError:
            return key, False

    existing_keys: set[str] = set()
    with ThreadPoolExecutor(
        max_workers=min(32, len(keys)),
        thread_name_prefix="bw-clip-probe",
    ) as executor:
        for key, present in executor.map(_probe, keys):
            if present:
                existing_keys.add(key)
    return existing_keys


def _process_candidate_row(
    row: BirdWeatherAudioCandidate,
    *,
    force: bool,
    existing_output_keys: set[str],
    local_audio_root: str | os.PathLike[str] | None,
    storage_retry_attempts: int,
    storage_retry_backoff_seconds: float,
    continue_on_storage_unavailable: bool,
    store: AudioStore,
) -> _AudioProcessingWorkerResult:
    stats = AudioProcessingStats()
    output_key = processed_clip_key(row)

    if not force and output_key in existing_output_keys:
        stats.skipped_existing = 1
        return _AudioProcessingWorkerResult(
            stats=stats,
            processed_row=(row.detection_id, row.detected_at, row.observation_date),
        )

    raw_key = "<unresolved>"
    try:
        local_raw_key = _resolve_local_raw_audio_key(
            row,
            local_audio_root=local_audio_root,
        )
        if local_raw_key is not None:
            raw_key = local_raw_key
            raw_bytes = _read_local_raw_audio_bytes(
                raw_key,
                local_audio_root=local_audio_root,
            )
        else:
            resolved_raw_key = _resolve_raw_audio_key(
                store,
                row,
                stats=stats,
                retry_attempts=storage_retry_attempts,
                retry_backoff_seconds=storage_retry_backoff_seconds,
            )
            if resolved_raw_key is None:
                stats.missing_raw = 1
                return _AudioProcessingWorkerResult(stats=stats)
            raw_key = resolved_raw_key
            raw_bytes = _with_storage_retry(
                store.get_object,
                raw_key,
                object_key=raw_key,
                operation_label="downloading raw audio",
                stats=stats,
                retry_attempts=storage_retry_attempts,
                retry_backoff_seconds=storage_retry_backoff_seconds,
                bucket=BUCKET_RAW_AUDIO,
            )
        clip, sample_rate = crop_audio_bytes(
            raw_bytes,
            start_s=row.audio_start_time_s,
            end_s=row.audio_end_time_s,
        )
    except AudioStoreUnavailableError as exc:
        stats.storage_unavailable += 1
        if continue_on_storage_unavailable:
            logger.warning(
                "BirdWeather raw audio unavailable for %s; continuing row-level repair.",
                raw_key,
            )
            return _AudioProcessingWorkerResult(stats=stats)
        logger.exception("BirdWeather raw audio unavailable for %s", raw_key)
        return _AudioProcessingWorkerResult(stats=stats, fatal_error=exc)
    except OSError:
        logger.exception("Failed to read local BirdWeather raw audio %s", raw_key)
        stats.missing_raw += 1
        return _AudioProcessingWorkerResult(stats=stats)
    except InvalidCropWindowError:
        stats.invalid_window += 1
        return _AudioProcessingWorkerResult(stats=stats)
    except UndecodableAudioError as exc:
        # Expected, recoverable data-quality issue (typically an empty/truncated
        # CDN download). Log a single concise line rather than a full traceback.
        logger.warning("Skipping undecodable raw audio %s: %s", raw_key, exc)
        stats.decode_failed += 1
        return _AudioProcessingWorkerResult(stats=stats)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Failed to decode raw audio %s", raw_key)
        stats.decode_failed += 1
        return _AudioProcessingWorkerResult(stats=stats)

    try:
        _with_storage_retry(
            store.upload_audio,
            output_key,
            encode_flac_bytes(clip, sample_rate),
            object_key=output_key,
            operation_label="uploading processed clip",
            stats=stats,
            retry_attempts=storage_retry_attempts,
            retry_backoff_seconds=storage_retry_backoff_seconds,
            bucket=BUCKET_PROCESSED_AUDIO,
            content_type="audio/flac",
        )
    except AudioStoreUnavailableError as exc:
        stats.storage_unavailable += 1
        if continue_on_storage_unavailable:
            logger.warning(
                "Processed-audio storage unavailable for %s; continuing row-level repair.",
                output_key,
            )
            return _AudioProcessingWorkerResult(stats=stats)
        logger.exception("Processed-audio storage unavailable for %s", output_key)
        return _AudioProcessingWorkerResult(stats=stats, fatal_error=exc)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Failed to upload processed clip %s", output_key)
        stats.upload_failed += 1
        return _AudioProcessingWorkerResult(stats=stats)

    stats.processed = 1
    return _AudioProcessingWorkerResult(
        stats=stats,
        processed_row=(row.detection_id, row.detected_at, row.observation_date),
    )


def _merge_audio_processing_stats(
    target: AudioProcessingStats,
    source: AudioProcessingStats,
) -> None:
    target.processed += source.processed
    target.skipped_existing += source.skipped_existing
    target.missing_raw += source.missing_raw
    target.invalid_window += source.invalid_window
    target.decode_failed += source.decode_failed
    target.upload_failed += source.upload_failed
    target.storage_unavailable += source.storage_unavailable
    target.storage_retries += source.storage_retries


class InvalidCropWindowError(ValueError):
    """Raised when a BirdWeather crop window is missing or empty."""


class UndecodableAudioError(ValueError):
    """Raised when raw audio bytes cannot be decoded (empty/truncated/corrupt).

    This is an expected, recoverable data-quality condition rather than a bug,
    so callers log it concisely and skip the row instead of emitting a full
    decoder traceback per file.
    """


def _decode_audio_bytes_with_soundfile(
    raw_bytes: bytes,
) -> tuple[NDArray[np.float32], int]:
    audio, sample_rate = sf.read(
        io.BytesIO(raw_bytes),
        dtype="float32",
        always_2d=False,
    )
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1, dtype=np.float32)
    return np.asarray(audio, dtype=np.float32), int(sample_rate)


def _decode_audio_bytes_with_ffmpeg(
    raw_bytes: bytes,
) -> tuple[NDArray[np.float32], int]:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-f",
                "wav",
                "-acodec",
                "pcm_f32le",
                "-ac",
                "1",
                "pipe:1",
            ],
            input=raw_bytes,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is not available to decode BirdWeather audio bytes"
        ) from exc

    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(
            "ffmpeg failed to decode BirdWeather audio bytes"
            f" (exit {result.returncode}): {stderr or 'no stderr'}"
        )

    return _decode_audio_bytes_with_soundfile(result.stdout)


def _decode_audio_bytes(raw_bytes: bytes) -> tuple[NDArray[np.float32], int]:
    if len(raw_bytes) < MIN_VALID_RAW_AUDIO_BYTES:
        raise UndecodableAudioError(
            f"raw audio body too small to decode ({len(raw_bytes)} byte(s))"
        )
    try:
        return _decode_audio_bytes_with_soundfile(raw_bytes)
    except (sf.LibsndfileError, OSError, RuntimeError, ValueError):
        pass
    try:
        return _decode_audio_bytes_with_ffmpeg(raw_bytes)
    except (sf.LibsndfileError, OSError, RuntimeError, ValueError) as exc:
        raise UndecodableAudioError(
            f"neither soundfile nor ffmpeg could decode the audio bytes: {exc}"
        ) from exc


def crop_audio_bytes(
    raw_bytes: bytes,
    *,
    start_s: float,
    end_s: float,
) -> tuple[NDArray[np.float32], int]:
    """Decode, crop, and convert raw audio bytes to mono float32 samples."""
    if end_s <= start_s:
        raise InvalidCropWindowError(
            f"Invalid crop window: start={start_s}, end={end_s}"
        )

    audio, sample_rate = _decode_audio_bytes(raw_bytes)

    start_sample = max(0, round(start_s * sample_rate))
    end_sample = min(len(audio), round(end_s * sample_rate))
    if end_sample <= start_sample:
        raise InvalidCropWindowError(
            f"Crop window outside audio bounds: start={start_s}, end={end_s}"
        )

    return np.asarray(audio[start_sample:end_sample], dtype=np.float32), sample_rate


def encode_flac_bytes(audio: NDArray[np.float32], sample_rate: int) -> bytes:
    """Encode mono float audio to FLAC bytes."""
    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="FLAC")
    return buffer.getvalue()


def _load_candidates(
    pool: ConnectionPool,
    *,
    lookback_days: int | None,
    limit: int | None,
    force: bool,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[BirdWeatherAudioCandidate]:
    filters = [
        "audio_url IS NOT NULL",
        "audio_start_time_s IS NOT NULL",
        "audio_end_time_s IS NOT NULL",
        "audio_end_time_s > audio_start_time_s",
    ]
    params: list[Any] = []

    if not force:
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
            audio_filename,
            audio_start_time_s,
            audio_end_time_s
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

    return [_candidate_from_record(record) for record in records]


def _candidate_from_record(record: dict[str, Any]) -> BirdWeatherAudioCandidate:
    return BirdWeatherAudioCandidate(
        detection_id=str(record["detection_id"]),
        detected_at=record["detected_at"],
        observation_date=record["observation_date"],
        station_id=str(record.get("station_id") or "unknown_station"),
        species_id=str(record.get("species_id") or "unknown_species"),
        audio_filename=record.get("audio_filename"),
        audio_start_time_s=float(record["audio_start_time_s"]),
        audio_end_time_s=float(record["audio_end_time_s"]),
    )


def _day_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _resolve_raw_audio_key(
    store: AudioStore,
    row: BirdWeatherAudioCandidate,
    *,
    stats: AudioProcessingStats,
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> str | None:
    for key in _candidate_raw_keys(row):
        if _with_storage_retry(
            store.exists,
            key,
            object_key=key,
            operation_label="checking raw audio",
            stats=stats,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            bucket=BUCKET_RAW_AUDIO,
        ):
            return key
    return None


def _resolve_local_raw_audio_key(
    row: BirdWeatherAudioCandidate,
    *,
    local_audio_root: str | os.PathLike[str] | None,
) -> str | None:
    if local_audio_root is None:
        return None

    for key in _candidate_raw_keys(row):
        if local_raw_audio_path_from_key(
            key,
            raw_root=local_audio_root,
        ).is_file():
            return key
    return None


def _read_local_raw_audio_bytes(
    raw_key: str,
    *,
    local_audio_root: str | os.PathLike[str] | None,
) -> bytes:
    return local_raw_audio_path_from_key(
        raw_key,
        raw_root=local_audio_root,
    ).read_bytes()


def _with_storage_retry(
    operation: Callable[..., Any],
    *operation_args: Any,
    object_key: str,
    operation_label: str,
    stats: AudioProcessingStats,
    retry_attempts: int,
    retry_backoff_seconds: float,
    **operation_kwargs: Any,
) -> Any:
    attempts = max(0, retry_attempts)
    backoff_seconds = max(0.0, retry_backoff_seconds)

    for attempt in range(attempts + 1):
        try:
            return operation(*operation_args, **operation_kwargs)
        except AudioStoreUnavailableError:
            if attempt >= attempts:
                raise

            stats.storage_retries += 1
            delay_seconds = backoff_seconds * (2**attempt)
            logger.warning(
                "BirdWeather audio storage unavailable while %s for %s; retrying in %.1f s (%d/%d)",
                operation_label,
                object_key,
                delay_seconds,
                attempt + 1,
                attempts,
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    msg = f"Storage retry loop exhausted unexpectedly for {object_key}"
    raise RuntimeError(msg)


def _candidate_raw_keys(row: BirdWeatherAudioCandidate) -> list[str]:
    names: list[str] = []
    if row.audio_filename:
        names.append(PurePosixPath(row.audio_filename).name)
    names.extend(
        f"{row.detection_id}_{row.species_id}{suffix}" for suffix in _AUDIO_SUFFIXES
    )
    names.extend(f"{row.detection_id}{suffix}" for suffix in _AUDIO_SUFFIXES)

    seen: set[str] = set()
    keys: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        keys.append(str(PurePosixPath(row.date_str, "audio", name)))
    return keys


def _mark_processed(
    pool: ConnectionPool,
    rows: list[tuple[str, datetime, date]],
) -> int:
    if not rows:
        return 0

    updated_rows = 0
    for detection_id, detected_at, observation_date in rows:
        day_start = _day_start(observation_date)
        day_end = day_start + timedelta(days=1)
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
                  AND NOT has_processed_clip
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
                SET has_processed_clip = true
                WHERE ctid = %s::tid
                  AND observation_date = %s
                  AND detection_id = %s
                  AND detected_at = %s
                  AND NOT target.has_processed_clip
                """,  # noqa: S608
                (
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


def _flush_processed_rows(
    pool: ConnectionPool,
    rows: list[tuple[str, datetime, date]],
    *,
    force: bool = False,
) -> int:
    if not rows:
        return 0
    if not force and len(rows) < _PROCESSED_MARK_BATCH_SIZE:
        return 0

    batch = list(rows)
    rows.clear()
    return _mark_processed(pool, batch)
