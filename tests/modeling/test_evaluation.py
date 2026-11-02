"""Tests for modeling validation report helpers."""

from __future__ import annotations

from datetime import date

import pandas as pd

from avian_biosurveillance.modeling.evaluation import build_validation_report


def test_build_validation_report_scores_sovon_mortality_context() -> None:
    report = build_validation_report(
        pd.DataFrame(
            [
                {
                    "species_id": "76",
                    "location_id": "g1",
                    "period_start": date(2026, 5, 4),
                    "final_score": 0.95,
                    "alert_level": "red",
                    "anomaly_streams": "population,embedding",
                    "top_detection_ids": ["det-1", "det-2"],
                    "sovon_lag_0_to_3mo_max_score": 0.95,
                    "sovon_has_context": True,
                    "quality_mask_reason": "ok",
                },
                {
                    "species_id": "76",
                    "location_id": "g2",
                    "period_start": "2026-05-04",
                    "final_score": 0.40,
                    "alert_level": "green",
                    "anomaly_streams": "none",
                    "sovon_lag_0_to_3mo_max_score": 0.10,
                    "sovon_has_context": False,
                    "quality_mask_reason": "ok",
                },
                {
                    "species_id": "35",
                    "location_id": "g3",
                    "period_start": "2026-05-04",
                    "final_score": 0.90,
                    "alert_level": "amber",
                    "anomaly_streams": "embedding",
                    "sovon_lag_0_to_3mo_max_score": 0.92,
                    "sovon_has_context": True,
                    "quality_mask_reason": "ok",
                },
            ]
        ),
        top_k=2,
    )

    metrics = report["weak_label_metrics"]["sovon_high_mortality_context"]
    assert metrics["positive_rows"] == 2
    assert metrics["precision_at_k"] == 1.0
    assert metrics["auroc"] == 1.0
    controls = report["negative_control_checks"]["weak_label_controls"][
        "sovon_high_mortality_context"
    ]
    assert controls["average_precision_lift_vs_controls"] > 0
    diagnostics = report["context_diagnostics"]["sovon_high_mortality_context"]
    assert diagnostics["availability_known"] is True
    assert diagnostics["available_rows"] == 2
    assert diagnostics["positive_rows_available"] == 2
    available = report["availability_stratified_metrics"][
        "sovon_high_mortality_context"
    ]
    assert available["available_rows"] == 2
    assert available["metrics_on_available_rows"]["precision_at_k"] == 1.0
    assert report["stream_counts"]["population"] == 1
    assert report["top_alerts"][0]["species_id"] == "76"
    assert report["top_alerts"][0]["top_detection_ids"] == ["det-1", "det-2"]
    assert "operational_review" not in report


def test_build_validation_report_adds_design_and_ensemble_diagnostics() -> None:
    report = build_validation_report(
        pd.DataFrame(
            [
                {
                    "species_id": "76",
                    "location_id": "g1",
                    "period_start": "2026-05-04",
                    "final_score": 0.96,
                    "model_anomaly_score": 0.96,
                    "population_anomaly_score": 0.8,
                    "embedding_drift_score": 0.9,
                    "alert_level": "red",
                    "sovon_lag_0_to_3mo_max_score": 0.95,
                    "sovon_has_context": True,
                },
                {
                    "species_id": "76",
                    "location_id": "g2",
                    "period_start": "2026-05-11",
                    "final_score": 0.90,
                    "model_anomaly_score": 0.90,
                    "population_anomaly_score": 0.7,
                    "embedding_drift_score": 0.4,
                    "alert_level": "amber",
                    "sovon_lag_0_to_3mo_max_score": 0.10,
                    "sovon_has_context": True,
                },
                {
                    "species_id": "999999999",
                    "location_id": "g3",
                    "period_start": "2026-05-11",
                    "final_score": 0.30,
                    "model_anomaly_score": 0.30,
                    "population_anomaly_score": 0.2,
                    "embedding_drift_score": 0.1,
                    "alert_level": "green",
                    "sovon_lag_0_to_3mo_max_score": 0.10,
                    "sovon_has_context": False,
                },
            ]
        ),
        top_k=2,
    )

    assert "alert_levels_are_queue_ranks" in report["caveats"]

    volume = report["expected_alert_volume_under_null"]
    assert volume["eligible_rows"] == 3
    assert volume["red_rows"] == 1
    assert volume["amber_rows"] == 1
    assert volume["weeks_observed"] == 2
    assert volume["expected_red_alerts_per_year_under_null"] is not None

    ensemble = report["ensemble_comparison"]["per_weak_label"][
        "sovon_high_mortality_context"
    ]
    assert "isolation_forest" in ensemble
    assert "max_fallback" in ensemble
    assert "auroc_lift_if_minus_fallback" in ensemble

    sweep = report["alert_threshold_sweep"]
    assert {entry["red_quantile"] for entry in sweep["red_quantile_sweep"]} == {
        0.97,
        0.99,
        0.995,
    }
    assert {entry["amber_quantile"] for entry in sweep["amber_quantile_sweep"]} == {
        0.90,
        0.95,
        0.97,
    }

    control = report["non_target_species_control"]
    assert control["role"] == "non_target_negative_control"
    assert control["non_target_rows"] == 1


