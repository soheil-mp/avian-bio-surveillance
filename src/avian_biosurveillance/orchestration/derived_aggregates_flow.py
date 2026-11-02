"""Derived-Aggregate Refresh Flow.

Refreshes the derived dimension/fact tables that move expensive backend
logic out of the Next.js API routes:

- ``species_tbl``           — populated by ``refresh_species_dim()``.
                               Holds canonical species metadata plus the
                               current activity envelope used across frontend
                               joins and analytical refresh jobs.
- ``movebank_tracks_tbl``   — populated by ``refresh_movebank_tracks()``.
                               Replaces the per-individual N+1 windowed
                               queries previously executed on every
                               ``/api/migration-tracks`` request.
- ``species_tracker_tbl``   — populated by ``refresh_species_tracker()``.
                               Pre-computes period/location/species tracker
                               rows with ecological and environmental metrics.
- ``species_weather_envelope_tbl`` — populated by
                               ``refresh_species_weather_envelope()`` after a
                               ``refresh_weather_samples()`` top-up. Holds the
                               per-species calling-climate IQR envelope read by
                               the Species Tracker "Climate" tab.

Each refresh is exposed as both a Dagster *task* (so it can be chained
inline from the upstream ingestion flow) and as standalone Dagster
*flows* (so they can run independently on a cron schedule).

Privileged DSN handling mirrors
:mod:`avian_biosurveillance.orchestration.station_summary_flow`.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from urllib.parse import quote_plus

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.database import ConnectionPool


load_runtime_env()

logger = logging.getLogger(__name__)

_SPECIES_TRACKER_LOCK_KEY = (1_931_499_917, 1_919_252_851)
_SPECIES_TRACKER_REFRESH_WORK_MEM_ENV = "SPECIES_TRACKER_REFRESH_WORK_MEM"
_DEFAULT_SPECIES_TRACKER_REFRESH_WORK_MEM = "512MB"
_SPECIES_TRACKER_REFRESH_STATEMENT_TIMEOUT_ENV = (
    "SPECIES_TRACKER_REFRESH_STATEMENT_TIMEOUT"
)
_DEFAULT_SPECIES_TRACKER_REFRESH_STATEMENT_TIMEOUT = "30min"
_VALID_SPECIES_TRACKER_REFRESH_SCOPES = frozenset({"full", "modeling"})

_WEATHER_ENVELOPE_LOCK_KEY = (1_734_019_440, 1_701_667_182)
_WEATHER_ENVELOPE_REFRESH_WORK_MEM_ENV = "WEATHER_ENVELOPE_REFRESH_WORK_MEM"
# Keeps the percentile sorts in RAM (server default spills to temp files on the
# shared spinning disk). 128MB stays well under the db container's hard cap when
# paired with max_parallel_workers_per_gather=0 below.
_DEFAULT_WEATHER_ENVELOPE_REFRESH_WORK_MEM = "128MB"
_WEATHER_ENVELOPE_REFRESH_STATEMENT_TIMEOUT_ENV = (
    "WEATHER_ENVELOPE_REFRESH_STATEMENT_TIMEOUT"
)
_DEFAULT_WEATHER_ENVELOPE_REFRESH_STATEMENT_TIMEOUT = "45min"


# ---------------------------------------------------------------------------
# Privileged-DSN resolution (mirrors station_summary_flow)
# ---------------------------------------------------------------------------

_DERIVED_DATABASE_URL_ENV = "STATION_SUMMARY_DATABASE_URL"
_DERIVED_DB_USER_ENV = "STATION_SUMMARY_DB_USER"
_DERIVED_DB_PASSWORD_ENV = "STATION_SUMMARY_DB_PASSWORD"
_DERIVED_DB_HOST_ENV = "STATION_SUMMARY_DB_HOST"
_DERIVED_DB_PORT_ENV = "STATION_SUMMARY_DB_PORT"
_DERIVED_DB_NAME_ENV = "STATION_SUMMARY_DB_NAME"


def _derived_dsn() -> str | None:
    """Build a privileged DSN if station-summary credentials are configured."""
    dsn = os.environ.get(_DERIVED_DATABASE_URL_ENV)
    if dsn:
        return dsn

    user = os.environ.get(_DERIVED_DB_USER_ENV)
    password = os.environ.get(_DERIVED_DB_PASSWORD_ENV)
    if not user or not password:
        return None

    host = os.environ.get(_DERIVED_DB_HOST_ENV, "timescaledb")
    port = os.environ.get(_DERIVED_DB_PORT_ENV, "5432")
    dbname = os.environ.get(
        _DERIVED_DB_NAME_ENV,
        os.environ.get("POSTGRES_DB", "avian_biosurveillance"),
    )
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
    )


def _derived_pool() -> ConnectionPool:
    """Build the pool used for privileged derived-table maintenance."""
    dsn = _derived_dsn()
    if dsn:
        logger.info(
            "Using privileged maintenance connection for derived-table refresh.",
        )
        return ConnectionPool(dsn=dsn, min_conn=1, max_conn=2)
    return ConnectionPool(min_conn=1, max_conn=2)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def refresh_species_dim_task() -> None:
    """Rebuild ``species_tbl`` via ``refresh_species_dim()``."""
    pool = _derived_pool()
    try:
        with pool.connection() as conn:
            previous_autocommit = conn.autocommit
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT refresh_species_dim()")
                    logger.info("species_tbl refreshed.")
            finally:
                conn.autocommit = previous_autocommit
    finally:
        pool.close()


def refresh_movebank_tracks_task(max_points: int = 500) -> None:
    """Rebuild ``movebank_tracks_tbl`` via ``refresh_movebank_tracks()``.

    Args:
        max_points: Soft cap on coordinates per individual (default 500).
    """
    pool = _derived_pool()
    try:
        with pool.connection() as conn:
            previous_autocommit = conn.autocommit
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT refresh_movebank_tracks(%s)",
                        (max_points,),
                    )
                    logger.info(
                        "movebank_tracks_tbl refreshed (max_points=%d).",
                        max_points,
                    )
            finally:
                conn.autocommit = previous_autocommit
    finally:
        pool.close()


def _species_tracker_refresh_work_mem() -> str:
    """Return the per-session work_mem used for tracker refreshes."""
    return os.environ.get(
        _SPECIES_TRACKER_REFRESH_WORK_MEM_ENV,
        _DEFAULT_SPECIES_TRACKER_REFRESH_WORK_MEM,
    )


def _species_tracker_refresh_statement_timeout() -> str:
    """Return the per-session statement_timeout used for tracker refreshes."""
    return os.environ.get(
        _SPECIES_TRACKER_REFRESH_STATEMENT_TIMEOUT_ENV,
        _DEFAULT_SPECIES_TRACKER_REFRESH_STATEMENT_TIMEOUT,
    )


def _coerce_refresh_date(value: date | str | None) -> date | None:
    """Normalize a tracker refresh date argument."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _normalize_refresh_window(
    start_date: date | str | None,
    end_date: date | str | None,
) -> tuple[date | None, date | None]:
    """Validate and normalize an optional tracker refresh window."""
    normalized_start = _coerce_refresh_date(start_date)
    normalized_end = _coerce_refresh_date(end_date)

    if (normalized_start is None) != (normalized_end is None):
        msg = "start_date and end_date must both be null or both be non-null"
        raise ValueError(msg)
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_end < normalized_start
    ):
        msg = f"end_date ({normalized_end}) must be >= start_date ({normalized_start})"
        raise ValueError(msg)

    return normalized_start, normalized_end


