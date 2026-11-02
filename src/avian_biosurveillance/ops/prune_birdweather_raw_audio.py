"""Safely prune BirdWeather raw-audio objects from MinIO.

Example commands:

    # Dry run one month from the mounted application code
    python -m avian_biosurveillance.ops.prune_birdweather_raw_audio \
        --start-date 2024-01-01 --end-date 2024-01-31 --chunk-days 30

    # Apply deletion for all history before 2026-01-01
    python -m avian_biosurveillance.ops.prune_birdweather_raw_audio \
        --before-date 2026-01-01 --chunk-days 30 --apply

This utility deletes only raw-audio objects whose linked BirdWeather rows all
already have cropped processed clips. Before deletion it verifies that the raw
object still exists and that every linked processed clip exists in MinIO.
Transient MinIO verification failures are retried; if verification still cannot
be completed, the candidate is skipped for safety.

The command is dry-run by default. Use ``--apply`` to actually delete verified
raw objects.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

from avian_biosurveillance.processing.birdweather_raw_storage import (
    local_raw_audio_path_from_key,
    resolve_local_birdweather_raw_root,
)

if TYPE_CHECKING:
    from avian_biosurveillance.db.database import ConnectionPool
    from avian_biosurveillance.db.minio_client import AudioStore


_BUCKET_RAW_AUDIO = "raw-audio"
_BUCKET_PROCESSED_AUDIO = "processed-audio"
_STORAGE_RETRY_ATTEMPTS = 5
_STORAGE_RETRY_BACKOFF_SECONDS = 0.5
_DATABASE_RETRY_ATTEMPTS = 3
_DATABASE_RETRY_BACKOFF_SECONDS = 0.5
_SEED_BATCH_MIN = 50
_SEED_BATCH_MULTIPLIER = 20
_PRUNE_PROGRESS_LOG_INTERVAL = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = (".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg")


@dataclass(frozen=True, slots=True)
class PruneCandidate:
    """One raw-audio object that is eligible for verification."""

    raw_object_key: str
    row_count: int
    processed_clip_keys: tuple[str, ...]
    first_observation_date: date
    last_observation_date: date
    raw_size_bytes: int | None


@dataclass(slots=True)
class PruneStats:
    """Summary emitted by the prune run."""

    candidate_objects: int = 0
    verified_objects: int = 0
    deleted_objects: int = 0
    storage_retries: int = 0
    skipped_missing_raw: int = 0
    skipped_missing_processed: int = 0
    skipped_storage_unavailable: int = 0
    skipped_delete_unavailable: int = 0
    candidate_bytes: int = 0
    verified_bytes: int = 0
    deleted_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _PruneVerificationCache:
    """Prefetched object listings used to verify candidates cheaply."""

    processed_clip_keys_by_date: dict[date, frozenset[str]]
    raw_object_keys_by_date: dict[date, frozenset[str]]
    unavailable_processed_dates: frozenset[date]
    unavailable_raw_dates: frozenset[date]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Safely prune BirdWeather MinIO raw-audio objects whose linked "
            "detections already have processed clips. Dry-run by default."
        )
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        help="Only consider rows on or after this observation date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        help="Only consider rows on or before this observation date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--before-date",
        type=date.fromisoformat,
        help=(
            "Only consider rows strictly before this observation date "
            "(YYYY-MM-DD)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of grouped raw objects to inspect in this run.",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=20,
        help="Number of verified candidate keys to print in the summary.",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=30,
        help=(
            "Query the requested history in chunks of this many observation "
            "days. Defaults to 30 for full-history cleanup runs."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete verified raw objects instead of only reporting them.",
    )
    args = parser.parse_args(argv)

    if args.end_date is not None and args.start_date is None:
        parser.error("--end-date requires --start-date.")
    if (
        args.start_date is not None
        and args.end_date is not None
        and args.end_date < args.start_date
    ):
        parser.error("--end-date must be on or after --start-date.")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero.")
    if args.preview < 0:
        parser.error("--preview cannot be negative.")
    if args.chunk_days <= 0:
        parser.error("--chunk-days must be greater than zero.")
    if args.apply and not any(
        value is not None
        for value in (args.start_date, args.end_date, args.before_date, args.limit)
    ):
        parser.error(
            "--apply requires a scope limiter such as --before-date, "
            "--start-date/--end-date, or --limit."
        )
    return args


def _audio_extension(audio_url: str | None) -> str:
    lowered = (audio_url or "").lower()
    for suffix in _AUDIO_SUFFIXES:
        if suffix in lowered:
            return suffix
    return ".flac"


def _resolved_audio_filename(row: dict[str, object]) -> str:
    audio_filename = row.get("audio_filename")
    if audio_filename:
        name = PurePosixPath(str(audio_filename)).name
        if PurePosixPath(name).suffix:
            return name
        return f"{name}{_audio_extension(row.get('audio_url'))}"
    return (
        f"{row['detection_id']}_{row['species_id']}"
        f"{_audio_extension(row.get('audio_url'))}"
    )


def _raw_object_key(row: dict[str, object]) -> str:
    return str(
        PurePosixPath(
            row["observation_date"].isoformat(),
            "audio",
            _resolved_audio_filename(row),
        )
    )


def _processed_clip_key(row: dict[str, object]) -> str:
    return str(
        PurePosixPath(
            "birdweather",
            row["observation_date"].isoformat(),
            str(row["station_id"]),
            f"{row['detection_id']}.flac",
        )
    )


def _sql_audio_extension_expr(
    *,
    audio_url_expr: str = "audio_url",
) -> str:
    lowered_audio_url = f"lower(coalesce({audio_url_expr}, ''))"
    return (
        "CASE "
        f"WHEN {lowered_audio_url} LIKE '%%.wav%%' THEN '.wav' "
        f"WHEN {lowered_audio_url} LIKE '%%.mp3%%' THEN '.mp3' "
        f"WHEN {lowered_audio_url} LIKE '%%.m4a%%' THEN '.m4a' "
        f"WHEN {lowered_audio_url} LIKE '%%.aac%%' THEN '.aac' "
        f"WHEN {lowered_audio_url} LIKE '%%.ogg%%' THEN '.ogg' "
        "ELSE '.flac' END"
    )


def _sql_resolved_audio_filename_expr(
    *,
    audio_url_expr: str = "audio_url",
    audio_filename_expr: str = "audio_filename",
    detection_id_expr: str = "detection_id::text",
    species_id_expr: str = "species_id::text",
) -> str:
    basename_expr = f"regexp_replace({audio_filename_expr}, '^.*/', '')"
    audio_extension_expr = _sql_audio_extension_expr(audio_url_expr=audio_url_expr)
    return (
        "CASE "
        f"WHEN {audio_filename_expr} IS NOT NULL AND {audio_filename_expr} <> '' "
        f"THEN CASE WHEN position('.' in {basename_expr}) > 0 "
        f"THEN {basename_expr} ELSE {basename_expr} || {audio_extension_expr} END "
        f"ELSE {detection_id_expr} || '_' || {species_id_expr} || {audio_extension_expr} "
        "END"
    )


def _seed_batch_size(limit: int | None) -> int:
    if limit is None:
        return _SEED_BATCH_MIN
    return max(_SEED_BATCH_MIN, limit * _SEED_BATCH_MULTIPLIER)


def _load_candidate_seed_rows(
    pool: ConnectionPool,
    *,
    start_date: date,
    end_date: date,
    cursor: tuple[date, str] | None,
    limit: int,
) -> list[dict[str, object]]:
    import psycopg2.extras

    resolved_audio_filename_expr = _sql_resolved_audio_filename_expr()
    cursor_filter = ""
    params: list[object] = [start_date, end_date, start_date, end_date]
    if cursor is not None:
        cursor_filter = """
          AND (
                observation_date > %s
             OR (observation_date = %s AND resolved_audio_filename > %s)
          )
        """
        params.extend([cursor[0], cursor[0], cursor[1]])
    params.append(limit)

    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH processed_rows AS (
                SELECT
                    observation_date,
                    detected_at,
                    detection_id::text AS detection_id,
                    COALESCE(species_id::text, '') AS species_id,
                    COALESCE(NULLIF(station_id::text, ''), 'unknown_station') AS station_id,
                    audio_url,
                    audio_filename,
                    audio_filesize_bytes,
                    {resolved_audio_filename_expr} AS resolved_audio_filename
                FROM birdweather_tbl
                WHERE audio_url IS NOT NULL
                  AND has_processed_clip
                  AND detected_at >= %s::date
                  AND detected_at < (%s::date + INTERVAL '1 day')
                  AND observation_date >= %s
                  AND observation_date <= %s
            )
            SELECT DISTINCT ON (observation_date, resolved_audio_filename)
                observation_date,
                detected_at,
                detection_id,
                species_id,
                station_id,
                audio_url,
                audio_filename,
                audio_filesize_bytes,
                resolved_audio_filename
            FROM processed_rows
            WHERE TRUE
              {cursor_filter}
            ORDER BY observation_date, resolved_audio_filename, detected_at, detection_id
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def _load_candidate_rows_for_seed(
    pool: ConnectionPool,
    *,
    observation_date: date,
    resolved_audio_filename: str,
) -> list[dict[str, object]]:
    import psycopg2.extras

    resolved_audio_filename_expr = _sql_resolved_audio_filename_expr()
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH candidate_rows AS (
                SELECT
                    observation_date,
                    detected_at,
                    detection_id::text AS detection_id,
                    COALESCE(species_id::text, '') AS species_id,
                    COALESCE(NULLIF(station_id::text, ''), 'unknown_station') AS station_id,
                    audio_url,
                    audio_filename,
                    audio_filesize_bytes,
                    has_processed_clip,
                    {resolved_audio_filename_expr} AS resolved_audio_filename
                FROM birdweather_tbl
                WHERE audio_url IS NOT NULL
                  AND detected_at >= %s::date
                  AND detected_at < (%s::date + INTERVAL '1 day')
                  AND observation_date = %s
            )
            SELECT
                observation_date,
                detected_at,
                detection_id,
                species_id,
                station_id,
                audio_url,
                audio_filename,
                audio_filesize_bytes,
                has_processed_clip
            FROM candidate_rows
            WHERE resolved_audio_filename = %s
            ORDER BY detected_at, detection_id
            """,
            (
                observation_date,
                observation_date,
                observation_date,
                resolved_audio_filename,
            ),
        )
        return [dict(row) for row in cur.fetchall()]