def test_build_validation_report_handles_missing_context_columns() -> None:
    report = build_validation_report(
        pd.DataFrame(
            [
                {
                    "species_id": "76",
                    "location_id": "g1",
                    "final_score": 0.2,
                    "alert_level": "green",
                    "quality_mask_reason": "ok",
                }
            ]
        )
    )

    metrics = report["weak_label_metrics"]["sovon_high_mortality_context"]
    assert metrics["positive_rows"] == 0
    assert metrics["auroc"] is None
    assert (
        report["negative_control_checks"]["weak_label_controls"][
            "sovon_high_mortality_context"
        ]["actual_average_precision"]
        is None
    )
    assert report["negative_control_checks"]["top_rows_evaluated"] == 1
    assert (
        report["context_diagnostics"]["sovon_high_mortality_context"][
            "availability_known"
        ]
        is False
    )
    assert (
        report["availability_stratified_metrics"]["sovon_high_mortality_context"][
            "available_rows"
        ]
        == 0
    )


def test_build_validation_report_stratifies_metrics_by_context_availability() -> None:
    report = build_validation_report(
        pd.DataFrame(
            [
                {
                    "species_id": "76",
                    "location_id": "g1",
                    "period_start": "2026-05-04",
                    "final_score": 0.99,
                    "alert_level": "red",
                    "sovon_lag_0_to_3mo_max_score": 0.10,
                    "sovon_has_context": False,
                    "quality_mask_reason": "ok",
                },
                {
                    "species_id": "76",
                    "location_id": "g2",
                    "period_start": "2026-05-04",
                    "final_score": 0.80,
                    "alert_level": "amber",
                    "sovon_lag_0_to_3mo_max_score": 0.95,
                    "sovon_has_context": True,
                    "quality_mask_reason": "ok",
                },
                {
                    "species_id": "76",
                    "location_id": "g3",
                    "period_start": "2026-05-04",
                    "final_score": 0.10,
                    "alert_level": "green",
                    "sovon_lag_0_to_3mo_max_score": 0.10,
                    "sovon_has_context": True,
                    "quality_mask_reason": "ok",
                },
            ]
        ),
        top_k=1,
    )

    assert (
        report["weak_label_metrics"]["sovon_high_mortality_context"]["precision_at_k"]
        == 0.0
    )
    available = report["availability_stratified_metrics"][
        "sovon_high_mortality_context"
    ]
    assert available["available_rows"] == 2
    assert available["metrics_on_available_rows"]["precision_at_k"] == 1.0


