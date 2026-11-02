"""BirdWeather ingestion flow.

Dagster tasks and flows for ingesting acoustic detection data from the
`BirdWeather <https://app.birdweather.com>`_ API. Provides two
entry-points:

``run_birdweather_flow``
    Ingest the last *N* hours of detections and deduplicate. Runs as a
    standalone scheduled deployment.

``run_birdweather_repair_flow``
    Scan ``[start_date, end_date)`` for missing dates and re-fetch any
    gaps. Deployed with ``concurrency_limit=1`` to prevent overlapping
    repair runs.

``deduplicate_birdweather_detections``
    Remove duplicate detection rows. Called automatically at the end of
    ``run_birdweather_flow`` when ``target="database"``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from avian_biosurveillance.config.species import registry as _sp_registry
from avian_biosurveillance.features.birdweather_features import (
    engineer_birdweather_audio_features,
)
from avian_biosurveillance.ingestion.birdweather import (
    AudioCollector as BirdweatherCollector,
)
from avian_biosurveillance.orchestration.derived_aggregates_flow import (
    refresh_species_dim_task,
    refresh_species_tracker_task,
    refresh_weather_envelope_task,
)
from avian_biosurveillance.orchestration.station_summary_flow import (
    refresh_station_summary_task,
)
from avian_biosurveillance.processing.birdweather_audio import (
    process_birdweather_audio_clips,
)


logger = logging.getLogger(__name__)

_DEFAULT_SPECIES_IDS: list[str] = _sp_registry.birdweather_ids
_BIRDWEATHER_CATCHUP_FOLLOWUP_BATCH_DAYS = 7


def _resolve_species(
    species_ids: list[str] | None,
    *,
    all_species: bool = False,
) -> list[str] | None:
    """Return *species_ids* or fall back to the config-driven default."""
    if all_species and species_ids:
        msg = "species_ids cannot be provided when all_species=True"
        raise ValueError(msg)
    if all_species:
        return None
    if species_ids is not None:
        return species_ids
    return list(_DEFAULT_SPECIES_IDS)


def _stat_failures(stats: object | None, fields: tuple[str, ...]) -> dict[str, int]:
    """Return non-zero failure counters from a processing stats object."""
    if stats is None:
        return {}
    failures: dict[str, int] = {}
    for field in fields:
        value = getattr(stats, field, 0)
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count:
            failures[field] = count
    return failures


def _raise_for_stat_failures(
    label: str,
    stats: object | None,
    fields: tuple[str, ...],
) -> None:
    failures = _stat_failures(stats, fields)
    if failures:
        msg = f"{label} reported failure counters: {failures}"
        raise RuntimeError(msg)


def _log_non_blocking_stat_failures(
    label: str,
    stats: object | None,
    fields: tuple[str, ...],
) -> None:
    """Log row-level failures that should not abort a bulk historical run."""
    failures = _stat_failures(stats, fields)
    if failures:
        logger.warning(
            "%s reported non-blocking failure counters: %s; continuing catchup.",
            label,
            failures,
        )


def _raise_for_audio_processing_followup_failures(
    label: str,
    stats: object | None,
) -> None:
    """Raise only for blocking processed-audio follow-up failures.

    Historical BirdWeather media can contain corrupt source objects or stale crop
    windows. The clip processor already audits those rows and continues, so the
    bulk catchup should only abort on storage/upload failures that leave the
    processed-audio bucket incomplete.
    """
    _log_non_blocking_stat_failures(label, stats, ("invalid_window", "decode_failed"))
    _raise_for_stat_failures(label, stats, ("upload_failed",))


def _raise_for_feature_engineering_followup_failures(
    label: str,
    stats: object | None,
) -> None:
    """Raise only for blocking feature-engineering follow-up failures."""
    _log_non_blocking_stat_failures(
        label,
        stats,
        ("missing_clip", "decode_failed", "background_failed"),
    )
    _raise_for_stat_failures(label, stats, ("storage_unavailable", "artifact_upload_failed"))


def _raise_for_raw_audio_backfill_failures(
    label: str,
    stats: object | None,
) -> None:
    """Raise for blocking raw-audio backfill failures.

    Historical BirdWeather media often disappears from the public CDN over time.
    We already audit archived objects without aborting the catchup. Treat the
    edge case where the downloader reports success but the temp file is missing
    the same way for catchup backfills. The same applies to objects the public
    CDN no longer serves (403 forbidden / 404 not found): they are unfetchable
    forever, so one stale upstream object must not fail the entire historical
    window.
    """
    failures = _stat_failures(stats, ("failed", "upload_failed"))
    if not failures:
        return

    # Statuses where the upstream object is permanently unavailable on the public
    # CDN (expired 403 forbidden / 404 not found, or the downloader reported
    # success but left no temp file). These can never be fetched, so a bulk
    # historical catchup skips them instead of aborting the whole window. Genuine
    # infra failures (upload_failed) and transient errors stay blocking.
    unavailable = 0
    for field in ("missing_file", "forbidden", "not_found"):
        try:
            unavailable += int(getattr(stats, field, 0) or 0)
        except (TypeError, ValueError):
            continue

    if unavailable and "failed" in failures:
        remaining_failed = max(0, failures["failed"] - unavailable)
        if remaining_failed == 0:
            logger.warning(
                "%s reported %d unavailable raw-audio object(s) "
                "(expired/forbidden/not-found on the CDN); continuing catchup.",
                label,
                unavailable,
            )
            failures.pop("failed", None)
        else:
            failures["failed"] = remaining_failed

    if failures:
        msg = f"{label} reported failure counters: {failures}"
        raise RuntimeError(msg)


# ==========================================================
# Helpers
# ==========================================================


def _find_missing_birdweather_dates(
    start_date: date,
    end_date: date | None = None,
) -> list[date]:
    """Return dates with no detections in [start_date, end_date).

    Queries ``birdweather_tbl`` for distinct ``observation_date`` values
    and compares against the expected calendar range.  Results are sorted
    in chronological order (oldest first) so the backfill walks forward
    from ``start_date`` toward ``end_date``.

    Args:
        start_date: First calendar date to check.
        end_date: Last calendar date (**exclusive**).  Defaults to today.

    Returns:
        Sorted list of :class:`~datetime.date` objects with no recorded
        detections, oldest first.
    """
    from avian_biosurveillance.db.database import ConnectionPool

    if end_date is None:
        end_date = date.today()

    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        with pool.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT observation_date "
                "FROM birdweather_tbl "
                "WHERE observation_date >= %s "
                "  AND observation_date < %s",
                (start_date, end_date),
            )
            existing_dates = {row[0] for row in cur.fetchall()}

        total_days = (end_date - start_date).days
        expected = {start_date + timedelta(days=d) for d in range(total_days)}
        return sorted(expected - existing_dates)
    finally:
        pool.close()


def _birdweather_date_has_detections(obs_date: date) -> bool:
    """Return whether BirdWeather already has detections for *obs_date*."""
    from avian_biosurveillance.db.database import ConnectionPool

    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        with pool.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM birdweather_tbl WHERE observation_date = %s LIMIT 1",
                (obs_date,),
            )
            return cur.fetchone() is not None
    finally:
        pool.close()


# ==========================================================
# BirdWeather Flow
# ==========================================================


def fetch_birdweather_data(
    hours: int = 24,
    country: str = "Netherlands",
    species_ids: list[str] | None = None,
    all_species: bool = False,
    min_confidence: float = 0.1,
    workers: int = 20,
    target: str = "database",
    download_audio: bool = False,
) -> object:
    """Fetch the latest BirdWeather detections.

    Delegates to :class:`~avian_biosurveillance.ingestion.birdweather.AudioCollector`
    for the actual API interaction, audio download, and persistence.

    Args:
        hours: Rolling look-back window in hours.
        country: Country filter for the API query.
        species_ids: BirdWeather species IDs to fetch.
            Falls back to ``config/target_species.json``.
        min_confidence: Minimum detection confidence (0-1).
        workers: Parallel download workers for audio files.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB/MinIO).
        download_audio: Whether to download raw soundscapes in this step.
            Dagster keeps this ``False`` for the fast table-ingestion asset and
            materializes raw audio through a downstream queue asset.

    Raises:
        Exception: Propagated from the collector on unrecoverable API
            errors (after Dagster's built-in retry budget is exhausted).
    """
    species_ids = _resolve_species(species_ids, all_species=all_species)

    logger.info(
        "Starting BirdWeather collection for the last %d hours.",
        hours,
    )
    collector = BirdweatherCollector()
    stats = collector.collect(
        hours=hours,
        country=country,
        species_ids=species_ids,
        min_confidence=min_confidence,
        download_audio=download_audio,
        workers=workers,
        target=target,
    )
    logger.info("BirdWeather ingestion complete.")
    return stats


def backfill_birdweather_day(
    obs_date: date,
    country: str = "Netherlands",
    species_ids: list[str] | None = None,
    all_species: bool = False,
    min_confidence: float = 0.1,
    workers: int = 20,
    download_audio: bool = False,
    target: str = "database",
) -> object:
    """Backfill BirdWeather detections for a single calendar day."""
    species_ids = _resolve_species(species_ids, all_species=all_species)
    collector = BirdweatherCollector()
    day_start = datetime.combine(obs_date, time.min)
    day_end = datetime.combine(obs_date, time(23, 59, 59))
    logger.info("Backfilling BirdWeather for %s.", obs_date.isoformat())
    stats = collector.collect(
        start_time=day_start,
        end_time=day_end,
        country=country,
        species_ids=species_ids,
        min_confidence=min_confidence,
        download_audio=download_audio,
        workers=workers,
        target=target,
    )
    logger.info("BirdWeather backfill for %s complete.", obs_date.isoformat())
    return stats


def backfill_birdweather_gaps(
    start_date: date,
    end_date: date | None = None,
    country: str = "Netherlands",
    species_ids: list[str] | None = None,
    all_species: bool = False,
    min_confidence: float = 0.1,
    workers: int = 20,
    download_audio: bool = False,
    target: str = "database",
    parallel_days: int = 1,
) -> None:
    """Detect and fill missing date gaps in BirdWeather data.

    Scans ``[start_date, end_date)`` for dates with zero detections
    and re-fetches them.  Each missing date is ingested as a full
    24-hour window via the collector.

    Args:
        start_date: First calendar date to check for gaps.
        end_date: Last date (**exclusive**).  Defaults to today.
        country: Country filter for the API query.
        species_ids: BirdWeather species IDs.  Falls back to the
            config-driven default.
        min_confidence: Minimum detection confidence (0-1).
        workers: Parallel download workers for audio files.
        download_audio: Whether to download audio during gap-filling.
            Defaults to ``False`` because older BirdWeather backfill
            windows commonly return CDN ``403`` responses for soundscape
            URLs even when recent-day ingestion succeeds.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB/MinIO).
        parallel_days: Number of days to backfill concurrently.  ``1``
            (default) preserves the original sequential behavior.
            Higher values trade API quota pressure for wall-clock
            speedup — keep ≤ 4 to avoid 429 storms from BirdWeather.
    """
    species_ids = _resolve_species(species_ids, all_species=all_species)
    missing = _find_missing_birdweather_dates(start_date, end_date)

    if not missing:
        logger.info(
            "Backfill check: no gaps from %s to %s.",
            start_date,
            end_date or date.today(),
        )
        return

    logger.info(
        "Backfill: %d missing date(s) from %s to %s (parallel_days=%d).",
        len(missing),
        start_date,
        end_date or date.today(),
        parallel_days,
    )

    def _fill_one(day: date) -> None:
        try:
            if target == "database" and _birdweather_date_has_detections(day):
                logger.info(
                    "Skipping BirdWeather backfill for %s because detections already exist.",
                    day.isoformat(),
                )
                return
            backfill_birdweather_day(
                obs_date=day,
                country=country,
                species_ids=species_ids,
                all_species=all_species,
                min_confidence=min_confidence,
                download_audio=download_audio,
                workers=workers,
                target=target,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Backfill failed for %s.",
                day.isoformat(),
            )
            raise

    if parallel_days <= 1:
        failed_days: list[date] = []
        for day in missing:
            try:
                _fill_one(day)
            except Exception:  # pylint: disable=broad-exception-caught
                failed_days.append(day)
        if failed_days:
            msg = "BirdWeather backfill failed for: " + ", ".join(
                day.isoformat() for day in failed_days
            )
            raise RuntimeError(msg)
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    failed_days: list[date] = []
    with ThreadPoolExecutor(
        max_workers=parallel_days,
        thread_name_prefix="bw-backfill",
    ) as pool:
        futures = {pool.submit(_fill_one, d): d for d in missing}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:  # pylint: disable=broad-exception-caught
                failed_days.append(futures[fut])
    if failed_days:
        msg = "BirdWeather backfill failed for: " + ", ".join(
            day.isoformat() for day in sorted(failed_days)
        )
        raise RuntimeError(msg)


def _backfill_birdweather_raw_audio(
    start_date: date,
    end_date: date | None,
    *,
    workers: int,
) -> object | None:
    """Backfill raw audio objects for detections already stored in the DB.

    ``run_birdweather_repair_flow`` uses an exclusive ``end_date`` while the
    raw-audio downloader expects an inclusive upper bound, so the requested
    range is translated here.
    """
    from avian_biosurveillance.processing.birdweather_raw_audio import (
        download_birdweather_raw_audio_objects,
    )

    end_exclusive = end_date or date.today()
    end_inclusive = end_exclusive - timedelta(days=1)
    if end_inclusive < start_date:
        logger.info(
            "BirdWeather raw-audio backfill skipped for empty range %s..%s.",
            start_date,
            end_exclusive,
        )
        return None

    stats_by_day: list[object] = []
    current = start_date
    while current <= end_inclusive:
        logger.info("Backfilling BirdWeather raw audio for %s.", current)
        stats = download_birdweather_raw_audio_objects(
            lookback_days=None,
            limit=None,
            start_date=current,
            end_date=current,
            workers=workers,
        )
        _raise_for_raw_audio_backfill_failures(
            f"BirdWeather raw audio {current.isoformat()}",
            stats,
        )
        stats_by_day.append(stats)
        current += timedelta(days=1)

    logger.info(
        "BirdWeather raw audio backfill complete: %s uploaded, %s already present, %s archived, %s failed.",
        sum(getattr(stats, "uploaded", 0) for stats in stats_by_day),
        sum(getattr(stats, "already_present", 0) for stats in stats_by_day),
        sum(getattr(stats, "archived", 0) for stats in stats_by_day),
        sum(getattr(stats, "failed", 0) for stats in stats_by_day),
    )
    return stats_by_day


def deduplicate_birdweather_detections(
    lookback_days: int = 2,
) -> None:
    """Remove duplicate detections sharing the same event key.

    Scopes to the last *lookback_days* to avoid a full hypertable scan.
    Retains the highest-confidence row for each
    ``(species_id, station_id, detected_at)`` group.

    Args:
        lookback_days: How many recent days to check for duplicates.
    """
    from avian_biosurveillance.db.database import ConnectionPool

    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        with pool.cursor() as cur:
            cur.execute(
                "DELETE FROM birdweather_tbl a "
                "USING birdweather_tbl b "
                "WHERE a.detected_at >= NOW() - INTERVAL '%s days' "
                "  AND b.detected_at >= NOW() - INTERVAL '%s days' "
                "  AND a.species_id   = b.species_id "
                "  AND a.station_id   = b.station_id "
                "  AND a.detected_at  = b.detected_at "
                "  AND ("
                "       COALESCE(a.confidence, -1) < COALESCE(b.confidence, -1)"
                "    OR (COALESCE(a.confidence, -1) = COALESCE(b.confidence, -1)"
                "        AND COALESCE(a.score, -1) < COALESCE(b.score, -1))"
                "    OR (COALESCE(a.confidence, -1) = COALESCE(b.confidence, -1)"
                "        AND COALESCE(a.score, -1) = COALESCE(b.score, -1)"
                "        AND a.detection_id < b.detection_id)"
                "  )",
                (lookback_days, lookback_days),
            )
            removed = cur.rowcount
        if removed:
            logger.info(
                "Dedup: removed %d duplicate detection(s).",
                removed,
            )
        else:
            logger.info("Dedup: no duplicates found.")
    finally:
        pool.close()


def _run_birdweather_catchup_followup(
    start_date: date,
    end_date: date,
    *,
    workers: int,
    download_audio: bool,
    run_tracker_refresh: bool,
) -> None:
    """Run BirdWeather catchup follow-up work for an inclusive date window."""
    logger.info(
        "BirdWeather catchup follow-up for %s to %s.",
        start_date,
        end_date,
    )
    deduplicate_birdweather_detections()
    refresh_station_summary_task()
    refresh_species_dim_task()
    if download_audio:
        _backfill_birdweather_raw_audio(
            start_date,
            end_date + timedelta(days=1),
            workers=workers,
        )
    current = start_date
    while current <= end_date:
        audio_stats = process_birdweather_audio_clips(
            lookback_days=None,
            limit=None,
            start_date=current,
            end_date=current,
            storage_retry_attempts=2,
        )
        _raise_for_audio_processing_followup_failures(
            f"BirdWeather audio processing {current.isoformat()}",
            audio_stats,
        )
        feature_stats = engineer_birdweather_audio_features(
            lookback_days=None,
            limit=None,
            start_date=current,
            end_date=current,
        )
        _raise_for_feature_engineering_followup_failures(
            f"BirdWeather feature engineering {current.isoformat()}",
            feature_stats,
        )
        current += timedelta(days=1)
    if run_tracker_refresh:
        logger.info(
            "BirdWeather catchup follow-up for %s to %s: refreshing species tracker; this step can take several minutes.",
            start_date,
            end_date,
        )
        refresh_species_tracker_task(start_date=start_date, end_date=end_date)
        logger.info(
            "BirdWeather catchup follow-up for %s to %s: species tracker refresh complete.",
            start_date,
            end_date,
        )


def _birdweather_run_name() -> str:
    """Generate run name: ``birdweather-ingest/YYYY-MM-DD``."""
    return f"birdweather-ingest/{date.today().isoformat()}"


def run_birdweather_flow(
    hours: int = 24,
    country: str = "Netherlands",
    species_ids: list[str] | None = None,
    all_species: bool = False,
    min_confidence: float = 0.1,
    workers: int = 20,
    target: str = "database",
    download_audio: bool = False,
) -> None:
    """BirdWeather ingestion flow (last *N* hours).

    Fetches recent detections and — when ``target="database"`` — runs
    a deduplication pass on the last 2 days to remove any duplicate
    rows created by overlapping collection windows, then refreshes
    ``birdweather_stations_tbl`` so downstream flows (e.g. Earth Engine)
    can use up-to-date station locations.

    Args:
        hours: Rolling look-back window in hours.
        country: Country filter for the API query.
        species_ids: BirdWeather species IDs to fetch.
        min_confidence: Minimum detection confidence (0-1).
        workers: Parallel download workers.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB/MinIO).
    """
    species_ids = _resolve_species(species_ids, all_species=all_species)

    fetch_birdweather_data(
        hours=hours,
        country=country,
        species_ids=species_ids,
        all_species=all_species,
        min_confidence=min_confidence,
        workers=workers,
        target=target,
        download_audio=download_audio,
    )

    if target == "database":
        deduplicate_birdweather_detections()
        refresh_station_summary_task()
        refresh_species_dim_task()
        refresh_species_tracker_task(wait_for_lock=False)
        refresh_weather_envelope_task(wait_for_lock=False)


def run_birdweather_catchup_flow(
    start_date: str,
    end_date: str | None = None,
    country: str = "Netherlands",
    species_ids: list[str] | None = None,
    all_species: bool = False,
    min_confidence: float = 0.1,
    workers: int = 20,
    download_audio: bool = True,
    target: str = "database",
    followup_batch_days: int = _BIRDWEATHER_CATCHUP_FOLLOWUP_BATCH_DAYS,
    run_tracker_refresh: bool = True,
) -> None:
    """Catch up BirdWeather day-by-day without sharing a mixed-source DAG.

    Unlike ``run_birdweather_repair_flow``, this flow explicitly attempts every
    requested calendar day even when the upstream API legitimately returns zero
    detections. That makes it safe for the sensor-owned historical backlog,
    where cursor progress must not depend on rows appearing in the database.

    When ``target="database"`` the flow batches BirdWeather-specific
    downstream steps over the completed catchup window. Per-day database
    table backfill stays table-only; ``download_audio`` controls the batched
    raw-audio follow-up stage. This keeps audio and feature work moving
    without rebuilding ``species_tracker_tbl`` for every individual day in the
    backlog.
    """
    species_ids = _resolve_species(species_ids, all_species=all_species)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else start

    if end < start:
        msg = f"end_date ({end}) must be >= start_date ({start})"
        raise ValueError(msg)
    if followup_batch_days < 1:
        msg = "followup_batch_days must be >= 1"
        raise ValueError(msg)

    current = start
    batch_start = start
    while current <= end:
        logger.info("BirdWeather catchup day %s.", current.isoformat())
        backfill_birdweather_day(
            obs_date=current,
            country=country,
            species_ids=species_ids,
            all_species=all_species,
            min_confidence=min_confidence,
            workers=workers,
            download_audio=download_audio if target != "database" else False,
            target=target,
        )

        if target == "database" and (
            (current - batch_start).days + 1 >= followup_batch_days or current == end
        ):
            _run_birdweather_catchup_followup(
                batch_start,
                current,
                workers=workers,
                download_audio=download_audio,
                run_tracker_refresh=run_tracker_refresh,
            )
            batch_start = current + timedelta(days=1)
        current += timedelta(days=1)


def _repair_run_name(
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate run name: ``birdweather-repair/YYYY-MM-DD..YYYY-MM-DD``."""
    end = end_date or date.today().isoformat()
    return f"birdweather-repair/{start_date}..{end}"