def _normalize_refresh_scope(refresh_scope: str) -> str:
    """Validate and normalize the tracker refresh scope."""
    normalized_scope = refresh_scope.strip().lower()
    if normalized_scope not in _VALID_SPECIES_TRACKER_REFRESH_SCOPES:
        msg = "refresh_scope must be one of: full, modeling"
        raise ValueError(msg)
    return normalized_scope


def refresh_species_tracker_task(
    wait_for_lock: bool = True,
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    refresh_scope: str = "full",
) -> bool:
    """Rebuild ``species_tracker_tbl`` via ``refresh_species_tracker()``.

    When ``start_date`` and ``end_date`` are provided, only tracker rows whose
    periods overlap that window are rebuilt.

    When ``refresh_scope`` is ``"modeling"``, only the weekly grid slice read
    by the modeling pipeline is rebuilt.

    When ``wait_for_lock`` is ``False``, the refresh is skipped if another
    tracker rebuild already holds the advisory lock.
    """
    normalized_start, normalized_end = _normalize_refresh_window(start_date, end_date)
    normalized_scope = _normalize_refresh_scope(refresh_scope)
    pool = _derived_pool()
    try:
        with pool.connection() as conn:
            previous_autocommit = conn.autocommit
            previous_work_mem: str | None = None
            previous_statement_timeout: str | None = None
            lock_acquired = False
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_setting('work_mem')")
                    current_setting_row = cur.fetchone()
                    previous_work_mem = (
                        str(current_setting_row[0])
                        if current_setting_row and current_setting_row[0]
                        else None
                    )
                    cur.execute("SELECT current_setting('statement_timeout')")
                    statement_timeout_row = cur.fetchone()
                    previous_statement_timeout = (
                        str(statement_timeout_row[0])
                        if statement_timeout_row and statement_timeout_row[0]
                        else None
                    )

                    if wait_for_lock:
                        logger.info("Waiting for species_tracker refresh advisory lock.")
                        cur.execute(
                            "SELECT pg_advisory_lock(%s, %s)",
                            _SPECIES_TRACKER_LOCK_KEY,
                        )
                        lock_acquired = True
                    else:
                        cur.execute(
                            "SELECT pg_try_advisory_lock(%s, %s)",
                            _SPECIES_TRACKER_LOCK_KEY,
                        )
                        row = cur.fetchone()
                        lock_acquired = bool(row[0]) if row else False
                        if not lock_acquired:
                            logger.info(
                                "Skipping species_tracker refresh because another refresh already holds the advisory lock."
                            )
                            return False

                    desired_work_mem = _species_tracker_refresh_work_mem()
                    desired_statement_timeout = (
                        _species_tracker_refresh_statement_timeout()
                    )
                    # The tracker refresh runs a large all-history CTE pipeline.
                    # Give this session more memory than the server default to
                    # reduce temp-file spill without changing the global setting.
                    cur.execute("SET work_mem TO %s", (desired_work_mem,))
                    cur.execute(
                        "SET statement_timeout TO %s",
                        (desired_statement_timeout,),
                    )
                    logger.info(
                        "Running species_tracker %s refresh for %s with work_mem=%s and statement_timeout=%s.",
                        normalized_scope,
                        (
                            f"{normalized_start}..{normalized_end}"
                            if normalized_start is not None
                            else "full history"
                        ),
                        desired_work_mem,
                        desired_statement_timeout,
                    )

                    try:
                        if normalized_start is None:
                            if normalized_scope == "full":
                                cur.execute("SELECT refresh_species_tracker()")
                            else:
                                cur.execute(
                                    "SELECT refresh_species_tracker(NULL::date, NULL::date, %s)",
                                    (normalized_scope,),
                                )
                        else:
                            if normalized_scope == "full":
                                cur.execute(
                                    "SELECT refresh_species_tracker(%s, %s)",
                                    (normalized_start, normalized_end),
                                )
                            else:
                                cur.execute(
                                    "SELECT refresh_species_tracker(%s, %s, %s)",
                                    (
                                        normalized_start,
                                        normalized_end,
                                        normalized_scope,
                                    ),
                                )
                        row = cur.fetchone()
                    finally:
                        if (
                            lock_acquired
                            and getattr(conn, "closed", 1) == 0
                            and not cur.closed
                        ):
                            try:
                                cur.execute(
                                    "SELECT pg_advisory_unlock(%s, %s)",
                                    _SPECIES_TRACKER_LOCK_KEY,
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to release species_tracker advisory lock because the connection was already closing.",
                                    exc_info=True,
                                )

                    inserted = row[0] if row else 0
                    logger.info(
                        "species_tracker_tbl %s refresh for %s completed (rows=%s).",
                        normalized_scope,
                        (
                            f"{normalized_start}..{normalized_end}"
                            if normalized_start is not None
                            else "full history"
                        ),
                        inserted,
                    )
                    return True
            finally:
                if getattr(conn, "closed", 1) == 0:
                    if previous_work_mem is not None:
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SET work_mem TO %s", (previous_work_mem,))
                        except Exception:
                            logger.warning(
                                "Failed to restore work_mem after species_tracker refresh because the connection was already closing.",
                                exc_info=True,
                            )
                    if previous_statement_timeout is not None:
                        try:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "SET statement_timeout TO %s",
                                    (previous_statement_timeout,),
                                )
                        except Exception:
                            logger.warning(
                                "Failed to restore statement_timeout after species_tracker refresh because the connection was already closing.",
                                exc_info=True,
                            )
                    try:
                        conn.autocommit = previous_autocommit
                    except Exception:
                        logger.warning(
                            "Failed to restore autocommit after species_tracker refresh because the connection was already closing.",
                            exc_info=True,
                        )
    finally:
        pool.close()

    return False