def test_build_validation_report_adds_species_month_sovon_alignment() -> None:
    report = build_validation_report(
        pd.DataFrame(
            [
                {
                    "species_id": "76",
                    "species_english": "Western Jackdaw",
                    "location_id": "g1",
                    "period_start": "2026-05-04",
                    "final_score": 0.92,
                    "population_anomaly_score": 0.92,
                    "alert_level": "red",
                    "sovon_mortality_score": 0.95,
                    "sovon_mortality_top_decile_flag": True,
                    "sovon_lag_0_to_3mo_max_score": 0.95,
                    "sovon_has_context": True,
                },
                {
                    "species_id": "76",
                    "species_english": "Western Jackdaw",
                    "location_id": "g2",
                    "period_start": "2026-05-11",
                    "final_score": 0.70,
                    "population_anomaly_score": 0.70,
                    "alert_level": "amber",
                    "sovon_mortality_score": 0.95,
                    "sovon_mortality_top_decile_flag": True,
                    "sovon_lag_0_to_3mo_max_score": 0.95,
                    "sovon_has_context": True,
                },
                {
                    "species_id": "35",
                    "species_english": "Common Blackbird",
                    "location_id": "g3",
                    "period_start": "2026-05-04",
                    "final_score": 0.60,
                    "population_anomaly_score": 0.60,
                    "alert_level": "amber",
                    "sovon_mortality_score": 0.10,
                    "sovon_mortality_top_decile_flag": False,
                    "sovon_lag_0_to_3mo_max_score": 0.10,
                    "sovon_has_context": True,
                },
                {
                    "species_id": "35",
                    "species_english": "Common Blackbird",
                    "location_id": "g3",
                    "period_start": "2026-06-01",
                    "final_score": 0.30,
                    "population_anomaly_score": 0.30,
                    "alert_level": "green",
                    "sovon_mortality_score": 0.0,
                    "sovon_mortality_top_decile_flag": False,
                    "sovon_lag_0_to_3mo_max_score": 0.0,
                    "sovon_has_context": True,
                },
            ]
        ),
        top_k=1,
    )

    monthly = report["monthly_alignment"]

    assert monthly["row_grain"] == "species_month_from_weekly_scores"
    assert monthly["source_week_rows"] == 4
    assert monthly["rows_scored"] == 3
    metrics = monthly["weak_label_metrics"]["sovon_same_month_high_mortality"]
    assert metrics["positive_rows"] == 1
    assert metrics["precision_at_k"] == 1.0
    assert metrics["auroc"] == 1.0
    recent_metrics = monthly["weak_label_metrics"]["sovon_recent_mortality_window"]
    assert recent_metrics["positive_rows"] == 1
    controls = monthly["negative_control_checks"]["weak_label_controls"]
    assert (
        "within_month_start_label_shift"
        in controls["sovon_same_month_high_mortality"]["controls"]
    )
    monthly_diagnostics = monthly["context_diagnostics"][
        "sovon_same_month_high_mortality"
    ]
    assert monthly_diagnostics["availability_known"] is True
    assert monthly_diagnostics["available_rows"] == 3
    assert monthly_diagnostics["positive_rows_available"] == 1
    assert monthly["top_months"][0]["species_id"] == "76"
    assert monthly["top_months"][0]["weeks_scored"] == 2
    assert monthly["top_months"][0]["monthly_peak_final_score"] == 0.92


def test_build_validation_report_adds_monthly_continuous_and_lag_metrics() -> None:
    report = build_validation_report(
        pd.DataFrame(
            [
                _sovon_month_row("2026-01-05", final=0.10, mortality=0.10, dead=1),
                _sovon_month_row(
                    "2026-02-02",
                    final=0.80,
                    mortality=0.80,
                    dead=8,
                    top_decile=True,
                ),
                _sovon_month_row("2026-03-02", final=0.20, mortality=0.20, dead=2),
                _sovon_month_row(
                    "2026-04-06",
                    final=0.60,
                    mortality=0.60,
                    dead=6,
                    top_decile=True,
                ),
            ]
        ),
        top_k=2,
    )

    monthly = report["monthly_alignment"]
    continuous = monthly["continuous_metrics"]
    peak_vs_mortality = continuous["peak_final_score_vs_mortality_score"]
    assert peak_vs_mortality["available_rows"] == 4
    assert peak_vs_mortality["positive_rows"] == 4
    assert peak_vs_mortality["spearman_rho"] == 1.0
    assert peak_vs_mortality["pearson_r"] == 1.0
    peak_vs_dead_birds = continuous["peak_final_score_vs_dead_bird_count"]
    assert peak_vs_dead_birds["spearman_rho"] == 1.0
    count_vs_mortality = continuous["peak_population_score_vs_mortality_score"]
    assert count_vs_mortality["spearman_rho"] == 1.0

    lead_lag = monthly["lead_lag_alignment"]
    assert lead_lag["available_rows"] == 4
    assert lead_lag["best_spearman_lag_months"] == 0
    assert lead_lag["same_month_spearman_rho"] == 1.0
    assert lead_lag["metrics_by_lag_months"]["1"]["available_rows"] == 3
    assert lead_lag["metrics_by_lag_months"]["1"]["spearman_rho"] == -1.0

    hit_rate = monthly["outbreak_month_hit_rate"]
    assert hit_rate["available_rows"] == 4
    assert hit_rate["positive_rows"] == 2
    assert hit_rate["alerted_positive_months"] == 2
    assert hit_rate["alerted_positive_share"] == 1.0
    assert hit_rate["positives_in_top_k"] == 2
    assert hit_rate["top_k_hit_rate"] == 1.0


