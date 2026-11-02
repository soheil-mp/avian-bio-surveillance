"""kNN embedding-drift scoring for BirdWeather processed clips."""

from __future__ import annotations

import json
import math
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.cluster import DBSCAN
from sklearn.neighbors import NearestNeighbors

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.utils.progress import tqdm


_WEEKS_PER_YEAR = 52

EMBEDDING_WEEKLY_COLUMNS = [
    "embedding_drift_score",
    "embedding_outlier_share",
    "embedding_clip_count_scored",
    "embedding_baseline_size",
    "embedding_baseline_level",
    "embedding_backend_clip_counts",
    "embedding_backend_scored_clip_counts",
    "embedding_primary_backend",
    "top_detection_ids",
    "top_audio_artifact_paths",
    "background_noise_summary",
    "audio_quality_flag",
]

EMBEDDING_CONTROL_COLUMNS = [
    "embedding_drift_control_score",
    "embedding_control_species_count",
    "embedding_control_available",
]

EMBEDDING_CLUSTER_COLUMNS = [
    "detection_id",
    "species_id",
    "location_type",
    "location_id",
    "period_start",
    "period_end",
    "station_id",
    "grid_cell_id",
    "embedding_model",
    "cluster_id",
    "cluster_label",
    "cluster_rank",
    "cluster_size",
    "cluster_member_share",
    "cluster_count",
    "cluster_centroid_distance",
    "cluster_distance_threshold",
    "cluster_quality",
    "is_cluster_exemplar",
    "cluster_exemplar_detection_id",
]


@dataclass(frozen=True, slots=True)
class EmbeddingDriftConfig:
    """Configuration for clip-level kNN embedding drift scoring."""

    seasonal_window_weeks: int = 2
    min_baseline_clips: int = 50
    min_week_clips: int = 3
    k_neighbors: int = 10
    top_clip_count: int = 5
    baseline_reference_sample: int = 3000
    min_confidence: float = 0.1
    background_summary_top_k: int = 3
    cluster_min_samples: int = 2
    cluster_distance_multiplier: float = 1.2
    cluster_distance_floor: float = 0.03
    cluster_distance_ceiling: float = 0.18


@dataclass(frozen=True, slots=True)
class _BaselineScorer:
    baseline_level: str
    baseline_size: int
    model: NearestNeighbors | None
    baseline_distances: NDArray[np.float64]


_BaselineCacheKey = tuple[str, str, str, str, str, int, int, int, int]
_BaselineScorerCache = dict[_BaselineCacheKey, _BaselineScorer]


def score_embedding_drift(
    rows: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig | None = None,
    target_period_start: date | str | None = None,
    target_period_end: date | str | None = None,
) -> pd.DataFrame:
    """Score recent clip embeddings and aggregate them to species-week rows."""
    config = config or EmbeddingDriftConfig()
    clips = _normalise_input(rows, config=config)
    if clips.empty:
        return _empty_weekly_scores()

    expanded = _expand_clip_locations(clips)
    if expanded.empty:
        return _empty_weekly_scores()

    target_rows = _filter_target_period(
        expanded,
        start_date=target_period_start,
        end_date=target_period_end,
    )
    if target_rows.empty:
        return _empty_weekly_scores()

    clip_scores = _score_target_clips(expanded, target_rows, config)
    if clip_scores.empty:
        return _empty_weekly_scores()
    return _aggregate_weekly_scores(clip_scores, config=config)


def score_embedding_clusters(
    rows: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig | None = None,
    target_period_start: date | str | None = None,
    target_period_end: date | str | None = None,
) -> pd.DataFrame:
    """Assign stable per-clip embedding clusters for recent scoring windows."""
    config = config or EmbeddingDriftConfig()
    clips = _normalise_input(rows, config=config)
    if clips.empty:
        return _empty_cluster_scores()

    expanded = _expand_clip_locations(clips)
    if expanded.empty:
        return _empty_cluster_scores()
    expanded = _filter_target_period(
        expanded,
        start_date=target_period_start,
        end_date=target_period_end,
    )
    if expanded.empty:
        return _empty_cluster_scores()

    rows_out: list[dict[str, Any]] = []
    group_columns = [
        "species_id",
        "location_type",
        "location_id",
        "period_start",
        "embedding_model",
    ]
    with tqdm(
        total=len(expanded),
        desc="Assign embedding clusters",
        unit="clips",
    ) as progress:
        for keys, group in expanded.groupby(group_columns, dropna=False):
            rows_out.extend(_cluster_group_payloads(keys, group, config=config))
            progress.update(len(group))

    if not rows_out:
        return _empty_cluster_scores()
    return (
        pd.DataFrame(rows_out)
        .sort_values(
            [
                "period_start",
                "species_id",
                "location_id",
                "cluster_rank",
                "detection_id",
            ],
            ascending=[False, True, True, True, True],
        )
        .reset_index(drop=True)
    )


def parse_embedding_vector(value: object) -> NDArray[np.float64] | None:
    """Parse pgvector text or array-like values into a numeric vector."""
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        vector = value.astype(np.float64, copy=False)
    elif isinstance(value, list | tuple):
        vector = np.asarray(value, dtype=np.float64)
    elif isinstance(value, str):
        trimmed = value.strip()
        if not trimmed.startswith("[") or not trimmed.endswith("]"):
            return None
        vector = np.asarray(
            [float(part.strip()) for part in trimmed[1:-1].split(",") if part.strip()],
            dtype=np.float64,
        )
    elif pd.isna(value):
        return None
    else:
        return None

    if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
        return None
    magnitude = float(np.linalg.norm(vector))
    if magnitude <= 1e-12:
        return None
    return vector / magnitude


