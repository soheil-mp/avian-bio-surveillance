"""Higher-level unsupervised anomaly scoring over bird-behavior streams."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.ensemble import IsolationForest


# M2: ``population_anomaly_score`` is already ``max(drop, spike)`` so feeding the
# drop/spike components as well over-weighted the population stream. The model
# uses the aggregate plus the standardized residual magnitude only.
# B1: ``embedding_drift_excess_score`` is the target drift in excess of the
# co-located non-target acoustic-scene drift, so shared scene shifts (weather,
# new noise sources) do not inflate the behavioral signal.
#
_POPULATION_FEATURE_COLUMNS = [
    "population_anomaly_score",
    "population_residual_abs",
]

_BIOACOUSTIC_FEATURE_COLUMNS = [
    "embedding_drift_excess_score",
    "embedding_outlier_share",
    "embedding_available",
    "embedding_coverage_score",
]

# These core columns are the default detector spine: the population-activity
# stream plus the bioacoustic (embedding-drift) stream.
_CORE_FEATURE_COLUMNS = [*_POPULATION_FEATURE_COLUMNS, *_BIOACOUSTIC_FEATURE_COLUMNS]
_VALID_MODEL_STREAMS = frozenset({"population", "bioacoustic"})

# Curated environmental context fed to the fusion matrix when enabled. This is a
# deliberately small, low-collinearity subset of the full environmental lag set
# (which is computed and attached upstream for context/evaluation). Keeping it
# small protects the acoustic+population spine from being numerically swamped by
# a large block of correlated weather columns in the IsolationForest, while
# still letting environmental conditions modulate the ranking. ``current`` plus
# a single medium (3-week) lag per Tier-1 driver, plus two drought indices.
_ENVIRONMENTAL_MODEL_COLUMNS = [
    "env_temp_mean_c_lag_0w",
    "env_temp_mean_c_lag_3w",
    "env_precip_mm_lag_3w",
    "env_soil_moisture_lag_3w",
    "env_ndvi_lag_3w",
    "env_spi_3m_lag_3w",
    "env_spei_3m_lag_3w",
]

# Retained for backwards compatibility / external references. The actual columns
# used in a run are reported on the scored frame as ``model_feature_columns``.
MODEL_FEATURE_COLUMNS = _CORE_FEATURE_COLUMNS


@dataclass(frozen=True, slots=True)
class BehaviorAnomalyModelConfig:
    """Configuration for deterministic higher-level anomaly scoring.

    ``contamination`` is forwarded to :class:`IsolationForest` but does not
    affect the emitted ranking: scoring uses ``-score_samples`` re-expressed as
    a uniform percentile rank, which is invariant to the contamination prior
    (that prior only shifts the unused ``predict``/``offset_`` decision
    boundary). Alert levels are therefore pure queue ranks selected by
    ``amber_quantile``/``red_quantile`` over the eligible rows of each run.

    ``include_environmental`` adds the curated environmental context columns to
    the matrix. It defaults to ``False`` so the detector stays acoustic+
    population-led; enable it only once the environmental sources populate the
    modeling window.
    """

    min_model_rows: int = 20
    contamination: float = 0.05
    random_state: int = 42
    amber_quantile: float = 0.95
    red_quantile: float = 0.99
    include_environmental: bool = False
    model_streams: tuple[str, ...] = ("population", "bioacoustic")


def score_behavior_anomalies(
    rows: pd.DataFrame,
    *,
    config: BehaviorAnomalyModelConfig | None = None,
) -> pd.DataFrame:
    """Score behavior anomalies without using mortality context."""
    config = config or BehaviorAnomalyModelConfig()
    if rows.empty:
        return rows

    frame = rows.copy()
    model_streams = _normalise_model_streams(config.model_streams)
    features = _model_features(
        frame,
        include_environmental=config.include_environmental,
        model_streams=model_streams,
    )
    feature_columns = list(features.columns)
    # A row can only be an anomaly if it carries a bird-derived signal (acoustic
    # drift or a population residual). Effort-eligible but signal-empty rows would
    # otherwise flood the IsolationForest with identical all-zero vectors,
    # collapsing the score distribution into a single tie block (observed: ~96% of
    # effort-eligible rows were empty, producing degenerate scores and alerts).
    eligible = _eligible_rows(frame) & _biological_signal_mask(
        features,
        model_streams=model_streams,
    )
    fallback = _fallback_stream_score(frame, model_streams=model_streams)
    model_score = pd.Series(0.0, index=frame.index, dtype="float64")
    fit_status = "fallback"

    eligible_features = features.loc[eligible].copy()
    if len(eligible_features) >= max(config.min_model_rows, 2):
        matrix = _robust_normalise(eligible_features)
        if np.isfinite(matrix).all() and np.nanstd(matrix) > 0:
            model = IsolationForest(
                n_estimators=200,
                contamination=float(config.contamination),
                random_state=int(config.random_state),
            )
            model.fit(matrix)
            # Rank of the raw anomaly score; the contamination prior does not
            # change this ordering (see ``BehaviorAnomalyModelConfig``).
            raw_scores = -model.score_samples(matrix)
            model_score.loc[eligible] = _rank_percentile(raw_scores)
            fit_status = "isolation_forest"
        else:
            model_score.loc[eligible] = fallback.loc[eligible]
            fit_status = "fallback_no_variance"
    else:
        model_score.loc[eligible] = fallback.loc[eligible]

    model_score = model_score.clip(lower=0.0, upper=1.0)
    frame["model_eligible"] = eligible.astype(bool)
    frame["model_fit_status"] = fit_status
    frame["model_feature_columns"] = ",".join(feature_columns)
    frame["model_streams"] = ",".join(model_streams)
    frame["model_anomaly_score"] = model_score
    frame["final_score"] = model_score
    frame["anomaly_streams"] = _stream_labels(frame, model_streams=model_streams)
    frame["scoring_mode"] = "retrospective"
    frame["alert_level"] = _alert_levels(
        model_score,
        eligible=eligible,
        amber_quantile=config.amber_quantile,
        red_quantile=config.red_quantile,
    )
    return frame


def score_behavior_anomalies_walk_forward(
    rows: pd.DataFrame,
    *,
    config: BehaviorAnomalyModelConfig | None = None,
) -> pd.DataFrame:
    """Prospective (expanding-window) version of :func:`score_behavior_anomalies`.

    The retrospective scorer fits one IsolationForest on the whole series and
    derives alert thresholds from the full score distribution, so the notion of
    "anomalous" for a given week is informed by weeks that had not yet happened.
    That is fine for a retrospective association study but contaminates an
    *early-warning* claim. This version removes that leakage at the fusion layer:

    For each week ``W`` (in temporal order) it fits the model and computes
    thresholds using only eligible rows from **strictly earlier** weeks
    (``period_start < W``), scales week ``W``'s rows with that training window's
    median/IQR, scores them with the past-trained model, and ranks them
    *within the week*. A week's rows are therefore assessed using only
    information available before that week, and the score answers "relative to
    what normal looked like in the past, which cells are most unusual this
    week" -- which is exactly the within-week question the spatial evaluation
    asks. The per-stream baselines that feed the model are already strictly
    trailing, so the whole chain is now walk-forward.

    Within-week ranking (rather than a cross-window percentile) is deliberate:
    each week is scored by a different-vintage model, so raw scores are not
    comparable across weeks, and the feature distribution is highly concentrated
    (embedding drift is zero for almost all rows), which makes a cross-window
    percentile collapse toward 1.0. Ranking within the week restores
    discrimination and gives a stable alert tail per week.

    Weeks without enough prior history (``< min_model_rows`` eligible earlier
    rows) fall back to the stream score and are left un-alerted -- the honest
    "model not yet operational" state at the cold start, rather than borrowing
    future weeks to fill the gap.
    """
    config = config or BehaviorAnomalyModelConfig()
    if rows.empty:
        return rows
    if "period_start" not in rows.columns:
        raise ValueError(
            "walk-forward scoring requires a 'period_start' column",
        )

    frame = rows.copy()
    model_streams = _normalise_model_streams(config.model_streams)
    features = _model_features(
        frame,
        include_environmental=config.include_environmental,
        model_streams=model_streams,
    )
    feature_columns = list(features.columns)
    # Only rows carrying a bird-derived signal (acoustic drift or a population
    # residual) are scoreable; signal-empty rows would otherwise flood both the
    # trailing training window and the within-week ranking with identical empty
    # vectors. This mirrors the gate in the retrospective scorer.
    eligible = _eligible_rows(frame) & _biological_signal_mask(
        features,
        model_streams=model_streams,
    )
    fallback = _fallback_stream_score(frame, model_streams=model_streams)
    weeks = pd.to_datetime(frame["period_start"])

    model_score = pd.Series(0.0, index=frame.index, dtype="float64")
    fit_status = pd.Series("fallback", index=frame.index, dtype="object")
    alert = pd.Series("green", index=frame.index, dtype="object")

    minimum_rows = max(config.min_model_rows, 2)
    for week in pd.unique(weeks.sort_values()):
        week_mask = eligible & (weeks == week)
        if not week_mask.any():
            continue
        train_mask = eligible & (weeks < week)
        train_features = features.loc[train_mask]
        scored = False
        if len(train_features) >= minimum_rows:
            medians, scale = _normalise_params(train_features)
            train_matrix = _apply_normalise(train_features, medians, scale)
            if np.isfinite(train_matrix).all() and np.nanstd(train_matrix) > 0:
                model = IsolationForest(
                    n_estimators=200,
                    contamination=float(config.contamination),
                    random_state=int(config.random_state),
                )
                model.fit(train_matrix)
                week_matrix = _apply_normalise(features.loc[week_mask], medians, scale)
                if np.isfinite(week_matrix).all():
                    # Score this week's rows with the past-trained model, then
                    # rank them within the week (see docstring: cross-window
                    # percentiles collapse on this concentrated distribution).
                    week_raw = -model.score_samples(week_matrix)
                    percentile = _rank_percentile(week_raw)
                    model_score.loc[week_mask] = percentile
                    levels = np.where(
                        (percentile >= config.red_quantile) & (percentile > 0),
                        "red",
                        np.where(
                            (percentile >= config.amber_quantile) & (percentile > 0),
                            "amber",
                            "green",
                        ),
                    )
                    alert.loc[week_mask] = levels
                    fit_status.loc[week_mask] = "isolation_forest"
                    scored = True
        if not scored:
            model_score.loc[week_mask] = fallback.loc[week_mask]
            fit_status.loc[week_mask] = "fallback"

    model_score = model_score.clip(lower=0.0, upper=1.0)
    frame["model_eligible"] = eligible.astype(bool)
    frame["model_fit_status"] = fit_status
    frame["model_feature_columns"] = ",".join(feature_columns)
    frame["model_streams"] = ",".join(model_streams)
    frame["model_anomaly_score"] = model_score
    frame["final_score"] = model_score
    frame["anomaly_streams"] = _stream_labels(frame, model_streams=model_streams)
    frame["scoring_mode"] = "walk_forward"
    frame["alert_level"] = alert.tolist()
    return frame


def _eligible_rows(frame: pd.DataFrame) -> pd.Series:
    # Defensive redundancy: dense panels are already built only for
    # sufficient-effort grid weeks upstream, so in the current pipeline this
    # gate is effectively a no-op. It is retained so future changes to panel
    # construction cannot silently let under-supported rows into scoring.
    if "sufficient_observation_effort" not in frame.columns:
        eligible = pd.Series(True, index=frame.index)
    else:
        eligible = (
            frame["sufficient_observation_effort"].astype("boolean").fillna(False)
        )
    if "quality_mask_reason" in frame.columns:
        reason = frame["quality_mask_reason"].astype("string").fillna("")
        eligible = eligible & ~reason.str.contains(
            "insufficient",
            case=False,
            regex=False,
        )
    return eligible.astype(bool)


def _model_features(
    frame: pd.DataFrame,
    *,
    include_environmental: bool = False,
    model_streams: tuple[str, ...] = ("population", "bioacoustic"),
) -> pd.DataFrame:
    model_streams = _normalise_model_streams(model_streams)
    features = pd.DataFrame(index=frame.index)
    features["population_anomaly_score"] = _numeric_column(
        frame,
        "population_anomaly_score",
        0.0,
    )
    if "population_residual_abs" in frame.columns:
        features["population_residual_abs"] = _numeric_column(
            frame,
            "population_residual_abs",
            0.0,
        )
    else:
        features["population_residual_abs"] = _numeric_column(
            frame,
            "population_residual_z",
            0.0,
        ).abs()
    drift = _numeric_column(frame, "embedding_drift_score", 0.0)
    # B1: subtract the co-located non-target acoustic-scene drift so that only
    # excess, species-specific drift contributes. Defaults to the raw drift
    # when no control score is available (control column absent -> 0.0).
    control = _numeric_column(frame, "embedding_drift_control_score", 0.0)
    features["embedding_drift_excess_score"] = (drift - control).clip(lower=0.0)
    features["embedding_outlier_share"] = _numeric_column(
        frame,
        "embedding_outlier_share",
        0.0,
    )
    baseline_size = _numeric_column(frame, "embedding_baseline_size", 0.0)
    if "embedding_available" in frame.columns:
        available = frame["embedding_available"].astype("boolean").fillna(False)
    else:
        available = baseline_size > 0
    features["embedding_available"] = available.astype(float)
    features["embedding_coverage_score"] = (
        baseline_size.clip(lower=0.0, upper=100.0) / 100.0
    )

    columns: list[str] = []
    if "population" in model_streams:
        columns.extend(_POPULATION_FEATURE_COLUMNS)
    if "bioacoustic" in model_streams:
        columns.extend(_BIOACOUSTIC_FEATURE_COLUMNS)
    if include_environmental:
        biological_signal = _biological_signal_mask(
            features,
            model_streams=model_streams,
        )
        env_features, env_columns = _environmental_model_features(
            frame, biological_signal=biological_signal
        )
        for column in env_columns:
            features[column] = env_features[column]
        columns.extend(env_columns)
    return features.loc[:, columns]


def _biological_signal_mask(
    features: pd.DataFrame,
    *,
    model_streams: tuple[str, ...] = ("population", "bioacoustic"),
) -> pd.Series:
    """Rows that carry a bird-derived signal (acoustic drift or population).

    The environmental block is *context* for the acoustic+population spine, not
    an independent alert driver. Allowing weather/drought variation to enter the
    matrix on rows with no biological signal lets the IsolationForest fabricate
    anomalies purely from environmental variation (observed empirically: a quiet
    window went from 0 to dozens of alerts when env was naively added). This
    mask marks rows where a real biological signal exists, so environmental
    features can modulate those rows while staying neutral elsewhere.
    """
    model_streams = _normalise_model_streams(model_streams)
    signal = pd.Series(False, index=features.index)
    if "bioacoustic" in model_streams:
        drift_excess = pd.to_numeric(
            features.get("embedding_drift_excess_score"), errors="coerce"
        ).fillna(0.0)
        signal |= drift_excess > 0.0
    if "population" in model_streams:
        population = pd.to_numeric(
            features.get("population_anomaly_score"), errors="coerce"
        ).fillna(0.0)
        signal |= population > 0.0
    return signal


def _environmental_model_features(
    frame: pd.DataFrame,
    *,
    biological_signal: pd.Series,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the NaN-safe, acoustic-led curated environmental matrix block.

    Two protections are layered here:

    1. NaN-safety: environmental sources are missing for most rows until the
       backfills land, so each curated column is median-imputed (never
       ``fillna(0)`` -- 0 is a real temperature/SPI value, so a fabricated 0
       would read as "cold/dry"). Columns that are entirely missing carry no
       information and are dropped rather than imputed to a constant.
    2. Acoustic-led gating: on rows with no biological signal the environmental
       value is replaced by the column median (its neutral centre). This keeps
       biologically-quiet rows at the centre of the environmental distribution
       so weather variation alone cannot push them into the anomaly tails;
       environment can only modulate rows that already carry an acoustic or
       population signal. The ``environmental_available`` indicator still
       records genuine coverage for downstream context.
    """
    out = pd.DataFrame(index=frame.index)
    used: list[str] = []
    quiet = ~biological_signal.astype(bool)
    for column in _ENVIRONMENTAL_MODEL_COLUMNS:
        if column not in frame.columns:
            continue
        series = pd.to_numeric(frame[column], errors="coerce")
        if not series.notna().any():
            # Entirely missing (e.g. source not yet backfilled): no signal.
            continue
        median = float(series.median())
        filled = series.fillna(median)
        # Neutralise environmental variation on biologically-quiet rows.
        filled = filled.mask(quiet, median)
        out[column] = filled
        used.append(column)

    if "environmental_has_context" in frame.columns:
        available = frame["environmental_has_context"].astype("boolean").fillna(False)
    else:
        available = pd.Series(False, index=frame.index, dtype="boolean")
    # Availability is reported only where a biological signal also exists, so the
    # indicator cannot itself become a lone driver on quiet rows.
    out["environmental_available"] = (
        available.astype(bool) & biological_signal.astype(bool)
    ).astype(float)
    used.append("environmental_available")
    return out, used