def _candidate_from_rows(
    raw_object_key: str,
    rows: list[dict[str, object]],
) -> PruneCandidate | None:
    if not rows or not all(bool(row["has_processed_clip"]) for row in rows):
        return None

    processed_clip_keys = {
        _processed_clip_key(row)
        for row in rows
        if row["has_processed_clip"]
    }
    raw_sizes = [
        int(size_bytes)
        for row in rows
        if (size_bytes := row.get("audio_filesize_bytes")) is not None
    ]
    observation_dates = [row["observation_date"] for row in rows]
    return PruneCandidate(
        raw_object_key=raw_object_key,
        row_count=len(rows),
        processed_clip_keys=tuple(sorted(processed_clip_keys)),
        first_observation_date=min(observation_dates),
        last_observation_date=max(observation_dates),
        raw_size_bytes=max(raw_sizes) if raw_sizes else None,
    )


def _load_candidates(
    pool: ConnectionPool,
    args: argparse.Namespace,
) -> list[PruneCandidate]:
    assert args.start_date is not None
    assert args.end_date is not None

    candidates: list[PruneCandidate] = []
    cursor: tuple[date, str] | None = None
    batch_size = _seed_batch_size(args.limit)

    while args.limit is None or len(candidates) < args.limit:
        seed_rows = _load_candidate_seed_rows(
            pool,
            start_date=args.start_date,
            end_date=args.end_date,
            cursor=cursor,
            limit=batch_size,
        )
        if not seed_rows:
            break

        for seed_row in seed_rows:
            observation_date = seed_row["observation_date"]
            resolved_audio_filename = str(seed_row["resolved_audio_filename"])
            raw_object_key = str(
                PurePosixPath(
                    observation_date.isoformat(),
                    "audio",
                    resolved_audio_filename,
                )
            )
            candidate_rows = _load_candidate_rows_for_seed(
                pool,
                observation_date=observation_date,
                resolved_audio_filename=resolved_audio_filename,
            )
            candidate = _candidate_from_rows(raw_object_key, candidate_rows)
            if candidate is None:
                continue

            candidates.append(candidate)
            if args.limit is not None and len(candidates) >= args.limit:
                return candidates

        last_seed_row = seed_rows[-1]
        cursor = (
            last_seed_row["observation_date"],
            str(last_seed_row["resolved_audio_filename"]),
        )
        if len(seed_rows) < batch_size:
            break

    return candidates


