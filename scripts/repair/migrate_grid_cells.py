#!/usr/bin/env python3
"""Roll out canonical grid cells on an existing TimescaleDB environment.

This one-off migration script makes the canonical ``grid_cells_tbl`` rollout
reproducible across environments by performing three steps safely:

1. Apply the baseline schema files with a privileged DDL connection.
2. Backfill canonical geodesic grid cells with the app write role.
3. Validate the foreign key from ``google_earth_engine_tbl`` once the
   canonical parent rows exist.

The baseline schema already uses the safe staged rollout pattern for populated
installations: the foreign key is created ``NOT VALID`` first and validated
only after backfill.

Usage::

    # Full rollout using credentials from .env
    python scripts/repair/migrate_grid_cells.py

    # Preview what would happen without changing the database
    python scripts/repair/migrate_grid_cells.py --dry-run

    # Skip schema apply if it was already run
    python scripts/repair/migrate_grid_cells.py --skip-schema

    # Override connection strings explicitly
    python scripts/repair/migrate_grid_cells.py \
        --ddl-dsn postgresql://root:...@localhost:15432/avian_biosurveillance \
        --app-dsn postgresql://root:...@localhost:15432/avian_biosurveillance
"""

# ruff: noqa: E402

# mypy: disable-error-code=import-untyped

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import psycopg2


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.grid import upsert_grid_cells
from avian_biosurveillance.db.models.grid import GridCell
from avian_biosurveillance.ingestion.earth_engine import EarthEngineGridCollector


load_runtime_env()

logger = logging.getLogger(__name__)

_DEFAULT_SCHEMA_PATH = _REPO_ROOT / "db" / "baseline"
_FK_NAME = "fk_geeg_grid_cell"


@dataclass(slots=True)
class RolloutStatus:
    grid_cells_count: int
    orphaned_grid_profiles: int
    fk_present: bool
    fk_validated: bool


@dataclass(slots=True)
class BackfillResult:
    generated: int
    upserted: int


@dataclass(slots=True)
class RolloutSummary:
    schema_applied: bool = False
    schema_path: str | None = None
    backfill: BackfillResult | None = None
    fk_validated: bool = False
    status: RolloutStatus | None = None


def _normalize_dsn(url: str) -> str:
    scheme_sep = "://"
    if scheme_sep not in url:
        return url

    scheme, rest = url.split(scheme_sep, 1)
    at_idx = rest.rfind("@")
    if at_idx == -1:
        return url

    creds, host_and_db = rest[:at_idx], rest[at_idx + 1 :]
    colon_idx = creds.find(":")
    if colon_idx == -1:
        return url

    user, password = creds[:colon_idx], creds[colon_idx + 1 :]
    return f"{scheme}://{quote_plus(user)}:{quote_plus(password)}@{host_and_db}"


def _build_dsn(*, user: str, password: str, host: str, port: str, dbname: str) -> str:
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
    )


def _resolve_host_port_db(
    *, host: str | None, port: str | None, dbname: str | None
) -> tuple[str, str, str]:
    resolved_host = host or os.environ.get("POSTGRES_HOST") or "localhost"
    resolved_port = port or os.environ.get("TSDB_PORT") or "5432"
    resolved_dbname = dbname or os.environ.get("POSTGRES_DB") or "avian_biosurveillance"
    return resolved_host, resolved_port, resolved_dbname


def _resolve_ddl_dsn(
    *, explicit_dsn: str | None, host: str | None, port: str | None, dbname: str | None
) -> str:
    if explicit_dsn:
        return _normalize_dsn(explicit_dsn)

    db_host, db_port, db_name = _resolve_host_port_db(
        host=host, port=port, dbname=dbname
    )

    credential_candidates = [
        (os.environ.get("POSTGRES_USER", "root"), os.environ.get("POSTGRES_PASSWORD")),
    ]

    for user, password in credential_candidates:
        if user and password:
            return _build_dsn(
                user=user, password=password, host=db_host, port=db_port, dbname=db_name
            )

    raise RuntimeError(
        "No privileged DDL credentials found. Provide --ddl-dsn or set "
        "POSTGRES_PASSWORD."
    )