def _fallback_stream_score(
    frame: pd.DataFrame,
    *,
    model_streams: tuple[str, ...] = ("population", "bioacoustic"),
) -> pd.Series:
    model_streams = _normalise_model_streams(model_streams)
    population = _numeric_column(frame, "population_anomaly_score", 0.0)
    embedding = _numeric_column(frame, "embedding_drift_score", 0.0)
    if "embedding_available" in frame.columns:
        embedding_available = (
            frame["embedding_available"].astype("boolean").fillna(False)
        )
    else:
        embedding_available = _numeric_column(frame, "embedding_baseline_size", 0.0) > 0
    columns: list[pd.Series] = []
    if "population" in model_streams:
        columns.append(population.rename("population"))
    if "bioacoustic" in model_streams:
        columns.append(embedding.where(embedding_available).rename("embedding"))
    if not columns:
        return pd.Series(0.0, index=frame.index, dtype="float64")
    return pd.concat(columns, axis="columns").max(axis="columns").fillna(0.0)


def _normalise_params(
    features: pd.DataFrame,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Median and IQR-scale per column, for robust standardisation.

    Exposed separately so the walk-forward scorer can fit the scaling on a
    training window and apply the *same* scaling to the rows being scored (using
    a row's own window to scale it would leak information across the split).
    """
    matrix = features.astype(float).to_numpy()
    medians = np.asarray(np.nanmedian(matrix, axis=0), dtype=np.float64)
    q75 = np.nanpercentile(matrix, 75, axis=0)
    q25 = np.nanpercentile(matrix, 25, axis=0)
    scale = np.where(q75 - q25 <= 1e-9, 1.0, q75 - q25)
    return medians, np.asarray(scale, dtype=np.float64)


def _apply_normalise(
    features: pd.DataFrame,
    medians: NDArray[np.float64],
    scale: NDArray[np.float64],
) -> NDArray[np.float64]:
    matrix = features.astype(float).to_numpy()
    result = (matrix - medians) / scale
    return np.asarray(result, dtype=np.float64)


def _robust_normalise(features: pd.DataFrame) -> NDArray[np.float64]:
    medians, scale = _normalise_params(features)
    return _apply_normalise(features, medians, scale)


def _rank_percentile(values: NDArray[np.float64]) -> NDArray[np.float64]:
    if len(values) <= 1:
        return np.zeros(len(values), dtype=float)
    order = pd.Series(values).rank(method="average").to_numpy() - 1.0
    return np.asarray(order / (len(values) - 1), dtype=float)


def _alert_levels(
    scores: pd.Series,
    *,
    eligible: pd.Series,
    amber_quantile: float,
    red_quantile: float,
) -> list[str]:
    eligible_scores = scores.loc[eligible].dropna()
    if eligible_scores.empty:
        return ["green"] * len(scores)
    amber_threshold = float(eligible_scores.quantile(amber_quantile))
    red_threshold = float(eligible_scores.quantile(red_quantile))
    # Degeneracy guard. When the model produces near-identical scores (e.g. an
    # IsolationForest with no usable feature variance), the rank percentiles
    # collapse into a tie block and the amber quantile lands on the score floor,
    # so ``score >= amber_threshold`` would flag essentially every eligible row.
    # There is no meaningful "top (1 - amber_quantile)" in a spike distribution,
    # so emit no alerts rather than alerting on everything. The intended flag
    # rate is ``1 - amber_quantile``; if the threshold would flag many multiples
    # of that, the score distribution carries no separation.
    positive = eligible_scores[eligible_scores > 0]
    if positive.empty:
        return ["green"] * len(scores)
    intended_rate = max(1.0 - amber_quantile, 1e-6)
    flagged_rate = float((positive >= amber_threshold).mean())
    if flagged_rate > min(0.5, intended_rate * 5.0):
        return ["green"] * len(scores)
    levels: list[str] = []
    for index, score in scores.items():
        if not bool(eligible.loc[index]):
            levels.append("green")
        elif score >= red_threshold and score > 0:
            levels.append("red")
        elif score >= amber_threshold and score > 0:
            levels.append("amber")
        else:
            levels.append("green")
    return levels


def _stream_labels(
    frame: pd.DataFrame,
    *,
    model_streams: tuple[str, ...] = ("population", "bioacoustic"),
) -> list[str]:
    model_streams = _normalise_model_streams(model_streams)
    population = _numeric_column(frame, "population_anomaly_score", 0.0)
    embedding = _numeric_column(frame, "embedding_drift_score", 0.0)
    if "embedding_available" in frame.columns:
        embedding_available = (
            frame["embedding_available"].astype("boolean").fillna(False)
        )
    else:
        embedding_available = _numeric_column(frame, "embedding_baseline_size", 0.0) > 0
    labels: list[str] = []
    for index in frame.index:
        streams: list[str] = []
        if "population" in model_streams and float(population.loc[index]) >= 0.95:
            streams.append("population")
        if (
            "bioacoustic" in model_streams
            and bool(embedding_available.loc[index])
            and float(embedding.loc[index]) >= 0.95
        ):
            streams.append("embedding")
        labels.append(",".join(streams) or "none")
    return labels


def _normalise_model_streams(model_streams: tuple[str, ...]) -> tuple[str, ...]:
    normalised = tuple(dict.fromkeys(str(item).strip().lower() for item in model_streams))
    if not normalised:
        msg = "model_streams must include at least one stream"
        raise ValueError(msg)
    invalid = sorted(set(normalised) - _VALID_MODEL_STREAMS)
    if invalid:
        msg = f"Unknown model stream(s): {', '.join(invalid)}"
        raise ValueError(msg)
    return normalised


def _numeric_column(frame: pd.DataFrame, column: str, default: float) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce").fillna(default)


__all__ = [
    "MODEL_FEATURE_COLUMNS",
    "BehaviorAnomalyModelConfig",
    "score_behavior_anomalies",
    "score_behavior_anomalies_walk_forward",
]
