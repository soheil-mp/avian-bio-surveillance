#!/usr/bin/env python3
"""Serialize Dagster instance storage migrations during container startup.

Both the Dagster webserver and daemon can start at the same time in Docker
Compose. Dagster's Postgres storage migrations are idempotent, but they should
still run before either service starts querying instance tables such as
``runs`` and ``event_logs``. This helper takes a Postgres advisory lock so both
startup paths can invoke it safely without racing each other.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Protocol, cast

import psycopg2  # type: ignore[import-untyped]
from alembic import util as alembic_util
from alembic.script import ScriptDirectory
from dagster_postgres.run_storage import run_storage


LOG_PREFIX = "[dagster-instance-migrate]"
_LOCK_KEY_1 = 1_498_372_417
_LOCK_KEY_2 = 1_832_843_379
_EMPTY_STORAGE_TABLES = ("runs", "event_logs", "job_ticks", "jobs")
_CURRENT_SCHEMA_MARKERS = (
    ("bulk_actions", "job_name"),
    ("concurrency_limits", "using_default_limit"),
    ("run_tags", "run_id"),
)


class _DbCursor(Protocol):
    def __enter__(self) -> _DbCursor: ...
    def __exit__(self, *_args: object) -> None: ...
    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None: ...
    def fetchone(self) -> tuple[object, ...] | None: ...


class _DbConnection(Protocol):
    autocommit: bool

    def cursor(self) -> _DbCursor: ...
    def close(self) -> None: ...


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    raise RuntimeError(f"{name} is required for Dagster instance migration")


def _connect_kwargs() -> dict[str, object]:
    return {
        "host": _required_env("DAGSTER_PG_HOST"),
        "port": int(_required_env("DAGSTER_PG_PORT")),
        "user": _required_env("DAGSTER_PG_USER"),
        "password": _required_env("DAGSTER_PG_PASSWORD"),
        "dbname": _required_env("DAGSTER_PG_DB"),
        "connect_timeout": int(
            os.getenv("DAGSTER_MIGRATE_CONNECT_TIMEOUT_SECONDS", "10")
        ),
        "application_name": "dagster_instance_migrate_lock",
    }


def _optional_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc

    return max(minimum, value)


def _lock_sql() -> str:
    return "SELECT pg_advisory_lock(%s, %s)"


def _unlock_sql() -> str:
    return "SELECT pg_advisory_unlock(%s, %s)"


def _is_missing_revision_error(output: str) -> bool:
    return (
        "Can't locate revision identified by" in output
        or "No such revision or branch" in output
    )


def _current_stamped_revision(connection: _DbConnection) -> str | None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version LIMIT 1")
            row = cursor.fetchone()
    except psycopg2.Error:
        return None

    if not row:
        return None

    revision = str(row[0]).strip()
    return revision or None


def _can_resolve_dagster_revision(revision: str) -> bool | None:
    try:
        script_directory = _dagster_script_directory()
        return script_directory.get_revision(revision) is not None
    except (alembic_util.CommandError, AttributeError):
        return None


def _dagster_script_directory() -> ScriptDirectory:
    alembic_config = cast(Any, run_storage).pg_alembic_config(run_storage.__file__)
    return ScriptDirectory.from_config(alembic_config)


def _current_dagster_head() -> str | None:
    try:
        return _dagster_script_directory().get_current_head()
    except (alembic_util.CommandError, AttributeError):
        return None


def _table_has_rows(connection: _DbConnection, table_name: str) -> bool | None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
            return cursor.fetchone() is not None
    except psycopg2.Error:
        return None


def _table_has_column(
    connection: _DbConnection, table_name: str, column_name: str
) -> bool | None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s "
                "LIMIT 1",
                (table_name, column_name),
            )
            return cursor.fetchone() is not None
    except psycopg2.Error:
        return None


def _can_restamp_unresolvable_revision(connection: _DbConnection) -> bool:
    table_emptiness = [
        _table_has_rows(connection, table_name) for table_name in _EMPTY_STORAGE_TABLES
    ]
    if any(has_rows is None or has_rows for has_rows in table_emptiness):
        return False

    schema_markers_present = [
        _table_has_column(connection, table_name, column_name)
        for table_name, column_name in _CURRENT_SCHEMA_MARKERS
    ]
    return all(marker is True for marker in schema_markers_present)


def _restamp_dagster_revision(connection: _DbConnection, revision: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM alembic_version")
        cursor.execute(
            "INSERT INTO alembic_version (version_num) VALUES (%s)",
            (revision,),
        )


def _connect_with_retry() -> Any:
    connect_kwargs = _connect_kwargs()
    max_attempts = _optional_int_env(
        "DAGSTER_MIGRATE_CONNECT_MAX_ATTEMPTS",
        10,
        minimum=1,
    )
    retry_seconds = _optional_int_env(
        "DAGSTER_MIGRATE_CONNECT_RETRY_SECONDS",
        2,
        minimum=0,
    )

    for attempt in range(1, max_attempts + 1):
        try:
            return psycopg2.connect(**connect_kwargs)
        except psycopg2.OperationalError as exc:
            if attempt >= max_attempts:
                raise

            retry_suffix = (
                f"; retrying in {retry_seconds}s"
                if retry_seconds > 0
                else "; retrying immediately"
            )
            print(
                f"{LOG_PREFIX} Dagster metadata database unavailable "
                f"(attempt {attempt}/{max_attempts}){retry_suffix}: {exc}",
                file=sys.stderr,
            )
            if retry_seconds > 0:
                time.sleep(retry_seconds)

    raise RuntimeError("Dagster metadata database connection retry loop exhausted")


def main() -> int:
    connection: _DbConnection = cast(_DbConnection, _connect_with_retry())
    connection.autocommit = True

    try:
        with connection.cursor() as cursor:
            print(f"{LOG_PREFIX} Waiting for Dagster migration lock.")
            cursor.execute(_lock_sql(), (_LOCK_KEY_1, _LOCK_KEY_2))
            print(f"{LOG_PREFIX} Acquired Dagster migration lock.")

        stamped_revision = _current_stamped_revision(connection)
        revision_is_resolvable = (
            _can_resolve_dagster_revision(stamped_revision)
            if stamped_revision
            else None
        )
        should_run_migration = True

        if stamped_revision and revision_is_resolvable is False:
            current_head = _current_dagster_head()
            if current_head and _can_restamp_unresolvable_revision(connection):
                _restamp_dagster_revision(connection, current_head)
                print(
                    f"{LOG_PREFIX} Restamped empty Dagster instance metadata "
                    f"from unresolved revision '{stamped_revision}' to current "
                    f"head '{current_head}'."
                )
                stamped_revision = current_head
                revision_is_resolvable = True

        if stamped_revision and revision_is_resolvable is False:
            print(
                f"{LOG_PREFIX} Skipping Dagster instance migration: "
                f"the current container image cannot resolve stamped Alembic "
                f"revision '{stamped_revision}' in the instance database.",
                file=sys.stderr,
            )
            should_run_migration = False

        try:
            if not should_run_migration:
                pass
            else:
                migration_result = subprocess.run(
                    ["dagster", "instance", "migrate"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if migration_result.stdout:
                    print(migration_result.stdout, end="")
                if migration_result.stderr:
                    print(migration_result.stderr, end="", file=sys.stderr)
        except subprocess.CalledProcessError as exc:
            output = f"{exc.stdout or ''}\n{exc.stderr or ''}"
            if _is_missing_revision_error(output):
                print(
                    f"{LOG_PREFIX} Skipping Dagster instance migration: "
                    "the current container image cannot resolve the stamped "
                    "Alembic revision in the instance database.",
                    file=sys.stderr,
                )
                if exc.stdout:
                    print(exc.stdout, end="")
            else:
                raise
        finally:
            with connection.cursor() as cursor:
                cursor.execute(_unlock_sql(), (_LOCK_KEY_1, _LOCK_KEY_2))
                print(f"{LOG_PREFIX} Released Dagster migration lock.")
    finally:
        connection.close()

    print(f"{LOG_PREFIX} Dagster instance migration completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
