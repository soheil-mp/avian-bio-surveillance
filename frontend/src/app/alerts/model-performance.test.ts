import { describe, expect, it } from "vitest";

import {
  aurocBand,
  aurocSkill,
  formatCount,
  formatMetric,
  formatPercent,
  parseModelPerformanceReport,
} from "./model-performance";

const CURRENT_SCHEMA = {
  run_id: "run-123",
  generated_at: "2025-02-10T00:00:00Z",
  validation: {
    report_version: "2",
    rows_scored: 4096,
    top_k: 50,
    score_summary: { min: -2, median: 0.1, mean: 0.2, p95: 1.5, max: 3.4 },
    alert_counts: { green: 4000, amber: 80, red: 16 },
    expected_alert_volume_under_null: {
      eligible_rows: 1200,
      weeks_observed: 52,
      mean_eligible_rows_per_week: 23.1,
      empirical_amber_fraction: 0.02,
      empirical_red_fraction: 0.004,
      expected_amber_alerts_per_year_under_null: 24,
      expected_red_alerts_per_year_under_null: 5,
      note: "null reference",
    },
    weak_label_metrics: {
      sovon_high_mortality_context: {
        positive_rows: 120,
        negative_rows: 3976,
        mean_positive_score: 0.8,
        mean_negative_score: 0.2,
        score_lift: 0.6,
        precision_at_k: 0.3,
        positives_in_top_k: 15,
        auroc: 0.62,
        average_precision: 0.18,
      },
      custom_context: {
        positive_rows: 40,
        negative_rows: 4056,
        auroc: 0.48,
        average_precision: 0.05,
      },
    },
    ensemble_comparison: {
      per_weak_label: {
        sovon_high_mortality_context: {
          isolation_forest: { auroc: 0.62, average_precision: 0.18 },
          max_fallback: { auroc: 0.55, average_precision: 0.12 },
          auroc_lift_if_minus_fallback: 0.07,
          average_precision_lift_if_minus_fallback: 0.06,
        },
      },
    },
    alert_threshold_sweep: {
      amber_quantile_sweep: [
        { amber_quantile: 0.95, score_threshold: 1.2, flagged_rows: 200, flagged_share: 0.05 },
      ],
      red_quantile_sweep: [
        {
          red_quantile: 0.99,
          score_threshold: 2.5,
          flagged_rows: 40,
          flagged_share: 0.01,
          weak_label_recall: {
            sovon_high_mortality_context: {
              positive_rows: 120,
              positives_flagged: 30,
              recall: 0.25,
              precision: 0.75,
            },
          },
        },
      ],
    },
    monthly_alignment: {
      weak_label_metrics: {
        sovon_same_month_high_mortality: {
          auroc: 0.64,
          average_precision: 0.2,
          positive_rows: 120,
          negative_rows: 3976,
        },
      },
      lead_lag_alignment: {
        metrics_by_lag_months: {
          "-1": { lag_months: -1, spearman_rho: 0.1, available_rows: 100, positive_rows: 10 },
          "0": { lag_months: 0, spearman_rho: 0.3, available_rows: 100, positive_rows: 10 },
          "1": { lag_months: 1, spearman_rho: 0.2, available_rows: 100, positive_rows: 10 },
        },
        best_spearman_lag_months: 0,
        best_spearman_rho: 0.3,
        same_month_spearman_rho: 0.3,
      },
      outbreak_month_hit_rate: {
        available_rows: 200,
        positive_rows: 20,
        top_k: 50,
        positives_in_top_k: 8,
        top_k_hit_rate: 0.4,
        alerted_positive_share: 0.35,
      },
    },
    non_target_species_control: {
      non_target_rows: 800,
      per_weak_label: {
        sovon_high_mortality_context: { auroc: 0.51 },
      },
    },
    spatial_validation: {
      status: "ok",
      score_variants: ["embedding"],
      horizons_weeks: [3],
      label_direction: "forward_early_warning",
      coverage: { total_grid_cells: 10 },
      evaluation_table: [
        {
          status: "ok",
          anomaly_type: "embedding",
          score_column: "embedding_drift_score",
          ground_truth_key: "sovon_mortality_spike",
          horizon_weeks: 3,
          alert_level: "amber",
          decision_rule: "top_5pct",
          alert_rate_cap: 0.05,
          threshold: 0.9,
          total_grid_cells: 10,
          common_grid_cells: 8,
          score_covered_grid_cells: 8,
          ground_truth_covered_grid_cells: 10,
          score_covered_rows: 100,
          ground_truth_covered_rows: 120,
          common_rows: 100,
          positive_rows: 12,
          negative_rows: 88,
          alert_rows: 5,
          true_positive_rows: 2,
          false_positive_rows: 3,
          true_negative_rows: 85,
          false_negative_rows: 10,
          alert_rate: 0.05,
          recall: 0.1667,
          precision: 0.4,
          f1: 0.235,
          specificity: 0.966,
          balanced_accuracy: 0.583,
        },
      ],
      per_variant: {},
    },
    best_f1_leaderboard: {
      rows: [
        {
          claim_type: "national_trend_alignment",
          grain: "national_week",
          signal: "bioacoustic",
          score_column: "embedding_drift_score",
          score_aggregation: "max",
          label: "label_sovon_mortality",
          horizon_weeks: 3,
          decision_rule: "max_f1_capped",
          alert_level: "optimized",
          label_optimized: true,
          upper_bound: false,
          f1: 0.72,
          precision: 0.75,
          recall: 0.69,
          alert_rate: 0.2,
          threshold: 0.61,
          positive_rows: 13,
          negative_rows: 91,
          common_rows: 104,
          common_units: 1,
          interpretation: "National trend alignment only.",
        },
      ],
      best_by_grain_signal: [
        {
          claim_type: "national_trend_alignment",
          grain: "national_week",
          signal: "bioacoustic",
          score_column: "embedding_drift_score",
          score_aggregation: "max",
          label: "label_sovon_mortality",
          horizon_weeks: 3,
          decision_rule: "max_f1_capped",
          alert_level: "optimized",
          label_optimized: true,
          upper_bound: false,
          f1: 0.72,
          precision: 0.75,
          recall: 0.69,
          alert_rate: 0.2,
          threshold: 0.61,
          positive_rows: 13,
          negative_rows: 91,
          common_rows: 104,
          common_units: 1,
        },
      ],
    },
    caveats: {
      unsupervised: "Detector is unsupervised; weak-label AUROC measures alignment only.",
      sparse: "Mortality labels are national species-month context.",
    },
  },
};