def run_birdweather_repair_flow(
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    country: str = "Netherlands",
    species_ids: list[str] | None = None,
    all_species: bool = False,
    min_confidence: float = 0.1,
    workers: int = 20,
    download_audio: bool = False,
    target: str = "database",
    parallel_days: int = 1,
    run_tracker_refresh: bool = True,
) -> None:
    """Standalone BirdWeather repair flow.

    Scans ``[start_date, end_date)`` for dates with zero detections
    and re-fetches them.  Safe to run concurrently with the daily
    pipeline; gaps are detected from the database so already-filled
    dates are skipped automatically.

    Deployed with ``concurrency_limit=1`` so only one repair can
    run at a time — if a previous run is still in progress, the next
    scheduled trigger is queued by Dagster (not skipped).

    Args:
        start_date: First date to check for gaps (``YYYY-MM-DD``).
            Defaults to ``2024-01-01``.
        end_date: Last date (**exclusive**, ``YYYY-MM-DD``).  Defaults
            to today.
        country: Country filter for the API query.
        species_ids: BirdWeather species IDs to fetch.
        min_confidence: Minimum detection confidence (0-1).
        workers: Parallel download workers.
        download_audio: Whether to download audio during historical
            repair windows. When enabled for ``target="database"``, the
            flow also backfills raw audio objects for the full requested date
            range so existing BirdWeather rows can repair missing audio.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB/MinIO).
        run_tracker_refresh: Whether to refresh ``species_tracker_tbl`` after
            the repair completes. Disable this for large backfills when the
            data landing pass should complete before heavyweight derived
            refreshes are rerun separately.
    """
    species_ids = _resolve_species(species_ids, all_species=all_species)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else None

    backfill_birdweather_gaps(
        start_date=start,
        end_date=end,
        country=country,
        species_ids=species_ids,
        all_species=all_species,
        min_confidence=min_confidence,
        workers=workers,
        download_audio=download_audio,
        target=target,
        parallel_days=parallel_days,
    )

    if target == "database":
        deduplicate_birdweather_detections()
        if download_audio:
            _backfill_birdweather_raw_audio(
                start,
                end,
                workers=workers,
            )
        refresh_station_summary_task()
        refresh_species_dim_task()
        if run_tracker_refresh:
            refresh_end = (end - timedelta(days=1)) if end is not None else date.today()
            if refresh_end < start:
                logger.info(
                    "BirdWeather repair skipped species tracker refresh because the requested window was empty (%s..%s).",
                    start,
                    end,
                )
            else:
                refresh_species_tracker_task(
                    start_date=start,
                    end_date=refresh_end,
                )
        else:
            logger.info(
                "BirdWeather repair skipped species tracker refresh for %s..%s.",
                start,
                end or date.today(),
            )
