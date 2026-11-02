"""Weak-label evaluation helpers for daily modeling artifacts."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from avian_biosurveillance.config.species import registry as species_registry


REPORT_VERSION = "weak_validation_v1"

WEEKLY_CONTEXT_SPECS: dict[str, dict[str, str]] = {
    "sovon_high_mortality_context": {
        "availability_column": "sovon_has_context",
        "source": "sovon_mortality",
        "row_grain": "species_location_week_with_attached_species_month_context",
        "role": "nationwide_month_context",
        "interpretation_boundary": "Supports nationwide species-month ecological alignment only after aggregation; it cannot confirm a local weekly alert.",
    },
}

MONTHLY_CONTEXT_SPECS: dict[str, dict[str, str]] = {
    "sovon_same_month_high_mortality": {
        "availability_column": "sovon_has_context",
        "source": "sovon_mortality",
        "row_grain": "species_month_from_weekly_scores",
        "role": "nationwide_same_month_alignment",
        "interpretation_boundary": "Supports same-month nationwide species-level ecological agreement only.",
    },
    "sovon_recent_mortality_window": {
        "availability_column": "sovon_has_context",
        "source": "sovon_mortality",
        "row_grain": "species_month_from_weekly_scores",
        "role": "nationwide_lagged_alignment",
        "interpretation_boundary": "Supports lagged nationwide species-level ecological agreement only.",
    },
}

BEST_F1_ALERT_RATE_CAP = 0.30
NATIONAL_LEAD_LAG_WEEKS = tuple(range(-4, 9))
NATIONAL_F1_HORIZONS_WEEKS = (0, 1, 3, 5)
BEST_F1_SCORE_SPECS: dict[str, str] = {
    "fusion": "final_score",
    "bioacoustic": "embedding_drift_score",
    "bioacoustic_excess": "embedding_drift_excess_score",
    "bioacoustic_outlier": "embedding_outlier_share",
    "population": "population_anomaly_score",
}


def build_validation_report(
    scored: pd.DataFrame,
    *,
    top_k: int = 25,
) -> dict[str, Any]:
    """Summarize weak-label validation and concentration checks for scores."""
    frame = scored.copy()
    if frame.empty:
        return {
            "report_version": REPORT_VERSION,
            "rows_scored": 0,
            "top_k": top_k,
            "caveats": _report_caveats(),
            "alert_counts": {},
            "expected_alert_volume_under_null": {"status": "no_rows_scored"},
            "stream_counts": {},
            "weak_label_metrics": {},
            "ensemble_comparison": {},
            "alert_threshold_sweep": {},
            "non_target_species_control": {},
            "availability_stratified_metrics": {},
            "context_diagnostics": {},
            "negative_control_checks": {},
            "monthly_alignment": _empty_monthly_alignment(top_k=top_k, source_rows=0),
            "best_f1_leaderboard": {
                "role": "tiered_best_f1_scorecard",
                "national_capped_alert_rate": BEST_F1_ALERT_RATE_CAP,
                "rows": [],
                "best_by_grain_signal": [],
                "national_trend_alignment": [],
            },
            "bioacoustic_coverage": _bioacoustic_coverage_diagnostics(frame),
            "top_alerts": [],
        }

    final_score = _numeric_column(frame, "final_score", 0.0)
    frame["_final_score"] = final_score
    top_frame = frame.sort_values("_final_score", ascending=False).head(top_k)

    weak_labels = {
        "sovon_high_mortality_context": _numeric_column(
            frame, "sovon_lag_0_to_3mo_max_score", 0.0
        )
        >= 0.90,
    }

    weak_label_metrics = {
        name: _binary_ranking_metrics(final_score, label, top_k=top_k)
        for name, label in weak_labels.items()
    }

    monthly_alignment = _monthly_alignment_report(frame, top_k=top_k)
    tiered_f1 = _tiered_best_f1_report(
        frame,
        monthly_alignment=monthly_alignment,
    )

    return {
        "report_version": REPORT_VERSION,
        "rows_scored": len(frame),
        "top_k": int(top_k),
        "caveats": _report_caveats(),
        "score_summary": _score_summary(final_score),
        "alert_counts": _value_counts(frame, "alert_level"),
        "expected_alert_volume_under_null": _expected_alert_volume_under_null(frame),
        "stream_counts": _stream_counts(frame),
        "weak_label_metrics": weak_label_metrics,
        "ensemble_comparison": _ensemble_comparison(
            frame,
            weak_labels=weak_labels,
            top_k=top_k,
        ),
        "alert_threshold_sweep": _alert_threshold_sweep(
            final_score=final_score,
            weak_labels=weak_labels,
        ),
        "non_target_species_control": _non_target_species_control(
            frame,
            weak_labels=weak_labels,
            top_k=top_k,
        ),
        "availability_stratified_metrics": _availability_stratified_metrics(
            frame,
            final_score=final_score,
            weak_labels=weak_labels,
            top_k=top_k,
            context_specs=WEEKLY_CONTEXT_SPECS,
        ),
        "context_diagnostics": _context_diagnostics(
            frame,
            weak_labels=weak_labels,
            context_specs=WEEKLY_CONTEXT_SPECS,
        ),
        "negative_control_checks": _negative_control_checks(
            frame,
            final_score=final_score,
            weak_labels=weak_labels,
            top_frame=top_frame,
            top_k=top_k,
        ),
        "monthly_alignment": monthly_alignment,
        "best_f1_leaderboard": tiered_f1,
        "bioacoustic_coverage": _bioacoustic_coverage_diagnostics(frame),
        "top_alerts": _top_alerts(top_frame),
    }


def _monthly_alignment_report(frame: pd.DataFrame, *, top_k: int) -> dict[str, Any]:
    monthly = _aggregate_species_month_scores(frame)
    if monthly.empty:
        return _empty_monthly_alignment(top_k=top_k, source_rows=len(frame))

    monthly_score = _numeric_column(monthly, "monthly_peak_final_score", 0.0)
    monthly["_monthly_peak_final_score"] = monthly_score
    top_frame = monthly.sort_values(
        "_monthly_peak_final_score",
        ascending=False,
    ).head(top_k)

    weak_labels = {
        "sovon_same_month_high_mortality": _truthy_column(
            monthly,
            "sovon_mortality_top_decile_flag",
        )
        | (_numeric_column(monthly, "sovon_mortality_score", 0.0) >= 0.90),
        "sovon_recent_mortality_window": _numeric_column(
            monthly,
            "sovon_lag_0_to_3mo_max_score",
            0.0,
        )
        >= 0.90,
    }
    same_month_label = weak_labels["sovon_same_month_high_mortality"]

    return {
        "row_grain": "species_month_from_weekly_scores",
        "source_week_rows": len(frame),
        "rows_scored": len(monthly),
        "top_k": int(top_k),
        "score_summary": _score_summary(monthly_score),
        "weak_label_metrics": {
            name: _binary_ranking_metrics(monthly_score, label, top_k=top_k)
            for name, label in weak_labels.items()
        },
        "continuous_metrics": _monthly_continuous_metrics(monthly),
        "lead_lag_alignment": _monthly_lead_lag_alignment(monthly),
        "outbreak_month_hit_rate": _monthly_outbreak_hit_rate(
            monthly,
            monthly_score=monthly_score,
            positive_month_label=same_month_label,
            top_k=top_k,
        ),
        "availability_stratified_metrics": _availability_stratified_metrics(
            monthly,
            final_score=monthly_score,
            weak_labels=weak_labels,
            top_k=top_k,
            context_specs=MONTHLY_CONTEXT_SPECS,
        ),
        "context_diagnostics": _context_diagnostics(
            monthly,
            weak_labels=weak_labels,
            context_specs=MONTHLY_CONTEXT_SPECS,
        ),
        "negative_control_checks": _negative_control_checks(
            monthly,
            final_score=monthly_score,
            weak_labels=weak_labels,
            top_frame=top_frame,
            top_k=top_k,
            group_column="month_start",
        ),
        "top_months": _top_months(top_frame),
    }


def _aggregate_species_month_scores(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()

    working = frame.copy()
    month_start = _month_start_column(working)
    if month_start is None:
        return pd.DataFrame()
    working["month_start"] = month_start
    working = working.loc[working["month_start"].notna()].copy()
    if working.empty:
        return pd.DataFrame()

    species_column = "species_id" if "species_id" in working.columns else None
    if species_column is None:
        working["species_id"] = "all"
        species_column = "species_id"

    working["_final_score"] = _numeric_column(working, "final_score", 0.0)
    working["_is_alerted"] = working.get("alert_level", "").isin(["amber", "red"])

    aggregations: dict[str, tuple[str, str | Any]] = {
        "monthly_peak_final_score": ("_final_score", "max"),
        "monthly_mean_final_score": ("_final_score", "mean"),
        "weeks_scored": ("_final_score", "size"),
        "weeks_alerted": ("_is_alerted", "sum"),
    }

    if "location_id" in working.columns:
        aggregations["locations_scored"] = ("location_id", "nunique")
    if "species_english" in working.columns:
        aggregations["species_english"] = ("species_english", _first_non_missing)
    if "sovon_species_key" in working.columns:
        aggregations["sovon_species_key"] = (
            "sovon_species_key",
            _first_non_missing,
        )
    if "sovon_mortality_score" in working.columns:
        aggregations["sovon_mortality_score"] = ("sovon_mortality_score", "max")
    if "sovon_dead_bird_count" in working.columns:
        aggregations["sovon_dead_bird_count"] = ("sovon_dead_bird_count", "max")
    if "sovon_n_total" in working.columns:
        aggregations["sovon_n_total"] = ("sovon_n_total", "max")
    if "sovon_mortality_rate" in working.columns:
        aggregations["sovon_mortality_rate"] = ("sovon_mortality_rate", "max")
    if "sovon_mortality_top_decile_flag" in working.columns:
        aggregations["sovon_mortality_top_decile_flag"] = (
            "sovon_mortality_top_decile_flag",
            "max",
        )
    if "sovon_lag_0_to_3mo_max_score" in working.columns:
        aggregations["sovon_lag_0_to_3mo_max_score"] = (
            "sovon_lag_0_to_3mo_max_score",
            "max",
        )
    if "sovon_has_context" in working.columns:
        aggregations["sovon_has_context"] = ("sovon_has_context", "max")
    if "population_anomaly_score" in working.columns:
        aggregations["monthly_peak_population_anomaly_score"] = (
            "population_anomaly_score",
            "max",
        )
    elif "count_anomaly_score" in working.columns:
        aggregations["monthly_peak_population_anomaly_score"] = (
            "count_anomaly_score",
            "max",
        )
    if "embedding_drift_score" in working.columns:
        aggregations["monthly_peak_embedding_drift_score"] = (
            "embedding_drift_score",
            "max",
        )

    monthly = (
        working.groupby([species_column, "month_start"], dropna=False)
        .agg(**aggregations)
        .reset_index()
    )
    monthly["month_start"] = pd.to_datetime(monthly["month_start"]).dt.normalize()
    return monthly


def _month_start_column(frame: pd.DataFrame) -> pd.Series | None:
    if "month_start" in frame.columns:
        month_start = pd.to_datetime(frame["month_start"], errors="coerce")
    elif "period_start" in frame.columns:
        month_start = pd.to_datetime(frame["period_start"], errors="coerce")
    else:
        return None
    return month_start.dt.to_period("M").dt.to_timestamp()


def _empty_monthly_alignment(*, top_k: int, source_rows: int) -> dict[str, Any]:
    return {
        "row_grain": "species_month_from_weekly_scores",
        "source_week_rows": int(source_rows),
        "rows_scored": 0,
        "top_k": int(top_k),
        "weak_label_metrics": {},
        "continuous_metrics": {},
        "lead_lag_alignment": {},
        "outbreak_month_hit_rate": {},
        "availability_stratified_metrics": {},
        "context_diagnostics": {},
        "negative_control_checks": {},
        "top_months": [],
    }


def _tiered_best_f1_report(
    frame: pd.DataFrame,
    *,
    monthly_alignment: dict[str, Any],
) -> dict[str, Any]:
    """Rank the best F1 operating points for national trend-alignment claims."""
    prepared = _ensure_best_f1_score_columns(frame.copy())
    national = _national_best_f1_report(prepared)
    rows = list(national["rows"])
    rows.sort(
        key=lambda row: (
            float(row.get("f1") or -1.0),
            float(row.get("recall") or -1.0),
            -float(row.get("alert_rate") or 1.0),
        ),
        reverse=True,
    )
    return {
        "role": "tiered_best_f1_scorecard",
        "threshold_policy": (
            "Rows marked label_optimized=true choose a threshold against weak "
            "labels. Use them for reported F1/operating-point selection, not as "
            "evidence that the unsupervised score was trained on labels."
        ),
        "national_capped_alert_rate": BEST_F1_ALERT_RATE_CAP,
        "rows": rows[:100],
        "best_by_grain_signal": _best_by_grain_signal(rows),
        "national_trend_alignment": national["lead_lag_scan"],
        "monthly_alignment_context": {
            "rows_scored": monthly_alignment.get("rows_scored", 0),
            "role": "existing_species_month_context",
        },
    }


def _ensure_best_f1_score_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    if (
        "embedding_drift_excess_score" not in frame.columns
        and "embedding_drift_score" in frame.columns
    ):
        drift = _numeric_column(frame, "embedding_drift_score", 0.0)
        control = _numeric_column(frame, "embedding_drift_control_score", 0.0)
        frame["embedding_drift_excess_score"] = (drift - control).clip(lower=0.0)
    return frame


def _bioacoustic_coverage_diagnostics(frame: pd.DataFrame) -> dict[str, Any]:
    """Summarize acoustic-score coverage and weak-label overlap."""
    if frame.empty:
        return {
            "role": "bioacoustic_coverage_diagnostics",
            "rows_scored": 0,
            "embedding_rows": 0,
            "embedding_clip_count_scored": 0,
            "by_embedding_backend": [],
            "by_week": [],
            "by_grid": [],
            "by_species": [],
            "overlap": {},
        }

    work = frame.copy()
    if "period_start" in work.columns:
        work["period_start"] = pd.to_datetime(
            work["period_start"],
            errors="coerce",
        ).dt.normalize()
    embedding_rows = _numeric_column(work, "embedding_clip_count_scored", 0.0) > 0
    if not bool(embedding_rows.any()) and "embedding_drift_score" in work.columns:
        embedding_rows = _numeric_column(work, "embedding_drift_score", 0.0) > 0
    sovon_positive = (
        (_numeric_column(work, "sovon_mortality_score", 0.0) >= 0.9)
        | _truthy_column(work, "sovon_mortality_top_decile_flag")
    )
    work["_has_embedding"] = embedding_rows
    work["_sovon_context"] = _availability_mask(work, "sovon_has_context")
    work["_sovon_positive"] = sovon_positive

    backend_clip_counts = _aggregate_backend_count_column(
        work,
        "embedding_backend_clip_counts",
    )
    backend_scored_counts = _aggregate_backend_count_column(
        work,
        "embedding_backend_scored_clip_counts",
    )
    if not backend_scored_counts and "embedding_primary_backend" in work.columns:
        backend_scored_counts = _aggregate_primary_backend_counts(work)

    backends = sorted(
        set(backend_clip_counts) | set(backend_scored_counts),
        key=lambda backend: (
            backend_scored_counts.get(backend, 0),
            backend_clip_counts.get(backend, 0),
            backend,
        ),
        reverse=True,
    )

    return {
        "role": "bioacoustic_coverage_diagnostics",
        "rows_scored": len(work),
        "embedding_rows": int(embedding_rows.sum()),
        "embedding_clip_count_scored": int(
            _numeric_column(work, "embedding_clip_count_scored", 0.0).sum()
        ),
        "by_embedding_backend": [
            {
                "embedding_backend": backend,
                "clip_rows": int(backend_clip_counts.get(backend, 0)),
                "scored_clip_rows": int(backend_scored_counts.get(backend, 0)),
            }
            for backend in backends
        ],
        "by_week": _coverage_group_rows(work, ["period_start"], limit=120),
        "by_grid": _coverage_group_rows(work, ["grid_cell_id"], limit=50),
        "by_species": _coverage_group_rows(work, ["species_id"], limit=50),
        "overlap": {
            "embedding_and_sovon_context_rows": int(
                (work["_has_embedding"] & work["_sovon_context"]).sum()
            ),
            "embedding_and_sovon_mortality_rows": int(
                (work["_has_embedding"] & work["_sovon_positive"]).sum()
            ),
        },
    }


def _aggregate_backend_count_column(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if column not in frame.columns:
        return {}
    counts: dict[str, int] = {}
    for value in frame[column].tolist():
        payload = _coerce_mapping(value)
        for backend, count in payload.items():
            backend_name = str(backend).strip() or "unknown"
            try:
                parsed = int(float(count))
            except (TypeError, ValueError):
                continue
            counts[backend_name] = counts.get(backend_name, 0) + max(parsed, 0)
    return counts


def _aggregate_primary_backend_counts(frame: pd.DataFrame) -> dict[str, int]:
    counts: dict[str, int] = {}
    scored = _numeric_column(frame, "embedding_clip_count_scored", 0.0)
    for index, value in frame["embedding_primary_backend"].items():
        backend = str(value).strip() if value is not None and not pd.isna(value) else ""
        if not backend:
            continue
        counts[backend] = counts.get(backend, 0) + int(scored.loc[index])
    return counts


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coverage_group_rows(
    frame: pd.DataFrame,
    group_columns: list[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not set(group_columns).issubset(frame.columns):
        return []
    rows: list[dict[str, Any]] = []
    clip_counts = _numeric_column(frame, "embedding_clip_count_scored", 0.0)
    for keys, group in frame.groupby(group_columns, dropna=False, sort=True):
        key_values = keys if isinstance(keys, tuple) else (keys,)
        record = {
            str(column): _json_value(value)
            for column, value in zip(group_columns, key_values, strict=False)
        }
        record.update(
            {
                "rows_scored": len(group),
                "embedding_rows": int(group["_has_embedding"].sum()),
                "embedding_clip_count_scored": int(clip_counts.loc[group.index].sum()),
                "sovon_context_rows": int(group["_sovon_context"].sum()),
                "sovon_mortality_rows": int(group["_sovon_positive"].sum()),
            }
        )
        rows.append(record)
    rows.sort(
        key=lambda row: (
            int(row["embedding_rows"]),
            int(row["embedding_clip_count_scored"]),
            int(row["sovon_mortality_rows"]),
        ),
        reverse=True,
    )
    return rows[:limit]


def _national_best_f1_report(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": [], "lead_lag_scan": []}

    rows: list[dict[str, Any]] = []
    lead_lag_scan: list[dict[str, Any]] = []
    for grain in ("national_week", "national_month"):
        national = _national_period_frame(frame, grain=grain)
        if national.empty:
            continue
        horizons = NATIONAL_F1_HORIZONS_WEEKS if grain == "national_week" else (0,)
        for signal, score_column in BEST_F1_SCORE_SPECS.items():
            for aggregation in ("mean", "max", "top_decile_mean"):
                national_score_column = f"{score_column}__{aggregation}"
                if national_score_column not in national.columns:
                    continue
                score = pd.to_numeric(
                    national[national_score_column],
                    errors="coerce",
                ).fillna(0.0)
                for label_name in _national_label_names(national):
                    event = national[label_name].fillna(False).astype(bool)
                    for horizon in horizons:
                        label = _forward_label_by_period(
                            national["period_start"],
                            event,
                            horizon_weeks=horizon,
                        )
                        for decision_rule, cap, upper_bound in (
                            ("max_f1_capped", BEST_F1_ALERT_RATE_CAP, False),
                            ("max_f1_uncapped_upper_bound", 1.0, True),
                        ):
                            metric = _best_f1_for_score(
                                score,
                                label,
                                max_alert_rate=cap,
                            )
                            if metric["status"] != "ok":
                                continue
                            rows.append(
                                {
                                    "claim_type": "national_trend_alignment",
                                    "grain": grain,
                                    "signal": signal,
                                    "score_column": score_column,
                                    "score_aggregation": aggregation,
                                    "label": label_name,
                                    "horizon_weeks": horizon,
                                    "decision_rule": decision_rule,
                                    "alert_level": "optimized",
                                    "label_optimized": True,
                                    "upper_bound": upper_bound,
                                    "f1": metric["f1"],
                                    "precision": metric["precision"],
                                    "recall": metric["recall"],
                                    "alert_rate": metric["alert_rate"],
                                    "threshold": metric["threshold"],
                                    "positive_rows": metric["positive_rows"],
                                    "negative_rows": metric["negative_rows"],
                                    "common_rows": metric["common_rows"],
                                    "common_units": 1,
                                    "true_positive_rows": metric[
                                        "true_positive_rows"
                                    ],
                                    "false_positive_rows": metric[
                                        "false_positive_rows"
                                    ],
                                    "false_negative_rows": metric[
                                        "false_negative_rows"
                                    ],
                                    "interpretation": (
                                        "National trend alignment only; this is "
                                        "not a grid-cell early-warning claim."
                                    ),
                                }
                            )
                    if grain == "national_week":
                        lead_lag_scan.extend(
                            _national_lead_lag_rows(
                                national,
                                score=score,
                                signal=signal,
                                score_aggregation=aggregation,
                                label_name=label_name,
                                event=event,
                            )
                        )
    return {"rows": rows, "lead_lag_scan": lead_lag_scan}


def _national_period_frame(frame: pd.DataFrame, *, grain: str) -> pd.DataFrame:
    work = frame.copy()
    if "period_start" not in work.columns:
        return pd.DataFrame()
    period = pd.to_datetime(work["period_start"], errors="coerce").dt.normalize()
    if grain == "national_month":
        period = period.dt.to_period("M").dt.to_timestamp()
    work["period_start"] = period
    work = work.loc[work["period_start"].notna()].copy()
    if work.empty:
        return pd.DataFrame()

    score_payload = {"period_start": sorted(work["period_start"].dropna().unique())}
    national = pd.DataFrame(score_payload)
    for score_column in BEST_F1_SCORE_SPECS.values():
        if score_column not in work.columns:
            continue
        values = pd.to_numeric(work[score_column], errors="coerce").fillna(0.0)
        scored = work.assign(_score=values)
        grouped = scored.groupby("period_start", dropna=False)["_score"]
        national[f"{score_column}__mean"] = national["period_start"].map(
            grouped.mean()
        )
        national[f"{score_column}__max"] = national["period_start"].map(grouped.max())
        national[f"{score_column}__top_decile_mean"] = national["period_start"].map(
            grouped.apply(_top_decile_mean)
        )

    labels = _national_label_frame(work)
    if labels.empty:
        return national
    merged = national.merge(labels, on="period_start", how="left")
    for label_name in _national_label_names(merged):
        merged[label_name] = merged[label_name].fillna(False).astype(bool)
    return merged


def _national_label_frame(work: pd.DataFrame) -> pd.DataFrame:
    periods = pd.DataFrame({"period_start": sorted(work["period_start"].unique())})
    if "sovon_mortality_score" in work.columns:
        work["sovon_mortality_score"] = pd.to_numeric(
            work["sovon_mortality_score"],
            errors="coerce",
        ).fillna(0.0)
        species_columns = [
            column
            for column in ("period_start", "species_id")
            if column in work.columns
        ]
        species_mortality = (
            work.groupby(species_columns, dropna=False)["sovon_mortality_score"]
            .max()
            .reset_index()
        )
        national_mortality = (
            species_mortality.groupby("period_start", dropna=False)[
                "sovon_mortality_score"
            ]
            .max()
            .reset_index()
        )
        periods = periods.merge(national_mortality, on="period_start", how="left")

    periods["label_sovon_mortality"] = (
        _numeric_column(periods, "sovon_mortality_score", 0.0) >= 0.9
    )
    return periods[
        [
            "period_start",
            *_national_label_names(periods),
        ]
    ]


def _national_label_names(frame: pd.DataFrame) -> list[str]:
    return [
        column for column in ("label_sovon_mortality",) if column in frame.columns
    ]


def _top_decile_mean(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return 0.0
    threshold = float(numeric.quantile(0.90))
    return float(numeric.loc[numeric >= threshold].mean())


def _forward_label_by_period(
    periods: pd.Series,
    event: pd.Series,
    *,
    horizon_weeks: int,
) -> pd.Series:
    period_values = pd.to_datetime(periods, errors="coerce")
    event_values = event.reindex(periods.index).fillna(False).astype(bool)
    out = pd.Series(False, index=periods.index)
    for index, period in period_values.items():
        if pd.isna(period):
            continue
        end = period + timedelta(weeks=horizon_weeks)
        out.loc[index] = bool(
            event_values.loc[(period_values >= period) & (period_values <= end)].any()
        )
    return out


def _best_f1_for_score(
    score: pd.Series,
    label: pd.Series,
    *,
    max_alert_rate: float,
) -> dict[str, Any]:
    score = pd.to_numeric(score, errors="coerce").fillna(0.0)
    label = label.reindex(score.index).fillna(False).astype(bool)
    common = score.notna()
    n_common = int(common.sum())
    n_positive = int((label & common).sum())
    n_negative = int((~label & common).sum())
    if n_common == 0:
        return {"status": "no_common_coverage"}
    if n_positive == 0:
        return {"status": "no_positives"}
    if n_negative == 0:
        return {"status": "no_contrast"}

    eligible = score.loc[common]
    ranked = eligible.loc[eligible > 0].sort_values(ascending=False, kind="mergesort")
    if ranked.empty:
        return {"status": "no_positive_scores"}

    max_alerts = min(len(ranked), math.floor(n_common * max_alert_rate))
    if max_alert_rate > 0 and max_alerts <= 0:
        max_alerts = 1

    best: dict[str, Any] | None = None
    for n_alert in range(1, max_alerts + 1):
        selected = ranked.head(n_alert)
        pred = pd.Series(False, index=score.index)
        pred.loc[selected.index] = True
        tp = int((pred & label & common).sum())
        fp = int((pred & ~label & common).sum())
        fn = int((~pred & label & common).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        candidate = {
            "status": "ok",
            "f1": _safe_float(f1),
            "precision": _safe_float(precision),
            "recall": _safe_float(recall),
            "alert_rate": _safe_float(n_alert / n_common),
            "threshold": _safe_float(float(selected.min())),
            "positive_rows": n_positive,
            "negative_rows": n_negative,
            "common_rows": n_common,
            "true_positive_rows": tp,
            "false_positive_rows": fp,
            "false_negative_rows": fn,
        }
        if best is None or (
            (candidate["f1"], candidate["recall"], -n_alert)
            > (best["f1"], best["recall"], -best["alert_rows"])
        ):
            best = {**candidate, "alert_rows": n_alert}
    return best or {"status": "no_threshold"}


def _national_lead_lag_rows(
    national: pd.DataFrame,
    *,
    score: pd.Series,
    signal: str,
    score_aggregation: str,
    label_name: str,
    event: pd.Series,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    order = national.sort_values("period_start", kind="mergesort").index
    score_ordered = score.loc[order].reset_index(drop=True)
    event_ordered = event.loc[order].fillna(False).astype(int).reset_index(drop=True)
    for lag_weeks in NATIONAL_LEAD_LAG_WEEKS:
        # Positive lag means the score leads the event by that many weekly rows.
        target = event_ordered.shift(-lag_weeks)
        available = score_ordered.notna() & target.notna()
        if int(available.sum()) < 3:
            rho = None
        else:
            rho = _series_correlation(
                score_ordered.loc[available],
                target.loc[available],
                method="spearman",
            )
        rows.append(
            {
                "grain": "national_week",
                "signal": signal,
                "score_aggregation": score_aggregation,
                "label": label_name,
                "lag_weeks": lag_weeks,
                "spearman_rho": rho,
                "available_rows": int(available.sum()),
                "positive_rows": (
                    int(target.loc[available].sum()) if int(available.sum()) else 0
                ),
                "interpretation": (
                    "Positive lag means anomaly score leads the national event "
                    "series; negative lag means it follows."
                ),
            }
        )
    return rows


def _best_by_grain_signal(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("grain") or ""), str(row.get("signal") or ""))
        current = best.get(key)
        if current is None or float(row.get("f1") or -1.0) > float(
            current.get("f1") or -1.0
        ):
            best[key] = row
    return sorted(
        best.values(),
        key=lambda row: float(row.get("f1") or -1.0),
        reverse=True,
    )


def _monthly_continuous_metrics(monthly: pd.DataFrame) -> dict[str, Any]:
    specs = {
        "peak_final_score_vs_mortality_score": (
            "monthly_peak_final_score",
            "sovon_mortality_score",
        ),
        "mean_final_score_vs_mortality_score": (
            "monthly_mean_final_score",
            "sovon_mortality_score",
        ),
        "peak_final_score_vs_dead_bird_count": (
            "monthly_peak_final_score",
            "sovon_dead_bird_count",
        ),
        "peak_population_score_vs_mortality_score": (
            "monthly_peak_population_anomaly_score",
            "sovon_mortality_score",
        ),
        "peak_embedding_score_vs_mortality_score": (
            "monthly_peak_embedding_drift_score",
            "sovon_mortality_score",
        ),
    }
    metrics: dict[str, Any] = {}
    for name, (score_column, target_column) in specs.items():
        if score_column not in monthly.columns or target_column not in monthly.columns:
            continue
        metrics[name] = _continuous_alignment_metric(
            monthly,
            score_column=score_column,
            target_column=target_column,
            source="sovon_mortality",
            role="nationwide_continuous_burden_alignment",
            interpretation_boundary="Supports nationwide species-month ecological agreement only; it cannot confirm local weekly alerts or disease diagnosis.",
        )
    return metrics


def _monthly_lead_lag_alignment(monthly: pd.DataFrame) -> dict[str, Any]:
    score_column = "monthly_peak_final_score"
    target_column = "sovon_mortality_score"
    if score_column not in monthly.columns or target_column not in monthly.columns:
        return {}

    by_lag: dict[str, Any] = {}
    best_lag_months: int | None = None
    best_spearman_rho: float | None = None
    best_absolute_rho = -1.0
    best_available_rows = -1
    score = _optional_numeric_column(monthly, score_column)
    for lag_months in range(-2, 3):
        aligned_target = _shifted_species_month_target(
            monthly,
            target_column=target_column,
            lag_months=lag_months,
        )
        metric = _continuous_alignment_summary(score, aligned_target)
        by_lag[str(lag_months)] = {"lag_months": lag_months, **metric}
        rho = metric["spearman_rho"]
        if rho is None:
            continue
        available_rows = int(metric["available_rows"])
        if (
            abs(rho) > best_absolute_rho
            or (
                math.isclose(abs(rho), best_absolute_rho)
                and available_rows > best_available_rows
            )
            or (
                math.isclose(abs(rho), best_absolute_rho)
                and available_rows == best_available_rows
                and best_lag_months is not None
                and abs(lag_months) < abs(best_lag_months)
            )
        ):
            best_absolute_rho = abs(rho)
            best_available_rows = available_rows
            best_lag_months = lag_months
            best_spearman_rho = rho

    same_month = by_lag.get(
        "0", {"available_rows": 0, "positive_rows": 0, "spearman_rho": None}
    )
    return {
        "source": "sovon_mortality",
        "role": "nationwide_lead_lag_alignment",
        "row_grain": "species_month_from_weekly_scores",
        "interpretation_boundary": "Compares nationwide species-month anomaly burden against Sovon mortality at shifted month offsets only; positive lag means mortality occurs after the anomaly month.",
        "score_column": score_column,
        "target_column": target_column,
        "available_rows": int(same_month["available_rows"]),
        "positive_rows": int(same_month["positive_rows"]),
        "metrics_by_lag_months": by_lag,
        "best_spearman_lag_months": best_lag_months,
        "best_spearman_rho": best_spearman_rho,
        "same_month_spearman_rho": same_month["spearman_rho"],
    }


def _monthly_outbreak_hit_rate(
    monthly: pd.DataFrame,
    *,
    monthly_score: pd.Series,
    positive_month_label: pd.Series,
    top_k: int,
) -> dict[str, Any]:
    positive = positive_month_label.fillna(False).astype(bool)
    available = _sovon_month_availability(monthly)
    alerted = _numeric_column(monthly, "weeks_alerted", 0.0) > 0
    top_n = min(max(top_k, 1), len(monthly))
    top_positive = positive.loc[
        monthly_score.sort_values(ascending=False).head(top_n).index
    ]
    positive_available = positive.loc[available]
    alerted_positive_months = int((positive & alerted & available).sum())
    positive_rows = int(positive_available.sum())
    return {
        "source": "sovon_mortality",
        "role": "nationwide_outbreak_month_recall",
        "row_grain": "species_month_from_weekly_scores",
        "interpretation_boundary": "Tracks whether high-mortality species-months are surfaced by any alerted week or by the top-ranked monthly queue; it does not validate local weekly geometry.",
        "available_rows": int(available.sum()),
        "positive_rows": positive_rows,
        "months_with_any_alert": int((alerted & available).sum()),
        "alerted_positive_months": alerted_positive_months,
        "alerted_positive_share": _safe_ratio(alerted_positive_months, positive_rows),
        "top_k": int(top_n),
        "positives_in_top_k": int(top_positive.sum()),
        "top_k_hit_rate": _safe_ratio(int(top_positive.sum()), positive_rows),
    }


def _continuous_alignment_metric(
    frame: pd.DataFrame,
    *,
    score_column: str,
    target_column: str,
    source: str,
    role: str,
    interpretation_boundary: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "role": role,
        "row_grain": "species_month_from_weekly_scores",
        "interpretation_boundary": interpretation_boundary,
        "score_column": score_column,
        "target_column": target_column,
        **_continuous_alignment_summary(
            _optional_numeric_column(frame, score_column),
            _optional_numeric_column(frame, target_column),
        ),
    }


def _continuous_alignment_summary(
    score: pd.Series,
    target: pd.Series,
) -> dict[str, Any]:
    aligned = pd.DataFrame(
        {
            "score": pd.to_numeric(score, errors="coerce"),
            "target": pd.to_numeric(target, errors="coerce"),
        }
    ).dropna()
    if aligned.empty:
        return {
            "available_rows": 0,
            "positive_rows": 0,
            "spearman_rho": None,
            "pearson_r": None,
        }

    target_positive = int((aligned["target"] > 0).sum())
    return {
        "available_rows": len(aligned),
        "positive_rows": target_positive,
        "spearman_rho": _series_correlation(
            aligned["score"], aligned["target"], method="spearman"
        ),
        "pearson_r": _series_correlation(
            aligned["score"], aligned["target"], method="pearson"
        ),
    }


def _optional_numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(pd.NA, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _series_correlation(
    left: pd.Series,
    right: pd.Series,
    *,
    method: str,
) -> float | None:
    aligned = pd.DataFrame(
        {
            "left": pd.to_numeric(left, errors="coerce"),
            "right": pd.to_numeric(right, errors="coerce"),
        }
    ).dropna()
    if len(aligned) < 2:
        return None
    if (
        aligned["left"].min() == aligned["left"].max()
        or aligned["right"].min() == aligned["right"].max()
    ):
        return None
    return _safe_float(aligned["left"].corr(aligned["right"], method=method))


def _shifted_species_month_target(
    monthly: pd.DataFrame,
    *,
    target_column: str,
    lag_months: int,
) -> pd.Series:
    if (
        target_column not in monthly.columns
        or "species_id" not in monthly.columns
        or "month_start" not in monthly.columns
    ):
        return pd.Series(pd.NA, index=monthly.index, dtype="float64")

    base = monthly[["species_id", "month_start"]].copy()
    base["row_index"] = monthly.index
    targets = monthly[["species_id", "month_start", target_column]].copy()
    targets["month_start"] = pd.to_datetime(targets["month_start"], errors="coerce")
    targets["aligned_month_start"] = targets["month_start"] - pd.DateOffset(
        months=lag_months
    )
    merged = base.merge(
        targets[["species_id", "aligned_month_start", target_column]],
        left_on=["species_id", "month_start"],
        right_on=["species_id", "aligned_month_start"],
        how="left",
    )
    return pd.to_numeric(
        merged.set_index("row_index")[target_column].reindex(monthly.index),
        errors="coerce",
    )


def _sovon_month_availability(monthly: pd.DataFrame) -> pd.Series:
    if "sovon_has_context" in monthly.columns:
        return _truthy_column(monthly, "sovon_has_context")
    for column in (
        "sovon_mortality_score",
        "sovon_dead_bird_count",
        "sovon_mortality_rate",
        "sovon_mortality_top_decile_flag",
    ):
        if column in monthly.columns:
            return monthly[column].notna()
    return pd.Series(False, index=monthly.index, dtype="bool")


def _availability_stratified_metrics(
    frame: pd.DataFrame,
    *,
    final_score: pd.Series,
    weak_labels: dict[str, pd.Series],
    top_k: int,
    context_specs: dict[str, dict[str, str]],
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name, label in weak_labels.items():
        spec = context_specs.get(name, {})
        availability_column = spec.get("availability_column")
        availability = _availability_mask(frame, availability_column)
        available_rows = int(availability.sum())
        metrics[name] = {
            "availability_column": availability_column,
            "availability_known": _availability_known(frame, availability_column),
            "available_rows": available_rows,
            "available_share": _safe_ratio(available_rows, len(frame)),
            "metrics_on_available_rows": (
                _binary_ranking_metrics(
                    final_score.loc[availability],
                    label.loc[availability],
                    top_k=top_k,
                )
                if available_rows
                else _empty_binary_ranking_metrics()
            ),
        }
    return metrics


def _context_diagnostics(
    frame: pd.DataFrame,
    *,
    weak_labels: dict[str, pd.Series],
    context_specs: dict[str, dict[str, str]],
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for name, label in weak_labels.items():
        spec = context_specs.get(name, {})
        availability_column = spec.get("availability_column")
        availability = _availability_mask(frame, availability_column)
        available_rows = int(availability.sum())
        label_bool = label.fillna(False).astype(bool)
        available_label = label_bool.loc[availability]
        diagnostics[name] = {
            "source": spec.get("source", name),
            "role": spec.get("role", "weak_context"),
            "row_grain": spec.get("row_grain", "unknown"),
            "availability_column": availability_column,
            "availability_known": _availability_known(frame, availability_column),
            "available_rows": available_rows,
            "unavailable_rows": int(len(frame) - available_rows),
            "available_share": _safe_ratio(available_rows, len(frame)),
            "positive_rows_all": int(label_bool.sum()),
            "positive_rows_available": int(available_label.sum()),
            "positive_share_available": _safe_ratio(
                int(available_label.sum()),
                available_rows,
            ),
            "interpretation_boundary": spec.get("interpretation_boundary", ""),
        }
    return diagnostics


def _availability_known(frame: pd.DataFrame, availability_column: str | None) -> bool:
    return bool(availability_column) and availability_column in frame.columns


def _availability_mask(
    frame: pd.DataFrame,
    availability_column: str | None,
) -> pd.Series:
    if not _availability_known(frame, availability_column):
        return pd.Series(False, index=frame.index, dtype="bool")
    return _truthy_column(frame, str(availability_column))


def _empty_binary_ranking_metrics() -> dict[str, Any]:
    return {
        "positive_rows": 0,
        "negative_rows": 0,
        "mean_positive_score": None,
        "mean_negative_score": None,
        "score_lift": None,
        "precision_at_k": None,
        "positives_in_top_k": 0,
        "auroc": None,
        "average_precision": None,
    }


def _binary_ranking_metrics(
    score: pd.Series,
    label: pd.Series,
    *,
    top_k: int,
) -> dict[str, Any]:
    label = label.fillna(False).astype(bool)
    score = pd.to_numeric(score, errors="coerce").fillna(0.0)
    positive_rows = int(label.sum())
    negative_rows = int((~label).sum())
    top_n = min(max(top_k, 1), len(score))
    top_label = label.loc[score.sort_values(ascending=False).head(top_n).index]

    mean_positive = _mean_or_none(score.loc[label])
    mean_negative = _mean_or_none(score.loc[~label])
    return {
        "positive_rows": positive_rows,
        "negative_rows": negative_rows,
        "mean_positive_score": mean_positive,
        "mean_negative_score": mean_negative,
        "score_lift": _subtract_or_none(mean_positive, mean_negative),
        "precision_at_k": _safe_float(top_label.mean()) if top_n else None,
        "positives_in_top_k": int(top_label.sum()),
        "auroc": _auroc(score, label),
        "average_precision": _average_precision(score, label),
    }


def _auroc(score: pd.Series, label: pd.Series) -> float | None:
    label = label.astype(bool)
    positive_count = int(label.sum())
    negative_count = int((~label).sum())
    if positive_count == 0 or negative_count == 0:
        return None

    ranks = score.rank(method="average", ascending=True)
    positive_rank_sum = float(ranks.loc[label].sum())
    auc = (positive_rank_sum - positive_count * (positive_count + 1) / 2) / (
        positive_count * negative_count
    )
    return _safe_float(auc)


def _average_precision(score: pd.Series, label: pd.Series) -> float | None:
    label = label.astype(bool)
    positive_count = int(label.sum())
    if positive_count == 0:
        return None

    ranked = pd.DataFrame({"score": score, "label": label}).sort_values(
        "score", ascending=False
    )
    cumulative_positives = ranked["label"].astype(int).cumsum()
    rank_number = pd.Series(range(1, len(ranked) + 1), index=ranked.index)
    precision_at_positive = (
        cumulative_positives.loc[ranked["label"]] / rank_number.loc[ranked["label"]]
    )
    if precision_at_positive.empty:
        return None
    return _safe_float(precision_at_positive.sum() / positive_count)


def _report_caveats() -> dict[str, Any]:
    return {
        "alert_levels_are_queue_ranks": (
            "Amber/red alert levels are per-run quantile ranks of the eligible "
            "queue (top ~5%/~1% by construction), not statistical significance "
            "or confirmed events. Alert volume is fixed by the quantile design "
            "and would appear even under pure noise."
        ),
        "multi_year_baseline_non_stationarity": (
            "Baselines pool multiple years of history and assume approximate "
            "seasonal stationarity. Long-term trends, monitoring-effort growth, "
            "and climate shifts can bias expected values; treat scores as "
            "relative anomaly ranks within a run, not absolute risk."
        ),
        "baseline_window_not_swept": (
            "seasonal_window_weeks and min_seasonal_history are fixed per run "
            "and not swept here; rerun with alternate values to assess "
            "stability."
        ),
        "weak_labels_only": (
            "All metrics use weak, retrospective SOVON mortality context labels "
            "at national species-month grain. They support ranking/triage, not "
            "diagnostic or causal claims."
        ),
    }


def _distinct_weeks(frame: pd.DataFrame) -> int:
    if "period_start" not in frame.columns:
        return 0
    weeks = pd.to_datetime(frame["period_start"], errors="coerce").dropna()
    if weeks.empty:
        return 0
    return int(weeks.dt.normalize().nunique())


def _expected_alert_volume_under_null(frame: pd.DataFrame) -> dict[str, Any]:
    """Report the by-construction alert volume implied by quantile thresholds.

    Because amber/red are assigned by per-run score quantiles, the number of
    alerts is a property of the threshold design, not of any true signal. This
    block annualises the observed volume so reviewers can calibrate expected
    queue sizes and recognise that the same volume would arise under noise.
    """
    levels = frame.get("alert_level")
    if levels is None:
        return {"status": "alert_level_unavailable"}
    levels = levels.fillna("none").astype(str)
    eligible_rows = int(levels.isin(["green", "amber", "red"]).sum())
    if eligible_rows == 0:
        return {"status": "no_eligible_rows"}
    amber_rows = int((levels == "amber").sum())
    red_rows = int((levels == "red").sum())
    weeks = _distinct_weeks(frame)
    amber_fraction = _safe_ratio(amber_rows, eligible_rows)
    red_fraction = _safe_ratio(red_rows, eligible_rows)
    rows_per_week = _safe_ratio(eligible_rows, weeks) if weeks else 0.0
    return {
        "role": "design_volume_reference",
        "eligible_rows": eligible_rows,
        "weeks_observed": weeks,
        "mean_eligible_rows_per_week": rows_per_week,
        "amber_rows": amber_rows,
        "red_rows": red_rows,
        "empirical_amber_fraction": amber_fraction,
        "empirical_red_fraction": red_fraction,
        "expected_amber_alerts_per_year_under_null": (
            _safe_float(amber_fraction * rows_per_week * 52) if weeks else None
        ),
        "expected_red_alerts_per_year_under_null": (
            _safe_float(red_fraction * rows_per_week * 52) if weeks else None
        ),
        "note": "Alert counts follow directly from the per-run quantile cutoffs; this volume would be produced even by random scores, so it sets reviewer expectations rather than indicating true events.",
    }


def _ensemble_comparison(
    frame: pd.DataFrame,
    *,
    weak_labels: dict[str, pd.Series],
    top_k: int,
) -> dict[str, Any]:
    """Compare the IsolationForest fusion against a trivial max-of-streams rule."""
    model_score = _numeric_column(frame, "model_anomaly_score", float("nan"))
    if model_score.isna().all():
        model_score = _numeric_column(frame, "final_score", 0.0)
    else:
        model_score = model_score.fillna(0.0)
    population = _numeric_column(frame, "population_anomaly_score", 0.0)
    embedding = _numeric_column(frame, "embedding_drift_score", 0.0)
    fallback = pd.concat([population, embedding], axis=1).max(axis=1)

    per_label: dict[str, Any] = {}
    for name, label in weak_labels.items():
        model_metrics = _binary_ranking_metrics(model_score, label, top_k=top_k)
        fallback_metrics = _binary_ranking_metrics(fallback, label, top_k=top_k)
        per_label[name] = {
            "isolation_forest": {
                "auroc": model_metrics["auroc"],
                "average_precision": model_metrics["average_precision"],
            },
            "max_fallback": {
                "auroc": fallback_metrics["auroc"],
                "average_precision": fallback_metrics["average_precision"],
            },
            "auroc_lift_if_minus_fallback": _subtract_or_none(
                model_metrics["auroc"], fallback_metrics["auroc"]
            ),
            "average_precision_lift_if_minus_fallback": _subtract_or_none(
                model_metrics["average_precision"],
                fallback_metrics["average_precision"],
            ),
        }
    return {
        "role": "ensemble_value_check",
        "model_score_column": "model_anomaly_score",
        "fallback_definition": "max(population_anomaly_score, embedding_drift_score)",
        "interpretation_boundary": "Compares the IsolationForest fusion against a trivial max-of-streams fallback on weak labels; small or negative lift means the learned fusion adds little over the simple rule.",
        "per_weak_label": per_label,
    }


def _alert_threshold_sweep(
    *,
    final_score: pd.Series,
    weak_labels: dict[str, pd.Series],
) -> dict[str, Any]:
    """Show how queue volume and weak-label recall vary with alert cutoffs."""
    score = pd.to_numeric(final_score, errors="coerce").fillna(0.0)
    total_rows = len(score)
    if total_rows == 0:
        return {
            "role": "threshold_sensitivity",
            "amber_quantile_sweep": [],
            "red_quantile_sweep": [],
        }

    amber_quantiles = (0.90, 0.95, 0.97)
    red_quantiles = (0.97, 0.99, 0.995)

    amber_sweep = [
        {
            "amber_quantile": quantile,
            "score_threshold": _safe_float(float(score.quantile(quantile))),
            "flagged_rows": int((score >= float(score.quantile(quantile))).sum()),
            "flagged_share": _safe_ratio(
                int((score >= float(score.quantile(quantile))).sum()), total_rows
            ),
        }
        for quantile in amber_quantiles
    ]

    red_sweep = []
    for quantile in red_quantiles:
        threshold = float(score.quantile(quantile))
        flagged = score >= threshold
        flagged_rows = int(flagged.sum())
        recall: dict[str, Any] = {}
        for name, label in weak_labels.items():
            lab = label.fillna(False).astype(bool)
            positives = int(lab.sum())
            captured = int((flagged & lab).sum())
            recall[name] = {
                "positive_rows": positives,
                "positives_flagged": captured,
                "recall": _safe_ratio(captured, positives),
                "precision": _safe_ratio(captured, flagged_rows),
            }
        red_sweep.append(
            {
                "red_quantile": quantile,
                "score_threshold": _safe_float(threshold),
                "flagged_rows": flagged_rows,
                "flagged_share": _safe_ratio(flagged_rows, total_rows),
                "weak_label_recall": recall,
            }
        )
    return {
        "role": "threshold_sensitivity",
        "interpretation_boundary": "Shows how queue volume and weak-label recall/precision change with alert quantile cutoffs; use it to size review capacity, not to claim an optimal operating point.",
        "amber_quantile_sweep": amber_sweep,
        "red_quantile_sweep": red_sweep,
    }


def _non_target_species_control(
    frame: pd.DataFrame,
    *,
    weak_labels: dict[str, pd.Series],
    top_k: int,
) -> dict[str, Any]:
    """Compute weak-label ranking metrics on non-target species as a control."""
    if "species_id" not in frame.columns:
        return {"status": "species_id_unavailable"}
    target_ids = {str(value) for value in species_registry.birdweather_ids}
    species = frame["species_id"].astype(str)
    non_target_mask = ~species.isin(target_ids)
    non_target_rows = int(non_target_mask.sum())
    if non_target_rows == 0:
        return {
            "status": "not_available_target_only",
            "non_target_rows": 0,
            "note": "Scoring covers monitored target species only, so a non-target negative-control AUROC cannot be computed from this run.",
        }
    score = _numeric_column(frame.loc[non_target_mask], "final_score", 0.0)
    per_label = {
        name: _binary_ranking_metrics(score, label.loc[non_target_mask], top_k=top_k)
        for name, label in weak_labels.items()
    }
    return {
        "role": "non_target_negative_control",
        "non_target_rows": non_target_rows,
        "interpretation_boundary": "AUROC near 0.5 on non-target species supports specificity; consistently high AUROC suggests weak-label leakage or shared confounding rather than species-specific signal.",
        "per_weak_label": per_label,
    }


def _negative_control_checks(
    frame: pd.DataFrame,
    *,
    final_score: pd.Series,
    weak_labels: dict[str, pd.Series],
    top_frame: pd.DataFrame,
    top_k: int,
    group_column: str = "period_start",
) -> dict[str, Any]:
    return {
        "configured_top_k": int(top_k),
        "top_rows_evaluated": len(top_frame),
        "max_species_share_top_k": _max_share(top_frame, "species_id"),
        "max_location_share_top_k": _max_share(top_frame, "location_id"),
        "max_station_share_top_k": _max_share(top_frame, "station_id"),
        "poor_quality_rows_top_k": _poor_quality_rows(top_frame),
        "weak_label_controls": {
            name: _weak_label_negative_controls(
                frame,
                final_score=final_score,
                label=label,
                top_k=top_k,
                group_column=group_column,
            )
            for name, label in weak_labels.items()
        },
        "note": "High concentration or weak control separation means thresholds should not be tightened from this validation slice alone.",
    }


def _weak_label_negative_controls(
    frame: pd.DataFrame,
    *,
    final_score: pd.Series,
    label: pd.Series,
    top_k: int,
    group_column: str,
) -> dict[str, Any]:
    actual = _binary_ranking_metrics(final_score, label, top_k=top_k)
    shift_name = (
        "within_week_label_shift"
        if group_column == "period_start"
        else f"within_{group_column}_label_shift"
    )
    controls = {
        "cyclic_label_shift": _binary_ranking_metrics(
            final_score,
            _cyclic_shift(label, _sort_index(frame)),
            top_k=top_k,
        ),
        shift_name: _binary_ranking_metrics(
            final_score,
            _within_group_shift(label, frame, group_column=group_column),
            top_k=top_k,
        ),
    }
    control_ap = [
        metrics["average_precision"]
        for metrics in controls.values()
        if metrics["average_precision"] is not None
    ]
    control_auroc = [
        metrics["auroc"]
        for metrics in controls.values()
        if metrics["auroc"] is not None
    ]
    return {
        "actual_average_precision": actual["average_precision"],
        "control_average_precision_mean": _mean_number(control_ap),
        "average_precision_lift_vs_controls": _subtract_or_none(
            actual["average_precision"], _mean_number(control_ap)
        ),
        "actual_auroc": actual["auroc"],
        "control_auroc_mean": _mean_number(control_auroc),
        "auroc_lift_vs_controls": _subtract_or_none(
            actual["auroc"], _mean_number(control_auroc)
        ),
        "controls": controls,
    }


def _top_alerts(top_frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = [
        "species_id",
        "species_english",
        "location_type",
        "location_id",
        "station_id",
        "grid_cell_id",
        "period_start",
        "model_anomaly_score",
        "final_score",
        "alert_level",
        "anomaly_streams",
        "population_anomaly_score",
        "embedding_drift_score",
        "sovon_lag_0_to_3mo_max_score",
        "quality_mask_reason",
        "top_detection_ids",
    ]
    available = [column for column in columns if column in top_frame]
    alerts = []
    for record in top_frame.loc[:, available].to_dict(orient="records"):
        alerts.append({key: _json_value(value) for key, value in record.items()})
    return alerts


def _top_months(top_frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = [
        "species_id",
        "species_english",
        "month_start",
        "monthly_peak_final_score",
        "monthly_mean_final_score",
        "weeks_scored",
        "weeks_alerted",
        "locations_scored",
        "monthly_peak_population_anomaly_score",
        "monthly_peak_embedding_drift_score",
        "sovon_mortality_score",
        "sovon_mortality_top_decile_flag",
        "sovon_lag_0_to_3mo_max_score",
        "sovon_has_context",
    ]
    available = [column for column in columns if column in top_frame]
    months = []
    for record in top_frame.loc[:, available].to_dict(orient="records"):
        months.append({key: _json_value(value) for key, value in record.items()})
    return months


def _score_summary(score: pd.Series) -> dict[str, float]:
    return {
        "min": _safe_float(score.min()),
        "median": _safe_float(score.median()),
        "mean": _safe_float(score.mean()),
        "p95": _safe_float(score.quantile(0.95)),
        "max": _safe_float(score.max()),
    }


def _stream_counts(frame: pd.DataFrame) -> dict[str, int]:
    if "anomaly_streams" not in frame.columns:
        return {}

    counts: dict[str, int] = {}
    for value in frame["anomaly_streams"].fillna("none").astype(str):
        for stream in value.split(","):
            stream = stream.strip() or "none"
            counts[stream] = counts.get(stream, 0) + 1
    return counts


def _value_counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if column not in frame.columns:
        return {}
    return {
        str(key): int(value)
        for key, value in frame[column].fillna("missing").value_counts().items()
    }


def _max_share(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame.columns:
        return None
    values = frame[column].dropna()
    if values.empty:
        return None
    return _safe_float(values.value_counts().max() / len(frame))


def _poor_quality_rows(frame: pd.DataFrame) -> int:
    if "quality_mask_reason" not in frame.columns:
        return 0
    quality = frame["quality_mask_reason"].fillna("ok").astype(str)
    return int((quality != "ok").sum())


def _sort_index(frame: pd.DataFrame) -> pd.Index:
    sort_columns = [
        column
        for column in (
            "period_start",
            "month_start",
            "species_id",
            "location_type",
            "location_id",
        )
        if column in frame.columns
    ]
    if not sort_columns:
        return frame.index
    return frame.sort_values(sort_columns, kind="mergesort").index


def _cyclic_shift(label: pd.Series, sort_index: pd.Index) -> pd.Series:
    ordered = label.fillna(False).astype(bool).reindex(sort_index)
    if len(ordered) <= 1:
        return label.fillna(False).astype(bool)
    shifted = ordered.shift(1)
    shifted.iloc[0] = ordered.iloc[-1]
    return shifted.reindex(label.index).astype(bool)


def _within_group_shift(
    label: pd.Series,
    frame: pd.DataFrame,
    *,
    group_column: str,
) -> pd.Series:
    if group_column not in frame.columns or len(frame) <= 1:
        return _cyclic_shift(label, _sort_index(frame))

    shifted = label.fillna(False).astype(bool).copy()
    for _, group in frame.groupby(group_column, dropna=False, sort=False):
        sort_index = _sort_index(group)
        if len(sort_index) <= 1:
            continue
        shifted.loc[sort_index] = _cyclic_shift(label.loc[sort_index], sort_index)
    return shifted.astype(bool)


def _numeric_column(frame: pd.DataFrame, column: str, default: float) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce").fillna(default)


def _truthy_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(False, index=frame.index, dtype="bool")

    values = frame[column]
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False)

    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().any():
        return numeric.fillna(0.0) > 0

    truthy = {"1", "TRUE", "T", "YES", "Y"}
    return values.fillna("").astype(str).str.strip().str.upper().isin(truthy)


def _first_non_missing(values: pd.Series) -> object:
    non_missing = values.dropna()
    if non_missing.empty:
        return None
    for value in non_missing:
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _mean_or_none(values: pd.Series) -> float | None:
    if values.empty:
        return None
    return _safe_float(values.mean())


def _mean_number(values: list[float]) -> float | None:
    if not values:
        return None
    return _safe_float(sum(values) / len(values))


def _subtract_or_none(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return _safe_float(left - right)


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return _safe_float(numerator / denominator)


def _safe_float(value: Any) -> float:
    result = float(value)
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return round(result, 6)


def _json_value(value: object) -> object:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    if pd.isna(value):
        return None
    return value