def _resolve_app_dsn(
    *, explicit_dsn: str | None, host: str | None, port: str | None, dbname: str | None
) -> str:
    if explicit_dsn:
        return _normalize_dsn(explicit_dsn)

    url = os.environ.get("DATABASE_URL")
    if url:
        return _normalize_dsn(url)

    db_host, db_port, db_name = _resolve_host_port_db(
        host=host, port=port, dbname=dbname
    )
    user = os.environ.get("POSTGRES_USER", "root")
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "No PostgreSQL credentials found. Provide --app-dsn or set "
            "POSTGRES_PASSWORD / DATABASE_URL."
        )

    return _build_dsn(
        user=user, password=password, host=db_host, port=db_port, dbname=db_name
    )


def _execute_sql_script(dsn: str, sql_text: str) -> None:
    with psycopg2.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql_text)


def apply_schema(schema_path: Path, *, ddl_dsn: str) -> None:
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema path not found: {schema_path}")

    paths = sorted(schema_path.glob("*.sql")) if schema_path.is_dir() else [schema_path]
    if not paths:
        raise FileNotFoundError(f"No SQL files found in schema path: {schema_path}")

    for path in paths:
        logger.info("Applying schema file: %s", path)
        sql_text = path.read_text(encoding="utf-8")
        _execute_sql_script(ddl_dsn, sql_text)


def _build_grid_records(grid_size_km: float) -> list[GridCell]:
    cells = EarthEngineGridCollector.discover_grid_cells(grid_size_km)
    return [
        GridCell.from_spec(
            grid_cell_id=cell.grid_cell_id,
            grid_system=cell.grid_system,
            source=cell.source,
            lat=cell.lat,
            lon=cell.lon,
            bbox_min_lat=cell.bbox_min_lat,
            bbox_min_lon=cell.bbox_min_lon,
            bbox_max_lat=cell.bbox_max_lat,
            bbox_max_lon=cell.bbox_max_lon,
            grid_size_km=cell.grid_size_km,
            cell_geojson=cell.cell_geojson,
        )
        for cell in cells
    ]


def backfill_grid_cells(*, app_dsn: str, grid_size_km: float) -> BackfillResult:
    records = _build_grid_records(grid_size_km)
    pool = ConnectionPool(dsn=app_dsn, min_conn=1, max_conn=2)
    try:
        upserted = upsert_grid_cells(records, pool=pool)
    finally:
        pool.close()

    return BackfillResult(generated=len(records), upserted=upserted)


def fetch_rollout_status(*, ddl_dsn: str) -> RolloutStatus:
    with psycopg2.connect(ddl_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.grid_cells_tbl') IS NOT NULL, "
            "       to_regclass("
            "'public.google_earth_engine_tbl'"
            ") IS NOT NULL"
        )
        grid_table_exists, profile_table_exists = cur.fetchone()

        grid_cells_count = 0
        orphaned_profiles = 0
        if grid_table_exists:
            cur.execute("SELECT COUNT(*) FROM grid_cells_tbl")
            grid_cells_count = int(cur.fetchone()[0])

        if grid_table_exists and profile_table_exists:
            cur.execute(
                "SELECT COUNT(*) "
                "FROM google_earth_engine_tbl geeg "
                "LEFT JOIN grid_cells_tbl gc "
                "  ON gc.grid_cell_id = geeg.grid_cell_id "
                "WHERE gc.grid_cell_id IS NULL"
            )
            orphaned_profiles = int(cur.fetchone()[0])

        cur.execute(
            "SELECT convalidated FROM pg_constraint WHERE conname = %s",
            (_FK_NAME,),
        )
        fk_row = cur.fetchone()

    return RolloutStatus(
        grid_cells_count=grid_cells_count,
        orphaned_grid_profiles=orphaned_profiles,
        fk_present=fk_row is not None,
        fk_validated=bool(fk_row and fk_row[0]),
    )


