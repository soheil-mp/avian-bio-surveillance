"""Focused tests for modeling-output database loaders."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

import avian_biosurveillance.db.loaders.modeling as modeling_loader


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((sql, params))

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConnection()

    def connection(self) -> _FakeConnection:
        return self.conn


def test_persist_modeling_run_replaces_run_rows_and_serializes_scores(
    monkeypatch,
) -> None:
    pool = _FakePool()
    execute_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def fake_execute_values(cur: Any, sql: str, values: list[tuple[Any, ...]]) -> None:
        execute_calls.append((sql, list(values)))

    monkeypatch.setattr(modeling_loader.psycopg2.extras, "execute_values", fake_execute_values)

    counts = modeling_loader.persist_modeling_run(
        run_id="run-1",
        run_kind="anomaly_scoring",
        generated_at=datetime(2026, 3, 9, tzinfo=UTC),
        completed_at=datetime(2026, 3, 9, 0, 5, tzinfo=UTC),
        config={"min_observation_days": 3},
        stats={"rows_scored": 1},
        source_coverage={"scores": {"row_count": 1}},
        validation_report={"validation": {"rows_scored": 1}},
        features=pd.DataFrame(
            [
                {
                    "species_id": "35",
                    "period_start": "2026-03-02",
                    "period_end": "2026-03-08",
                    "total_detections": 7,
                }
            ]
        ),
        scores=pd.DataFrame(
            [
                {
                    "species_id": "35",
                    "period_start": "2026-03-02",
                    "period_end": "2026-03-08",
                    "model_anomaly_score": 0.7,
                    "final_score": 0.9,
                    "top_detection_ids": "det-1, det-2",
                    "anomaly_streams": ["population", "embedding"],
                }
            ]
        ),
        pool=pool,  # type: ignore[arg-type]
        chunk_size=1,
    )

    assert counts == {
        "feature_rows": 1,
        "score_rows": 1,
        "cluster_rows": 0,
    }
    assert pool.conn.committed is True
    assert pool.conn.rolled_back is False
    assert len(pool.conn.cursor_obj.statements) == 3
    assert any(sql == modeling_loader._RUN_SQL for sql, _ in execute_calls)
    score_values = next(
        values for sql, values in execute_calls if sql == modeling_loader._SCORE_SQL
    )
    assert score_values[0][21] == ["det-1", "det-2"]
    assert score_values[0][22] == ["population", "embedding"]


def test_persist_modeling_run_replaces_only_requested_period_window(
    monkeypatch,
) -> None:
    pool = _FakePool()
    execute_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def fake_execute_values(cur: Any, sql: str, values: list[tuple[Any, ...]]) -> None:
        execute_calls.append((sql, list(values)))

    monkeypatch.setattr(modeling_loader.psycopg2.extras, "execute_values", fake_execute_values)

    modeling_loader.persist_modeling_run(
        run_id="run-windowed",
        run_kind="anomaly_scoring",
        generated_at=datetime(2026, 3, 9, tzinfo=UTC),
        scores=pd.DataFrame(
            [
                {
                    "species_id": "35",
                    "period_start": "2026-03-02",
                    "period_end": "2026-03-08",
                }
            ]
        ),
        pool=pool,  # type: ignore[arg-type]
        replace_existing_run=False,
        replace_period_start=date(2026, 3, 2),
        replace_period_end=date(2026, 3, 8),
    )

    delete_statements = pool.conn.cursor_obj.statements
    assert len(delete_statements) == 3
    assert all(
        params == ("run-windowed", date(2026, 3, 2), date(2026, 3, 8))
        for _, params in delete_statements
    )
    assert any(sql == modeling_loader._RUN_SQL for sql, _ in execute_calls)
    assert any(sql == modeling_loader._SCORE_SQL for sql, _ in execute_calls)


def test_persist_tuning_leaderboard_upserts_run_and_rows(monkeypatch) -> None:
    pool = _FakePool()
    execute_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def fake_execute_values(cur: Any, sql: str, values: list[tuple[Any, ...]]) -> None:
        execute_calls.append((sql, list(values)))

    monkeypatch.setattr(modeling_loader.psycopg2.extras, "execute_values", fake_execute_values)

    count = modeling_loader.persist_tuning_leaderboard(
        tuning_run_id="tune-1",
        generated_at=datetime(2026, 3, 9, tzinfo=UTC),
        mode="axis",
        objective_weights={"sovon_mortality_auroc": 1.0},
        negative_control_penalty=0.5,
        top_k=25,
        leaderboard=pd.DataFrame(
            [
                {
                    "rank": 1,
                    "config_index": 0,
                    "config_label": "baseline",
                    "objective": 0.42,
                    "overrides": "{}",
                }
            ]
        ),
        payload={"config_count": 1},
        pool=pool,  # type: ignore[arg-type]
        chunk_size=1,
    )

    assert count == 1
    assert pool.conn.committed is True
    assert any(sql == modeling_loader._TUNING_RUN_SQL for sql, _ in execute_calls)
    row_values = next(
        values for sql, values in execute_calls if sql == modeling_loader._TUNING_ROW_SQL
    )
    assert row_values[0][1:5] == (1, 0, "baseline", 0.42)