def _weather_envelope_refresh_work_mem() -> str:
    """Return the per-session work_mem used for the envelope rebuild."""
    return os.environ.get(
        _WEATHER_ENVELOPE_REFRESH_WORK_MEM_ENV,
        _DEFAULT_WEATHER_ENVELOPE_REFRESH_WORK_MEM,
    )


def _weather_envelope_refresh_statement_timeout() -> str:
    """Return the per-session statement_timeout used for the envelope rebuild."""
    return os.environ.get(
        _WEATHER_ENVELOPE_REFRESH_STATEMENT_TIMEOUT_ENV,
        _DEFAULT_WEATHER_ENVELOPE_REFRESH_STATEMENT_TIMEOUT,
    )


def refresh_weather_envelope_task(wait_for_lock: bool = True) -> bool:
    """Top up ``weather_samples_tbl`` then rebuild ``species_weather_envelope_tbl``.

    Runs ``refresh_weather_samples()`` (a cheap incremental extract of the six
    weather floats from detections newer than whatever is already staged)
    followed by ``refresh_species_weather_envelope()`` (the per-species IQR
    rebuild that feeds the Species Tracker "Climate" tab).

    The envelope rebuild runs percentile sorts over the whole
    ``weather_samples_tbl``. A raised per-session ``work_mem`` keeps those in
    RAM -- the server default spills to temp files and is pathologically slow on
    the shared spinning disk -- and ``max_parallel_workers_per_gather=0`` bounds
    peak memory under the database container's hard cap. All three settings are
    restored afterwards so the elevated values never leak to the pool.

    When ``wait_for_lock`` is ``False``, the refresh is skipped if another
    envelope rebuild already holds the advisory lock.
    """
    pool = _derived_pool()
    try:
        with pool.connection() as conn:
            previous_autocommit = conn.autocommit
            previous_work_mem: str | None = None
            previous_statement_timeout: str | None = None
            previous_max_parallel: str | None = None
            lock_acquired = False
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    # Foot-gun guard: refresh_weather_samples() degrades to a
                    # full station_weather JSONB scan across every chunk on an
                    # empty table. Require the one-time historical backfill
                    # (scripts/dev/backfill_weather_samples.sh) to have run.
                    cur.execute("SELECT EXISTS(SELECT 1 FROM weather_samples_tbl)")
                    seeded_row = cur.fetchone()
                    if not (seeded_row and seeded_row[0]):
                        logger.warning(
                            "Skipping weather envelope refresh because "
                            "weather_samples_tbl is empty; run "
                            "scripts/dev/backfill_weather_samples.sh first."
                        )
                        return False

                    cur.execute("SELECT current_setting('work_mem')")
                    work_mem_row = cur.fetchone()
                    previous_work_mem = (
                        str(work_mem_row[0])
                        if work_mem_row and work_mem_row[0]
                        else None
                    )
                    cur.execute("SELECT current_setting('statement_timeout')")
                    statement_timeout_row = cur.fetchone()
                    previous_statement_timeout = (
                        str(statement_timeout_row[0])
                        if statement_timeout_row and statement_timeout_row[0]
                        else None
                    )
                    cur.execute(
                        "SELECT current_setting('max_parallel_workers_per_gather')"
                    )
                    max_parallel_row = cur.fetchone()
                    previous_max_parallel = (
                        str(max_parallel_row[0])
                        if max_parallel_row and max_parallel_row[0]
                        else None
                    )

                    if wait_for_lock:
                        logger.info("Waiting for weather envelope refresh advisory lock.")
                        cur.execute(
                            "SELECT pg_advisory_lock(%s, %s)",
                            _WEATHER_ENVELOPE_LOCK_KEY,
                        )
                        lock_acquired = True
                    else:
                        cur.execute(
                            "SELECT pg_try_advisory_lock(%s, %s)",
                            _WEATHER_ENVELOPE_LOCK_KEY,
                        )
                        row = cur.fetchone()
                        lock_acquired = bool(row[0]) if row else False
                        if not lock_acquired:
                            logger.info(
                                "Skipping weather envelope refresh because another refresh already holds the advisory lock."
                            )
                            return False

                    desired_work_mem = _weather_envelope_refresh_work_mem()
                    desired_statement_timeout = (
                        _weather_envelope_refresh_statement_timeout()
                    )
                    cur.execute("SET work_mem TO %s", (desired_work_mem,))
                    cur.execute(
                        "SET statement_timeout TO %s",
                        (desired_statement_timeout,),
                    )
                    cur.execute("SET max_parallel_workers_per_gather TO 0")
                    logger.info(
                        "Running weather envelope refresh with work_mem=%s, statement_timeout=%s, parallelism disabled.",
                        desired_work_mem,
                        desired_statement_timeout,
                    )

                    try:
                        cur.execute("SELECT refresh_weather_samples()")
                        topup_row = cur.fetchone()
                        inserted = topup_row[0] if topup_row else 0
                        logger.info(
                            "weather_samples_tbl topped up (new rows=%s).",
                            inserted,
                        )
                        cur.execute("SELECT refresh_species_weather_envelope()")
                        logger.info("species_weather_envelope_tbl rebuilt.")
                    finally:
                        if (
                            lock_acquired
                            and getattr(conn, "closed", 1) == 0
                            and not cur.closed
                        ):
                            try:
                                cur.execute(
                                    "SELECT pg_advisory_unlock(%s, %s)",
                                    _WEATHER_ENVELOPE_LOCK_KEY,
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to release weather envelope advisory lock because the connection was already closing.",
                                    exc_info=True,
                                )
                    return True
            finally:
                if getattr(conn, "closed", 1) == 0:
                    if previous_work_mem is not None:
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SET work_mem TO %s", (previous_work_mem,))
                        except Exception:
                            logger.warning(
                                "Failed to restore work_mem after weather envelope refresh because the connection was already closing.",
                                exc_info=True,
                            )
                    if previous_statement_timeout is not None:
                        try:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "SET statement_timeout TO %s",
                                    (previous_statement_timeout,),
                                )
                        except Exception:
                            logger.warning(
                                "Failed to restore statement_timeout after weather envelope refresh because the connection was already closing.",
                                exc_info=True,
                            )
                    if previous_max_parallel is not None:
                        try:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "SET max_parallel_workers_per_gather TO %s",
                                    (previous_max_parallel,),
                                )
                        except Exception:
                            logger.warning(
                                "Failed to restore max_parallel_workers_per_gather after weather envelope refresh because the connection was already closing.",
                                exc_info=True,
                            )
                    try:
                        conn.autocommit = previous_autocommit
                    except Exception:
                        logger.warning(
                            "Failed to restore autocommit after weather envelope refresh because the connection was already closing.",
                            exc_info=True,
                        )
    finally:
        pool.close()

    return False


# ---------------------------------------------------------------------------
# Standalone flows
# ---------------------------------------------------------------------------


def _derived_run_name() -> str:
    return f"derived-aggregates-refresh/{date.today().isoformat()}"


def run_derived_aggregates_flow(max_points: int = 500) -> None:
    """Refresh every derived dimension/fact table.

    Standalone post-processing flow combining both refresh tasks so
    they can be triggered as a unit (e.g. on container startup or via a
    nightly cron schedule).
    """
    refresh_species_dim_task()
    refresh_movebank_tracks_task(max_points=max_points)
    refresh_species_tracker_task()
    refresh_weather_envelope_task()
