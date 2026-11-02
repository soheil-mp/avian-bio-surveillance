"""Tests for serialized Dagster instance migration startup helper."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest


class _MigrateModule(Protocol):
    _LOCK_KEY_1: int
    _LOCK_KEY_2: int

    def _connect_kwargs(self) -> dict[str, object]: ...
    def main(self) -> int: ...


def _load_migrate_module() -> _MigrateModule:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ops"
        / "dagster_migrate_instance.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dagster_migrate_instance",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load dagster_migrate_instance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_MigrateModule, module)


migrate = _load_migrate_module()


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self._last_sql: str | None = None
        self._last_params: tuple[object, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self._last_sql = sql
        self._last_params = params
        if (
            sql
            in {"SELECT pg_advisory_lock(%s, %s)", "SELECT pg_advisory_unlock(%s, %s)"}
            and params is not None
        ):
            self._connection.statements.append((sql, params))
        if sql == "DELETE FROM alembic_version":
            self._connection.stamped_revision = None
        if (
            sql == "INSERT INTO alembic_version (version_num) VALUES (%s)"
            and params is not None
        ):
            self._connection.stamped_revision = str(params[0])

    def fetchone(self) -> tuple[str] | None:
        if self._last_sql == "SELECT version_num FROM alembic_version LIMIT 1":
            if self._connection.stamped_revision is None:
                return None
            return (self._connection.stamped_revision,)
        if self._last_sql == (
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s "
            "LIMIT 1"
        ):
            if self._last_params is None:
                return None
            marker = (str(self._last_params[0]), str(self._last_params[1]))
            if marker in self._connection.marker_columns:
                return ("1",)
        if self._last_sql and self._last_sql.startswith("SELECT 1 FROM "):
            table_name = self._last_sql.split()[3]
            if self._connection.table_rows.get(table_name, 0) > 0:
                return ("1",)
            return None
        return None


class _Connection:
    def __init__(
        self,
        *,
        stamped_revision: str | None = None,
        table_rows: dict[str, int] | None = None,
        marker_columns: set[tuple[str, str]] | None = None,
    ) -> None:
        self.autocommit = False
        self.closed = False
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.stamped_revision = stamped_revision
        self.table_rows = table_rows or {}
        self.marker_columns = marker_columns or set()

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def _set_pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAGSTER_PG_HOST", "timescaledb")
    monkeypatch.setenv("DAGSTER_PG_PORT", "5432")
    monkeypatch.setenv("DAGSTER_PG_USER", "root")
    monkeypatch.setenv("DAGSTER_PG_PASSWORD", "secret")
    monkeypatch.setenv("DAGSTER_PG_DB", "dagster_db")


def _migration_lock_key() -> tuple[int, int]:
    return (
        cast(int, migrate._LOCK_KEY_1),
        cast(int, migrate._LOCK_KEY_2),
    )


def test_connect_kwargs_reads_dagster_postgres_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pg_env(monkeypatch)
    monkeypatch.setenv("DAGSTER_MIGRATE_CONNECT_TIMEOUT_SECONDS", "25")

    connect_kwargs = cast(
        Callable[[], dict[str, object]],
        migrate._connect_kwargs,
    )
    kwargs = connect_kwargs()

    assert kwargs == {
        "host": "timescaledb",
        "port": 5432,
        "user": "root",
        "password": "secret",
        "dbname": "dagster_db",
        "connect_timeout": 25,
        "application_name": "dagster_instance_migrate_lock",
    }


def test_main_serializes_migration_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pg_env(monkeypatch)
    connection = _Connection()
    subprocess_calls: list[dict[str, object]] = []
    lock_key = _migration_lock_key()

    monkeypatch.setattr(migrate.psycopg2, "connect", lambda **_kwargs: connection)

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=["dagster", "instance", "migrate"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(migrate.subprocess, "run", _run)

    result = migrate.main()

    assert result == 0
    assert connection.autocommit is True
    assert connection.closed is True
    assert connection.statements == [
        (
            "SELECT pg_advisory_lock(%s, %s)",
            lock_key,
        ),
        (
            "SELECT pg_advisory_unlock(%s, %s)",
            lock_key,
        ),
    ]
    assert subprocess_calls == [
        {
            "args": (["dagster", "instance", "migrate"],),
            "kwargs": {
                "check": True,
                "capture_output": True,
                "text": True,
            },
        }
    ]


def test_main_retries_database_connection_before_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pg_env(monkeypatch)
    connection = _Connection()
    connect_attempts = 0
    sleep_calls: list[int] = []
    lock_key = _migration_lock_key()
    operational_error = migrate.psycopg2.OperationalError

    monkeypatch.setenv("DAGSTER_MIGRATE_CONNECT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("DAGSTER_MIGRATE_CONNECT_RETRY_SECONDS", "4")

    def _connect(**_kwargs: object) -> _Connection:
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts == 1:
            raise operational_error("the database system is starting up")
        return connection

    monkeypatch.setattr(migrate.psycopg2, "connect", _connect)
    monkeypatch.setattr(migrate.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(
        migrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["dagster", "instance", "migrate"],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    result = migrate.main()

    assert result == 0
    assert connect_attempts == 2
    assert sleep_calls == [4]
    assert connection.statements == [
        ("SELECT pg_advisory_lock(%s, %s)", lock_key),
        ("SELECT pg_advisory_unlock(%s, %s)", lock_key),
    ]


def test_main_skips_preflight_when_stamped_revision_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_pg_env(monkeypatch)
    connection = _Connection(stamped_revision="29b539ebc72a")
    lock_key = _migration_lock_key()
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(migrate.psycopg2, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(migrate, "_can_resolve_dagster_revision", lambda _rev: False)

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=["dagster", "instance", "migrate"], returncode=0
        )

    monkeypatch.setattr(migrate.subprocess, "run", _run)

    result = migrate.main()

    assert result == 0
    assert subprocess_calls == []
    assert connection.closed is True
    assert connection.statements == [
        ("SELECT pg_advisory_lock(%s, %s)", lock_key),
        ("SELECT pg_advisory_unlock(%s, %s)", lock_key),
    ]
    assert (
        "cannot resolve stamped Alembic revision '29b539ebc72a'"
        in capsys.readouterr().err
    )


def test_main_restamps_empty_current_schema_when_revision_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_pg_env(monkeypatch)
    connection = _Connection(
        stamped_revision="29b539ebc72a",
        table_rows={
            "runs": 0,
            "event_logs": 0,
            "job_ticks": 0,
            "jobs": 0,
        },
        marker_columns={
            ("bulk_actions", "job_name"),
            ("concurrency_limits", "using_default_limit"),
            ("run_tags", "run_id"),
        },
    )
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(migrate.psycopg2, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(migrate, "_can_resolve_dagster_revision", lambda _rev: False)
    monkeypatch.setattr(migrate, "_current_dagster_head", lambda: "7e2f3204cf8e")

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=["dagster", "instance", "migrate"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(migrate.subprocess, "run", _run)

    result = migrate.main()

    assert result == 0
    assert connection.stamped_revision == "7e2f3204cf8e"
    assert subprocess_calls
    captured = capsys.readouterr()
    assert "Restamped empty Dagster instance metadata" in captured.out
    assert "Skipping Dagster instance migration" not in captured.err


def test_main_releases_lock_when_migration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_pg_env(monkeypatch)
    connection = _Connection()
    lock_key = _migration_lock_key()

    monkeypatch.setattr(migrate.psycopg2, "connect", lambda **_kwargs: connection)

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(migrate.subprocess, "run", _fail)

    with pytest.raises(RuntimeError, match="boom"):
        migrate.main()

    assert connection.closed is True
    assert connection.statements[-1] == (
        "SELECT pg_advisory_unlock(%s, %s)",
        lock_key,
    )


def test_main_skips_unresolvable_revision_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_pg_env(monkeypatch)
    connection = _Connection()
    lock_key = _migration_lock_key()
    monkeypatch.setattr(migrate.psycopg2, "connect", lambda **_kwargs: connection)

    def _missing_revision(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["dagster", "instance", "migrate"],
            stderr="alembic.util.exc.CommandError: Can't locate revision identified by '29b539ebc72a'",
        )

    monkeypatch.setattr(migrate.subprocess, "run", _missing_revision)

    result = migrate.main()

    assert result == 0
    assert connection.closed is True
    assert connection.statements[-1] == (
        "SELECT pg_advisory_unlock(%s, %s)",
        lock_key,
    )
    captured = capsys.readouterr()
    assert "Skipping Dagster instance migration" in captured.err
    assert "Can't locate revision identified by" not in captured.err