def validate_grid_foreign_key(*, ddl_dsn: str) -> bool:
    status = fetch_rollout_status(ddl_dsn=ddl_dsn)
    if not status.fk_present:
        raise RuntimeError(
            f"Constraint {_FK_NAME} does not exist. Apply the schema first."
        )
    if status.orphaned_grid_profiles:
        raise RuntimeError(
            "Cannot validate canonical grid FK while orphaned grid profiles remain: "
            f"{status.orphaned_grid_profiles}"
        )
    if status.fk_validated:
        logger.info("Constraint %s is already validated", _FK_NAME)
        return True

    logger.info("Validating constraint %s", _FK_NAME)
    with psycopg2.connect(ddl_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE google_earth_engine_tbl VALIDATE CONSTRAINT {_FK_NAME}"
            )

    return fetch_rollout_status(ddl_dsn=ddl_dsn).fk_validated


def _print_summary(summary: RolloutSummary) -> None:
    print("═" * 68)
    print("Canonical grid rollout summary")
    print("═" * 68)

    if summary.schema_path:
        action = "applied" if summary.schema_applied else "skipped"
        print(f"Schema: {action} ({summary.schema_path})")

    if summary.backfill:
        print(
            "Backfill: generated "
            f"{summary.backfill.generated:,} cells; upserted {summary.backfill.upserted:,}"
        )

    if summary.status:
        print(f"grid_cells_tbl rows: {summary.status.grid_cells_count:,}")
        print(f"Orphaned grid profiles: {summary.status.orphaned_grid_profiles:,}")
        print(f"FK present: {'yes' if summary.status.fk_present else 'no'}")
        print(f"FK validated: {'yes' if summary.status.fk_validated else 'no'}")

    print("═" * 68)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the canonical grid-cell schema, backfill geodesic grid cells, "
            "and validate the Earth Engine grid foreign key."
        ),
    )
    parser.add_argument(
        "--schema-path",
        type=Path,
        default=_DEFAULT_SCHEMA_PATH,
        help="Path to a baseline SQL file or directory of ordered SQL files.",
    )
    parser.add_argument(
        "--grid-size-km",
        type=float,
        default=5.0,
        help="Canonical grid resolution to generate for the backfill.",
    )
    parser.add_argument(
        "--ddl-dsn", help="Privileged DSN used to apply schema and validate the FK."
    )
    parser.add_argument(
        "--app-dsn", help="Write-role DSN used to upsert canonical grid cells."
    )
    parser.add_argument("--host", help="Database host override for env-derived DSNs.")
    parser.add_argument("--port", help="Database port override for env-derived DSNs.")
    parser.add_argument("--dbname", help="Database name override for env-derived DSNs.")
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="Do not re-apply the schema before backfill.",
    )
    parser.add_argument(
        "--skip-backfill",
        action="store_true",
        help="Do not upsert canonical grid cells.",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Do not validate the canonical foreign key after backfill.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect state and generated cell counts without changing the DB.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ddl_dsn = _resolve_ddl_dsn(
        explicit_dsn=args.ddl_dsn,
        host=args.host,
        port=args.port,
        dbname=args.dbname,
    )
    app_dsn = _resolve_app_dsn(
        explicit_dsn=args.app_dsn,
        host=args.host,
        port=args.port,
        dbname=args.dbname,
    )

    summary = RolloutSummary(schema_path=str(args.schema_path))

    try:
        if args.dry_run:
            preview_records = _build_grid_records(args.grid_size_km)
            summary.backfill = BackfillResult(
                generated=len(preview_records), upserted=0
            )
            summary.status = fetch_rollout_status(ddl_dsn=ddl_dsn)
            _print_summary(summary)
            print("Dry run only: no schema changes, no backfill, no FK validation.")
            return 0

        if not args.skip_schema:
            apply_schema(args.schema_path, ddl_dsn=ddl_dsn)
            summary.schema_applied = True

        if not args.skip_backfill:
            summary.backfill = backfill_grid_cells(
                app_dsn=app_dsn, grid_size_km=args.grid_size_km
            )

        if not args.skip_validate:
            summary.fk_validated = validate_grid_foreign_key(ddl_dsn=ddl_dsn)

        summary.status = fetch_rollout_status(ddl_dsn=ddl_dsn)
        _print_summary(summary)

        if summary.status.orphaned_grid_profiles:
            return 1
        if not args.skip_validate and not summary.status.fk_validated:
            return 1
        return 0
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        psycopg2.Error,
    ) as exc:
        logger.exception("Canonical grid rollout failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