def attach_embedding_weekly_features(
    features: pd.DataFrame,
    embedding_weekly: pd.DataFrame,
) -> pd.DataFrame:
    """Attach weekly embedding-drift metrics to species-week features."""
    frame = features.copy()
    if frame.empty:
        return _ensure_embedding_columns(frame)
    if embedding_weekly.empty:
        return _ensure_embedding_columns(frame)

    keys = ["species_id", "location_type", "location_id", "period_start"]
    available_columns = [
        column for column in EMBEDDING_WEEKLY_COLUMNS if column in embedding_weekly
    ]
    left = frame.copy()
    right = embedding_weekly[[*keys, *available_columns]].copy()
    left["period_start"] = pd.to_datetime(left["period_start"]).dt.strftime("%Y-%m-%d")
    right["period_start"] = pd.to_datetime(right["period_start"]).dt.strftime(
        "%Y-%m-%d"
    )
    merged = left.merge(right.drop_duplicates(keys), on=keys, how="left")
    return _ensure_embedding_columns(merged)


def score_embedding_scene_control(
    weekly_scores: pd.DataFrame,
    *,
    target_species_ids: set[str],
    min_control_species: int = 1,
) -> pd.DataFrame:
    """Summarise co-located non-target acoustic-scene drift per location-week.

    The control captures drift shared across the *non-target* acoustic community
    in the same grid cell and week. Subtracting it from a target species' raw
    drift isolates species-specific change from scene-wide shifts (e.g. weather,
    hardware, or recording-condition changes) that inflate kNN distances for
    every species at once.

    Args:
        weekly_scores: Output of :func:`score_embedding_drift` covering both
            target and non-target species (e.g. an all-species scoring pass).
        target_species_ids: BirdWeather ids of monitored target species. These
            are excluded so the control reflects only the surrounding scene.
        min_control_species: Minimum distinct non-target species required for a
            location-week control to be considered available.

    Returns:
        A frame keyed by ``location_type``/``location_id``/``period_start`` with
        ``embedding_drift_control_score`` (median non-target drift),
        ``embedding_control_species_count`` and ``embedding_control_available``.
    """
    if weekly_scores.empty:
        return _empty_scene_control()

    targets = {str(value) for value in target_species_ids}
    frame = weekly_scores.copy()
    frame["species_id"] = frame["species_id"].astype(str)
    control = frame.loc[
        (~frame["species_id"].isin(targets))
        & (
            pd.to_numeric(frame["embedding_baseline_size"], errors="coerce").fillna(0)
            > 0
        )
    ].copy()
    if control.empty:
        return _empty_scene_control()

    control["embedding_drift_score"] = pd.to_numeric(
        control["embedding_drift_score"], errors="coerce"
    ).fillna(0.0)
    keys = ["location_type", "location_id", "period_start"]
    aggregated = (
        control.groupby(keys, dropna=False)
        .agg(
            embedding_drift_control_score=("embedding_drift_score", "median"),
            embedding_control_species_count=("species_id", "nunique"),
        )
        .reset_index()
    )
    aggregated["embedding_control_available"] = aggregated[
        "embedding_control_species_count"
    ] >= max(min_control_species, 1)
    # A location-week without enough non-target species offers no usable scene
    # baseline; report a zero control so downstream excess equals raw drift.
    insufficient = ~aggregated["embedding_control_available"]
    aggregated.loc[insufficient, "embedding_drift_control_score"] = 0.0
    return aggregated


def attach_embedding_control(
    features: pd.DataFrame,
    scene_control: pd.DataFrame,
) -> pd.DataFrame:
    """Attach co-located scene-control drift to species-week features."""
    frame = features.copy()
    if frame.empty:
        return _ensure_embedding_control_columns(frame)
    if scene_control is None or scene_control.empty:
        return _ensure_embedding_control_columns(frame)

    keys = ["location_type", "location_id", "period_start"]
    left = frame.copy()
    right = (
        scene_control[[*keys, *EMBEDDING_CONTROL_COLUMNS]].drop_duplicates(keys).copy()
    )
    left["period_start"] = pd.to_datetime(left["period_start"]).dt.strftime("%Y-%m-%d")
    right["period_start"] = pd.to_datetime(right["period_start"]).dt.strftime(
        "%Y-%m-%d"
    )
    merged = left.merge(right, on=keys, how="left")
    return _ensure_embedding_control_columns(merged)


def _normalise_input(
    rows: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig,
) -> pd.DataFrame:
    frame = rows.copy()
    if frame.empty:
        return frame

    required = {
        "detection_id",
        "detected_at",
        "species_id",
        "station_id",
    }
    if "embedding_array" not in frame.columns:
        required = required | {"embedding_vector"}
    missing = sorted(required - set(frame.columns))
    if missing:
        msg = f"Missing required embedding columns: {', '.join(missing)}"
        raise ValueError(msg)

    frame["species_id"] = frame["species_id"].astype(str)
    frame["station_id"] = frame["station_id"].astype(str)
    frame["detected_at"] = pd.to_datetime(frame["detected_at"], utc=True)
    frame["period_start"] = _week_start(frame["detected_at"])
    frame["week_of_year"] = frame["period_start"].dt.isocalendar().week.astype(int)
    if "confidence" not in frame.columns:
        frame["confidence"] = np.nan
    frame["confidence"] = pd.to_numeric(frame["confidence"], errors="coerce")
    frame["species_confidence_threshold"] = frame["species_id"].map(
        species_registry.recommended_threshold_by_birdweather_id
    )
    frame["species_confidence_threshold"] = frame[
        "species_confidence_threshold"
    ].fillna(config.min_confidence)
    frame = frame.loc[
        frame["confidence"].fillna(float("-inf"))
        >= frame["species_confidence_threshold"]
    ].copy()
    if frame.empty:
        return frame

    if "grid_cell_id" not in frame.columns:
        frame["grid_cell_id"] = pd.NA
    if "processed_clip_path" not in frame.columns:
        frame["processed_clip_path"] = pd.NA
    if "embedding_model" not in frame.columns:
        frame["embedding_model"] = "unknown"

    background = frame.get("background_sound_clf")
    if background is None:
        background = pd.Series(pd.NA, index=frame.index)
    frame["background_top_label"] = background.map(_extract_background_top_label)
    frame["background_top_score"] = background.map(_extract_background_top_score)
    local_times = frame["detected_at"].dt.tz_convert("Europe/Amsterdam")
    frame["daypart"] = local_times.dt.hour.map(_hour_to_daypart)

    # ``embedding_array`` may already be present when a memory-efficient
    # caller parsed + normalised the embeddings during loading; reuse it as-is
    # (identical to parsing the text here, since the same parser is used) so we
    # never hold the raw text and the parsed array at the same time.
    if "embedding_array" not in frame.columns:
        frame["embedding_array"] = frame["embedding_vector"].map(
            parse_embedding_vector
        )
    if "embedding_vector" in frame.columns:
        frame = frame.drop(columns=["embedding_vector"])
    return frame.dropna(subset=["embedding_array"]).reset_index(drop=True)