def _available_observation_bounds(
    pool: ConnectionPool,
    *,
    before_date: date | None,
) -> tuple[date | None, date | None]:
    with pool.cursor() as cur:
        earliest_params: list[object] = []
        earliest_filter = ""
        if before_date is not None:
            earliest_filter = "AND detected_at < %s::date"
            earliest_params.append(before_date)
        cur.execute(
            f"""
            SELECT observation_date
            FROM birdweather_tbl
            WHERE audio_url IS NOT NULL
              {earliest_filter}
            ORDER BY detected_at ASC
            LIMIT 1
            """,
            earliest_params,
        )
        earliest_row = cur.fetchone()

        latest_params: list[object] = []
        latest_filter = ""
        if before_date is not None:
            latest_filter = "AND detected_at < %s::date"
            latest_params.append(before_date)
        cur.execute(
            f"""
            SELECT observation_date
            FROM birdweather_tbl
            WHERE audio_url IS NOT NULL
              {latest_filter}
            ORDER BY detected_at DESC
            LIMIT 1
            """,
            latest_params,
        )
        latest_row = cur.fetchone()

    if earliest_row is None or latest_row is None:
        return None, None
    return earliest_row[0], latest_row[0]


def _resolve_requested_window(
    pool: ConnectionPool,
    args: argparse.Namespace,
) -> tuple[date, date] | None:
    if args.start_date is not None:
        window_start = args.start_date
        if args.end_date is not None:
            window_end = args.end_date
        elif args.before_date is not None:
            window_end = args.before_date - timedelta(days=1)
        else:
            _, available_end = _available_observation_bounds(
                pool,
                before_date=None,
            )
            if available_end is None:
                return None
            window_end = available_end

        if args.before_date is not None:
            window_end = min(window_end, args.before_date - timedelta(days=1))
        if window_start > window_end:
            return None
        return window_start, window_end

    available_start, available_end = _available_observation_bounds(
        pool,
        before_date=args.before_date,
    )
    if available_start is None or available_end is None:
        return None

    window_start = args.start_date or available_start
    window_end = args.end_date or available_end
    if args.before_date is not None:
        window_end = min(window_end, args.before_date - timedelta(days=1))

    window_start = max(window_start, available_start)
    window_end = min(window_end, available_end)
    if window_start > window_end:
        return None
    return window_start, window_end