def test_build_validation_report_adds_tiered_best_f1_leaderboard() -> None:
    weeks = pd.date_range("2024-07-01", periods=10, freq="7D")
    rows = []
    for i, week in enumerate(weeks):
        is_event = i in {2, 7}
        score = 0.95 if is_event else 0.2 - i * 0.005
        rows.append(
            {
                "species_id": "76",
                "location_type": "grid",
                "location_id": f"g{i}",
                "grid_cell_id": f"g{i}",
                "period_start": week,
                "final_score": score,
                "embedding_drift_score": score,
                "embedding_drift_control_score": 0.05,
                "embedding_outlier_share": 0.8 if is_event else 0.05,
                "embedding_clip_count_scored": 3 if is_event else 1,
                "embedding_backend_clip_counts": {
                    "perch_v2": 2 if is_event else 1,
                    "birdnet": 1 if is_event else 0,
                },
                "embedding_backend_scored_clip_counts": {
                    "perch_v2": 2 if is_event else 1,
                    "birdnet": 1 if is_event else 0,
                },
                "embedding_primary_backend": "perch_v2",
                "population_anomaly_score": 0.7 if is_event else 0.05,
                "alert_level": "red" if is_event else "green",
                "sovon_mortality_score": 0.95 if is_event else 0.1,
                "sovon_mortality_top_decile_flag": is_event,
                "sovon_has_context": True,
            }
        )

    report = build_validation_report(pd.DataFrame(rows), top_k=3)
    leaderboard = report["best_f1_leaderboard"]
    national_rows = [
        row
        for row in leaderboard["rows"]
        if row["grain"] == "national_week"
        and row["signal"] == "bioacoustic"
        and row["label"] == "label_sovon_mortality"
        and row["horizon_weeks"] == 0
    ]

    assert leaderboard["national_capped_alert_rate"] == 0.30
    assert any(row["decision_rule"] == "max_f1_capped" for row in national_rows)
    assert any(row["upper_bound"] for row in national_rows)
    capped = next(
        row for row in national_rows if row["decision_rule"] == "max_f1_capped"
    )
    assert capped["claim_type"] == "national_trend_alignment"
    assert capped["label_optimized"] is True
    assert capped["alert_rate"] <= 0.30
    assert capped["f1"] == 1.0
    assert "not a grid-cell early-warning" in capped["interpretation"]
    assert leaderboard["best_by_grain_signal"]
    assert leaderboard["national_trend_alignment"]
    assert {
        "bioacoustic_excess",
        "bioacoustic_outlier",
        "population",
    }.issubset({row["signal"] for row in leaderboard["rows"]})
    coverage = report["bioacoustic_coverage"]
    assert coverage["overlap"]["embedding_and_sovon_mortality_rows"] == 2
    assert coverage["by_embedding_backend"][0]["embedding_backend"] == "perch_v2"
    assert {
        row["embedding_backend"] for row in coverage["by_embedding_backend"]
    } == {"perch_v2", "birdnet"}
    assert coverage["by_week"][0]["embedding_rows"] == 1


def _sovon_month_row(
    period_start: str,
    *,
    final: float,
    mortality: float,
    dead: int,
    top_decile: bool = False,
) -> dict[str, object]:
    return {
        "species_id": "76",
        "species_english": "Western Jackdaw",
        "location_id": "g1",
        "period_start": period_start,
        "final_score": final,
        "population_anomaly_score": final,
        "embedding_drift_score": max(final - 0.05, 0.0),
        "alert_level": "red" if top_decile else "green",
        "sovon_dead_bird_count": dead,
        "sovon_n_total": 100,
        "sovon_mortality_rate": dead / 100,
        "sovon_mortality_score": mortality,
        "sovon_mortality_top_decile_flag": top_decile,
        "sovon_lag_0_to_3mo_max_score": mortality,
        "sovon_has_context": True,
    }