const STALE_SCHEMA = {
  validation: {
    rows_scored: 10,
    alert_counts: { red: 1 },
    monthly_alignment: {
      availability_stratified_metrics: {
        sovon_same_month_high_mortality: {
          available_rows: 90,
          metrics_on_available_rows: {
            auroc: 0.6,
            average_precision: 0.15,
            positive_rows: 12,
          },
        },
      },
    },
    caveats: ["legacy caveat"],
  },
};

describe("parseModelPerformanceReport", () => {
  it("returns null for null input", () => {
    expect(parseModelPerformanceReport(null)).toBeNull();
  });

  it("returns null for malformed JSON", () => {
    expect(parseModelPerformanceReport("{not json")).toBeNull();
  });

  it("returns null for a non-object payload", () => {
    expect(parseModelPerformanceReport("42")).toBeNull();
  });

  it("parses the current schema", () => {
    const report = parseModelPerformanceReport(JSON.stringify(CURRENT_SCHEMA));
    expect(report).not.toBeNull();
    if (!report) return;

    expect(report.available).toBe(true);
    expect(report.runId).toBe("run-123");
    expect(report.generatedAt).toBe("2025-02-10T00:00:00Z");
    expect(report.reportVersion).toBe("2");
    expect(report.rowsScored).toBe(4096);
    expect(report.topK).toBe(50);
    expect(report.alertCounts).toEqual({ green: 4000, amber: 80, red: 16 });

    expect(report.expectedUnderNull?.expectedRedPerYear).toBe(5);
    expect(report.scoreSummary?.p95).toBe(1.5);

    // Primary mortality first, in WEAK_LABEL_ORDER.
    expect(report.weakLabels[0]?.key).toBe("sovon_high_mortality_context");
    expect(report.weakLabels[0]?.auroc).toBe(0.62);
    expect(report.weakLabels[1]?.key).toBe("custom_context");

    expect(report.primaryMortality?.auroc).toBe(0.64);

    expect(report.ensemble[0]?.isolationForestAuroc).toBe(0.62);
    expect(report.ensemble[0]?.aurocLift).toBe(0.07);

    expect(report.amberSweep[0]?.flaggedShare).toBe(0.05);
    expect(report.redSweep[0]?.primaryRecall).toBe(0.25);
    expect(report.redSweep[0]?.primaryPrecision).toBe(0.75);

    // Lead/lag points sorted by lag.
    expect(report.leadLag?.points.map((p) => p.lagMonths)).toEqual([-1, 0, 1]);
    expect(report.leadLag?.bestSpearmanRho).toBe(0.3);

    expect(report.outbreakHitRate?.topKHitRate).toBe(0.4);

    expect(report.negativeControl?.rows).toBe(800);
    expect(report.negativeControl?.perLabel[0]?.deviationFromHalf).toBeCloseTo(0.01, 5);
    expect(report.bestF1Leaderboard).toHaveLength(1);
    expect(report.bestF1Leaderboard[0]?.grain).toBe("national_week");
    expect(report.bestF1Leaderboard[0]?.labelOptimized).toBe(true);
    expect(report.bestF1Leaderboard[0]?.f1).toBe(0.72);
    expect(report.bestF1ByGrainSignal[0]?.signal).toBe("bioacoustic");

    expect(report.caveats).toHaveLength(2);
    expect(report.spatialValidation?.evaluationTable[0]?.groundTruthKey).toBe(
      "sovon_mortality_spike",
    );
    expect(report.spatialValidation?.evaluationTable[0]?.balancedAccuracy).toBeCloseTo(
      0.583,
    );
  });

  it("parses primary mortality from the stale availability-stratified schema", () => {
    const report = parseModelPerformanceReport(JSON.stringify(STALE_SCHEMA));
    expect(report).not.toBeNull();
    if (!report) return;
    expect(report.primaryMortality?.auroc).toBe(0.6);
    expect(report.primaryMortality?.availableRows).toBe(90);
    expect(report.caveats).toEqual(["legacy caveat"]);
    expect(report.weakLabels).toHaveLength(0);
  });

  it("tolerates a bare report without the validation envelope", () => {
    const bare = { rows_scored: 7, alert_counts: { amber: 2 } };
    const report = parseModelPerformanceReport(JSON.stringify(bare));
    expect(report?.rowsScored).toBe(7);
    expect(report?.alertCounts.amber).toBe(2);
  });
});

