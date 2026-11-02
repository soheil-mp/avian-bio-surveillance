"""Tests for derived aggregate refresh helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest

from avian_biosurveillance.orchestration import derived_aggregates_flow


_SPECIES_TRACKER_LOCK_KEY = (1_931_499_917, 1_919_252_851)
_WEATHER_ENVELOPE_LOCK_KEY = (1_734_019_440, 1_701_667_182)


class _Cursor:
    def __init__(
        self,
        *,
        fail_refresh: bool = False,
        try_lock_result: bool = True,
        samples_seeded: bool = True,
    ) -> None:
        self.fail_refresh = fail_refresh
        self.try_lock_result = try_lock_result
        self.samples_seeded = samples_seeded
        self.statements: list[tuple[str, object | None]] = []
        self.closed = False

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object | None = None) -> None:
        self.statements.append((sql, params))
        if self.fail_refresh and sql.startswith("SELECT refresh_species_tracker"):
            raise RuntimeError("refresh failed")

    def fetchone(self) -> tuple[object, ...]:
        sql, _params = self.statements[-1]
        if sql == "SELECT current_setting('work_mem')":
            return ("4MB",)
        if sql == "SELECT current_setting('statement_timeout')":
            return ("1s",)
        if sql == "SELECT current_setting('max_parallel_workers_per_gather')":
            return ("8",)
        if sql == "SELECT EXISTS(SELECT 1 FROM weather_samples_tbl)":
            return (self.samples_seeded,)
        if sql == "SELECT pg_try_advisory_lock(%s, %s)":
            return (self.try_lock_result,)
        return (42,)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.autocommit = False
        self.closed = 0
        self.cursor_value = cursor

    def cursor(self) -> _Cursor:
        return self.cursor_value


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection
        self.closed = False

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value

    def close(self) -> None:
        self.closed = True


def test_refresh_species_tracker_task_serializes_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)
    pool = _Pool(connection)
    monkeypatch.setattr(derived_aggregates_flow, "_derived_pool", lambda: pool)

    derived_aggregates_flow.refresh_species_tracker_task()

    assert cursor.statements == [
        ("SELECT current_setting('work_mem')", None),
        ("SELECT current_setting('statement_timeout')", None),
        (
            "SELECT pg_advisory_lock(%s, %s)",
            _SPECIES_TRACKER_LOCK_KEY,
        ),
        ("SET work_mem TO %s", ("512MB",)),
        ("SET statement_timeout TO %s", ("30min",)),
        ("SELECT refresh_species_tracker()", None),
        (
            "SELECT pg_advisory_unlock(%s, %s)",
            _SPECIES_TRACKER_LOCK_KEY,
        ),
        ("SET work_mem TO %s", ("4MB",)),
        ("SET statement_timeout TO %s", ("1s",)),
    ]
    assert pool.closed is True
    assert connection.autocommit is False


def test_refresh_species_tracker_task_supports_scoped_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    pool = _Pool(_Connection(cursor))
    monkeypatch.setattr(derived_aggregates_flow, "_derived_pool", lambda: pool)

    derived_aggregates_flow.refresh_species_tracker_task(
        wait_for_lock=False,
        start_date="2024-01-01",
        end_date=date(2024, 1, 31),
    )

    assert (
        "SELECT refresh_species_tracker(%s, %s)",
        (date(2024, 1, 1), date(2024, 1, 31)),
    ) in cursor.statements
    assert (
        "SELECT pg_try_advisory_lock(%s, %s)",
        _SPECIES_TRACKER_LOCK_KEY,
    ) in cursor.statements
    assert pool.closed is True


def test_refresh_species_tracker_task_supports_modeling_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    pool = _Pool(_Connection(cursor))
    monkeypatch.setattr(derived_aggregates_flow, "_derived_pool", lambda: pool)

    derived_aggregates_flow.refresh_species_tracker_task(
        wait_for_lock=False,
        start_date="2024-01-01",
        end_date=date(2024, 1, 31),
        refresh_scope="modeling",
    )

    assert (
        "SELECT refresh_species_tracker(%s, %s, %s)",
        (date(2024, 1, 1), date(2024, 1, 31), "modeling"),
    ) in cursor.statements
    assert pool.closed is True


def test_refresh_species_tracker_task_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError, match="refresh_scope must be one of: full, modeling"):
        derived_aggregates_flow.refresh_species_tracker_task(refresh_scope="weekly")


def test_refresh_species_tracker_task_releases_lock_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(fail_refresh=True)
    pool = _Pool(_Connection(cursor))
    monkeypatch.setattr(derived_aggregates_flow, "_derived_pool", lambda: pool)

    with pytest.raises(RuntimeError, match="refresh failed"):
        derived_aggregates_flow.refresh_species_tracker_task()

    assert (
        "SELECT pg_advisory_unlock(%s, %s)",
        _SPECIES_TRACKER_LOCK_KEY,
    ) in cursor.statements
    assert pool.closed is True


def test_refresh_weather_envelope_task_runs_topup_then_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    connection = _Connection(cursor)
    pool = _Pool(connection)
    monkeypatch.setattr(derived_aggregates_flow, "_derived_pool", lambda: pool)

    result = derived_aggregates_flow.refresh_weather_envelope_task()

    assert result is True
    assert cursor.statements == [
        ("SELECT EXISTS(SELECT 1 FROM weather_samples_tbl)", None),
        ("SELECT current_setting('work_mem')", None),
        ("SELECT current_setting('statement_timeout')", None),
        ("SELECT current_setting('max_parallel_workers_per_gather')", None),
        ("SELECT pg_advisory_lock(%s, %s)", _WEATHER_ENVELOPE_LOCK_KEY),
        ("SET work_mem TO %s", ("128MB",)),
        ("SET statement_timeout TO %s", ("45min",)),
        ("SET max_parallel_workers_per_gather TO 0", None),
        ("SELECT refresh_weather_samples()", None),
        ("SELECT refresh_species_weather_envelope()", None),
        ("SELECT pg_advisory_unlock(%s, %s)", _WEATHER_ENVELOPE_LOCK_KEY),
        ("SET work_mem TO %s", ("4MB",)),
        ("SET statement_timeout TO %s", ("1s",)),
        ("SET max_parallel_workers_per_gather TO %s", ("8",)),
    ]
    assert pool.closed is True
    assert connection.autocommit is False


def test_refresh_weather_envelope_task_skips_when_samples_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(samples_seeded=False)
    pool = _Pool(_Connection(cursor))
    monkeypatch.setattr(derived_aggregates_flow, "_derived_pool", lambda: pool)

    result = derived_aggregates_flow.refresh_weather_envelope_task()

    assert result is False
    # Guard short-circuits before any lock/work_mem/refresh work.
    assert cursor.statements == [
        ("SELECT EXISTS(SELECT 1 FROM weather_samples_tbl)", None),
    ]
    assert pool.closed is True
