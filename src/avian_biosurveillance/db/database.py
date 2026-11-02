"""TimescaleDB connection pool and transaction helpers.

Provides a thin wrapper around :mod:`psycopg2` with:

* Thread-safe connection pooling (:class:`ConnectionPool`).
* Runtime config from ``config/runtime.env`` plus secret env overrides.
* Context-managed transactions via :meth:`ConnectionPool.cursor`.
* A module-level singleton via :func:`get_pool` / :func:`close_pool`.
* A quick :func:`healthcheck` that verifies tables + TimescaleDB extension.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool

from avian_biosurveillance.config.runtime import load_runtime_env


load_runtime_env()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_DSN_TEMPLATE = "postgresql://{user}:{password}@{host}:{port}/{dbname}"

# Required runtime relations from db/ (tables, views, and continuous
# aggregates that the application queries directly).
_EXPECTED_TABLES = frozenset(
    {
        "birdweather_tbl",
        "birdweather_daily_cagg_tbl",
        "knmi_tbl",
        "knmi_daily_cagg_tbl",
        "sovon_tbl",
        "ebird_tbl",
        "movebank_tbl",
        "movebank_tracks_tbl",
        "google_earth_engine_tbl",
        "grid_cells_tbl",
        "google_earth_engine_grid_tbl",
        "cds_drought_tbl",
        "seismic_tbl",
        "birdweather_stations_tbl",
        "species_tbl",
        "data_quality_tbl",
        "modeling_runs_tbl",
        "modeling_species_week_features_tbl",
        "modeling_species_week_scores_tbl",
        "modeling_embedding_clusters_tbl",
        "modeling_tuning_runs_tbl",
        "modeling_tuning_leaderboard_rows_tbl",
    }
)


def _build_dsn() -> str:
    """Resolve a libpq DSN from environment variables.

    Priority:
        1. ``DATABASE_URL`` (full connection string; normally injected for
           containers from ``config/runtime.env`` + secrets).
        2. Individual ``POSTGRES_*`` values, with ports/hosts sourced from
           ``config/runtime.env``.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return _safe_encode_dsn(url)

    user = os.environ.get("POSTGRES_USER", "root")
    password = os.environ.get("POSTGRES_PASSWORD", "")

    return _DEFAULT_DSN_TEMPLATE.format(
        user=quote_plus(user),
        password=quote_plus(password),
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["TSDB_PORT"],
        dbname=os.environ.get("POSTGRES_DB", "avian_biosurveillance"),
    )


def _safe_encode_dsn(url: str) -> str:
    """Re-encode the password in a ``postgresql://`` DSN.

    Passwords containing URL-reserved characters (``#``, ``@``, ``%``)
    break Python's :class:`urllib.parse.urlparse` and most database
    drivers because ``#`` is treated as a fragment delimiter.  This
    function splits the URL manually and re-encodes the password with
    :func:`~urllib.parse.quote_plus`.
    """
    scheme_sep = "://"
    if scheme_sep not in url:
        return url  # not a URL-style DSN — return as-is
    scheme, rest = url.split(scheme_sep, 1)
    at_idx = rest.rfind("@")
    if at_idx == -1:
        return url  # no credentials
    creds, host_and_db = rest[:at_idx], rest[at_idx + 1 :]
    colon_idx = creds.find(":")
    if colon_idx == -1:
        return url  # no password
    user, password = creds[:colon_idx], creds[colon_idx + 1 :]
    return f"{scheme}://{quote_plus(user)}:{quote_plus(password)}@{host_and_db}"


# ---------------------------------------------------------------------------
# Connection Pool
# ---------------------------------------------------------------------------


class ConnectionPool:
    """Thread-safe psycopg2 connection pool with transaction helpers.

    Args:
        dsn: PostgreSQL connection string.  Defaults to env-based resolution.
        min_conn: Minimum number of connections to keep open.
        max_conn: Maximum number of connections allowed.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        min_conn: int = 2,
        max_conn: int = 10,
    ) -> None:
        self._dsn = dsn or _build_dsn()
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=min_conn,
            maxconn=max_conn,
            dsn=self._dsn,
        )
        logger.info("Connection pool created (min=%d, max=%d)", min_conn, max_conn)

    # -- Context-managed cursor -----------------------------------------

    @contextmanager
    def cursor(self, **kwargs: Any) -> Generator[Any, None, None]:
        """Yield a DB cursor inside an auto-committing transaction.

        On success the transaction is committed; on exception it is
        rolled back and the exception re-raised.

        Usage::

            with pool.cursor() as cur:
                cur.execute("SELECT 1")
        """
        conn = self._pool.getconn()
        discard_connection = False
        try:
            with conn.cursor(**kwargs) as cur:
                yield cur
            conn.commit()
        except Exception:
            if conn.closed:
                discard_connection = True
            else:
                try:
                    conn.rollback()
                except Exception:
                    discard_connection = True
            raise
        finally:
            if discard_connection or conn.closed:
                self._pool.putconn(conn, close=True)
            else:
                self._pool.putconn(conn)

    @contextmanager
    def connection(self) -> Generator[Any, None, None]:
        """Yield a raw connection for callers that need multi-statement control."""
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    # -- Lifecycle --------------------------------------------------

    def close(self) -> None:
        """Close all connections in the pool."""
        self._pool.closeall()
        logger.info("Connection pool closed.")

    @property
    def dsn(self) -> str:
        """Return the connection string (password masked)."""
        parts = self._dsn.split("@")
        if len(parts) > 1:
            return "***@" + parts[-1]
        return "***"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_pool: ConnectionPool | None = None


def get_pool(**kwargs: Any) -> ConnectionPool:
    """Return (or create) the module-level connection pool.

    Keyword arguments are forwarded to :class:`ConnectionPool` on first call.
    Subsequent calls return the existing pool regardless of kwargs.
    """
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ConnectionPool(**kwargs)
    return _pool


def close_pool() -> None:
    """Close and discard the module-level pool."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def healthcheck(pool: ConnectionPool | None = None) -> dict[str, Any]:
    """Verify TimescaleDB connectivity, extension, and schema.

    Returns a dict with keys:
        ``ok``            - bool, True if all checks pass
        ``timescaledb``   - installed TimescaleDB version or None
        ``tables_found``  - set of expected tables that exist
        ``tables_missing``- set of expected tables that are absent
        ``hypertables``   - list of hypertable names

    Raises:
        psycopg2.Error: on connection failure.
    """
    pool = pool or get_pool()
    result: dict[str, Any] = {
        "ok": False,
        "timescaledb": None,
        "tables_found": set(),
        "tables_missing": set(),
        "hypertables": [],
    }

    with pool.cursor() as cur:
        # 1. TimescaleDB extension version
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        row = cur.fetchone()
        result["timescaledb"] = row[0] if row else None

        # 2. Check expected tables and views exist
        cur.execute(
            "SELECT relname "
            "FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace "
            "AND relkind IN ('r', 'v', 'm', 'p')"
        )
        existing = {r[0] for r in cur.fetchall()}
        result["tables_found"] = _EXPECTED_TABLES & existing
        result["tables_missing"] = _EXPECTED_TABLES - existing

        # 3. List hypertables
        if result["timescaledb"]:
            cur.execute(
                "SELECT hypertable_name FROM timescaledb_information.hypertables "
                "WHERE hypertable_schema = 'public'"
            )
            result["hypertables"] = [r[0] for r in cur.fetchall()]

    result["ok"] = (
        result["timescaledb"] is not None and len(result["tables_missing"]) == 0
    )
    return result
