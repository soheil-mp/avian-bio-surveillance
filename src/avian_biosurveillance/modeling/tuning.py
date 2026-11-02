"""Tuning and model-selection sweep for the unsupervised anomaly model.

This module reruns :func:`run_model_anomaly_scoring` across a small, principled
grid of *unsupervised* configuration knobs and ranks the resulting configurations
by how well the produced anomaly scores align with weak biological context
signals (SOVON nationwide bird mortality).

Integrity rule
--------------
The IsolationForest model is **never** trained on these weak labels. They are used
strictly as a model-*selection* criterion: we keep the configuration whose
unsupervised scores happen to rank known mortality context highest, while
a non-target-species negative control guards against configurations that merely
inflate alignment everywhere (a sign of confounding rather than signal).

The sweep is deterministic, idempotent, and on-demand. It persists a tuning
leaderboard in Postgres alongside the candidate modeling runs.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

import pandas as pd

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.modeling import (
    load_modeling_run,
    persist_tuning_leaderboard,
)
from avian_biosurveillance.modeling.pipeline import run_model_anomaly_scoring
from avian_biosurveillance.modeling.settings import SETTINGS


logger = logging.getLogger(__name__)

DEFAULT_TUNING_CONFIG_FILENAME = "modeling_tuning.json"

# Knobs that the sweep is allowed to vary. Each name must be an accepted
# keyword argument of ``run_model_anomaly_scoring``.
ALLOWED_GRID_KNOBS: frozenset[str] = frozenset(
    {
        "seasonal_window_weeks",
        "min_location_history",
        "min_seasonal_history",
        "min_observation_days",
        "min_station_availability_ratio",
        "min_station_baseline_history",
        "target_species_only",
        "include_embedding_drift",
        "include_embedding_control",
        "embedding_min_control_species",
        "embedding_min_confidence",
        "embedding_min_baseline_clips",
        "embedding_min_week_clips",
        "embedding_k_neighbors",
        "anomaly_min_model_rows",
        "anomaly_contamination",
        "amber_quantile",
        "red_quantile",
    }
)

# Maps each tunable knob to the live baseline value used as the centre point of
# axis (one-factor-at-a-time) sweeps and for de-duplicating configs.
_KNOB_BASELINE: dict[str, Callable[[], Any]] = {
    "seasonal_window_weeks": lambda: SETTINGS.feature_set["seasonal_window_weeks"],
    "min_location_history": lambda: SETTINGS.feature_set["min_location_history"],
    "min_seasonal_history": lambda: SETTINGS.feature_set["min_seasonal_history"],
    "min_observation_days": lambda: SETTINGS.feature_set["min_observation_days"],
    "min_station_availability_ratio": lambda: float(
        SETTINGS.feature_set.get("min_station_availability_ratio", 0.5)
    ),
    "min_station_baseline_history": lambda: int(
        SETTINGS.feature_set.get("min_station_baseline_history", 4)
    ),
    "target_species_only": lambda: SETTINGS.feature_set["target_species_only"],
    "include_embedding_drift": lambda: SETTINGS.embedding_drift[
        "include_embedding_drift"
    ],
    "include_embedding_control": lambda: SETTINGS.embedding_drift.get(
        "include_embedding_control", True
    ),
    "embedding_min_control_species": lambda: SETTINGS.embedding_drift.get(
        "min_control_species", 1
    ),
    "embedding_min_confidence": lambda: SETTINGS.embedding_drift["min_confidence"],
    "embedding_min_baseline_clips": lambda: SETTINGS.embedding_drift[
        "min_baseline_clips"
    ],
    "embedding_min_week_clips": lambda: SETTINGS.embedding_drift["min_week_clips"],
    "embedding_k_neighbors": lambda: SETTINGS.embedding_drift["k_neighbors"],
    "anomaly_min_model_rows": lambda: SETTINGS.anomaly_model["min_model_rows"],
    "anomaly_contamination": lambda: SETTINGS.anomaly_model["contamination"],
    "amber_quantile": lambda: SETTINGS.anomaly_model["amber_quantile"],
    "red_quantile": lambda: SETTINGS.anomaly_model["red_quantile"],
}

_OBJECTIVE_WEIGHT_KEYS = (
    "sovon_mortality_auroc",
    "sovon_mortality_ap",
    "mean_weak_label_auroc",
    "best_lead_lag_abs_spearman",
)

_SOVON_MONTHLY_LABEL = "sovon_same_month_high_mortality"


@dataclass(frozen=True, slots=True)
class TuningSweepConfig:
    """Validated configuration for a tuning sweep."""

    grid: dict[str, list[Any]]
    objective_weights: dict[str, float]
    negative_control_penalty: float
    max_configs: int
    mode: str
    top_k: int


def _resolve_config_path(config_path: str | Path | None) -> Path:
    """Locate the tuning config, preferring the working-directory ``config/``."""
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(f"Tuning config not found: {path}")
        return path

    candidates = (
        Path.cwd() / "config" / DEFAULT_TUNING_CONFIG_FILENAME,
        Path(__file__).resolve().parents[3] / "config" / DEFAULT_TUNING_CONFIG_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate config/"
        f"{DEFAULT_TUNING_CONFIG_FILENAME}. Searched: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def load_tuning_config(config_path: str | Path | None = None) -> TuningSweepConfig:
    """Load and validate the tuning sweep configuration from JSON."""
    path = _resolve_config_path(config_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    grid_raw = payload.get("grid", {})
    if not isinstance(grid_raw, Mapping) or not grid_raw:
        raise ValueError("Tuning config 'grid' must be a non-empty object.")

    unknown = set(grid_raw) - ALLOWED_GRID_KNOBS
    if unknown:
        raise ValueError(
            "Unknown tuning knob(s): "
            + ", ".join(sorted(unknown))
            + ". Allowed knobs: "
            + ", ".join(sorted(ALLOWED_GRID_KNOBS))
        )

    grid: dict[str, list[Any]] = {}
    for knob, values in grid_raw.items():
        if not isinstance(values, Sequence) or isinstance(values, str) or not values:
            raise ValueError(f"Grid values for '{knob}' must be a non-empty list.")
        grid[knob] = list(values)

    mode = str(payload.get("mode", "axis")).lower()
    if mode not in {"axis", "product"}:
        raise ValueError("Tuning config 'mode' must be 'axis' or 'product'.")

    weights_raw = payload.get("objective_weights", {})
    if not isinstance(weights_raw, Mapping):
        raise ValueError("Tuning config 'objective_weights' must be an object.")
    objective_weights = {
        key: float(weights_raw.get(key, 0.0)) for key in _OBJECTIVE_WEIGHT_KEYS
    }

    return TuningSweepConfig(
        grid=grid,
        objective_weights=objective_weights,
        negative_control_penalty=float(payload.get("negative_control_penalty", 0.0)),
        max_configs=int(payload.get("max_configs", 24)),
        mode=mode,
        top_k=int(payload.get("top_k", 25)),
    )


def _baseline_value(knob: str) -> Any:
    return _KNOB_BASELINE[knob]()


def expand_grid(config: TuningSweepConfig) -> list[dict[str, Any]]:
    """Expand the grid into a deterministic, de-duplicated list of overrides.

    The empty dict ``{}`` represents the live baseline configuration. In ``axis``
    mode each non-baseline grid value is applied on its own (one factor at a
    time); in ``product`` mode the full Cartesian product is enumerated. Configs
    that resolve to the baseline are collapsed, and the result is capped at
    ``max_configs`` (the baseline always survives the cap).
    """
    baseline_full = {knob: _baseline_value(knob) for knob in config.grid}

    raw_configs: list[dict[str, Any]] = [{}]
    if config.mode == "axis":
        for knob, values in config.grid.items():
            for value in values:
                if value != baseline_full[knob]:
                    raw_configs.append({knob: value})
    else:  # product
        knobs = list(config.grid)
        for combo in itertools.product(*(config.grid[knob] for knob in knobs)):
            raw_configs.append(dict(zip(knobs, combo, strict=True)))

    seen: set[tuple[tuple[str, Any], ...]] = set()
    deduped: list[dict[str, Any]] = []
    for overrides in raw_configs:
        merged = {**baseline_full, **overrides}
        signature = tuple(sorted((knob, merged[knob]) for knob in baseline_full))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(overrides)

    if config.max_configs > 0:
        deduped = deduped[: config.max_configs]
    return deduped


def _auroc_skill(auroc: float | None) -> float:
    """Map AUROC to above-random skill in ``[0, 1]`` (0.5 -> 0, 1.0 -> 1)."""
    if auroc is None:
        return 0.0
    return max(0.0, (float(auroc) - 0.5) * 2.0)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # drop NaN


def score_objective(
    report: Mapping[str, Any],
    *,
    weights: Mapping[str, float],
    negative_control_penalty: float,
) -> dict[str, Any]:
    """Score a validation report into a single selection objective.

    Higher is better. The objective rewards above-random ranking of weak labels
    (SOVON nationwide mortality as the primary north star, then the mean across
    all weak labels, then the best monthly lead-lag rank correlation) and
    penalises alignment on non-target species, which would indicate confounding
    rather than species-specific signal.
    """
    monthly = report.get("monthly_alignment", {}) or {}
    sovon = (monthly.get("weak_label_metrics", {}) or {}).get(
        _SOVON_MONTHLY_LABEL, {}
    ) or {}
    sovon_auroc = _as_float(sovon.get("auroc"))
    sovon_ap = _as_float(sovon.get("average_precision"))

    weak_metrics = report.get("weak_label_metrics", {}) or {}
    weak_aurocs = [
        value
        for value in (_as_float(m.get("auroc")) for m in weak_metrics.values())
        if value is not None
    ]
    mean_weak_auroc = fmean(weak_aurocs) if weak_aurocs else None

    lead_lag = monthly.get("lead_lag_alignment", {}) or {}
    best_rho = _as_float(lead_lag.get("best_spearman_rho"))
    best_abs_rho = abs(best_rho) if best_rho is not None else None

    control = report.get("non_target_species_control", {}) or {}
    control_deviations = [
        abs(value - 0.5) * 2.0
        for value in (
            _as_float(m.get("auroc"))
            for m in (control.get("per_weak_label", {}) or {}).values()
        )
        if value is not None
    ]
    control_penalty_term = fmean(control_deviations) if control_deviations else 0.0

    components = {
        "sovon_mortality_auroc_skill": _auroc_skill(sovon_auroc),
        "sovon_mortality_ap": sovon_ap or 0.0,
        "mean_weak_label_auroc_skill": _auroc_skill(mean_weak_auroc),
        "best_lead_lag_abs_spearman": best_abs_rho or 0.0,
    }
    objective = (
        weights.get("sovon_mortality_auroc", 0.0)
        * components["sovon_mortality_auroc_skill"]
        + weights.get("sovon_mortality_ap", 0.0) * components["sovon_mortality_ap"]
        + weights.get("mean_weak_label_auroc", 0.0)
        * components["mean_weak_label_auroc_skill"]
        + weights.get("best_lead_lag_abs_spearman", 0.0)
        * components["best_lead_lag_abs_spearman"]
        - negative_control_penalty * control_penalty_term
    )

    return {
        "objective": objective,
        "components": components,
        "negative_control_penalty_term": control_penalty_term,
        "metrics": {
            "sovon_mortality_auroc": sovon_auroc,
            "sovon_mortality_ap": sovon_ap,
            "mean_weak_label_auroc": mean_weak_auroc,
            "best_lead_lag_abs_spearman": best_abs_rho,
            "best_lead_lag_lag_months": lead_lag.get("best_spearman_lag_months"),
            "rows_scored": report.get("rows_scored"),
            "non_target_rows": control.get("non_target_rows", 0),
        },
    }


def _config_label(overrides: Mapping[str, Any]) -> str:
    if not overrides:
        return "baseline"
    return "cfg_" + "_".join(f"{knob}-{overrides[knob]}" for knob in sorted(overrides))


def _default_run_config(
    overrides: Mapping[str, Any],
    *,
    pool: ConnectionPool | None,
    tuning_run_id: str,
) -> dict[str, Any]:
    """Run the full pipeline for one config and return its validation report."""
    stats = run_model_anomaly_scoring(
        pool=pool,
        parent_tuning_run_id=tuning_run_id,
        **overrides,
    )
    payload = load_modeling_run(run_id=stats.run_id, pool=pool)
    if payload is None:
        raise ValueError(f"Persisted modeling run {stats.run_id} was not found.")
    report = payload.get("validation_report") or {}
    if not isinstance(report, dict):
        raise ValueError(
            f"Persisted modeling run {stats.run_id} has no validation report."
        )
    validation = report.get("validation", {})
    if not isinstance(validation, dict):
        raise ValueError(
            f"Persisted modeling run {stats.run_id} is missing a validation object."
        )
    return validation


def run_sweep(
    config: TuningSweepConfig | None = None,
    *,
    pool: ConnectionPool | None = None,
    run_config_fn: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> pd.DataFrame:
    """Run the tuning sweep and persist a ranked leaderboard.

    Parameters
    ----------
    config:
        Sweep configuration. Loaded from ``config/modeling_tuning.json`` when omitted.
    pool:
        Optional shared connection pool reused across all pipeline runs.
    run_config_fn:
        Injectable callable mapping an overrides dict to a validation report dict.
        Defaults to running the full pipeline. Override in tests to avoid DB access.

    Returns
    -------
    pandas.DataFrame
        The leaderboard, ranked by objective (best first).
    """
    config = config or load_tuning_config()
    tuning_run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    if run_config_fn is None:

        def _pipeline_run_config(overrides: Mapping[str, Any]) -> Mapping[str, Any]:
            return _default_run_config(
                overrides,
                pool=pool,
                tuning_run_id=tuning_run_id,
            )

        run_config_fn = _pipeline_run_config

    overrides_list = expand_grid(config)
    baseline_full = {knob: _baseline_value(knob) for knob in config.grid}
    logger.info("Evaluating %d tuning configuration(s).", len(overrides_list))

    records: list[dict[str, Any]] = []
    for index, overrides in enumerate(overrides_list):
        label = _config_label(overrides)
        logger.info("Tuning config %d/%d: %s", index + 1, len(overrides_list), label)
        report = run_config_fn(overrides)
        scored = score_objective(
            report,
            weights=config.objective_weights,
            negative_control_penalty=config.negative_control_penalty,
        )
        resolved = {**baseline_full, **overrides}
        record: dict[str, Any] = {
            "config_index": index,
            "config_label": label,
            "overrides": json.dumps(overrides, sort_keys=True),
            "objective": scored["objective"],
            "negative_control_penalty_term": scored["negative_control_penalty_term"],
        }
        record.update({f"knob_{knob}": resolved[knob] for knob in baseline_full})
        record.update(
            {f"component_{key}": val for key, val in scored["components"].items()}
        )
        record.update({f"metric_{key}": val for key, val in scored["metrics"].items()})
        records.append(record)

    leaderboard = pd.DataFrame.from_records(records)
    if not leaderboard.empty:
        leaderboard = leaderboard.sort_values(
            "objective", ascending=False, kind="stable"
        ).reset_index(drop=True)
        leaderboard.insert(0, "rank", range(1, len(leaderboard) + 1))

    _persist_leaderboard(
        leaderboard,
        tuning_run_id=tuning_run_id,
        config=config,
        pool=pool,
    )
    return leaderboard


def _persist_leaderboard(
    leaderboard: pd.DataFrame,
    *,
    tuning_run_id: str,
    config: TuningSweepConfig,
    pool: ConnectionPool | None,
) -> None:
    generated_at = datetime.now(UTC)
    payload = {
        "run_id": tuning_run_id,
        "generated_at": generated_at.isoformat(),
        "mode": config.mode,
        "objective_weights": config.objective_weights,
        "negative_control_penalty": config.negative_control_penalty,
        "top_k": config.top_k,
        "config_count": len(leaderboard),
        "leaderboard": leaderboard.to_dict(orient="records"),
    }
    persist_tuning_leaderboard(
        tuning_run_id=tuning_run_id,
        generated_at=generated_at,
        mode=config.mode,
        objective_weights=config.objective_weights,
        negative_control_penalty=config.negative_control_penalty,
        top_k=config.top_k,
        leaderboard=leaderboard,
        payload=payload,
        pool=pool,
    )
    logger.info(
        "Persisted tuning leaderboard (%d rows) to Postgres run %s.",
        len(leaderboard),
        tuning_run_id,
    )


def main() -> None:
    """CLI entry point: run the sweep using the default configuration."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    leaderboard = run_sweep()
    if leaderboard.empty:
        logger.warning("Tuning sweep produced no configurations.")
        return
    top = leaderboard.head(5)
    logger.info("Top configurations by objective:\n%s", top.to_string(index=False))


if __name__ == "__main__":
    main()
