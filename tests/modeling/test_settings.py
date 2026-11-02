"""Tests for modeling settings loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from avian_biosurveillance.modeling.settings import load_modeling_settings


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_modeling_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _lean_config() -> dict[str, Any]:
    return {
        "feature_set": {
            "target_species_only": True,
            "seasonal_window_weeks": 2,
            "min_observation_days": 3,
            "min_location_history": 20,
            "min_seasonal_history": 8,
        },
        "embedding_drift": {
            "include_embedding_drift": True,
            "min_confidence": 0.1,
            "min_baseline_clips": 50,
            "min_week_clips": 3,
            "k_neighbors": 10,
            "top_clip_count": 5,
            "cluster_min_samples": 2,
            "cluster_distance_multiplier": 1.2,
            "cluster_distance_floor": 0.03,
            "cluster_distance_ceiling": 0.18,
            "background_summary_top_k": 3,
        },
        "anomaly_model": {
            "min_model_rows": 20,
            "contamination": 0.05,
            "random_state": 42,
            "amber_quantile": 0.95,
            "red_quantile": 0.99,
        },
    }


def test_load_modeling_settings_loads_checked_in_config() -> None:
    config_path = _project_root() / "config" / "modeling.json"

    settings = load_modeling_settings(config_path)
    payload = _read_modeling_json(config_path)

    assert settings.source_path == config_path
    assert (
        settings.feature_set["min_observation_days"]
        == payload["feature_set"]["min_observation_days"]
    )
    assert settings.anomaly_model["red_quantile"] == pytest.approx(
        payload["anomaly_model"]["red_quantile"]
    )


def test_load_modeling_settings_loads_from_source_checkout_when_cwd_has_no_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_modeling_settings()

    assert settings.source_path is not None
    assert settings.source_path.name == "modeling.json"


def test_load_modeling_settings_uses_explicit_file(tmp_path: Path) -> None:
    config_path = tmp_path / "modeling.json"
    config_path.write_text(json.dumps(_lean_config()), encoding="utf-8")

    settings = load_modeling_settings(config_path)

    assert settings.source_path == config_path
    assert settings.feature_set["min_location_history"] == 20
    assert settings.anomaly_model["contamination"] == pytest.approx(0.05)


def test_load_modeling_settings_ignores_underscore_comment_keys(
    tmp_path: Path,
) -> None:
    config = _lean_config()
    config["_comment"] = "annotation only"
    config["anomaly_model"]["_comment"] = "ignored"
    config_path = tmp_path / "modeling.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    settings = load_modeling_settings(config_path)

    assert "_comment" not in settings.anomaly_model
    assert settings.anomaly_model["amber_quantile"] == pytest.approx(0.95)


def test_load_modeling_settings_rejects_removed_sections(
    tmp_path: Path,
) -> None:
    config = _lean_config()
    config["fusion"] = {"amber_alert_threshold": 0.95}
    config_path = tmp_path / "modeling.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="removed section"):
        load_modeling_settings(config_path)


def test_load_modeling_settings_raises_when_explicit_file_missing_and_no_other_config_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError):
        load_modeling_settings(tmp_path / "missing.json")


def test_load_modeling_settings_raises_when_required_section_missing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "modeling.json"
    config_path.write_text(
        json.dumps({"feature_set": {"min_observation_days": 3}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing object section"):
        load_modeling_settings(config_path)


def test_load_modeling_settings_returns_independent_dicts() -> None:
    first = load_modeling_settings(_project_root() / "config" / "modeling.json")
    first.feature_set["min_observation_days"] = 7
    second = load_modeling_settings(_project_root() / "config" / "modeling.json")

    assert second.feature_set["min_observation_days"] != 7
