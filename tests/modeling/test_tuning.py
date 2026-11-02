"""Tests for the anomaly-model tuning sweep harness."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from avian_biosurveillance.modeling import tuning
from avian_biosurveillance.modeling.tuning import (
    TuningSweepConfig,
    expand_grid,
    load_tuning_config,
    run_sweep,
    score_objective,
)


def _axis_config(**overrides: Any) -> TuningSweepConfig:
    base: dict[str, Any] = {
        "grid": {
            "seasonal_window_weeks": [2, 3, 4],
            "include_embedding_control": [True, False],
        },
        "objective_weights": {
            "sovon_mortality_auroc": 0.35,
            "sovon_mortality_ap": 0.15,
            "mean_weak_label_auroc": 0.3,
            "best_lead_lag_abs_spearman": 0.2,
        },
        "negative_control_penalty": 0.5,
        "max_configs": 24,
        "mode": "axis",
        "top_k": 25,
    }
    base.update(overrides)
    return TuningSweepConfig(**base)


def _report(
    *,
    sovon_auroc: float | None,
    sovon_ap: float | None = 0.4,
    weak_aurocs: tuple[float, ...] = (0.6, 0.7),
    best_rho: float | None = 0.5,
    non_target_aurocs: tuple[float, ...] = (0.5,),
) -> dict[str, Any]:
    """Build a minimal validation report exposing the keys the objective reads."""
    return {
        "rows_scored": 100,
        "weak_label_metrics": {
            f"label_{i}": {"auroc": value} for i, value in enumerate(weak_aurocs)
        },
        "monthly_alignment": {
            "weak_label_metrics": {
                "sovon_same_month_high_mortality": {
                    "auroc": sovon_auroc,
                    "average_precision": sovon_ap,
                }
            },
            "lead_lag_alignment": {
                "best_spearman_rho": best_rho,
                "best_spearman_lag_months": 1,
            },
        },
        "non_target_species_control": {
            "non_target_rows": 50,
            "per_weak_label": {
                f"label_{i}": {"auroc": value}
                for i, value in enumerate(non_target_aurocs)
            },
        },
    }


def test_expand_grid_axis_mode_is_one_factor_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin baseline so the dedup of baseline-equal values is deterministic.
    baselines = {"seasonal_window_weeks": 2, "include_embedding_control": True}
    monkeypatch.setitem(tuning._KNOB_BASELINE, "seasonal_window_weeks", lambda: 2)
    monkeypatch.setitem(
        tuning._KNOB_BASELINE, "include_embedding_control", lambda: True
    )

    configs = expand_grid(_axis_config())

    # baseline ({}) + 2 non-baseline seasonal values + 1 non-baseline control value.
    assert configs[0] == {}
    assert {"seasonal_window_weeks": 3} in configs
    assert {"seasonal_window_weeks": 4} in configs
    assert {"include_embedding_control": False} in configs
    # baseline values are not re-emitted as standalone configs.
    assert {"seasonal_window_weeks": 2} not in configs
    assert {"include_embedding_control": True} not in configs
    assert len(configs) == 4
    assert all(k in baselines for c in configs for k in c)


def test_expand_grid_product_mode_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(tuning._KNOB_BASELINE, "seasonal_window_weeks", lambda: 2)
    monkeypatch.setitem(
        tuning._KNOB_BASELINE, "include_embedding_control", lambda: True
    )
    config = _axis_config(mode="product", max_configs=3)

    configs = expand_grid(config)

    assert len(configs) == 3  # capped
    # Determinism: same call yields identical ordering.
    assert expand_grid(config) == configs


def test_score_objective_rewards_alignment_and_penalises_control() -> None:
    weights = {
        "sovon_mortality_auroc": 0.35,
        "sovon_mortality_ap": 0.15,
        "mean_weak_label_auroc": 0.3,
        "best_lead_lag_abs_spearman": 0.2,
    }
    strong = score_objective(
        _report(sovon_auroc=0.9, weak_aurocs=(0.8, 0.85), best_rho=0.6),
        weights=weights,
        negative_control_penalty=0.5,
    )
    weak = score_objective(
        _report(sovon_auroc=0.55, weak_aurocs=(0.52, 0.5), best_rho=0.05),
        weights=weights,
        negative_control_penalty=0.5,
    )
    assert strong["objective"] > weak["objective"]
    # Random ranking (AUROC 0.5) contributes no skill.
    assert strong["components"]["sovon_mortality_auroc_skill"] == pytest.approx(0.8)

    leaky = score_objective(
        _report(sovon_auroc=0.9, non_target_aurocs=(0.9, 0.95)),
        weights=weights,
        negative_control_penalty=0.5,
    )
    clean = score_objective(
        _report(sovon_auroc=0.9, non_target_aurocs=(0.5, 0.5)),
        weights=weights,
        negative_control_penalty=0.5,
    )
    assert clean["objective"] > leaky["objective"]
    assert leaky["negative_control_penalty_term"] > 0.0


def test_score_objective_handles_missing_metrics() -> None:
    result = score_objective(
        {"rows_scored": 0},
        weights={"sovon_mortality_auroc": 1.0},
        negative_control_penalty=1.0,
    )
    assert result["objective"] == pytest.approx(0.0)
    assert result["metrics"]["sovon_mortality_auroc"] is None


def test_run_sweep_ranks_and_persists_leaderboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(tuning._KNOB_BASELINE, "seasonal_window_weeks", lambda: 2)
    monkeypatch.setitem(
        tuning._KNOB_BASELINE, "include_embedding_control", lambda: True
    )
    config = _axis_config()

    # Make one specific config clearly the best so ranking is testable.
    def fake_run(overrides: Mapping[str, Any]) -> Mapping[str, Any]:
        if overrides.get("seasonal_window_weeks") == 3:
            return _report(sovon_auroc=0.95, weak_aurocs=(0.9, 0.9), best_rho=0.7)
        return _report(sovon_auroc=0.55, weak_aurocs=(0.5, 0.5), best_rho=0.1)

    persisted: dict[str, Any] = {}

    def fake_persist_tuning_leaderboard(**kwargs: Any) -> None:
        persisted.update(kwargs)

    monkeypatch.setattr(
        tuning,
        "persist_tuning_leaderboard",
        fake_persist_tuning_leaderboard,
    )

    leaderboard = run_sweep(config, run_config_fn=fake_run)

    assert list(leaderboard["rank"]) == sorted(leaderboard["rank"])
    assert leaderboard.iloc[0]["rank"] == 1
    assert leaderboard.iloc[0]["knob_seasonal_window_weeks"] == 3
    assert leaderboard["objective"].is_monotonic_decreasing
    assert persisted["mode"] == "axis"
    pd_leaderboard = persisted["leaderboard"]
    assert pd_leaderboard.equals(leaderboard)
    payload = persisted["payload"]
    assert payload["config_count"] == len(leaderboard)
    assert payload["leaderboard"][0]["knob_seasonal_window_weeks"] == 3


def test_load_tuning_config_rejects_unknown_knob(tmp_path: Path) -> None:
    path = tmp_path / "modeling_tuning.json"
    path.write_text(json.dumps({"grid": {"not_a_real_knob": [1, 2]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown tuning knob"):
        load_tuning_config(path)


def test_load_tuning_config_reads_repo_default() -> None:
    config = load_tuning_config()
    assert config.grid
    assert set(config.objective_weights) == {
        "sovon_mortality_auroc",
        "sovon_mortality_ap",
        "mean_weak_label_auroc",
        "best_lead_lag_abs_spearman",
    }
    assert config.mode in {"axis", "product"}
