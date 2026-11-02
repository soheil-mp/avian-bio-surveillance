"""Tests for the legacy modeling artifact importer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORTER_PATH = REPO_ROOT / "scripts" / "ops" / "import_modeling_artifacts.py"


def _legacy_csv_name(stem: str) -> str:
    return f"{stem}_latest.csv"


def _load_importer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "import_modeling_artifacts",
        IMPORTER_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_artifacts_persists_score_and_tuning_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    importer = _load_importer()
    modeling_dir = tmp_path / "modeling"
    modeling_dir.mkdir()

    (modeling_dir / "species_week_anomaly_scores_latest.json").write_text(
        json.dumps(
            {
                "run_id": "legacy-run",
                "generated_at": "2026-03-09T00:00:00Z",
                "config": {"min_observation_days": 3},
                "stats": {"rows_scored": 1},
                "source_coverage": {"scored": {"row_count": 1}},
            }
        ),
        encoding="utf-8",
    )
    (modeling_dir / "species_week_validation_report_latest.json").write_text(
        json.dumps({"validation": {"rows_scored": 1}}),
        encoding="utf-8",
    )
    pd.DataFrame(
        [{"species_id": "35", "period_start": "2026-03-02", "total_detections": 7}]
    ).to_csv(modeling_dir / _legacy_csv_name("species_week_features"), index=False)
    pd.DataFrame(
        [
            {
                "species_id": "35",
                "period_start": "2026-03-02",
                "final_score": 0.9,
                "top_detection_ids": "det-1",
            }
        ]
    ).to_csv(
        modeling_dir / _legacy_csv_name("species_week_anomaly_scores"),
        index=False,
    )
    pd.DataFrame(
        [{"detection_id": "det-1", "period_start": "2026-03-02", "cluster_id": "c1"}]
    ).to_csv(
        modeling_dir / _legacy_csv_name("species_week_embedding_clusters"),
        index=False,
    )
    pd.DataFrame(
        [
            {
                "species_id": "61",
                "period_start": "2026-03-09",
                "total_detections": 3,
            }
        ]
    ).to_csv(modeling_dir / "species_week_features_legacy-run-2.csv", index=False)
    pd.DataFrame(
        [
            {
                "species_id": "61",
                "period_start": "2026-03-09",
                "final_score": 0.7,
                "top_detection_ids": "det-2",
            }
        ]
    ).to_csv(
        modeling_dir / "species_week_anomaly_scores_legacy-run-2.csv",
        index=False,
    )

    (modeling_dir / "model_tuning_leaderboard_latest.json").write_text(
        json.dumps(
            {
                "run_id": "legacy-tuning",
                "generated_at": "2026-03-09T00:00:00Z",
                "mode": "axis",
                "objective_weights": {"sovon_mortality_auroc": 1.0},
                "negative_control_penalty": 0.5,
                "top_k": 25,
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "rank": 1,
                "config_index": 0,
                "config_label": "baseline",
                "objective": 0.1,
                "overrides": "{}",
            }
        ]
    ).to_csv(modeling_dir / "model_tuning_leaderboard_latest.csv", index=False)

    persisted_runs: list[dict[str, Any]] = []
    persisted_tuning: list[dict[str, Any]] = []

    monkeypatch.setattr(
        importer,
        "persist_modeling_run",
        lambda **kwargs: persisted_runs.append(kwargs),
    )
    monkeypatch.setattr(
        importer,
        "persist_tuning_leaderboard",
        lambda **kwargs: persisted_tuning.append(kwargs),
    )

    counts = importer.import_artifacts(
        modeling_dir=modeling_dir,
        pool=object(),
    )

    assert counts == {
        "score_runs": 2,
        "tuning_runs": 1,
    }
    score_run = next(run for run in persisted_runs if run["run_kind"] == "anomaly_scoring")
    assert score_run["run_id"] == "legacy-run"
    assert len(score_run["features"]) == 1
    assert len(score_run["scores"]) == 1
    assert len(score_run["clusters"]) == 1
    assert score_run["validation_report"] == {"validation": {"rows_scored": 1}}
    stamped_run = next(
        run for run in persisted_runs if run["run_id"] == "legacy-run-2"
    )
    assert len(stamped_run["features"]) == 1
    assert len(stamped_run["scores"]) == 1

    assert len(persisted_tuning) == 1
    assert persisted_tuning[0]["tuning_run_id"] == "legacy-tuning"
    assert len(persisted_tuning[0]["leaderboard"]) == 1
