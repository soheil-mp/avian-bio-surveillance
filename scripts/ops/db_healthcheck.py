#!/usr/bin/env python3
"""Smoke-test for TimescaleDB and MinIO infrastructure.

Run after ``docker compose up`` to verify the DB schema, TimescaleDB
extension, hypertable configuration, and MinIO buckets are all in place.

Usage::

    # From repo root (with containers running)
    python scripts/ops/db_healthcheck.py

    # Or with a custom DATABASE_URL
    DATABASE_URL=postgresql://avian:pass@localhost:5432/avian_biosurveillance \
        python scripts/ops/db_healthcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.config.runtime import load_runtime_env  # noqa: E402


load_runtime_env()


def _ok(label: str) -> str:
    return f"  ✓ {label}"


def _fail(label: str) -> str:
    return f"  ✗ {label}"


def main() -> int:
    """Run all health checks and print results."""
    passed = True

    # ----- TimescaleDB -----
    print("━" * 60)
    print("TimescaleDB Health Check")
    print("━" * 60)

    try:
        from avian_biosurveillance.db.database import ConnectionPool, healthcheck

        pool = ConnectionPool(min_conn=1, max_conn=2)
        result = healthcheck(pool)

        if result["timescaledb"]:
            print(_ok(f"TimescaleDB extension v{result['timescaledb']}"))
        else:
            print(_fail("TimescaleDB extension NOT found"))
            passed = False

        if result["tables_found"]:
            for table_name in sorted(result["tables_found"]):
                print(_ok(f"Table: {table_name}"))
        if result["tables_missing"]:
            for table_name in sorted(result["tables_missing"]):
                print(_fail(f"Table MISSING: {table_name}"))
            passed = False

        if result["hypertables"]:
            for hypertable_name in result["hypertables"]:
                print(_ok(f"Hypertable: {hypertable_name}"))
        else:
            print(_fail("No hypertables found"))
            passed = False

        # Bonus: check continuous aggregate
        with pool.cursor() as cur:
            cur.execute(
                "SELECT view_name FROM timescaledb_information.continuous_aggregates "
                "WHERE view_schema = 'public'"
            )
            continuous_aggregates = [row[0] for row in cur.fetchall()]
            if continuous_aggregates:
                for aggregate_name in continuous_aggregates:
                    print(_ok(f"Continuous aggregate: {aggregate_name}"))
            else:
                print(_fail("No continuous aggregates found"))
                passed = False

        # Row counts
        print()
        print("Row counts:")
        for table_name in sorted(result["tables_found"]):
            with pool.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
                count = cur.fetchone()[0]
                print(f"  {table_name}: {count:,} rows")

        pool.close()

    except Exception as exc:
        print(_fail(f"Connection failed: {exc}"))
        passed = False

    # ----- MinIO -----
    print()
    print("━" * 60)
    print("MinIO Health Check")
    print("━" * 60)

    try:
        from avian_biosurveillance.db.minio_client import AudioStore

        store = AudioStore()
        minio_result = store.healthcheck()

        for bucket_name in minio_result["buckets_found"]:
            print(_ok(f"Bucket: {bucket_name}"))
        for bucket_name in minio_result["buckets_missing"]:
            print(_fail(f"Bucket MISSING: {bucket_name}"))
            passed = False

    except Exception as exc:
        print(_fail(f"MinIO connection failed: {exc}"))
        passed = False

    # ----- Summary -----
    print()
    print("━" * 60)
    if passed:
        print("✓ All checks passed")
    else:
        print("✗ Some checks FAILED — review output above")
    print("━" * 60)

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