def _extract_background_top_label(value: object) -> str:
    """Return the dominant background-noise label for a clip, lowercased."""
    payload = _coerce_background_payload(value)
    if not payload:
        return ""
    tags = payload.get("tags") or []
    if not isinstance(tags, list) or not tags:
        return ""
    label = tags[0].get("label") if isinstance(tags[0], dict) else None
    return str(label).strip().lower() if label is not None else ""


def _extract_background_top_score(value: object) -> float:
    """Return the score of the dominant background-noise tag for a clip."""
    payload = _coerce_background_payload(value)
    if not payload:
        return 0.0
    tags = payload.get("tags") or []
    if not isinstance(tags, list) or not tags:
        return 0.0
    if not isinstance(tags[0], dict):
        return 0.0
    score = tags[0].get("score")
    try:
        return float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _coerce_background_payload(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    if pd.isna(value):
        return {}
    return {}


def _week_start(values: pd.Series) -> pd.Series:
    return (
        values.dt.normalize() - pd.to_timedelta(values.dt.weekday, unit="D")
    ).dt.tz_localize(None)


def _expand_clip_locations(clips: pd.DataFrame) -> pd.DataFrame:
    frame = clips.loc[clips["grid_cell_id"].notna()].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["location_type"] = "grid"
    frame["location_id"] = frame["grid_cell_id"].astype(str)
    return frame.reset_index(drop=True)


def _filter_target_period(
    frame: pd.DataFrame,
    *,
    start_date: date | str | None,
    end_date: date | str | None,
) -> pd.DataFrame:
    if frame.empty or (start_date is None and end_date is None):
        return frame
    periods = pd.to_datetime(frame["period_start"], errors="coerce").dt.normalize()
    mask = periods.notna()
    if start_date is not None:
        mask &= periods >= pd.Timestamp(start_date).normalize()
    if end_date is not None:
        mask &= periods <= pd.Timestamp(end_date).normalize()
    return frame.loc[mask].reset_index(drop=True)


def _location_row(
    row: dict[str, Any],
    location_type: str,
    location_id: str,
) -> dict[str, Any]:
    copied = dict(row)
    copied["location_type"] = location_type
    copied["location_id"] = location_id
    return copied


def _score_clip(
    expanded: pd.DataFrame,
    row: dict[str, Any],
    config: EmbeddingDriftConfig,
    *,
    baseline_cache: _BaselineScorerCache | None = None,
) -> dict[str, Any]:
    scorer = _baseline_scorer(expanded, row, config, baseline_cache=baseline_cache)
    if scorer.model is None:
        return _clip_payload(
            row,
            baseline_level="none",
            baseline_size=0,
            embedding_distance=np.nan,
            embedding_drift_score=0.0,
        )

    target_vector = np.asarray(row["embedding_array"], dtype=np.float64)
    distance, score = _score_vector_with_scorer(target_vector, scorer)
    return _clip_payload(
        row,
        baseline_level=scorer.baseline_level,
        baseline_size=scorer.baseline_size,
        embedding_distance=distance,
        embedding_drift_score=score,
    )


_BASELINE_SAMPLE_SEED = 20240601


def _none_scorer() -> _BaselineScorer:
    return _BaselineScorer(
        baseline_level="none",
        baseline_size=0,
        model=None,
        baseline_distances=np.asarray([], dtype=np.float64),
    )


def _baseline_neighbor_distances_sampled(
    baseline_vectors: NDArray[np.float64],
    k_neighbors: int,
    sample: int,
    model: NearestNeighbors,
) -> NDArray[np.float64]:
    """Reference kNN-distance distribution for a baseline.

    Identical to :func:`_baseline_neighbor_distances` when the baseline has at
    most ``sample`` rows. For larger baselines the distribution is estimated
    from ``sample`` fixed-seed random baseline points, each scored against the
    *full* baseline (via the already-fitted ``model``); individual distances stay
    exact and only the percentile reference is Monte-Carlo estimated. This bounds
    the cost from O(n^2) to O(sample * n) while leaving target distances and
    small-baseline scores unchanged.
    """
    n = len(baseline_vectors)
    if n < 2:
        return np.asarray([], dtype=np.float64)
    neighbors = max(2, min(k_neighbors + 1, n))
    if sample is None or sample <= 0 or n <= sample:
        query = baseline_vectors
    else:
        rng = np.random.default_rng(_BASELINE_SAMPLE_SEED)
        idx = rng.choice(n, size=sample, replace=False)
        idx.sort()
        query = baseline_vectors[idx]
    distances, _ = model.kneighbors(query, n_neighbors=neighbors)
    return np.asarray(distances[:, 1:].mean(axis=1), dtype=np.float64)


class _DriftBaselineIndex:
    """Fast, exact baseline resolution for :func:`_score_target_clips`.

    Precomputes per-(species, location, daypart) row-position indices over
    ``expanded`` so each target group's baseline is selected by slicing
    position arrays instead of a full boolean scan. The cascade is evaluated
    lazily (grid_daypart -> grid -> network_daypart -> network), stopping at the
    first level with >= ``min_baseline_clips`` rows. The two cross-cell network
    levels do not depend on ``location_id`` and are reused across cells via a
    small LRU cache. Baselines are kept in full for exact target queries; only
    the percentile reference distribution is sub-sampled (see
    :func:`_baseline_neighbor_distances_sampled`), so small baselines match
    :func:`_baseline_scorer` / :func:`_select_baseline` exactly.
    """

    def __init__(self, expanded: pd.DataFrame, config: EmbeddingDriftConfig) -> None:
        self._config = config
        self._emb = expanded["embedding_array"].to_numpy()
        self._period = expanded["period_start"].to_numpy()
        self._week = expanded["week_of_year"].to_numpy().astype(np.int64)
        self._idx_sp_loc_dp = expanded.groupby(
            ["species_id", "location_id", "daypart"], sort=False
        ).indices
        self._idx_sp_loc = expanded.groupby(
            ["species_id", "location_id"], sort=False
        ).indices
        self._idx_sp_dp = expanded.groupby(
            ["species_id", "daypart"], sort=False
        ).indices
        self._idx_sp = expanded.groupby("species_id", sort=False).indices
        self._network_cache: OrderedDict[tuple[Any, ...], _BaselineScorer] = (
            OrderedDict()
        )
        self._network_cache_max = 6

    def _select(
        self,
        base_positions: NDArray[np.int64] | None,
        period_start: np.datetime64,
        week: int,
    ) -> NDArray[np.int64]:
        if base_positions is None or base_positions.size == 0:
            return np.asarray([], dtype=np.int64)
        sub_period = self._period[base_positions]
        sub_week = self._week[base_positions]
        period_mask = sub_period < period_start
        diff = np.abs(sub_week - week)
        week_mask = (
            np.minimum(diff, _WEEKS_PER_YEAR - diff)
            <= self._config.seasonal_window_weeks
        )
        return base_positions[period_mask & week_mask]

    def _build(self, level: str, positions: NDArray[np.int64]) -> _BaselineScorer:
        baseline_vectors = _stack_vectors([self._emb[pos] for pos in positions])
        neighbors = max(1, min(self._config.k_neighbors, len(baseline_vectors)))
        model = NearestNeighbors(n_neighbors=neighbors, metric="cosine")
        model.fit(baseline_vectors)
        return _BaselineScorer(
            baseline_level=level,
            baseline_size=len(baseline_vectors),
            model=model,
            baseline_distances=_baseline_neighbor_distances_sampled(
                baseline_vectors,
                self._config.k_neighbors,
                self._config.baseline_reference_sample,
                model,
            ),
        )

    def _network_scorer(
        self,
        key: tuple[Any, ...],
        *,
        level: str,
        base_positions: NDArray[np.int64] | None,
        period_start: np.datetime64,
        week: int,
    ) -> _BaselineScorer:
        cached = self._network_cache.get(key)
        if cached is not None:
            self._network_cache.move_to_end(key)
            return cached
        selected = self._select(base_positions, period_start, week)
        if selected.size >= self._config.min_baseline_clips:
            scorer = self._build(level, selected)
        else:
            scorer = _none_scorer()
        self._network_cache[key] = scorer
        self._network_cache.move_to_end(key)
        while len(self._network_cache) > self._network_cache_max:
            self._network_cache.popitem(last=False)
        return scorer

    def scorer_for(self, row: dict[str, Any]) -> _BaselineScorer:
        species = row["species_id"]
        location = row["location_id"]
        daypart = row["daypart"]
        week = int(row["week_of_year"])
        period_ts = row["period_start"]
        period_start = np.datetime64(period_ts)
        min_clips = self._config.min_baseline_clips

        selected = self._select(
            self._idx_sp_loc_dp.get((species, location, daypart)),
            period_start,
            week,
        )
        if selected.size >= min_clips:
            return self._build("grid_daypart", selected)

        selected = self._select(
            self._idx_sp_loc.get((species, location)),
            period_start,
            week,
        )
        if selected.size >= min_clips:
            return self._build("grid", selected)

        scorer = self._network_scorer(
            ("network_daypart", species, daypart, week, period_ts),
            level="network_daypart",
            base_positions=self._idx_sp_dp.get((species, daypart)),
            period_start=period_start,
            week=week,
        )
        if scorer.baseline_level != "none":
            return scorer

        return self._network_scorer(
            ("network", species, week, period_ts),
            level="network",
            base_positions=self._idx_sp.get(species),
            period_start=period_start,
            week=week,
        )


def _score_target_clips(
    expanded: pd.DataFrame,
    target_rows: pd.DataFrame,
    config: EmbeddingDriftConfig,
) -> pd.DataFrame:
    """Score target clips per (species, cell, daypart, week) baseline group.

    Each group is scored against the baseline returned by
    :func:`_select_baseline`; selection is accelerated via
    :class:`_DriftBaselineIndex` and, for large baselines, the percentile
    reference distribution is Monte-Carlo estimated (see
    :func:`_baseline_neighbor_distances_sampled`) while target distances stay
    exact. Group iteration is reordered so cells sharing a network baseline are
    consecutive (keeps the network cache small); per-clip scores are independent
    of group order.
    """
    if target_rows.empty:
        return pd.DataFrame()

    index = _DriftBaselineIndex(expanded, config)
    ordered = target_rows.sort_values(
        ["species_id", "period_start", "week_of_year", "daypart", "location_id"],
        kind="stable",
    )
    group_columns = [
        "species_id",
        "location_type",
        "location_id",
        "daypart",
        "period_start",
        "week_of_year",
    ]
    payloads: list[dict[str, Any]] = []
    with tqdm(
        total=len(target_rows),
        desc="Score embedding drift clips",
        unit="clips",
    ) as progress:
        for _keys, group in ordered.groupby(group_columns, dropna=False, sort=False):
            records = group.to_dict(orient="records")
            if not records:
                progress.update(len(group))
                continue
            scorer = index.scorer_for(records[0])
            if scorer.model is None:
                payloads.extend(
                    _clip_payload(
                        row,
                        baseline_level="none",
                        baseline_size=0,
                        embedding_distance=np.nan,
                        embedding_drift_score=0.0,
                    )
                    for row in records
                )
                progress.update(len(group))
                continue

            vectors = _stack_vectors(
                [
                    np.asarray(row["embedding_array"], dtype=np.float64)
                    for row in records
                ]
            )
            distances, scores = _score_vectors_with_scorer(vectors, scorer)
            payloads.extend(
                _clip_payload(
                    row,
                    baseline_level=scorer.baseline_level,
                    baseline_size=scorer.baseline_size,
                    embedding_distance=float(distance),
                    embedding_drift_score=float(score),
                )
                for row, distance, score in zip(
                    records,
                    distances,
                    scores,
                    strict=False,
                )
            )
            progress.update(len(group))

    return pd.DataFrame(payloads)


def _baseline_scorer(
    expanded: pd.DataFrame,
    row: dict[str, Any],
    config: EmbeddingDriftConfig,
    *,
    baseline_cache: _BaselineScorerCache | None = None,
) -> _BaselineScorer:
    key = _baseline_cache_key(row, config)
    if baseline_cache is not None and key in baseline_cache:
        return baseline_cache[key]

    baseline_level, baseline = _select_baseline(expanded, row, config)
    if baseline.empty:
        scorer = _BaselineScorer(
            baseline_level="none",
            baseline_size=0,
            model=None,
            baseline_distances=np.asarray([], dtype=np.float64),
        )
    else:
        baseline_vectors = _stack_vectors(baseline["embedding_array"].tolist())
        neighbors = max(1, min(config.k_neighbors, len(baseline_vectors)))
        model = NearestNeighbors(n_neighbors=neighbors, metric="cosine")
        model.fit(baseline_vectors)
        scorer = _BaselineScorer(
            baseline_level=baseline_level,
            baseline_size=len(baseline_vectors),
            model=model,
            baseline_distances=_baseline_neighbor_distances(
                baseline_vectors,
                config.k_neighbors,
            ),
        )

    if baseline_cache is not None:
        baseline_cache[key] = scorer
    return scorer


def _baseline_cache_key(
    row: dict[str, Any],
    config: EmbeddingDriftConfig,
) -> _BaselineCacheKey:
    return (
        str(row["species_id"]),
        str(row["location_type"]),
        str(row["location_id"]),
        str(row["daypart"]),
        str(row["period_start"]),
        int(row["week_of_year"]),
        int(config.seasonal_window_weeks),
        int(config.min_baseline_clips),
        int(config.k_neighbors),
    )


def _select_baseline(
    expanded: pd.DataFrame,
    row: dict[str, Any],
    config: EmbeddingDriftConfig,
) -> tuple[str, pd.DataFrame]:
    for level, baseline in _candidate_baselines(expanded, row, config):
        if len(baseline) >= config.min_baseline_clips:
            return level, baseline
    return "none", pd.DataFrame()


def _candidate_baselines(
    expanded: pd.DataFrame,
    row: dict[str, Any],
    config: EmbeddingDriftConfig,
) -> list[tuple[str, pd.DataFrame]]:
    species_id = str(row["species_id"])
    period_start = row["period_start"]
    week = int(row["week_of_year"])
    location_type = str(row["location_type"])
    location_id = str(row["location_id"])

    results: list[tuple[str, pd.DataFrame]] = []
    exact_daypart = expanded.loc[
        (expanded["species_id"] == species_id)
        & (expanded["location_type"] == location_type)
        & (expanded["location_id"] == location_id)
        & (expanded["daypart"] == row["daypart"])
        & (expanded["period_start"] < period_start)
    ]
    results.append(("grid_daypart", _seasonal_window(exact_daypart, week, config)))

    exact = expanded.loc[
        (expanded["species_id"] == species_id)
        & (expanded["location_type"] == location_type)
        & (expanded["location_id"] == location_id)
        & (expanded["period_start"] < period_start)
    ]
    results.append(("grid", _seasonal_window(exact, week, config)))

    network_daypart = expanded.loc[
        (expanded["species_id"] == species_id)
        & (expanded["daypart"] == row["daypart"])
        & (expanded["period_start"] < period_start)
    ]
    results.append(("network_daypart", _seasonal_window(network_daypart, week, config)))

    network = expanded.loc[
        (expanded["species_id"] == species_id)
        & (expanded["period_start"] < period_start)
    ]
    results.append(("network", _seasonal_window(network, week, config)))
    return results


def _seasonal_window(
    rows: pd.DataFrame,
    week: int,
    config: EmbeddingDriftConfig,
) -> pd.DataFrame:
    if rows.empty:
        return rows
    return rows.loc[
        rows["week_of_year"].map(
            lambda value: (
                _week_distance(int(value), week) <= config.seasonal_window_weeks
            ),
        )
    ]


def _score_vector_against_baseline(
    target_vector: NDArray[np.float64],
    baseline_vectors: NDArray[np.float64],
    *,
    k_neighbors: int,
) -> tuple[float, float]:
    neighbors = max(1, min(k_neighbors, len(baseline_vectors)))
    model = NearestNeighbors(n_neighbors=neighbors, metric="cosine")
    model.fit(baseline_vectors)
    return _score_vector_with_scorer(
        target_vector,
        _BaselineScorer(
            baseline_level="direct",
            baseline_size=len(baseline_vectors),
            model=model,
            baseline_distances=_baseline_neighbor_distances(
                baseline_vectors,
                k_neighbors,
            ),
        ),
    )


def _score_vector_with_scorer(
    target_vector: NDArray[np.float64],
    scorer: _BaselineScorer,
) -> tuple[float, float]:
    if scorer.model is None:
        return np.nan, 0.0
    distances, _ = scorer.model.kneighbors(target_vector.reshape(1, -1))
    target_distance = float(np.mean(distances[0]))
    if scorer.baseline_distances.size == 0:
        return target_distance, 0.0
    score = float(np.mean(scorer.baseline_distances <= target_distance))
    return target_distance, score


def _score_vectors_with_scorer(
    target_vectors: NDArray[np.float64],
    scorer: _BaselineScorer,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    if scorer.model is None or len(target_vectors) == 0:
        return (
            np.asarray([], dtype=np.float64),
            np.asarray([], dtype=np.float64),
        )
    distances, _ = scorer.model.kneighbors(target_vectors)
    target_distances = np.asarray(distances.mean(axis=1), dtype=np.float64)
    if scorer.baseline_distances.size == 0:
        return target_distances, np.zeros(len(target_distances), dtype=np.float64)
    sorted_baseline = np.sort(scorer.baseline_distances)
    ranks = np.searchsorted(sorted_baseline, target_distances, side="right")
    scores = ranks.astype(np.float64) / float(sorted_baseline.size)
    return target_distances, scores


def _baseline_neighbor_distances(
    baseline_vectors: NDArray[np.float64],
    k_neighbors: int,
) -> NDArray[np.float64]:
    if len(baseline_vectors) < 2:
        return np.asarray([], dtype=np.float64)
    neighbors = max(2, min(k_neighbors + 1, len(baseline_vectors)))
    model = NearestNeighbors(n_neighbors=neighbors, metric="cosine")
    model.fit(baseline_vectors)
    distances, _ = model.kneighbors(baseline_vectors)
    neighbor_distances: NDArray[np.float64] = np.asarray(
        distances[:, 1:].mean(axis=1),
        dtype=np.float64,
    )
    return neighbor_distances


def _stack_vectors(values: list[NDArray[np.float64]]) -> NDArray[np.float64]:
    return np.vstack(values).astype(np.float64)


def _clip_payload(
    row: dict[str, Any],
    *,
    baseline_level: str,
    baseline_size: int,
    embedding_distance: float,
    embedding_drift_score: float,
) -> dict[str, Any]:
    return {
        "detection_id": row["detection_id"],
        "species_id": row["species_id"],
        "location_type": row["location_type"],
        "location_id": row["location_id"],
        "station_id": row["station_id"],
        "grid_cell_id": row.get("grid_cell_id"),
        "period_start": row["period_start"].date().isoformat(),
        "detected_at": row["detected_at"],
        "confidence": row.get("confidence"),
        "embedding_model": row.get("embedding_model", "unknown"),
        "processed_clip_path": row.get("processed_clip_path"),
        "background_top_label": row.get("background_top_label", ""),
        "background_top_score": row.get("background_top_score", 0.0),
        "embedding_baseline_level": baseline_level,
        "embedding_baseline_size": baseline_size,
        "embedding_distance": embedding_distance,
        "embedding_drift_score": embedding_drift_score,
    }


def _cluster_group_payloads(
    keys: tuple[Any, ...],
    group: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig,
) -> list[dict[str, Any]]:
    species_id, location_type, location_id, _period_start, embedding_model = keys
    if group.empty:
        return []

    threshold = _cluster_distance_threshold(group, config=config)
    min_samples = max(int(config.cluster_min_samples), 2)
    labels = _cluster_labels(group, min_samples=min_samples, threshold=threshold)
    cluster_rows = group.reset_index(drop=True).copy()
    cluster_rows["cluster_label_raw"] = labels
    cluster_rows["period_start"] = pd.to_datetime(cluster_rows["period_start"])
    cluster_rows["period_end"] = cluster_rows["period_start"] + pd.to_timedelta(
        6, unit="D"
    )
    cluster_rows["cluster_count"] = len({label for label in labels if label >= 0})

    cluster_metadata = _cluster_metadata(cluster_rows)
    rank_by_label = {
        label: index + 1
        for index, (label, _meta) in enumerate(
            sorted(
                cluster_metadata.items(),
                key=lambda item: (
                    -item[1]["cluster_size"],
                    item[1]["cluster_centroid_distance_mean"],
                    item[1]["cluster_exemplar_detection_id"],
                ),
            )
        )
    }

    rows_out: list[dict[str, Any]] = []
    for record in cluster_rows.to_dict(orient="records"):
        raw_label = int(record["cluster_label_raw"])
        meta = cluster_metadata.get(raw_label)
        if meta is None:
            rows_out.append(
                _cluster_payload(
                    record,
                    species_id=species_id,
                    location_type=location_type,
                    location_id=location_id,
                    embedding_model=embedding_model,
                    cluster_id="",
                    cluster_label="Noise",
                    cluster_rank=0,
                    cluster_size=0,
                    cluster_member_share=0.0,
                    cluster_count=int(cluster_rows["cluster_count"].iloc[0]),
                    cluster_centroid_distance=np.nan,
                    cluster_distance_threshold=threshold,
                    cluster_quality=(
                        "small_sample" if len(cluster_rows) < min_samples else "noise"
                    ),
                    is_cluster_exemplar=False,
                    cluster_exemplar_detection_id="",
                )
            )
            continue

        cluster_rank = rank_by_label[raw_label]
        rows_out.append(
            _cluster_payload(
                record,
                species_id=species_id,
                location_type=location_type,
                location_id=location_id,
                embedding_model=embedding_model,
                cluster_id=f"cluster-{cluster_rank:02d}",
                cluster_label=f"Cluster {cluster_rank}",
                cluster_rank=cluster_rank,
                cluster_size=int(meta["cluster_size"]),
                cluster_member_share=float(meta["cluster_size"] / len(cluster_rows)),
                cluster_count=int(cluster_rows["cluster_count"].iloc[0]),
                cluster_centroid_distance=float(
                    meta["distance_by_detection_id"][record["detection_id"]]
                ),
                cluster_distance_threshold=threshold,
                cluster_quality="clustered",
                is_cluster_exemplar=record["detection_id"]
                == meta["cluster_exemplar_detection_id"],
                cluster_exemplar_detection_id=str(
                    meta["cluster_exemplar_detection_id"]
                ),
            )
        )

    return rows_out


def _cluster_labels(
    group: pd.DataFrame,
    *,
    min_samples: int,
    threshold: float,
) -> NDArray[np.int64]:
    if len(group) < min_samples:
        return np.full(len(group), -1, dtype=np.int64)
    vectors = _stack_vectors(group["embedding_array"].tolist())
    clustering = DBSCAN(eps=threshold, min_samples=min_samples, metric="cosine")
    return np.asarray(clustering.fit_predict(vectors), dtype=np.int64)


def _cluster_distance_threshold(
    group: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig,
) -> float:
    if len(group) < 2:
        return float(config.cluster_distance_floor)
    vectors = _stack_vectors(group["embedding_array"].tolist())
    nearest_distances = _nearest_neighbor_distances(vectors)
    if nearest_distances.size == 0:
        return float(config.cluster_distance_floor)
    median_distance = float(np.median(nearest_distances))
    scaled = median_distance * float(config.cluster_distance_multiplier)
    bounded = min(
        max(scaled, float(config.cluster_distance_floor)),
        float(config.cluster_distance_ceiling),
    )
    return float(bounded)


def _nearest_neighbor_distances(
    vectors: NDArray[np.float64],
) -> NDArray[np.float64]:
    if len(vectors) < 2:
        return np.asarray([], dtype=np.float64)
    model = NearestNeighbors(n_neighbors=2, metric="cosine")
    model.fit(vectors)
    distances, _ = model.kneighbors(vectors)
    return np.asarray(distances[:, 1], dtype=np.float64)


def _cluster_metadata(
    cluster_rows: pd.DataFrame,
) -> dict[int, dict[str, Any]]:
    metadata: dict[int, dict[str, Any]] = {}
    for label, members in cluster_rows.groupby("cluster_label_raw", dropna=False):
        cluster_label = int(label)
        if cluster_label < 0:
            continue
        vectors = _stack_vectors(members["embedding_array"].tolist())
        centroid = _normalized_centroid(vectors)
        distances = np.asarray(
            [_cosine_distance(vector, centroid) for vector in vectors],
            dtype=np.float64,
        )
        detection_ids = members["detection_id"].astype(str).tolist()
        if len(distances):
            # Deterministic, order-independent exemplar: smallest centroid
            # distance, ties broken by detection_id so the choice does not depend
            # on clip load order (all-at-once vs streamed per species).
            exemplar_index = min(
                range(len(distances)),
                key=lambda i: (float(distances[i]), detection_ids[i]),
            )
        else:
            exemplar_index = 0
        distance_by_detection_id = {
            detection_id: float(distance)
            for detection_id, distance in zip(detection_ids, distances, strict=False)
        }
        metadata[cluster_label] = {
            "cluster_size": len(members),
            "cluster_exemplar_detection_id": detection_ids[exemplar_index],
            "cluster_centroid_distance_mean": float(distances.mean())
            if len(distances)
            else 0.0,
            "distance_by_detection_id": distance_by_detection_id,
        }
    return metadata


def _normalized_centroid(vectors: NDArray[np.float64]) -> NDArray[np.float64]:
    centroid = np.mean(vectors, axis=0)
    magnitude = float(np.linalg.norm(centroid))
    if magnitude <= 1e-12:
        return np.asarray(vectors[0], dtype=np.float64)
    return np.asarray(centroid / magnitude, dtype=np.float64)


def _cosine_distance(
    left: NDArray[np.float64],
    right: NDArray[np.float64],
) -> float:
    similarity = float(np.clip(np.dot(left, right), -1.0, 1.0))
    return float(max(0.0, 1.0 - similarity))


def _cluster_payload(
    row: dict[str, Any],
    *,
    species_id: Any,
    location_type: Any,
    location_id: Any,
    embedding_model: Any,
    cluster_id: str,
    cluster_label: str,
    cluster_rank: int,
    cluster_size: int,
    cluster_member_share: float,
    cluster_count: int,
    cluster_centroid_distance: float,
    cluster_distance_threshold: float,
    cluster_quality: str,
    is_cluster_exemplar: bool,
    cluster_exemplar_detection_id: str,
) -> dict[str, Any]:
    return {
        "detection_id": str(row["detection_id"]),
        "species_id": str(species_id),
        "location_type": str(location_type),
        "location_id": str(location_id),
        "period_start": pd.to_datetime(row["period_start"]).date().isoformat(),
        "period_end": pd.to_datetime(row["period_end"]).date().isoformat(),
        "station_id": str(row.get("station_id", ""))
        if row.get("station_id") is not None
        else "",
        "grid_cell_id": (
            str(row["grid_cell_id"])
            if row.get("grid_cell_id") is not None
            and not pd.isna(row.get("grid_cell_id"))
            else ""
        ),
        "embedding_model": str(embedding_model),
        "cluster_id": cluster_id,
        "cluster_label": cluster_label,
        "cluster_rank": int(cluster_rank),
        "cluster_size": int(cluster_size),
        "cluster_member_share": float(cluster_member_share),
        "cluster_count": int(cluster_count),
        "cluster_centroid_distance": float(cluster_centroid_distance)
        if np.isfinite(cluster_centroid_distance)
        else np.nan,
        "cluster_distance_threshold": float(cluster_distance_threshold),
        "cluster_quality": cluster_quality,
        "is_cluster_exemplar": bool(is_cluster_exemplar),
        "cluster_exemplar_detection_id": cluster_exemplar_detection_id,
    }


def _aggregate_weekly_scores(
    clip_scores: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig,
) -> pd.DataFrame:
    rows = []
    group_columns = ["species_id", "location_type", "location_id", "period_start"]
    for keys, group in clip_scores.groupby(group_columns, dropna=False):
        scored_group = group.loc[group["embedding_baseline_size"] > 0].copy()
        if scored_group.empty:
            rows.append(_weekly_payload(keys, group, scored_group, config=config))
            continue
        scored_group = scored_group.sort_values(
            ["embedding_drift_score", "detection_id"],
            ascending=[False, True],
        )
        rows.append(_weekly_payload(keys, group, scored_group, config=config))
    if not rows:
        return _empty_weekly_scores()
    return (
        pd.DataFrame(rows)
        .sort_values(
            ["period_start", "embedding_drift_score"],
            ascending=[False, False],
        )
        .reset_index(drop=True)
    )


def _weekly_payload(
    keys: tuple[Any, ...],
    group: pd.DataFrame,
    scored_group: pd.DataFrame,
    *,
    config: EmbeddingDriftConfig,
) -> dict[str, Any]:
    species_id, location_type, location_id, period_start = keys
    if scored_group.empty:
        drift_score = 0.0
        outlier_share = 0.0
        baseline_size = 0
        baseline_level = "none"
        top = _fallback_top_clips(group, top_n=config.top_clip_count)
        top_detection_ids = ",".join(top["detection_id"].astype(str).tolist())
        top_audio_paths = ",".join(
            top["processed_clip_path"].dropna().astype(str).tolist()
        )
    else:
        top_n = max(1, math.ceil(len(scored_group) * 0.10))
        top_scores = scored_group["embedding_drift_score"].head(top_n)
        drift_score = float(top_scores.median())
        outlier_share = float((scored_group["embedding_drift_score"] >= 0.95).mean())
        baseline_size = int(scored_group["embedding_baseline_size"].median())
        baseline_level = str(scored_group["embedding_baseline_level"].mode().iloc[0])
        top = scored_group.head(config.top_clip_count)
        top_detection_ids = ",".join(top["detection_id"].astype(str).tolist())
        top_audio_paths = ",".join(
            top["processed_clip_path"].dropna().astype(str).tolist()
        )

    clip_count = len(scored_group)
    backend_clip_counts = _embedding_backend_counts(group)
    backend_scored_clip_counts = _embedding_backend_counts(scored_group)
    return {
        "species_id": species_id,
        "location_type": location_type,
        "location_id": location_id,
        "period_start": period_start,
        "embedding_drift_score": drift_score,
        "embedding_outlier_share": outlier_share,
        "embedding_clip_count_scored": clip_count,
        "embedding_baseline_size": baseline_size,
        "embedding_baseline_level": baseline_level,
        "embedding_backend_clip_counts": backend_clip_counts,
        "embedding_backend_scored_clip_counts": backend_scored_clip_counts,
        "embedding_primary_backend": _primary_embedding_backend(
            scored_group if not scored_group.empty else group
        ),
        "top_detection_ids": top_detection_ids,
        "top_audio_artifact_paths": top_audio_paths,
        "background_noise_summary": _background_noise_summary(
            group.get("background_top_label", None),
            top_k=config.background_summary_top_k,
        ),
        "audio_quality_flag": _audio_quality_flag(
            total_clip_count=len(group),
            scored_clip_count=clip_count,
            min_week_clips=config.min_week_clips,
        ),
    }


def _embedding_backend_counts(group: pd.DataFrame) -> dict[str, int]:
    if group.empty or "embedding_model" not in group.columns:
        return {}
    cleaned = (
        group["embedding_model"]
        .fillna("unknown")
        .astype(str)
        .str.strip()
        .replace("", "unknown")
    )
    return {str(key): int(value) for key, value in cleaned.value_counts().items()}


def _primary_embedding_backend(group: pd.DataFrame) -> str:
    counts = _embedding_backend_counts(group)
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _fallback_top_clips(group: pd.DataFrame, *, top_n: int) -> pd.DataFrame:
    if group.empty:
        return group.head(0)

    ranked = group.copy()
    ranked["confidence"] = pd.to_numeric(
        ranked.get("confidence", -1.0), errors="coerce"
    ).fillna(-1.0)
    ranked["detected_at"] = pd.to_datetime(
        ranked.get("detected_at"), errors="coerce", utc=True
    )
    return ranked.sort_values(
        ["confidence", "detected_at", "detection_id"],
        ascending=[False, False, True],
        na_position="last",
    ).head(max(top_n, 1))


def _background_noise_summary(
    labels: pd.Series | None,
    *,
    top_k: int,
) -> str:
    """Return a compact ``label:count`` summary of dominant clip backgrounds."""
    if labels is None:
        return ""
    cleaned = [
        str(value).strip().lower()
        for value in labels.tolist()
        if value is not None and not pd.isna(value) and str(value).strip()
    ]
    if not cleaned:
        return ""
    counter: Counter[str] = Counter(cleaned)
    items = counter.most_common(max(top_k, 1))
    return ",".join(f"{label}:{count}" for label, count in items)


def _audio_quality_flag(
    *,
    total_clip_count: int,
    scored_clip_count: int,
    min_week_clips: int,
) -> str:
    if scored_clip_count <= 0:
        return "sparse_embedding_baseline"
    if total_clip_count < min_week_clips:
        return "low_clip_count"
    return "ok"


def _ensure_embedding_columns(frame: pd.DataFrame) -> pd.DataFrame:
    defaults: dict[str, Any] = {
        "embedding_drift_score": 0.0,
        "embedding_outlier_share": 0.0,
        "embedding_clip_count_scored": 0,
        "embedding_baseline_size": 0,
        "embedding_baseline_level": "none",
        "embedding_backend_clip_counts": {},
        "embedding_backend_scored_clip_counts": {},
        "embedding_primary_backend": "",
        "top_detection_ids": "",
        "top_audio_artifact_paths": "",
        "background_noise_summary": "",
        "audio_quality_flag": "no_embedding_data",
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
    for column in (
        "embedding_drift_score",
        "embedding_outlier_share",
        "embedding_clip_count_scored",
        "embedding_baseline_size",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0)
    frame["audio_quality_flag"] = frame["audio_quality_flag"].fillna(
        "no_embedding_data"
    )
    frame["background_noise_summary"] = (
        frame["background_noise_summary"].fillna("").astype(str)
    )
    return frame


def _ensure_embedding_control_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if "embedding_drift_control_score" not in frame.columns:
        frame["embedding_drift_control_score"] = 0.0
    if "embedding_control_species_count" not in frame.columns:
        frame["embedding_control_species_count"] = 0
    if "embedding_control_available" not in frame.columns:
        frame["embedding_control_available"] = False
    frame["embedding_drift_control_score"] = pd.to_numeric(
        frame["embedding_drift_control_score"], errors="coerce"
    ).fillna(0.0)
    frame["embedding_control_species_count"] = (
        pd.to_numeric(frame["embedding_control_species_count"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    frame["embedding_control_available"] = (
        frame["embedding_control_available"]
        .where(frame["embedding_control_available"].notna(), False)
        .astype(bool)
    )
    return frame


def _empty_scene_control() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "location_type",
            "location_id",
            "period_start",
            *EMBEDDING_CONTROL_COLUMNS,
        ]
    )


def _week_distance(left: int, right: int) -> int:
    distance = abs(left - right)
    return min(distance, _WEEKS_PER_YEAR - distance)


def _hour_to_daypart(hour: int) -> str:
    if 5 <= hour < 8:
        return "dawn"
    if 8 <= hour < 18:
        return "day"
    if 18 <= hour < 22:
        return "dusk"
    return "night"


def _empty_weekly_scores() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "species_id",
            "location_type",
            "location_id",
            "period_start",
            *EMBEDDING_WEEKLY_COLUMNS,
        ]
    )


def _empty_cluster_scores() -> pd.DataFrame:
    return pd.DataFrame(columns=EMBEDDING_CLUSTER_COLUMNS)
