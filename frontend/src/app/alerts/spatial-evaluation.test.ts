import { describe, expect, it } from "vitest";

import {
  EVALUATION_LABEL_TAXONOMY,
  filterSpatialEvaluationTableRows,
  hasSpatialEvaluationContrast,
  listSpatialValidationLabels,
  parseSpatialValidation,
  SPATIAL_ALERT_LEVEL_NAMES,
  SPATIAL_LABEL_NAMES,
  SPATIAL_VARIANT_NAMES,
  spatialVerdict,
} from "./spatial-evaluation";

// Mirrors the legacy `spatial_validation` section structure of stored
// validation reports.
const SECTION = {
  status: "ok",
  score_variants: [
    "fusion",
    "embedding",
    "bioacoustic_excess",
    "bioacoustic_outlier",
    "population",
  ],
  horizons_weeks: [1, 3, 5],
  label_direction: "forward_early_warning",
  coverage: {
    total_grid_cells: 277,
    acoustic_filter_applied: true,
    acoustic_coverage_columns: ["community_active_days", "embedding_clip_count_scored"],
    acoustic_covered_rows: 38000,
    acoustic_covered_share: 0.91,
    acoustic_eligibility_share: 0.65,
  },
  evaluation_table: [
    {
      status: "not_spatial",
      anomaly_type: "embedding",
      score_column: "embedding_drift_score",
      ground_truth_key: "sovon_mortality_spike",
      horizon_weeks: 3,
      alert_level: "amber",
      decision_rule: "top_5pct",
      ground_truth_scope: "species_month_context",
      spatially_resolved: false,
      alert_rate_cap: 0.05,
      threshold: 0.91,
      total_grid_cells: null,
      common_grid_cells: null,
      score_covered_grid_cells: null,
      ground_truth_covered_grid_cells: null,
      score_covered_rows: 38000,
      ground_truth_covered_rows: 41721,
      common_rows: 38000,
      positive_rows: 1000,
      negative_rows: 37000,
      alert_rows: 1900,
      true_positive_rows: 120,
      false_positive_rows: 1780,
      true_negative_rows: 35220,
      false_negative_rows: 880,
      alert_rate: null,
      recall: null,
      precision: null,
      f1: null,
      specificity: null,
      balanced_accuracy: null,
    },
    {
      status: "no_positives",
      anomaly_type: "embedding",
      score_column: "embedding_drift_score",
      ground_truth_key: "secondary_reference",
      horizon_weeks: 3,
      alert_level: "amber",
      decision_rule: "top_5pct",
      alert_rate_cap: 0.05,
      threshold: 0.91,
      total_grid_cells: 277,
      common_grid_cells: 8,
      score_covered_grid_cells: 240,
      ground_truth_covered_grid_cells: 8,
      score_covered_rows: 38000,
      ground_truth_covered_rows: 96,
      common_rows: 90,
      positive_rows: 0,
      negative_rows: 90,
      alert_rows: 5,
      true_positive_rows: 0,
      false_positive_rows: 5,
      true_negative_rows: 85,
      false_negative_rows: 0,
      alert_rate: 0.0556,
      recall: null,
      precision: 0,
      f1: null,
      specificity: 0.944,
      balanced_accuracy: null,
    },
    {
      status: "ok",
      anomaly_type: "embedding",
      score_column: "embedding_drift_score",
      ground_truth_key: "sovon_mortality_spike",
      horizon_weeks: 3,
      alert_level: "optimized",
      decision_rule: "max_f1_capped",
      ground_truth_scope: "species_month_context",
      spatially_resolved: false,
      alert_rate_cap: 0.3,
      threshold: 0.82,
      total_grid_cells: null,
      common_grid_cells: null,
      score_covered_grid_cells: null,
      ground_truth_covered_grid_cells: null,
      score_covered_rows: 38000,
      ground_truth_covered_rows: 41721,
      common_rows: 38000,
      positive_rows: 1000,
      negative_rows: 37000,
      alert_rows: 3800,
      true_positive_rows: 260,
      false_positive_rows: 3540,
      true_negative_rows: 33460,
      false_negative_rows: 740,
      alert_rate: null,
      recall: null,
      precision: null,
      f1: null,
      specificity: null,
      balanced_accuracy: null,
    },
  ],
  per_variant: {
    fusion: {
      score_column: "final_score",
      forward_ranking: {
        sovon_mortality_spike: {
          "3": {
            status: "ok",
            positive_rows: 15214,
            negative_rows: 26507,
            evaluable_rows: 41721,
            score_lift: 0.0011,
            precision_at_k: 0.12,
            auroc: 0.503,
            average_precision: 0.37,
          },
        },
        secondary_reference: {
          "3": {
            status: "no_contrast",
            positive_rows: 0,
            negative_rows: 0,
            evaluable_rows: 16,
          },
        },
        regional_reference: {
          "3": {
            status: "ok",
            positive_rows: 12,
            negative_rows: 40,
            evaluable_rows: 52,
            score_lift: 0.08,
            precision_at_k: 0.24,
            auroc: 0.61,
            average_precision: 0.42,
          },
        },
      },
      within_week_case_control: {
        sovon_mortality_spike: {
          "3": {
            status: "ok",
            contrast_weeks: 62,
            case_rows: 15214,
            mean_week_lift: 0.0011,
            permutation_p_value: 0.31,
            n_permutations: 200,
          },
        },
      },
      lead_time: {
        alert_quantile: 0.95,
        secondary_reference: {
          "5": {
            events: 16,
            detected_events: 16,
            detection_rate: 1.0,
            mean_lead_weeks: 3.1,
            median_lead_weeks: 5.0,
            max_lead_weeks: 5,
          },
        },
        regional_reference: {
          "5": {
            events: 12,
            detected_events: 7,
            detection_rate: 0.58,
            mean_lead_weeks: 2.4,
            median_lead_weeks: 2.0,
            max_lead_weeks: 5,
          },
        },
      },
    },
    embedding: { score_column: "embedding_drift_score" },
    population: { score_column: "population_anomaly_score" },
  },
  operating_point: {
    status: "ok",
    decision_column: "alert_level",
    operating_points: {
      amber_red: {
        regional_reference: {
          n_eligible: 52,
          n_alert: 18,
          alert_rate: 0.346,
          by_horizon: {
            "3": {
              n_positive: 12,
              n_detected: 9,
              recall: 0.75,
              masked_precision: 0.5,
              status: "ok",
            },
          },
        },
      },
      red_only: {
        regional_reference: {
          n_eligible: 52,
          n_alert: 4,
          alert_rate: 0.0769,
          by_horizon: {
            "3": {
              n_positive: 12,
              n_detected: 3,
              recall: 0.25,
              masked_precision: 0.75,
              status: "ok",
            },
          },
        },
      },
    },
  },
};