def _iter_windows(
    start_date: date,
    end_date: date,
    *,
    chunk_days: int,
):
    cursor = start_date
    while cursor <= end_date:
        window_end = min(end_date, cursor + timedelta(days=chunk_days - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def _window_args(
    args: argparse.Namespace,
    *,
    start_date: date,
    end_date: date,
    limit: int | None,
) -> argparse.Namespace:
    return argparse.Namespace(
        start_date=start_date,
        end_date=end_date,
        before_date=None,
        limit=limit,
        preview=args.preview,
        chunk_days=args.chunk_days,
        apply=args.apply,
    )


def _format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"

    units = ("KiB", "MiB", "GiB", "TiB")
    value = float(size_bytes)
    for unit in units:
        value /= 1024.0
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TiB"


def _with_storage_retry(
    operation: Callable[..., Any],
    *operation_args: Any,
    object_key: str,
    operation_label: str,
    stats: PruneStats,
    retry_attempts: int,
    retry_backoff_seconds: float,
    **operation_kwargs: Any,
) -> Any:
    from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError

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
                "MinIO unavailable while %s for %s; retrying in %.1f s (%d/%d)",
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


def _build_prune_verification_cache(
    candidates: list[PruneCandidate],
    *,
    store: AudioStore,
    stats: PruneStats,
    include_raw_objects: bool,
) -> _PruneVerificationCache:
    from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError

    processed_clip_keys_by_date: dict[date, frozenset[str]] = {}
    raw_object_keys_by_date: dict[date, frozenset[str]] = {}
    unavailable_processed_dates: set[date] = set()
    unavailable_raw_dates: set[date] = set()

    for observation_date in sorted(
        {candidate.first_observation_date for candidate in candidates}
    ):
        processed_prefix = f"birdweather/{observation_date.isoformat()}/"
        try:
            processed_clip_keys_by_date[observation_date] = frozenset(
                _with_storage_retry(
                    store.list_objects,
                    processed_prefix,
                    object_key=processed_prefix,
                    operation_label="listing processed clips",
                    stats=stats,
                    retry_attempts=_STORAGE_RETRY_ATTEMPTS,
                    retry_backoff_seconds=_STORAGE_RETRY_BACKOFF_SECONDS,
                    bucket=_BUCKET_PROCESSED_AUDIO,
                    recursive=True,
                )
            )
        except AudioStoreUnavailableError:
            unavailable_processed_dates.add(observation_date)

        if not include_raw_objects:
            continue

        raw_prefix = f"{observation_date.isoformat()}/audio/"
        try:
            raw_object_keys_by_date[observation_date] = frozenset(
                _with_storage_retry(
                    store.list_objects,
                    raw_prefix,
                    object_key=raw_prefix,
                    operation_label="listing raw objects",
                    stats=stats,
                    retry_attempts=_STORAGE_RETRY_ATTEMPTS,
                    retry_backoff_seconds=_STORAGE_RETRY_BACKOFF_SECONDS,
                    bucket=_BUCKET_RAW_AUDIO,
                    recursive=True,
                )
            )
        except AudioStoreUnavailableError:
            unavailable_raw_dates.add(observation_date)

    return _PruneVerificationCache(
        processed_clip_keys_by_date=processed_clip_keys_by_date,
        raw_object_keys_by_date=raw_object_keys_by_date,
        unavailable_processed_dates=frozenset(unavailable_processed_dates),
        unavailable_raw_dates=frozenset(unavailable_raw_dates),
    )


def _log_prune_progress(
    *,
    label: str,
    handled: int,
    total: int,
    stats: PruneStats,
) -> None:
    logger.info(
        "%s progress: handled %d/%d candidate(s); verified=%d deleted=%d missing_raw=%d missing_processed=%d storage_unavailable=%d delete_unavailable=%d.",
        label,
        handled,
        total,
        stats.verified_objects,
        stats.deleted_objects,
        stats.skipped_missing_raw,
        stats.skipped_missing_processed,
        stats.skipped_storage_unavailable,
        stats.skipped_delete_unavailable,
    )


def _verify_candidate(
    candidate: PruneCandidate,
    store: AudioStore,
    stats: PruneStats,
    *,
    cache: _PruneVerificationCache | None = None,
) -> tuple[bool | None, tuple[str, ...]]:
    from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError

    observation_date = candidate.first_observation_date
    if cache is not None:
        if observation_date in cache.unavailable_raw_dates:
            return None, ()
        if observation_date in cache.unavailable_processed_dates:
            return None, ()

        raw_object_keys = cache.raw_object_keys_by_date.get(observation_date)
        if raw_object_keys is not None and candidate.raw_object_key not in raw_object_keys:
            return False, ()

        processed_clip_keys = cache.processed_clip_keys_by_date.get(
            observation_date,
            frozenset(),
        )
        missing_processed = tuple(
            key for key in candidate.processed_clip_keys if key not in processed_clip_keys
        )
        return not missing_processed, missing_processed

    try:
        raw_exists = _with_storage_retry(
            store.exists,
            candidate.raw_object_key,
            object_key=candidate.raw_object_key,
            operation_label="checking raw object",
            stats=stats,
            retry_attempts=_STORAGE_RETRY_ATTEMPTS,
            retry_backoff_seconds=_STORAGE_RETRY_BACKOFF_SECONDS,
            bucket=_BUCKET_RAW_AUDIO,
        )
    except AudioStoreUnavailableError:
        return None, ()

    if not raw_exists:
        return False, ()

    missing_processed: list[str] = []
    for key in candidate.processed_clip_keys:
        try:
            processed_exists = _with_storage_retry(
                store.exists,
                key,
                object_key=key,
                operation_label="checking processed clip",
                stats=stats,
                retry_attempts=_STORAGE_RETRY_ATTEMPTS,
                retry_backoff_seconds=_STORAGE_RETRY_BACKOFF_SECONDS,
                bucket=_BUCKET_PROCESSED_AUDIO,
            )
        except AudioStoreUnavailableError:
            return None, ()

        if not processed_exists:
            missing_processed.append(key)

    return not missing_processed, tuple(missing_processed)


def _verify_local_candidate(
    candidate: PruneCandidate,
    *,
    raw_root: str | None,
    store: AudioStore,
    stats: PruneStats,
    cache: _PruneVerificationCache | None = None,
) -> tuple[bool | None, tuple[str, ...]]:
    raw_path = local_raw_audio_path_from_key(
        candidate.raw_object_key,
        raw_root=raw_root,
    )
    if not raw_path.is_file():
        return False, ()

    observation_date = candidate.first_observation_date
    if cache is not None:
        if observation_date in cache.unavailable_processed_dates:
            return None, ()

        processed_clip_keys = cache.processed_clip_keys_by_date.get(
            observation_date,
            frozenset(),
        )
        missing_processed = tuple(
            key for key in candidate.processed_clip_keys if key not in processed_clip_keys
        )
        return not missing_processed, missing_processed

    missing_processed: list[str] = []
    for key in candidate.processed_clip_keys:
        try:
            processed_exists = _with_storage_retry(
                store.exists,
                key,
                object_key=key,
                operation_label="checking processed clip",
                stats=stats,
                retry_attempts=_STORAGE_RETRY_ATTEMPTS,
                retry_backoff_seconds=_STORAGE_RETRY_BACKOFF_SECONDS,
                bucket=_BUCKET_PROCESSED_AUDIO,
            )
        except Exception as exc:  # pragma: no cover - storage error type tested elsewhere
            from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError

            if isinstance(exc, AudioStoreUnavailableError):
                return None, ()
            raise

        if not processed_exists:
            missing_processed.append(key)

    return not missing_processed, tuple(missing_processed)


def _present_raw_filenames(
    raw_root: str | None,
    observation_date: date,
) -> set[str]:
    """Return raw-audio filenames physically present on disk for one day.

    Pruning only ever deletes local raw files, so the candidate set is bounded
    by what is actually staged under ``<raw_root>/<day>/audio/``. Listing that
    directory is O(files-present) and lets the caller skip the expensive per-day
    ``birdweather_tbl`` candidate scan when nothing remains to prune.
    """
    audio_dir = (
        resolve_local_birdweather_raw_root(raw_root)
        / observation_date.isoformat()
        / "audio"
    )
    try:
        with os.scandir(audio_dir) as entries:
            return {entry.name for entry in entries if entry.is_file()}
    except (FileNotFoundError, NotADirectoryError):
        return set()


def _load_local_prune_candidates(
    pool: ConnectionPool,
    *,
    start_date: date,
    end_date: date,
    limit: int | None,
    raw_root: str | None,
) -> list[PruneCandidate]:
    import psycopg2
    import psycopg2.extras

    candidates: list[PruneCandidate] = []
    for observation_date, _window_end in _iter_windows(
        start_date,
        end_date,
        chunk_days=1,
    ):
        present_filenames = _present_raw_filenames(raw_root, observation_date)
        if not present_filenames:
            # Nothing is staged on disk for this day, so there is nothing to
            # prune. Skip the per-day candidate scan entirely -- this keeps
            # re-pruning already-cleaned days fast instead of forcing a full
            # chunk read of birdweather_tbl for zero deletions.
            continue

        db_attempt = 0
        while True:
            try:
                with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT
                            observation_date,
                            detection_id::text AS detection_id,
                            COALESCE(species_id::text, '') AS species_id,
                            COALESCE(NULLIF(station_id::text, ''), 'unknown_station') AS station_id,
                            audio_url,
                            audio_filename,
                            audio_filesize_bytes,
                            has_processed_clip
                        FROM birdweather_tbl
                        WHERE audio_url IS NOT NULL
                          AND detected_at >= %s::date
                          AND detected_at < (%s::date + INTERVAL '1 day')
                          AND observation_date = %s
                        """,
                        (observation_date, observation_date, observation_date),
                    )
                    day_rows = [dict(row) for row in cur.fetchall()]
                break
            except (psycopg2.InterfaceError, psycopg2.OperationalError) as exc:
                db_attempt += 1
                if db_attempt >= _DATABASE_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "BirdWeather local prune candidate load lost DB connection for %s "
                    "(attempt %d/%d): %s. Retrying in %.1fs.",
                    observation_date.isoformat(),
                    db_attempt,
                    _DATABASE_RETRY_ATTEMPTS,
                    exc,
                    _DATABASE_RETRY_BACKOFF_SECONDS,
                )
                time.sleep(_DATABASE_RETRY_BACKOFF_SECONDS)

        rows_by_raw_object_key: dict[str, list[dict[str, object]]] = {}
        for row in day_rows:
            if _resolved_audio_filename(row) not in present_filenames:
                # Only consider rows whose raw file is actually staged on disk.
                continue
            rows_by_raw_object_key.setdefault(_raw_object_key(row), []).append(row)

        for raw_object_key in sorted(rows_by_raw_object_key):
            candidate = _candidate_from_rows(
                raw_object_key,
                rows_by_raw_object_key[raw_object_key],
            )
            if candidate is None:
                continue

            candidates.append(candidate)
            if limit is not None and len(candidates) >= limit:
                return candidates

    return candidates


def prune_birdweather_local_raw_audio_files(
    *,
    start_date: date,
    end_date: date,
    raw_root: str,
    limit: int | None = None,
    pool: ConnectionPool | None = None,
    store: AudioStore | None = None,
    apply: bool = True,
    verify_processed: bool = True,
) -> tuple[PruneStats, list[PruneCandidate], list[tuple[str, tuple[str, ...]]]]:
    """Delete verified local BirdWeather raw-audio files for a date window.

    When ``verify_processed`` is False, skip the MinIO re-list (which is
    prohibitively slow when the object store is on spinning disk) and trust the
    upstream process-phase confirmation that clips were uploaded; the local raw
    file is still confirmed present before deletion.
    """
    from avian_biosurveillance.db.database import ConnectionPool as RuntimeConnectionPool
    from avian_biosurveillance.db.minio_client import AudioStore as RuntimeAudioStore

    own_pool = pool is None
    pool = pool or RuntimeConnectionPool(min_conn=1, max_conn=4)
    store = store or RuntimeAudioStore()

    stats = PruneStats()
    verified_preview: list[PruneCandidate] = []
    missing_processed_examples: list[tuple[str, tuple[str, ...]]] = []

    try:
        candidates = _load_local_prune_candidates(
            pool,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            raw_root=raw_root,
        )
        stats.candidate_objects = len(candidates)
        stats.candidate_bytes = sum(candidate.raw_size_bytes or 0 for candidate in candidates)
        verification_cache = (
            _build_prune_verification_cache(
                candidates,
                store=store,
                stats=stats,
                include_raw_objects=False,
            )
            if verify_processed
            else None
        )

        for index, candidate in enumerate(candidates, start=1):
            if verify_processed:
                is_safe, missing_processed = _verify_local_candidate(
                    candidate,
                    raw_root=raw_root,
                    store=store,
                    stats=stats,
                    cache=verification_cache,
                )
            else:
                # Trust the upstream process-phase confirmation: candidates are
                # already restricted to fully-processed rows, and _run_day aborts
                # before prune if any upload was unavailable. Skip the
                # HDD-prohibitive MinIO re-list and delete the local raw once we
                # confirm the local file is present.
                is_safe = local_raw_audio_path_from_key(
                    candidate.raw_object_key,
                    raw_root=raw_root,
                ).is_file()
                missing_processed = ()
            if is_safe is None:
                stats.skipped_storage_unavailable += 1
                logger.warning(
                    "Skipping local raw %s because processed-clip verification "
                    "was unavailable; NOT deleting it.",
                    candidate.raw_object_key,
                )
                continue
            elif is_safe is False:
                raw_path = local_raw_audio_path_from_key(
                    candidate.raw_object_key,
                    raw_root=raw_root,
                )
                if not raw_path.is_file():
                    stats.skipped_missing_raw += 1
                else:
                    stats.skipped_missing_processed += 1
                    if len(missing_processed_examples) < 20:
                        missing_processed_examples.append(
                            (candidate.raw_object_key, missing_processed)
                        )
                continue

            stats.verified_objects += 1
            stats.verified_bytes += int(candidate.raw_size_bytes or 0)
            verified_preview.append(candidate)
            if not apply:
                continue

            raw_path = local_raw_audio_path_from_key(
                candidate.raw_object_key,
                raw_root=raw_root,
            )
            try:
                raw_path.unlink()
            except FileNotFoundError:
                stats.skipped_missing_raw += 1
                continue
            except OSError:
                logger.exception("Failed to delete local raw audio %s", raw_path)
                stats.skipped_delete_unavailable += 1
                continue

            stats.deleted_objects += 1
            stats.deleted_bytes += int(candidate.raw_size_bytes or 0)

            if index % _PRUNE_PROGRESS_LOG_INTERVAL == 0 or index == len(candidates):
                _log_prune_progress(
                    label="BirdWeather local prune",
                    handled=index,
                    total=len(candidates),
                    stats=stats,
                )

        return stats, verified_preview, missing_processed_examples
    finally:
        if own_pool:
            pool.close()



def _log_verified_preview(
    candidates: list[PruneCandidate],
    *,
    limit: int,
) -> None:
    if not candidates or limit == 0:
        return

    for candidate in candidates[:limit]:
        logger.info(
            "Verified raw candidate: %s rows=%d clips=%d window=%s..%s size=%s",
            candidate.raw_object_key,
            candidate.row_count,
            len(candidate.processed_clip_keys),
            candidate.first_observation_date,
            candidate.last_observation_date,
            _format_bytes(candidate.raw_size_bytes or 0),
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    from avian_biosurveillance.config.runtime import load_runtime_env
    from avian_biosurveillance.db.database import ConnectionPool
    from avian_biosurveillance.db.minio_client import (
        AudioStore,
        AudioStoreUnavailableError,
    )

    load_runtime_env()
    pool = ConnectionPool(min_conn=1, max_conn=4)
    store = AudioStore()
    stats = PruneStats()
    verified_preview: list[PruneCandidate] = []
    missing_processed_examples: list[tuple[str, tuple[str, ...]]] = []

    try:
        requested_window = _resolve_requested_window(pool, args)
        if requested_window is None:
            logger.info("No BirdWeather raw-audio rows matched the requested scope.")
            return 0

        window_start, window_end = requested_window
        logger.info(
            "Scanning BirdWeather raw-audio candidates across %s..%s in %d-day chunk(s).",
            window_start,
            window_end,
            args.chunk_days,
        )

        remaining_limit = args.limit
        deleted_progress = 0
        for batch_start, batch_end in _iter_windows(
            window_start,
            window_end,
            chunk_days=args.chunk_days,
        ):
            if remaining_limit is not None and remaining_limit <= 0:
                break

            batch_args = _window_args(
                args,
                start_date=batch_start,
                end_date=batch_end,
                limit=remaining_limit,
            )
            candidates = _load_candidates(pool, batch_args)
            if not candidates:
                continue

            batch_bytes = sum(candidate.raw_size_bytes or 0 for candidate in candidates)
            stats.candidate_objects += len(candidates)
            stats.candidate_bytes += batch_bytes
            logger.info(
                "Loaded %d raw-audio candidate object(s) for %s..%s (%s).",
                len(candidates),
                batch_start,
                batch_end,
                _format_bytes(batch_bytes),
            )
            verification_cache = _build_prune_verification_cache(
                candidates,
                store=store,
                stats=stats,
                include_raw_objects=True,
            )

            for index, candidate in enumerate(candidates, start=1):
                is_safe, missing_processed = _verify_candidate(
                    candidate,
                    store,
                    stats,
                    cache=verification_cache,
                )
                if is_safe is None:
                    stats.skipped_storage_unavailable += 1
                    logger.warning(
                        "Skipping %s because MinIO verification remained unavailable after retries.",
                        candidate.raw_object_key,
                    )
                    continue
                if not is_safe and not missing_processed:
                    stats.skipped_missing_raw += 1
                    continue
                if not is_safe:
                    stats.skipped_missing_processed += 1
                    if len(missing_processed_examples) < 5:
                        missing_processed_examples.append(
                            (candidate.raw_object_key, missing_processed[:3])
                        )
                    continue

                stats.verified_objects += 1
                stats.verified_bytes += candidate.raw_size_bytes or 0
                if len(verified_preview) < args.preview:
                    verified_preview.append(candidate)

                if not args.apply:
                    continue

                try:
                    deleted = _with_storage_retry(
                        store.delete_object,
                        candidate.raw_object_key,
                        object_key=candidate.raw_object_key,
                        operation_label="deleting raw object",
                        stats=stats,
                        retry_attempts=_STORAGE_RETRY_ATTEMPTS,
                        retry_backoff_seconds=_STORAGE_RETRY_BACKOFF_SECONDS,
                        bucket=_BUCKET_RAW_AUDIO,
                    )
                except AudioStoreUnavailableError:
                    stats.skipped_delete_unavailable += 1
                    logger.warning(
                        "Could not delete %s because MinIO remained unavailable after retries.",
                        candidate.raw_object_key,
                    )
                    continue

                if deleted:
                    stats.deleted_objects += 1
                    stats.deleted_bytes += candidate.raw_size_bytes or 0
                    deleted_progress += 1
                    if deleted_progress % 100 == 0:
                        logger.info(
                            "Deleted %d verified raw object(s) so far.",
                            deleted_progress,
                        )

                if index % _PRUNE_PROGRESS_LOG_INTERVAL == 0 or index == len(candidates):
                    _log_prune_progress(
                        label="BirdWeather raw prune",
                        handled=index,
                        total=len(candidates),
                        stats=stats,
                    )

            if remaining_limit is not None:
                remaining_limit -= len(candidates)

        logger.info(
            "Verified %d/%d raw object(s) safe to delete after MinIO checks (%s).",
            stats.verified_objects,
            stats.candidate_objects,
            _format_bytes(stats.verified_bytes),
        )
        if stats.skipped_missing_raw:
            logger.info(
                "Skipped %d candidate object(s) because the raw object was already absent.",
                stats.skipped_missing_raw,
            )
        if stats.skipped_missing_processed:
            logger.info(
                "Skipped %d candidate object(s) because one or more processed clips were missing.",
                stats.skipped_missing_processed,
            )
            for raw_key, missing_processed in missing_processed_examples:
                logger.warning(
                    "Processed clips missing for %s: %s",
                    raw_key,
                    ", ".join(missing_processed),
                )
        if stats.storage_retries:
            logger.info(
                "Retried MinIO operations %d time(s) after transient storage errors.",
                stats.storage_retries,
            )
        if stats.skipped_storage_unavailable:
            logger.warning(
                "Skipped %d candidate object(s) because MinIO verification remained unavailable after retries.",
                stats.skipped_storage_unavailable,
            )
        if stats.skipped_delete_unavailable:
            logger.warning(
                "Could not delete %d verified raw object(s) because MinIO delete remained unavailable after retries.",
                stats.skipped_delete_unavailable,
            )

        _log_verified_preview(verified_preview, limit=args.preview)

        if not args.apply:
            logger.info(
                "Dry run only. Re-run with --apply to delete the %d verified raw object(s).",
                stats.verified_objects,
            )
            return 0

        logger.info(
            "Deleted %d raw object(s), reclaiming an estimated %s.",
            stats.deleted_objects,
            _format_bytes(stats.deleted_bytes),
        )
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("BirdWeather raw-audio prune failed.")
        raise