describe("aurocSkill", () => {
  it("returns null for null", () => {
    expect(aurocSkill(null)).toBeNull();
  });
  it("clamps below-chance to zero", () => {
    expect(aurocSkill(0.4)).toBe(0);
  });
  it("doubles the distance above chance", () => {
    expect(aurocSkill(0.75)).toBeCloseTo(0.5, 5);
  });
});

describe("aurocBand", () => {
  it("classifies bands", () => {
    expect(aurocBand(null)).toBe("unknown");
    expect(aurocBand(0.4)).toBe("below");
    expect(aurocBand(0.5)).toBe("chance");
    expect(aurocBand(0.6)).toBe("weak");
    expect(aurocBand(0.8)).toBe("strong");
  });
});

describe("formatters", () => {
  it("formatMetric", () => {
    expect(formatMetric(null)).toBe("n/a");
    expect(formatMetric(0.1234)).toBe("0.123");
    expect(formatMetric(0.1, 1)).toBe("0.1");
  });
  it("formatPercent", () => {
    expect(formatPercent(null)).toBe("n/a");
    expect(formatPercent(0.5, 0)).toBe("50%");
    expect(formatPercent(0.1234, 1)).toBe("12.3%");
  });
  it("formatCount", () => {
    expect(formatCount(null)).toBe("n/a");
    expect(formatCount(1234.6)).toBe("1,235");
  });
});