describe("parseSpatialValidation", () => {
  it("parses the section nested under validation", () => {
    const report = parseSpatialValidation({ validation: { spatial_validation: SECTION } });
    expect(report).not.toBeNull();
    expect(report?.status).toBe("ok");
    expect(report?.labelDirection).toBe("forward_early_warning");
    expect(report?.scoreVariants).toEqual([
      "fusion",
      "embedding",
      "bioacoustic_excess",
      "bioacoustic_outlier",
      "population",
    ]);
    expect(report?.horizonsWeeks).toEqual([1, 3, 5]);
  });

  it("parses the bare section", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report?.variants).toHaveLength(3);
  });

  it("extracts metrics, case-control, and lead-time for a variant", () => {
    const report = parseSpatialValidation(SECTION);
    const fusion = report?.variants.find((v) => v.variant === "fusion");
    expect(fusion?.scoreColumn).toBe("final_score");

    const sovon = fusion?.forwardRanking.sovon_mortality_spike["3"];
    expect(sovon?.auroc).toBeCloseTo(0.503);
    expect(sovon?.averagePrecision).toBeCloseTo(0.37);

    const cc = fusion?.caseControl.sovon_mortality_spike["3"];
    expect(cc?.permutationPValue).toBeCloseTo(0.31);
    expect(cc?.meanWeekLift).toBeCloseTo(0.0011);

    const lead = fusion?.leadTime.secondary_reference["5"];
    expect(lead?.detectionRate).toBe(1.0);
    expect(lead?.medianLeadWeeks).toBe(5.0);
    expect(fusion?.leadTime.regional_reference["5"].events).toBe(12);
  });

  it("skips scalar bookkeeping keys like alert_quantile in lead_time", () => {
    const report = parseSpatialValidation(SECTION);
    const fusion = report?.variants.find((v) => v.variant === "fusion");
    // alert_quantile is a scalar, not an event->horizon map, so it must not
    // become a bogus label entry.
    expect(Object.keys(fusion?.leadTime ?? {})).not.toContain("alert_quantile");
    expect(Object.keys(fusion?.leadTime ?? {})).toContain("secondary_reference");
  });

  it("carries the no_contrast status through", () => {
    const report = parseSpatialValidation(SECTION);
    const fusion = report?.variants.find((v) => v.variant === "fusion");
    expect(fusion?.forwardRanking.secondary_reference["3"].status).toBe("no_contrast");
  });

  it("parses coverage", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report?.coverage.totalGridCells).toBe(277);
    expect(report?.coverage.acousticCoveredShare).toBeCloseTo(0.91);
  });

  it("parses the common-coverage evaluation table", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report?.evaluationTable).toHaveLength(3);
    const row = report?.evaluationTable[0];
    expect(row?.anomalyType).toBe("embedding");
    expect(row?.groundTruthKey).toBe("sovon_mortality_spike");
    expect(row?.alertLevel).toBe("amber");
    expect(row?.status).toBe("not_spatial");
    expect(row?.groundTruthScope).toBe("species_month_context");
    expect(row?.spatiallyResolved).toBe(false);
    expect(row?.commonGridCells).toBeNull();
    expect(row?.commonRows).toBe(38000);
    expect(row?.recall).toBeNull();
    expect(row?.balancedAccuracy).toBeNull();
  });

  it("derives zero F1 for old reports with zero precision and recall", () => {
    const report = parseSpatialValidation({
      ...SECTION,
      evaluation_table: [
        {
          status: "ok",
          anomaly_type: "embedding",
          score_column: "embedding_drift_score",
          ground_truth_key: "regional_reference",
          horizon_weeks: 3,
          alert_level: "amber",
          decision_rule: "top_5pct",
          ground_truth_scope: "grid_week",
          spatially_resolved: true,
          positive_rows: 4,
          negative_rows: 96,
          alert_rows: 2,
          true_positive_rows: 0,
          false_positive_rows: 2,
          true_negative_rows: 94,
          false_negative_rows: 4,
          alert_rate: 0.02,
          recall: 0,
          precision: 0,
          f1: null,
        },
      ],
    });

    expect(report?.evaluationTable[0]?.f1).toBe(0);
  });

  it("parses dual-grain reports and keeps admin-area first", () => {
    const adminSection = {
      ...SECTION,
      spatial_grain: "admin_area",
      unit_label: "admin areas",
      coverage: {
        ...SECTION.coverage,
        spatial_grain: "admin_area",
        unit_label: "admin areas",
        total_units: 20,
        admin_label_units: 6,
        admin_score_units: 5,
        unmatched_admin_label_units: 1,
        crosswalk_source: "boundary_join",
      },
      evaluation_table: SECTION.evaluation_table.map((row) => ({
        ...row,
        spatial_grain: "admin_area",
        spatial_unit_label: "admin areas",
        total_units: row.total_grid_cells,
        common_units: row.common_grid_cells,
      })),
    };
    const report = parseSpatialValidation({
      validation: {
        spatial_validation: {
          ...SECTION,
          grid: SECTION,
          admin_area: adminSection,
        },
      },
    });

    expect(report?.grainReports).toHaveLength(2);
    expect(report?.grainReports[0]?.spatialGrain).toBe("admin_area");
    expect(report?.grainReports[0]?.coverage.crosswalkSource).toBe(
      "boundary_join",
    );
    expect(report?.grainReports[0]?.coverage.unmatchedAdminLabelUnits).toBe(1);
  });

  it("filters the evaluation table by anomaly type, alert level, and horizon", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report).not.toBeNull();
    const amberRows = filterSpatialEvaluationTableRows(report!, {
      anomalyType: "embedding",
      alertLevel: "amber",
      horizonWeeks: 3,
    });
    expect(amberRows.map((row) => row.groundTruthKey)).toEqual([
      "sovon_mortality_spike",
      "secondary_reference",
    ]);

    const optimizedRows = filterSpatialEvaluationTableRows(report!, {
      anomalyType: "embedding",
      alertLevel: "optimized",
      horizonWeeks: 3,
    });
    expect(optimizedRows).toHaveLength(1);
    expect(optimizedRows[0]?.decisionRule).toBe("max_f1_capped");
    expect(optimizedRows[0]?.alertRateCap).toBeCloseTo(0.3);
    expect(optimizedRows[0]?.spatiallyResolved).toBe(false);
  });

  it("returns null for a missing or empty section", () => {
    expect(parseSpatialValidation(null)).toBeNull();
    expect(parseSpatialValidation({})).toBeNull();
    expect(
      parseSpatialValidation({ spatial_validation: { status: "no_spatial_rows" } }),
    ).toBeNull();
  });

  it("exposes friendly display names", () => {
    expect(SPATIAL_LABEL_NAMES.sovon_mortality_spike).toMatch(/SOVON/);
    expect(SPATIAL_VARIANT_NAMES.embedding).toMatch(/Bioacoustic/);
    expect(SPATIAL_VARIANT_NAMES.bioacoustic_excess).toBe("Bioacoustic excess");
    expect(SPATIAL_VARIANT_NAMES.bioacoustic_outlier).toMatch(/outlier share/);
    expect(SPATIAL_ALERT_LEVEL_NAMES.optimized).toBe("Optimized");
  });

  it("keeps the agreed evaluation taxonomy visible when the report is sparse", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report).not.toBeNull();
    expect(listSpatialValidationLabels(report!)).toEqual([
      ...EVALUATION_LABEL_TAXONOMY,
      "secondary_reference",
      "regional_reference",
    ]);
  });

  it("distinguishes usable contrast from sparse selections", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report).not.toBeNull();
    expect(hasSpatialEvaluationContrast(report!, "sovon_mortality_spike", 3)).toBe(true);
    expect(hasSpatialEvaluationContrast(report!, "secondary_reference", 3)).toBe(false);
    expect(hasSpatialEvaluationContrast(report!, "unknown_reference", 3)).toBe(false);
  });

  it("parses acoustic-eligibility coverage", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report?.coverage.acousticFilterApplied).toBe(true);
    expect(report?.coverage.acousticCoverageColumns).toContain(
      "embedding_clip_count_scored",
    );
    expect(report?.coverage.acousticEligibilityShare).toBeCloseTo(0.65);
  });

  it("parses the operating-point scorecard (recall + alert-rate)", () => {
    const report = parseSpatialValidation(SECTION);
    const op = report?.operatingPoint;
    expect(op?.status).toBe("ok");
    expect(op?.decisionColumn).toBe("alert_level");
    const amberRed = op?.operatingPoints.amber_red.regional_reference;
    expect(amberRed?.alertRate).toBeCloseTo(0.346);
    expect(amberRed?.byHorizon["3"].recall).toBeCloseTo(0.75);
    expect(amberRed?.byHorizon["3"].maskedPrecision).toBeCloseTo(0.5);
    expect(amberRed?.byHorizon["3"].nDetected).toBe(9);
    const redOnly = op?.operatingPoints.red_only.regional_reference;
    expect(redOnly?.byHorizon["3"].recall).toBeCloseTo(0.25);
  });

  it("returns a null operating point when the block is absent", () => {
    const report = parseSpatialValidation({ ...SECTION, operating_point: undefined });
    expect(report).not.toBeNull();
    expect(report?.operatingPoint).toBeNull();
  });
});


