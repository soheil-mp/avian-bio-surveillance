"""BirdWeather feature engineering pipeline."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import PurePosixPath
from typing import Any

import numpy as np
import psycopg2.extras
import soundfile as sf
from minio.error import S3Error

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import (
    BUCKET_PROCESSED_AUDIO,
    AudioStore,
    AudioStoreUnavailableError,
)
from avian_biosurveillance.features.audio_artifacts import (
    render_bar_waveform_png,
    render_smooth_waveform_png,
    render_spectrogram_png,
)
from avian_biosurveillance.features.embeddings import (
    EmbeddingResult,
    compute_embedding_results_batch,
    warm_embedding_models,
)
from avian_biosurveillance.ingestion.background_sound_classification import (
    AmbientTag,
    BackgroundSoundClassifier,
)
from avian_biosurveillance.processing.birdweather_audio import (
    BirdWeatherAudioCandidate,
    processed_clip_key,
)


logger = logging.getLogger(__name__)

_QUALIFIED_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_DEFAULT_EMBEDDING_BATCH_SIZE = 32


@dataclass(slots=True)
class FeatureEngineeringStats:
    """Summary emitted by the BirdWeather feature engineering pipeline."""

    candidates: int = 0
    featured: int = 0
    missing_clip: int = 0
    storage_retries: int = 0
    storage_unavailable: int = 0
    decode_failed: int = 0
    artifact_upload_failed: int = 0
    artifacts_uploaded: int = 0
    background_classified: int = 0
    background_failed: int = 0
    embeddings_written: int = 0
    embeddings_skipped: int = 0
    birdnet_embeddings_written: int = 0
    birdnet_embeddings_skipped: int = 0
    perch_v2_embeddings_written: int = 0
    perch_v2_embeddings_skipped: int = 0
    db_updated: int = 0

    def to_metadata(self) -> dict[str, int]:
        return asdict(self)


def engineer_birdweather_audio_features(
    *,
    lookback_days: int | None = 2,
    limit: int | None = 500,
    force: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
    shard_count: int | None = None,
    shard_index: int | None = None,
    background_device: str | None = None,
    background_top_k: int | None = 5,
    background_min_confidence: float = 0.10,
    include_background_classification: bool = True,
    include_feature_artifacts: bool = True,
    embedding_models: Sequence[str] | None = None,
    progress_every: int | None = None,
    progress_label: str | None = None,
    continue_on_storage_unavailable: bool = False,
    storage_retry_attempts: int = 0,
    storage_retry_backoff_seconds: float = 1.0,
    store: AudioStore | None = None,
    pool: ConnectionPool | None = None,
) -> FeatureEngineeringStats:
    """Generate BirdWeather embeddings and optional feature artifacts from processed clips."""
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=4)
    store = store or AudioStore()
    stats = FeatureEngineeringStats()
    classifier: BackgroundSoundClassifier | None = None
    selected_models = (
        tuple(embedding_models)
        if embedding_models is not None
        else ("birdnet", "perch_v2")
    )

    try:
        rows = _load_feature_candidates(
            pool,
            lookback_days=lookback_days,
            limit=limit,
            force=force,
            start_date=start_date,
            end_date=end_date,
            shard_count=shard_count,
            shard_index=shard_index,
            include_background_classification=include_background_classification,
            embedding_models=selected_models,
        )
        stats.candidates = len(rows)
        total_rows = len(rows)
        progress_interval = None
        if progress_every is not None and progress_every > 0:
            progress_interval = int(progress_every)

        if total_rows and progress_interval:
            label = progress_label or _feature_progress_label(start_date, end_date)
            logger.info(
                "%s loaded %d candidate row(s); logging every %d row(s).",
                label,
                total_rows,
                progress_interval,
            )

        if total_rows:
            _warm_embedding_models(selected_models)

        embedding_batch: list[_FeatureWorkItem] = []

        def _flush_embedding_batch() -> None:
            if not embedding_batch:
                return
            _process_embedding_batch(
                embedding_batch,
                pool=pool,
                stats=stats,
                progress_interval=progress_interval,
                progress_label=progress_label,
                start_date=start_date,
                end_date=end_date,
                total_rows=total_rows,
                embedding_models=selected_models,
                force=force,
            )
            embedding_batch.clear()

        embedding_batch_size = _resolve_embedding_batch_size()
        for prepared in _iter_prepared_clips(
            rows,
            store,
            max_workers=_resolve_prefetch_workers(),
            retry_attempts=storage_retry_attempts,
            retry_backoff_seconds=storage_retry_backoff_seconds,
        ):
            index = prepared.index
            row = prepared.row
            clip_key = prepared.clip_key
            stats.storage_retries += prepared.storage_retries

            if prepared.status == "storage_unavailable":
                _flush_embedding_batch()
                logger.error(
                    "Processed-audio storage unavailable for %s",
                    clip_key,
                    exc_info=prepared.error,
                )
                if not continue_on_storage_unavailable:
                    raise prepared.error  # type: ignore[misc]
                stats.storage_unavailable += 1
                _log_feature_progress(
                    progress_interval,
                    index,
                    total_rows,
                    stats,
                    progress_label,
                    start_date,
                    end_date,
                )
                continue

            if prepared.status == "missing":
                _flush_embedding_batch()
                stats.missing_clip += 1
                _log_feature_progress(
                    progress_interval,
                    index,
                    total_rows,
                    stats,
                    progress_label,
                    start_date,
                    end_date,
                )
                continue

            if prepared.status == "decode_failed":
                _flush_embedding_batch()
                logger.error(
                    "Failed to decode processed clip %s",
                    clip_key,
                    exc_info=prepared.error,
                )
                stats.decode_failed += 1
                _log_feature_progress(
                    progress_interval,
                    index,
                    total_rows,
                    stats,
                    progress_label,
                    start_date,
                    end_date,
                )
                continue

            audio = prepared.audio
            sample_rate = prepared.sample_rate
            if audio is None or sample_rate is None:
                # Only status == "ok" reaches this point; this guard narrows the
                # optional fields for the type checker and is never hit.
                continue

            artifact_manifest = _base_artifact_manifest(clip_key)
            if include_feature_artifacts:
                try:
                    artifact_manifest.update(
                        _upload_feature_artifacts(
                            store,
                            row,
                            audio,
                            sample_rate,
                        )
                    )
                    stats.artifacts_uploaded += 3
                except (
                    AudioStoreUnavailableError,
                    OSError,
                    RuntimeError,
                    S3Error,
                    TypeError,
                    ValueError,
                ) as exc:
                    logger.exception("Failed to upload feature artifacts for %s", clip_key)
                    stats.artifact_upload_failed += 1
                    artifact_manifest.update(
                        _artifact_failure_manifest(
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
            else:
                artifact_manifest["artifact_upload_status"] = "skipped"

            background_payload: dict[str, Any] | None = None
            if include_background_classification:
                try:
                    if classifier is None:
                        classifier = BackgroundSoundClassifier(
                            device=background_device,
                            top_k=background_top_k,
                            min_confidence=background_min_confidence,
                        )
                    background_tags = classifier.classify_array(audio, sample_rate)
                    background_payload = _background_payload("classified", background_tags)
                    stats.background_classified += 1
                except (
                    AttributeError,
                    ImportError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    background_payload = {
                        "status": "classification_failed",
                        "tags": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    stats.background_failed += 1

            embedding_batch.append(
                _FeatureWorkItem(
                    prepared=prepared,
                    background_payload=background_payload,
                    artifact_manifest=artifact_manifest,
                )
            )
            if len(embedding_batch) >= embedding_batch_size:
                _flush_embedding_batch()

        _flush_embedding_batch()

        return stats
    finally:
        if own_pool:
            pool.close()


def _base_artifact_manifest(clip_key: str) -> dict[str, Any]:
    return {
        "processed_clip": f"{BUCKET_PROCESSED_AUDIO}/{clip_key}",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def _artifact_failure_manifest(
    *,
    error: str,
) -> dict[str, Any]:
    return {
        "artifact_upload_status": "failed",
        "artifact_upload_error": error,
    }


def _feature_progress_label(
    start_date: date | None,
    end_date: date | None,
) -> str:
    if start_date is not None and end_date is not None:
        if start_date == end_date:
            return f"[{start_date.isoformat()}]"
        return f"[{start_date.isoformat()} .. {end_date.isoformat()}]"
    return "[birdweather-features]"


def _log_feature_progress(
    progress_interval: int | None,
    index: int,
    total_rows: int,
    stats: FeatureEngineeringStats,
    progress_label: str | None,
    start_date: date | None,
    end_date: date | None,
) -> None:
    if not progress_interval:
        return
    if not (index % progress_interval == 0 or index == total_rows):
        return
    label = progress_label or _feature_progress_label(start_date, end_date)
    logger.info(
        "%s progress %d/%d rows: featured=%d db_updated=%d "
        "embeddings_written=%d embeddings_skipped=%d "
        "birdnet=%d written/%d skipped perch_v2=%d written/%d skipped "
        "missing_clip=%d decode_failed=%d storage_unavailable=%d artifact_upload_failed=%d.",
        label,
        index,
        total_rows,
        stats.featured,
        stats.db_updated,
        stats.embeddings_written,
        stats.embeddings_skipped,
        stats.birdnet_embeddings_written,
        stats.birdnet_embeddings_skipped,
        stats.perch_v2_embeddings_written,
        stats.perch_v2_embeddings_skipped,
        stats.missing_clip,
        stats.decode_failed,
        stats.storage_unavailable,
        stats.artifact_upload_failed,
    )


@dataclass(slots=True)
class _PreparedClip:
    """A processed clip fetched and decoded ahead of the feature-engineering step."""

    index: int
    row: BirdWeatherAudioCandidate
    clip_key: str
    status: str  # "ok" | "missing" | "decode_failed" | "storage_unavailable"
    audio: np.ndarray | None = None
    sample_rate: int | None = None
    storage_retries: int = 0
    error: BaseException | None = None


@dataclass(slots=True)
class _FeatureWorkItem:
    """A prepared clip plus per-row context waiting for batched embedding."""

    prepared: _PreparedClip
    background_payload: dict[str, Any] | None
    artifact_manifest: dict[str, Any]


def _warm_embedding_models(providers: Sequence[str] | None = None) -> None:
    logger.info("Warming BirdWeather embedding models before clip processing.")
    warmup_results = warm_embedding_models(providers=providers)
    unavailable = [
        result for result in warmup_results.values() if result.status != "ready"
    ]
    if not unavailable:
        logger.info("BirdWeather embedding models are ready.")
        return

    for result in unavailable:
        logger.warning(
            "Embedding backend %s is not ready: %s",
            result.provider,
            result.error or result.status,
        )


def _resolve_embedding_batch_size() -> int:
    """Resolve the embedding batch size from the environment."""
    raw = os.environ.get(
        "BIRDWEATHER_EMBEDDING_BATCH_SIZE",
        str(_DEFAULT_EMBEDDING_BATCH_SIZE),
    ).strip()
    try:
        batch_size = int(raw)
    except ValueError:
        batch_size = _DEFAULT_EMBEDDING_BATCH_SIZE
    return max(1, batch_size)


def _process_embedding_batch(
    items: list[_FeatureWorkItem],
    *,
    pool: ConnectionPool,
    stats: FeatureEngineeringStats,
    progress_interval: int | None,
    progress_label: str | None,
    start_date: date | None,
    end_date: date | None,
    total_rows: int,
    embedding_models: Sequence[str] | None = None,
    force: bool = False,
) -> None:
    for item in items:
        if item.prepared.audio is None or item.prepared.sample_rate is None:
            msg = f"Prepared clip {item.prepared.clip_key} is missing audio"
            raise RuntimeError(msg)

    selected = (
        list(embedding_models)
        if embedding_models is not None
        else ["birdnet", "perch_v2"]
    )

    # Compute each selected model only for the rows that are actually missing
    # it (unless force=True). A row that already has, say, BirdNET is not
    # re-embedded for BirdNET even in "both" mode -- only the missing model is
    # computed. Per-row results are merged, and the DB write preserves any
    # model that was not recomputed via COALESCE.
    results_by_item: list[dict[str, EmbeddingResult]] = [{} for _ in items]
    for provider_name in selected:
        target_indexes = [
            index
            for index, item in enumerate(items)
            if _needs_embedding_model(item.prepared.row, provider_name, force=force)
        ]
        if not target_indexes:
            continue
        clips: list[tuple[np.ndarray, int]] = [
            (items[index].prepared.audio, items[index].prepared.sample_rate)
            for index in target_indexes
        ]
        provider_results = compute_embedding_results_batch(
            clips,
            providers=(provider_name,),
        )
        if len(provider_results) != len(target_indexes):
            msg = (
                "Embedding batch returned "
                f"{len(provider_results)} result(s) for {len(target_indexes)} clip(s)"
            )
            raise RuntimeError(msg)
        for index, result in zip(target_indexes, provider_results, strict=True):
            results_by_item[index].update(result)

    for item, embedding_results in zip(items, results_by_item, strict=True):
        item.artifact_manifest["embeddings"] = {
            name: result.manifest() for name, result in embedding_results.items()
        }
        _accumulate_embedding_stats(stats, embedding_results)

        if _update_feature_row(
            pool,
            item.prepared.row,
            background_payload=item.background_payload,
            embedding_results=embedding_results,
            artifact_manifest=item.artifact_manifest,
        ):
            stats.db_updated += 1
            stats.featured += 1

        _log_feature_progress(
            progress_interval,
            item.prepared.index,
            total_rows,
            stats,
            progress_label,
            start_date,
            end_date,
        )


def _needs_embedding_model(
    row: BirdWeatherAudioCandidate,
    provider_name: str,
    *,
    force: bool,
) -> bool:
    """Return True when ``provider_name`` must be computed for ``row``.

    With ``force`` every selected model is recomputed. Otherwise a model is
    only computed when the row does not already have that embedding populated,
    so an already-present model is never re-embedded.
    """
    if force:
        return True
    if provider_name == "birdnet":
        return not row.birdnet_embedding_present
    if provider_name == "perch_v2":
        return not row.perch_v2_embedding_present
    return True


def _accumulate_embedding_stats(
    stats: FeatureEngineeringStats,
    embedding_results: dict[str, EmbeddingResult],
) -> None:
    for provider, result in embedding_results.items():
        if result.status == "embedded":
            stats.embeddings_written += 1
            if provider == "birdnet":
                stats.birdnet_embeddings_written += 1
            elif provider == "perch_v2":
                stats.perch_v2_embeddings_written += 1
            continue

        _log_first_embedding_skip(stats, provider, result)
        stats.embeddings_skipped += 1
        if provider == "birdnet":
            stats.birdnet_embeddings_skipped += 1
        elif provider == "perch_v2":
            stats.perch_v2_embeddings_skipped += 1


def _log_first_embedding_skip(
    stats: FeatureEngineeringStats,
    provider: str,
    result: EmbeddingResult,
) -> None:
    if provider == "birdnet":
        previous_skips = stats.birdnet_embeddings_skipped
    elif provider == "perch_v2":
        previous_skips = stats.perch_v2_embeddings_skipped
    else:
        previous_skips = stats.embeddings_skipped

    if previous_skips:
        return
    logger.warning(
        "First skipped %s embedding result: status=%s error=%s metadata=%s.",
        provider,
        result.status,
        result.error or "",
        result.metadata or {},
    )


def _resolve_prefetch_workers() -> int:
    """Resolve the prefetch worker count from the environment (default 8)."""
    raw = os.environ.get("BIRDWEATHER_FEATURE_PREFETCH_WORKERS", "8").strip()
    try:
        workers = int(raw)
    except ValueError:
        workers = 8
    return max(1, workers)


def _download_clip_with_retry(
    store: AudioStore,
    clip_key: str,
    *,
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> tuple[bytes | None, int]:
    """Download a processed clip, retrying transient storage errors.

    Returns the clip bytes (or ``None`` when missing) and the number of retries
    consumed. Retry counting is returned rather than mutating shared stats so
    this can run safely inside prefetch worker threads.
    """
    attempts = max(0, retry_attempts)
    backoff_seconds = max(0.0, retry_backoff_seconds)
    retries = 0

    for attempt in range(attempts + 1):
        try:
            clip_bytes = store.get_object_if_exists(
                clip_key,
                bucket=BUCKET_PROCESSED_AUDIO,
            )
            return clip_bytes, retries
        except AudioStoreUnavailableError:
            if attempt >= attempts:
                raise

            retries += 1
            delay_seconds = backoff_seconds * (2**attempt)
            logger.warning(
                "Processed-audio storage unavailable while downloading processed clip "
                "for %s; retrying in %.1f s (%d/%d)",
                clip_key,
                delay_seconds,
                attempt + 1,
                attempts,
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    msg = f"Storage retry loop exhausted unexpectedly for {clip_key}"
    raise RuntimeError(msg)


def _prepare_clip(
    index: int,
    row: BirdWeatherAudioCandidate,
    store: AudioStore,
    *,
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> _PreparedClip:
    """Fetch and decode a single processed clip (runs in a worker thread)."""
    clip_key = processed_clip_key(row)
    try:
        clip_bytes, retries = _download_clip_with_retry(
            store,
            clip_key,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
        )
    except AudioStoreUnavailableError as exc:
        return _PreparedClip(
            index=index,
            row=row,
            clip_key=clip_key,
            status="storage_unavailable",
            storage_retries=max(0, retry_attempts),
            error=exc,
        )

    if clip_bytes is None:
        return _PreparedClip(
            index=index,
            row=row,
            clip_key=clip_key,
            status="missing",
            storage_retries=retries,
        )

    try:
        audio, sample_rate = _decode_processed_clip(clip_bytes)
    except (OSError, RuntimeError, S3Error, ValueError, sf.LibsndfileError) as exc:
        return _PreparedClip(
            index=index,
            row=row,
            clip_key=clip_key,
            status="decode_failed",
            storage_retries=retries,
            error=exc,
        )

    return _PreparedClip(
        index=index,
        row=row,
        clip_key=clip_key,
        status="ok",
        audio=audio,
        sample_rate=sample_rate,
        storage_retries=retries,
    )


def _iter_prepared_clips(
    rows: list[BirdWeatherAudioCandidate],
    store: AudioStore,
    *,
    max_workers: int,
    retry_attempts: int,
    retry_backoff_seconds: float,
) -> Iterator[_PreparedClip]:
    """Yield prepared clips in input order while prefetching ahead concurrently.

    Downloads and decodes overlap across worker threads so the GPU/CPU embedding
    step is not stalled waiting on object storage. Results are always yielded in
    the original row order to preserve deterministic stats, progress logging and
    ``continue_on_storage_unavailable`` semantics.
    """
    if max_workers <= 1:
        for index, row in enumerate(rows, start=1):
            yield _prepare_clip(
                index,
                row,
                store,
                retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
            )
        return

    max_inflight = max_workers * 2
    pending: deque[Future[_PreparedClip]] = deque()
    indexed_rows = enumerate(rows, start=1)

    def _submit_next(executor: ThreadPoolExecutor) -> bool:
        try:
            index, row = next(indexed_rows)
        except StopIteration:
            return False
        pending.append(
            executor.submit(
                _prepare_clip,
                index,
                row,
                store,
                retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
            )
        )
        return True

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for _ in range(max_inflight):
            if not _submit_next(executor):
                break
        while pending:
            prepared = pending.popleft().result()
            _submit_next(executor)
            yield prepared


def _with_storage_retry(
    operation: Callable[..., Any],
    *operation_args: Any,
    clip_key: str,
    operation_label: str,
    stats: FeatureEngineeringStats,
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
                "Processed-audio storage unavailable while %s for %s; retrying in %.1f s (%d/%d)",
                operation_label,
                clip_key,
                delay_seconds,
                attempt + 1,
                attempts,
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    msg = f"Storage retry loop exhausted unexpectedly for {clip_key}"
    raise RuntimeError(msg)


def _load_feature_candidates(
    pool: ConnectionPool,
    *,
    lookback_days: int | None,
    limit: int | None,
    force: bool,
    start_date: date | None = None,
    end_date: date | None = None,
    shard_count: int | None = None,
    shard_index: int | None = None,
    include_background_classification: bool = True,
    embedding_models: Sequence[str] | None = None,
) -> list[BirdWeatherAudioCandidate]:
    filters = ["has_processed_clip"]
    params: list[Any] = []

    if (shard_count is None) != (shard_index is None):
        msg = "shard_count and shard_index must be provided together"
        raise ValueError(msg)
    if shard_count is not None:
        if shard_count < 1:
            msg = "shard_count must be >= 1"
            raise ValueError(msg)
        if shard_index is None or shard_index < 0 or shard_index >= shard_count:
            msg = "shard_index must be in [0, shard_count)"
            raise ValueError(msg)

    if not force:
        models = (
            set(embedding_models)
            if embedding_models is not None
            else {"birdnet", "perch_v2"}
        )
        pending_filters: list[str] = []
        if "birdnet" in models:
            pending_filters.append("birdnet_embedding IS NULL")
        if "perch_v2" in models:
            pending_filters.append("perch_v2_embedding IS NULL")
        if include_background_classification:
            pending_filters = [
                "feature_artifacts IS NULL",
                "background_sound_clf IS NULL",
                "COALESCE(background_sound_clf ->> 'status', '') = 'classification_failed'",
                *pending_filters,
            ]
        filters.append("(" + " OR ".join(pending_filters) + ")")
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
    if start_date is None and end_date is None and lookback_days is not None:
        filters.append("detected_at >= (CURRENT_DATE - %s::int)::timestamptz")
        params.append(lookback_days)
    if shard_count is not None:
        filters.append(
            "mod((hashtext(detection_id)::bigint & 2147483647), %s) = %s"
        )
        params.extend((shard_count, shard_index))

    sql_template = """
        SELECT
            detection_id,
            detected_at,
            observation_date,
            tableoid::regclass::text AS chunk_name,
            ctid::text AS chunk_tid,
            station_id,
            species_id,
            audio_filename,
            COALESCE(audio_start_time_s, 0.0) AS audio_start_time_s,
            COALESCE(audio_end_time_s, 0.0) AS audio_end_time_s,
            (birdnet_embedding IS NOT NULL) AS birdnet_embedding_present,
            (perch_v2_embedding IS NOT NULL) AS perch_v2_embedding_present
        FROM birdweather_tbl
        WHERE {filters}
    """
    sql = sql_template.format(filters=" AND ".join(filters))
    if limit is not None:
        sql += " ORDER BY detected_at DESC LIMIT %s"
        params.append(limit)

    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Historical backfills on the BirdWeather hypertable can fan out into
        # many parallel workers and thrash disk reads. Keep candidate loading
        # single-session and bounded instead.
        cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        cur.execute(sql, params)
        records = cur.fetchall()

    return [
        BirdWeatherAudioCandidate(
            detection_id=str(record["detection_id"]),
            detected_at=record["detected_at"],
            observation_date=record["observation_date"],
            chunk_name=str(record["chunk_name"]) if record.get("chunk_name") else None,
            chunk_tid=str(record["chunk_tid"]) if record.get("chunk_tid") else None,
            station_id=str(record.get("station_id") or "unknown_station"),
            species_id=str(record.get("species_id") or "unknown_species"),
            audio_filename=record.get("audio_filename"),
            audio_start_time_s=float(record["audio_start_time_s"]),
            audio_end_time_s=float(record["audio_end_time_s"]),
            birdnet_embedding_present=bool(record.get("birdnet_embedding_present")),
            perch_v2_embedding_present=bool(record.get("perch_v2_embedding_present")),
        )
        for record in records
    ]


def _decode_processed_clip(clip_bytes: bytes) -> tuple[np.ndarray, int]:
    audio, sample_rate = sf.read(
        io.BytesIO(clip_bytes),
        dtype="float32",
        always_2d=False,
    )
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1, dtype=np.float32)
    if audio.size == 0:
        raise ValueError("processed clip is empty")
    return np.asarray(audio, dtype=np.float32), sample_rate


def _upload_feature_artifacts(
    store: AudioStore,
    row: BirdWeatherAudioCandidate,
    audio: np.ndarray,
    sample_rate: int,
) -> dict[str, Any]:
    base_key = str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}.png",
        )
    )
    smooth_key = str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}_smooth.png",
        )
    )

    spectrogram_object = store.upload_spectrogram(
        base_key,
        render_spectrogram_png(audio, sample_rate),
    )
    waveform_object = store.upload_waveform(
        base_key,
        render_bar_waveform_png(audio),
    )
    smooth_waveform_object = store.upload_waveform(
        smooth_key,
        render_smooth_waveform_png(audio),
    )

    return {
        "spectrogram": spectrogram_object,
        "waveform": waveform_object,
        "smooth_waveform": smooth_waveform_object,
    }


def _background_payload(status: str, tags: list[AmbientTag]) -> dict[str, Any]:
    return {
        "status": status,
        "tags": [asdict(tag) for tag in tags],
    }


def _update_feature_row(
    pool: ConnectionPool,
    row: BirdWeatherAudioCandidate,
    *,
    background_payload: dict[str, Any] | None,
    embedding_results: dict[str, EmbeddingResult],
    artifact_manifest: dict[str, Any],
) -> bool:
    birdnet_result = embedding_results.get("birdnet")
    perch_result = embedding_results.get("perch_v2")
    birdnet_vector = (
        _pgvector_literal(birdnet_result.values) if birdnet_result is not None else None
    )
    perch_vector = (
        _pgvector_literal(perch_result.values) if perch_result is not None else None
    )
    update_args = (
        *(
            [json.dumps(background_payload)]
            if background_payload is not None
            else []
        ),
        birdnet_vector,
        perch_vector,
        json.dumps(artifact_manifest),
        row.detection_id,
        row.detected_at,
    )

    with pool.cursor() as cur:
        if row.chunk_name and row.chunk_tid and _execute_feature_row_update(
            cur,
            chunk_name=row.chunk_name,
            chunk_tid=row.chunk_tid,
            background_payload=background_payload,
            update_args=update_args,
        ):
            return True

        day_start, day_end = _day_bounds(
            row.observation_date,
            tzinfo=row.detected_at.tzinfo,
        )
        cur.execute(
            """
            SELECT tableoid::regclass::text AS chunk_name, ctid::text AS chunk_tid
            FROM birdweather_tbl
            WHERE detected_at >= %s
              AND detected_at < %s
              AND observation_date = %s
              AND detection_id = %s
              AND detected_at = %s
            LIMIT 1
            """,
            (
                day_start,
                day_end,
                row.observation_date,
                row.detection_id,
                row.detected_at,
            ),
        )
        target_row = cur.fetchone()
        if target_row is None:
            return False

        chunk_name, chunk_tid = target_row
        return _execute_feature_row_update(
            cur,
            chunk_name=str(chunk_name),
            chunk_tid=str(chunk_tid),
            background_payload=background_payload,
            update_args=update_args,
        )


def _execute_feature_row_update(
    cur: Any,
    *,
    chunk_name: str,
    chunk_tid: str,
    background_payload: dict[str, Any] | None,
    update_args: tuple[object, ...],
) -> bool:
    background_sql = (
        "%s::jsonb" if background_payload is not None else "background_sound_clf"
    )
    cur.execute(
        f"""
        UPDATE {_quote_qualified_identifier(chunk_name)}
        SET background_sound_clf = {background_sql},
            birdnet_embedding = COALESCE(%s::vector, birdnet_embedding),
            perch_v2_embedding = COALESCE(%s::vector, perch_v2_embedding),
            feature_artifacts = COALESCE(feature_artifacts, '{{}}'::jsonb) || %s::jsonb,
            has_processed_clip = true
        WHERE ctid = %s::tid
          AND detection_id = %s
          AND detected_at = %s
        """,  # noqa: S608
        (
            *update_args[:-2],
            chunk_tid,
            *update_args[-2:],
        ),
    )
    return int(cur.rowcount or 0) > 0


def _day_bounds(
    value: date,
    *,
    tzinfo: tzinfo | None,
) -> tuple[datetime, datetime]:
    day_start = datetime(value.year, value.month, value.day, tzinfo=tzinfo or UTC)
    return day_start, day_start + timedelta(days=1)


def _day_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _quote_qualified_identifier(value: str) -> str:
    parts = value.split(".")
    if not parts or any(not _QUALIFIED_IDENTIFIER_RE.fullmatch(part) for part in parts):
        msg = f"unsafe SQL identifier: {value!r}"
        raise ValueError(msg)
    return ".".join(f'"{part}"' for part in parts)


def _pgvector_literal(values: list[float] | None) -> str | None:
    if values is None:
        return None
    return "[" + ",".join(format(value, ".8g") for value in values) + "]"
