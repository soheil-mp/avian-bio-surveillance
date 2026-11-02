"""Tests for the modeling backfill CLI wrapper."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd
import pytest

import avian_biosurveillance.ops.run_modeling_backfill as cli_module
from avian_biosurveillance.modeling.schemas import ModelingRunStats


class _FakePool:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _stub_embedding_preload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "_load_embedding_clip_rows",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )


def test_run_modeling_backfill_passes_cli_options(monkeypatch, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        captured.update(kwargs)
        return ModelingRunStats(run_id="run-1", rows_loaded=2, rows_scored=1)

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(
        [
            "--scoring-mode",
            "walk_forward",
            "--all-species",
            "--no-embedding-drift",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert captured == {
        "scoring_mode": "walk_forward",
        "target_species_only": False,
        "include_embedding_drift": False,
        "include_embedding_clusters": False,
        "include_embedding_control": False,
        "include_sovon_context": True,
        "skip_evaluation": False,
        "include_environmental": False,
        "model_streams": ("population",),
    }
    assert '"run_id": "run-1"' in output


def test_run_modeling_backfill_refreshes_derived_before_modeling(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_refresh_species_dim_task() -> None:
        calls.append(("species_dim", {}))

    def fake_refresh_species_tracker_task(**kwargs: Any) -> bool:
        calls.append(("species_tracker", kwargs))
        return True

    def fake_load_species_tracker_coverage() -> cli_module.SpeciesTrackerCoverage:
        calls.append(("coverage", {}))
        return cli_module.SpeciesTrackerCoverage(
            min_period_start=date(2024, 1, 1),
            max_period_start=date(2025, 12, 29),
            weekly_periods=105,
            rows=10_000,
        )

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        calls.append(("modeling", kwargs))
        return ModelingRunStats(run_id="run-2", rows_loaded=10, rows_scored=8)

    monkeypatch.setattr(
        cli_module,
        "refresh_species_dim_task",
        fake_refresh_species_dim_task,
    )
    monkeypatch.setattr(
        cli_module,
        "refresh_species_tracker_task",
        fake_refresh_species_tracker_task,
    )
    monkeypatch.setattr(
        cli_module,
        "_load_species_tracker_coverage",
        fake_load_species_tracker_coverage,
    )
    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(
        [
            "--refresh-derived",
            "--refresh-start-date",
            "2024-01-01",
            "--refresh-end-date",
            "2025-12-31",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [
        ("species_dim", {}),
        (
            "species_tracker",
            {
                "start_date": date(2024, 1, 1),
                "end_date": date(2025, 12, 31),
                "refresh_scope": "modeling",
            },
        ),
        ("coverage", {}),
        (
            "modeling",
            {
                "scoring_mode": "retrospective",
                "target_species_only": True,
                "include_embedding_drift": True,
                "include_embedding_clusters": True,
                "include_embedding_control": True,
                "include_sovon_context": True,
                "skip_evaluation": False,
                "include_environmental": False,
                "model_streams": ("population", "bioacoustic"),
            },
        ),
    ]
    assert '"run_id": "run-2"' in output


def test_run_modeling_backfill_stops_when_tracker_coverage_is_incomplete(
    monkeypatch,
    capsys,
) -> None:
    run_called = False

    def fake_run_model_anomaly_scoring(**_: Any) -> ModelingRunStats:
        nonlocal run_called
        run_called = True
        return ModelingRunStats(run_id="run-should-not-start")

    monkeypatch.setattr(cli_module, "refresh_species_dim_task", lambda: None)
    monkeypatch.setattr(
        cli_module,
        "refresh_species_tracker_task",
        lambda **_: True,
    )
    monkeypatch.setattr(
        cli_module,
        "_load_species_tracker_coverage",
        lambda: cli_module.SpeciesTrackerCoverage(
            min_period_start=date(2024, 1, 1),
            max_period_start=date(2025, 6, 30),
            weekly_periods=62,
            rows=41_721,
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(
        [
            "--refresh-derived",
            "--refresh-end-date",
            "2025-12-31",
        ]
    )

    assert exit_code == 1
    assert not run_called
    assert capsys.readouterr().out == ""


def test_run_modeling_backfill_windowed_refreshes_scores_and_skips_complete(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(cli_module, "ConnectionPool", _FakePool)
    monkeypatch.setattr(
        cli_module,
        "refresh_species_dim_task",
        lambda: calls.append(("species_dim", {})),
    )

    def fake_refresh_species_tracker_task(**kwargs: Any) -> bool:
        calls.append(("species_tracker", kwargs))
        return True

    def fake_window_already_complete(**kwargs: Any) -> bool:
        calls.append(("skip_check", kwargs))
        return kwargs["start_date"] == date(2024, 1, 1)

    def fake_load_species_tracker_coverage(
        **kwargs: Any,
    ) -> cli_module.SpeciesTrackerCoverage:
        calls.append(("coverage", kwargs))
        return cli_module.SpeciesTrackerCoverage(
            min_period_start=date(2024, 1, 8),
            max_period_start=date(2024, 1, 8),
            weekly_periods=1,
            rows=100,
        )

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        calls.append(("modeling", kwargs))
        return ModelingRunStats(
            run_id=kwargs["run_id"], rows_loaded=100, rows_scored=10
        )

    def fake_mark_window_complete(**kwargs: Any) -> None:
        calls.append(("mark_window", kwargs))

    def fake_load_aggregate_run_stats(**kwargs: Any) -> ModelingRunStats:
        calls.append(("aggregate_stats", kwargs))
        return ModelingRunStats(run_id=kwargs["run_id"], rows_scored=10)

    def fake_finalize_windowed_run(**kwargs: Any) -> None:
        calls.append(("finalize", kwargs))

    monkeypatch.setattr(
        cli_module,
        "refresh_species_tracker_task",
        fake_refresh_species_tracker_task,
    )
    monkeypatch.setattr(
        cli_module,
        "_window_already_complete",
        fake_window_already_complete,
    )
    monkeypatch.setattr(
        cli_module,
        "_load_species_tracker_coverage",
        fake_load_species_tracker_coverage,
    )
    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )
    monkeypatch.setattr(
        cli_module,
        "_mark_window_complete",
        fake_mark_window_complete,
    )
    monkeypatch.setattr(
        cli_module,
        "_load_aggregate_run_stats",
        fake_load_aggregate_run_stats,
    )
    monkeypatch.setattr(
        cli_module,
        "_finalize_windowed_run",
        fake_finalize_windowed_run,
    )

    exit_code = cli_module.main(
        [
            "--windowed",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-14",
            "--window-days",
            "7",
            "--run-id",
            "aggregate-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["run_id"] == "aggregate-run"
    assert payload["windows_total"] == 2
    assert payload["windows_processed"] == 1
    assert payload["windows_skipped"] == 1
    assert payload["model_passes"] == 1
    assert [name for name, _ in calls].count("species_dim") == 1
    assert [name for name, _ in calls].count("species_tracker") == 1
    modeling_call = next(kwargs for name, kwargs in calls if name == "modeling")
    assert modeling_call["source_start_date"] == date(2024, 1, 1)
    assert modeling_call["source_end_date"] == date(2024, 1, 8)
    assert modeling_call["output_period_start"] == date(2024, 1, 8)
    assert modeling_call["output_period_end"] == date(2024, 1, 8)
    assert modeling_call["run_id"] == "aggregate-run"
    assert modeling_call["include_embedding_clusters"] is True
    assert modeling_call["include_embedding_control"] is True
    assert modeling_call["replace_existing_run"] is False
    finalize_call = next(kwargs for name, kwargs in calls if name == "finalize")
    assert finalize_call["windows_total"] == 2
    assert finalize_call["windows_processed"] == 1
    assert finalize_call["windows_skipped"] == 1
    assert finalize_call["model_passes"] == 1


def test_run_modeling_backfill_chunks_range_tracker_refresh(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli_module,
        "refresh_species_tracker_task",
        lambda **kwargs: calls.append(kwargs),
    )

    windows = cli_module._iter_windows(
        date(2024, 1, 1),
        date(2024, 3, 10),
        window_days=7,
    )

    cli_module._refresh_species_tracker_range(
        windows=windows,
        refresh_scope="modeling",
        chunk_windows=8,
    )

    assert calls == [
        {
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 2, 25),
            "refresh_scope": "modeling",
        },
        {
            "start_date": date(2024, 2, 26),
            "end_date": date(2024, 3, 10),
            "refresh_scope": "modeling",
        },
    ]


def test_run_modeling_backfill_windowed_can_disable_embedding_extras(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(cli_module, "ConnectionPool", _FakePool)
    monkeypatch.setattr(cli_module, "refresh_species_dim_task", lambda: None)
    monkeypatch.setattr(cli_module, "refresh_species_tracker_task", lambda **_: True)
    monkeypatch.setattr(
        cli_module,
        "_load_species_tracker_coverage",
        lambda **_: cli_module.SpeciesTrackerCoverage(
            min_period_start=date(2024, 1, 1),
            max_period_start=date(2024, 1, 1),
            weekly_periods=1,
            rows=100,
        ),
    )
    monkeypatch.setattr(cli_module, "_window_already_complete", lambda **_: False)
    monkeypatch.setattr(cli_module, "_mark_window_complete", lambda **_: None)
    monkeypatch.setattr(
        cli_module,
        "_load_aggregate_run_stats",
        lambda **kwargs: ModelingRunStats(run_id=kwargs["run_id"], rows_scored=1),
    )
    monkeypatch.setattr(cli_module, "_finalize_windowed_run", lambda **_: None)

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        captured.update(kwargs)
        return ModelingRunStats(run_id=kwargs["run_id"], rows_scored=1)

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(
        [
            "--windowed",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-07",
            "--no-embedding-clusters",
            "--no-embedding-control",
        ]
    )

    assert exit_code == 0
    assert captured["include_embedding_drift"] is True
    assert captured["include_embedding_clusters"] is False
    assert captured["include_embedding_control"] is False
    assert "embedding_no_clusters_no_control" in captured["run_id"]
    assert json.loads(capsys.readouterr().out)["windows_processed"] == 1


def test_run_modeling_backfill_single_window_batches_skip_embedding_preload(
    monkeypatch,
    capsys,
) -> None:
    preload_calls: list[dict[str, Any]] = []
    modeling_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(cli_module, "ConnectionPool", _FakePool)
    monkeypatch.setattr(cli_module, "_window_already_complete", lambda **_: False)
    monkeypatch.setattr(cli_module, "_mark_window_complete", lambda **_: None)
    monkeypatch.setattr(
        cli_module,
        "_load_aggregate_run_stats",
        lambda **kwargs: ModelingRunStats(run_id=kwargs["run_id"], rows_scored=2),
    )
    monkeypatch.setattr(cli_module, "_finalize_windowed_run", lambda **_: None)

    def fake_load_embedding_clip_rows(*_args: Any, **kwargs: Any) -> pd.DataFrame:
        preload_calls.append(kwargs)
        return pd.DataFrame()

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        modeling_calls.append(kwargs)
        return ModelingRunStats(run_id=kwargs["run_id"], rows_loaded=10, rows_scored=1)

    monkeypatch.setattr(
        cli_module,
        "_load_embedding_clip_rows",
        fake_load_embedding_clip_rows,
    )
    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(
        [
            "--windowed",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-14",
            "--window-days",
            "7",
            "--scoring-batch-windows",
            "1",
            "--no-derived-refresh",
        ]
    )

    assert exit_code == 0
    assert preload_calls == []
    assert len(modeling_calls) == 2
    assert all("preloaded_embedding_clip_rows" not in call for call in modeling_calls)
    assert json.loads(capsys.readouterr().out)["model_passes"] == 2


def test_run_modeling_backfill_bioacoustic_preset_sets_single_stream(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        captured.update(kwargs)
        return ModelingRunStats(run_id="bio-run", rows_loaded=2, rows_scored=1)

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(["--preset", "bioacoustic"])

    assert exit_code == 0
    assert captured["model_streams"] == ("bioacoustic",)
    assert captured["include_embedding_drift"] is True
    assert captured["include_embedding_control"] is True
    assert captured["include_sovon_context"] is True
    assert captured["skip_evaluation"] is False
    assert captured["include_environmental"] is False
    assert '"run_id": "bio-run"' in capsys.readouterr().out


def test_run_modeling_backfill_species_id_limits_modeling_species(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        captured.update(kwargs)
        return ModelingRunStats(run_id="species-run", rows_loaded=2, rows_scored=1)

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(
        ["--preset", "bioacoustic", "--species-id", "35", "--species-id", "61"]
    )

    assert exit_code == 0
    assert captured["target_species_only"] is True
    assert captured["species_ids"] == ("35", "61")
    assert captured["model_streams"] == ("bioacoustic",)
    assert '"run_id": "species-run"' in capsys.readouterr().out


def test_run_modeling_backfill_no_evaluation_skips_validation(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        captured.update(kwargs)
        return ModelingRunStats(run_id="scores-only-run", rows_loaded=2, rows_scored=1)

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )

    exit_code = cli_module.main(["--preset", "bioacoustic", "--no-evaluation"])

    assert exit_code == 0
    assert captured["model_streams"] == ("bioacoustic",)
    assert captured["include_sovon_context"] is False
    assert captured["skip_evaluation"] is True
    assert '"run_id": "scores-only-run"' in capsys.readouterr().out


def test_run_modeling_backfill_batches_pending_windows(monkeypatch, capsys) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(cli_module, "ConnectionPool", _FakePool)
    monkeypatch.setattr(cli_module, "_window_already_complete", lambda **_: False)

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        calls.append(("modeling", kwargs))
        return ModelingRunStats(
            run_id=kwargs["run_id"], rows_loaded=100, rows_scored=20
        )

    def fake_mark_window_complete(**kwargs: Any) -> None:
        calls.append(("mark_window", kwargs))

    def fake_finalize_windowed_run(**kwargs: Any) -> None:
        calls.append(("finalize", kwargs))

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )
    monkeypatch.setattr(
        cli_module,
        "_mark_window_complete",
        fake_mark_window_complete,
    )
    monkeypatch.setattr(
        cli_module,
        "_load_aggregate_run_stats",
        lambda **kwargs: ModelingRunStats(run_id=kwargs["run_id"], rows_scored=40),
    )
    monkeypatch.setattr(
        cli_module, "_finalize_windowed_run", fake_finalize_windowed_run
    )

    exit_code = cli_module.main(
        [
            "--windowed",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-28",
            "--window-days",
            "7",
            "--scoring-batch-windows",
            "2",
            "--no-derived-refresh",
            "--run-id",
            "aggregate-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    modeling_calls = [kwargs for name, kwargs in calls if name == "modeling"]
    mark_calls = [kwargs for name, kwargs in calls if name == "mark_window"]
    finalize_call = next(kwargs for name, kwargs in calls if name == "finalize")

    assert exit_code == 0
    assert payload["windows_total"] == 4
    assert payload["windows_processed"] == 4
    assert payload["windows_skipped"] == 0
    assert payload["model_passes"] == 2
    assert len(modeling_calls) == 2
    assert len(mark_calls) == 4
    assert modeling_calls[0]["source_start_date"] == date(2024, 1, 1)
    assert modeling_calls[0]["source_end_date"] == date(2024, 1, 8)
    assert modeling_calls[0]["output_period_start"] == date(2024, 1, 1)
    assert modeling_calls[0]["output_period_end"] == date(2024, 1, 8)
    assert modeling_calls[1]["source_start_date"] == date(2024, 1, 1)
    assert modeling_calls[1]["source_end_date"] == date(2024, 1, 22)
    assert modeling_calls[1]["output_period_start"] == date(2024, 1, 15)
    assert modeling_calls[1]["output_period_end"] == date(2024, 1, 22)
    assert finalize_call["model_passes"] == 2


def test_run_modeling_backfill_flushes_before_skipped_window(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(cli_module, "ConnectionPool", _FakePool)

    def fake_window_already_complete(**kwargs: Any) -> bool:
        return kwargs["start_date"] == date(2024, 1, 8)

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        calls.append(("modeling", kwargs))
        return ModelingRunStats(
            run_id=kwargs["run_id"], rows_loaded=100, rows_scored=10
        )

    monkeypatch.setattr(
        cli_module,
        "_window_already_complete",
        fake_window_already_complete,
    )
    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )
    monkeypatch.setattr(
        cli_module,
        "_mark_window_complete",
        lambda **kwargs: calls.append(("mark_window", kwargs)),
    )
    monkeypatch.setattr(
        cli_module,
        "_load_aggregate_run_stats",
        lambda **kwargs: ModelingRunStats(run_id=kwargs["run_id"], rows_scored=20),
    )
    monkeypatch.setattr(
        cli_module,
        "_finalize_windowed_run",
        lambda **kwargs: calls.append(("finalize", kwargs)),
    )

    exit_code = cli_module.main(
        [
            "--windowed",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-21",
            "--window-days",
            "7",
            "--scoring-batch-windows",
            "8",
            "--no-derived-refresh",
            "--run-id",
            "aggregate-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    modeling_calls = [kwargs for name, kwargs in calls if name == "modeling"]

    assert exit_code == 0
    assert payload["windows_processed"] == 2
    assert payload["windows_skipped"] == 1
    assert payload["model_passes"] == 2
    assert len(modeling_calls) == 2
    assert modeling_calls[0]["output_period_start"] == date(2024, 1, 1)
    assert modeling_calls[0]["output_period_end"] == date(2024, 1, 1)
    assert modeling_calls[1]["output_period_start"] == date(2024, 1, 15)
    assert modeling_calls[1]["output_period_end"] == date(2024, 1, 15)


def test_run_modeling_backfill_fast_mode_uses_one_lightweight_model_pass(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(cli_module, "ConnectionPool", _FakePool)
    monkeypatch.setattr(cli_module, "_window_already_complete", lambda **_: False)

    def fake_run_model_anomaly_scoring(**kwargs: Any) -> ModelingRunStats:
        calls.append(("modeling", kwargs))
        return ModelingRunStats(
            run_id=kwargs["run_id"], rows_loaded=100, rows_scored=40
        )

    monkeypatch.setattr(
        cli_module,
        "run_model_anomaly_scoring",
        fake_run_model_anomaly_scoring,
    )
    monkeypatch.setattr(
        cli_module,
        "_mark_window_complete",
        lambda **kwargs: calls.append(("mark_window", kwargs)),
    )
    monkeypatch.setattr(
        cli_module,
        "_load_aggregate_run_stats",
        lambda **kwargs: ModelingRunStats(run_id=kwargs["run_id"], rows_scored=40),
    )
    monkeypatch.setattr(
        cli_module,
        "_finalize_windowed_run",
        lambda **kwargs: calls.append(("finalize", kwargs)),
    )

    exit_code = cli_module.main(
        [
            "--windowed",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2024-01-28",
            "--window-days",
            "7",
            "--fast",
            "--no-derived-refresh",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    modeling_calls = [kwargs for name, kwargs in calls if name == "modeling"]
    finalize_call = next(kwargs for name, kwargs in calls if name == "finalize")

    assert exit_code == 0
    assert payload["windows_total"] == 4
    assert payload["windows_processed"] == 4
    assert payload["model_passes"] == 1
    assert len(modeling_calls) == 1
    assert modeling_calls[0]["include_embedding_drift"] is False
    assert modeling_calls[0]["include_embedding_clusters"] is False
    assert modeling_calls[0]["include_embedding_control"] is False
    assert modeling_calls[0]["include_sovon_context"] is False
    assert modeling_calls[0]["skip_evaluation"] is True
    assert modeling_calls[0]["output_period_start"] == date(2024, 1, 1)
    assert modeling_calls[0]["output_period_end"] == date(2024, 1, 22)
    assert "no_embedding" in modeling_calls[0]["run_id"]
    assert finalize_call["config"]["fast"] is True
    assert finalize_call["config"]["include_sovon_context"] is False
    assert finalize_call["config"]["skip_evaluation"] is True
    assert finalize_call["config"]["scoring_batch_windows"] == 4