describe("parseRunContext (via parseSpatialValidation)", () => {
  const WRAPPER = {
    run_id:
      "anomaly_scoring_backfill_20240101_20240331_7d_retrospective_target_embedding_clusters_control",
    scoring_mode: "retrospective",
    validation: { rows_scored: 3752, spatial_validation: SECTION },
  };

  it("plumbs run id, scoring mode, rows scored, and the window from the wrapper", () => {
    const report = parseSpatialValidation(WRAPPER);
    expect(report).not.toBeNull();
    const ctx = report!.runContext;
    expect(ctx.runId).toBe(WRAPPER.run_id);
    expect(ctx.scoringMode).toBe("retrospective");
    expect(ctx.rowsScored).toBe(3752);
    // window is encoded in the run id (…_YYYYMMDD_YYYYMMDD_…).
    expect(ctx.windowStart).toBe("2024-01-01");
    expect(ctx.windowEnd).toBe("2024-03-31");
  });

  it("leaves run-context fields null when only the bare section is given", () => {
    const report = parseSpatialValidation(SECTION);
    expect(report).not.toBeNull();
    const ctx = report!.runContext;
    expect(ctx.runId).toBeNull();
    expect(ctx.scoringMode).toBeNull();
    expect(ctx.rowsScored).toBeNull();
    expect(ctx.windowStart).toBeNull();
    expect(ctx.windowEnd).toBeNull();
  });
});

describe("spatialVerdict", () => {
  const entry = (
    alertRate: number | null,
    recall: number | null,
    nPositive: number,
    status = "ok",
  ) => ({
    nEligible: 100,
    nAlert: 10,
    alertRate,
    byHorizon: {
      "3": { status, nPositive, nDetected: 5, recall, maskedPrecision: 0.5 },
    },
  });

  it("returns skill when recall beats the alert base rate", () => {
    expect(spatialVerdict(entry(0.1, 0.8, 10), 3)).toBe("skill");
  });

  it("returns no_skill when recall does not beat the alert base rate", () => {
    expect(spatialVerdict(entry(0.3, 0.2, 10), 3)).toBe("no_skill");
  });

  it("returns inconclusive when there are no positives to measure against", () => {
    expect(spatialVerdict(entry(0.1, null, 0), 3)).toBe("inconclusive");
  });

  it("returns inconclusive when the horizon metric did not compute", () => {
    expect(spatialVerdict(entry(0.1, 0.8, 10, "no_contrast"), 3)).toBe("inconclusive");
  });

  it("flags an implausible alert share as uncalibrated regardless of recall", () => {
    expect(spatialVerdict(entry(0.76, 1.0, 10), 3)).toBe("uncalibrated");
  });

  it("works on a parsed report's real entries", () => {
    const op = parseSpatialValidation(SECTION)!.operatingPoint!;
    // amber_red regional reference: recall 0.75 > alert_rate 0.346 -> skill.
    expect(spatialVerdict(op.operatingPoints.amber_red.regional_reference, 3)).toBe("skill");
  });
});
