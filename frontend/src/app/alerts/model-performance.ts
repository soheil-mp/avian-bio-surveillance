import type {
  ModelPerformanceBestF1Row,
  ModelPerformanceControlRow,
  ModelPerformanceEnsembleRow,
  ModelPerformanceLeadLagPoint,
  ModelPerformanceReport,
  ModelPerformanceThresholdRow,
  ModelPerformanceWeakLabel,
} from "./types";
import { parseSpatialValidation } from "./spatial-evaluation";

// Friendly labels for the weak-label keys emitted by build_validation_report.
const WEAK_LABEL_LABELS: Record<string, string> = {
  sovon_high_mortality_context: "Sovon high mortality (nationwide month)",
};

// Display order: primary mortality first.
const WEAK_LABEL_ORDER = ["sovon_high_mortality_context"] as const;

const PRIMARY_WEAK_LABEL_KEY = "sovon_high_mortality_context";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toFiniteNumberOrNull(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toNumber(value: unknown): number {
  return toFiniteNumberOrNull(value) ?? 0;
}

function readString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function weakLabelLabel(key: string): string {
  return WEAK_LABEL_LABELS[key] ?? key;
}

function parseWeakLabelMetrics(
  metrics: Record<string, unknown>,
): ModelPerformanceWeakLabel[] {
  const rows: ModelPerformanceWeakLabel[] = [];
  const seen = new Set<string>();
  const orderedKeys = [
    ...WEAK_LABEL_ORDER.filter((key) => key in metrics),
    ...Object.keys(metrics).filter(
      (key) => !WEAK_LABEL_ORDER.includes(key as (typeof WEAK_LABEL_ORDER)[number]),
    ),
  ];

  for (const key of orderedKeys) {
    if (seen.has(key)) continue;
    seen.add(key);
    const entry = metrics[key];
    if (!isRecord(entry)) continue;
    rows.push({
      key,
      label: weakLabelLabel(key),
      role: readString(entry.role),
      positiveRows: toNumber(entry.positive_rows),
      negativeRows: toNumber(entry.negative_rows),
      auroc: toFiniteNumberOrNull(entry.auroc),
      averagePrecision: toFiniteNumberOrNull(entry.average_precision),
      precisionAtK: toFiniteNumberOrNull(entry.precision_at_k),
      positivesInTopK: toFiniteNumberOrNull(entry.positives_in_top_k),
      scoreLift: toFiniteNumberOrNull(entry.score_lift),
      meanPositiveScore: toFiniteNumberOrNull(entry.mean_positive_score),
      meanNegativeScore: toFiniteNumberOrNull(entry.mean_negative_score),
    });
  }

  return rows;
}

function parseEnsembleComparison(
  ensemble: unknown,
): ModelPerformanceEnsembleRow[] {
  if (!isRecord(ensemble)) return [];
  const perLabel = ensemble.per_weak_label;
  if (!isRecord(perLabel)) return [];

  const rows: ModelPerformanceEnsembleRow[] = [];
  const orderedKeys = [
    ...WEAK_LABEL_ORDER.filter((key) => key in perLabel),
    ...Object.keys(perLabel).filter(
      (key) => !WEAK_LABEL_ORDER.includes(key as (typeof WEAK_LABEL_ORDER)[number]),
    ),
  ];

  for (const key of orderedKeys) {
    const entry = perLabel[key];
    if (!isRecord(entry)) continue;
    const isolationForest = isRecord(entry.isolation_forest)
      ? entry.isolation_forest
      : {};
    const fallback = isRecord(entry.max_fallback) ? entry.max_fallback : {};
    rows.push({
      key,
      label: weakLabelLabel(key),
      isolationForestAuroc: toFiniteNumberOrNull(isolationForest.auroc),
      isolationForestAp: toFiniteNumberOrNull(isolationForest.average_precision),
      fallbackAuroc: toFiniteNumberOrNull(fallback.auroc),
      fallbackAp: toFiniteNumberOrNull(fallback.average_precision),
      aurocLift: toFiniteNumberOrNull(entry.auroc_lift_if_minus_fallback),
      apLift: toFiniteNumberOrNull(
        entry.average_precision_lift_if_minus_fallback,
      ),
    });
  }

  return rows;
}

function parseThresholdSweep(
  sweep: unknown,
): { amberSweep: ModelPerformanceThresholdRow[]; redSweep: ModelPerformanceThresholdRow[] } {
  if (!isRecord(sweep)) {
    return { amberSweep: [], redSweep: [] };
  }

  const amberRaw = Array.isArray(sweep.amber_quantile_sweep)
    ? sweep.amber_quantile_sweep
    : [];
  const redRaw = Array.isArray(sweep.red_quantile_sweep)
    ? sweep.red_quantile_sweep
    : [];

  const amberSweep: ModelPerformanceThresholdRow[] = amberRaw
    .filter(isRecord)
    .map((entry) => ({
      quantile: toNumber(entry.amber_quantile),
      scoreThreshold: toFiniteNumberOrNull(entry.score_threshold),
      flaggedRows: toNumber(entry.flagged_rows),
      flaggedShare: toFiniteNumberOrNull(entry.flagged_share),
      primaryRecall: null,
      primaryPrecision: null,
    }));

  const redSweep: ModelPerformanceThresholdRow[] = redRaw
    .filter(isRecord)
    .map((entry) => {
      const recall = isRecord(entry.weak_label_recall)
        ? entry.weak_label_recall
        : {};
      const primary = isRecord(recall[PRIMARY_WEAK_LABEL_KEY])
        ? (recall[PRIMARY_WEAK_LABEL_KEY] as Record<string, unknown>)
        : {};
      return {
        quantile: toNumber(entry.red_quantile),
        scoreThreshold: toFiniteNumberOrNull(entry.score_threshold),
        flaggedRows: toNumber(entry.flagged_rows),
        flaggedShare: toFiniteNumberOrNull(entry.flagged_share),
        primaryRecall: toFiniteNumberOrNull(primary.recall),
        primaryPrecision: toFiniteNumberOrNull(primary.precision),
      };
    });

  return { amberSweep, redSweep };
}

function parsePrimaryMortality(
  monthlyAlignment: Record<string, unknown>,
): ModelPerformanceReport["primaryMortality"] {
  const weakLabelMetrics = monthlyAlignment.weak_label_metrics;
  if (isRecord(weakLabelMetrics)) {
    const sameMonth = weakLabelMetrics.sovon_same_month_high_mortality;
    if (isRecord(sameMonth)) {
      return {
        auroc: toFiniteNumberOrNull(sameMonth.auroc),
        averagePrecision: toFiniteNumberOrNull(sameMonth.average_precision),
        positiveRows: toFiniteNumberOrNull(sameMonth.positive_rows),
        availableRows: toFiniteNumberOrNull(sameMonth.negative_rows),
      };
    }
  }

  // Stale-schema fallback: availability_stratified_metrics nesting.
  const stratified = monthlyAlignment.availability_stratified_metrics;
  if (isRecord(stratified)) {
    const sameMonth = stratified.sovon_same_month_high_mortality;
    if (isRecord(sameMonth) && isRecord(sameMonth.metrics_on_available_rows)) {
      const metrics = sameMonth.metrics_on_available_rows;
      return {
        auroc: toFiniteNumberOrNull(metrics.auroc),
        averagePrecision: toFiniteNumberOrNull(metrics.average_precision),
        positiveRows: toFiniteNumberOrNull(metrics.positive_rows),
        availableRows: toFiniteNumberOrNull(sameMonth.available_rows),
      };
    }
  }

  return null;
}

function parseLeadLag(
  monthlyAlignment: Record<string, unknown>,
): ModelPerformanceReport["leadLag"] {
  const leadLag = monthlyAlignment.lead_lag_alignment;
  if (!isRecord(leadLag)) return null;

  const byLag = isRecord(leadLag.metrics_by_lag_months)
    ? leadLag.metrics_by_lag_months
    : {};
  const points: ModelPerformanceLeadLagPoint[] = Object.values(byLag)
    .filter(isRecord)
    .map((entry) => ({
      lagMonths: toNumber(entry.lag_months),
      spearmanRho: toFiniteNumberOrNull(entry.spearman_rho),
      availableRows: toNumber(entry.available_rows),
      positiveRows: toNumber(entry.positive_rows),
    }))
    .sort((left, right) => left.lagMonths - right.lagMonths);

  if (
    points.length === 0 &&
    leadLag.best_spearman_rho === undefined &&
    leadLag.same_month_spearman_rho === undefined
  ) {
    return null;
  }

  return {
    points,
    bestLagMonths: toFiniteNumberOrNull(leadLag.best_spearman_lag_months),
    bestSpearmanRho: toFiniteNumberOrNull(leadLag.best_spearman_rho),
    sameMonthSpearmanRho: toFiniteNumberOrNull(leadLag.same_month_spearman_rho),
  };
}

function parseOutbreakHitRate(
  monthlyAlignment: Record<string, unknown>,
): ModelPerformanceReport["outbreakHitRate"] {
  const hitRate = monthlyAlignment.outbreak_month_hit_rate;
  if (!isRecord(hitRate) || Object.keys(hitRate).length === 0) return null;

  return {
    availableRows: toNumber(hitRate.available_rows),
    positiveRows: toNumber(hitRate.positive_rows),
    topK: toFiniteNumberOrNull(hitRate.top_k),
    positivesInTopK: toFiniteNumberOrNull(hitRate.positives_in_top_k),
    topKHitRate: toFiniteNumberOrNull(hitRate.top_k_hit_rate),
    alertedPositiveShare: toFiniteNumberOrNull(hitRate.alerted_positive_share),
  };
}

function parseNegativeControl(
  control: unknown,
): ModelPerformanceReport["negativeControl"] {
  if (!isRecord(control)) return null;

  const status = readString(control.status);
  const perLabelRaw = isRecord(control.per_weak_label)
    ? control.per_weak_label
    : {};
  const perLabel: ModelPerformanceControlRow[] = [];
  const orderedKeys = [
    ...WEAK_LABEL_ORDER.filter((key) => key in perLabelRaw),
    ...Object.keys(perLabelRaw).filter(
      (key) => !WEAK_LABEL_ORDER.includes(key as (typeof WEAK_LABEL_ORDER)[number]),
    ),
  ];

  for (const key of orderedKeys) {
    const entry = perLabelRaw[key];
    if (!isRecord(entry)) continue;
    const auroc = toFiniteNumberOrNull(entry.auroc);
    perLabel.push({
      key,
      label: weakLabelLabel(key),
      auroc,
      deviationFromHalf: auroc === null ? null : Math.abs(auroc - 0.5),
    });
  }

  if (status === null && perLabel.length === 0) return null;

  return {
    status,
    rows: toNumber(control.non_target_rows),
    perLabel,
  };
}

function parseCaveats(caveats: unknown): string[] {
  if (Array.isArray(caveats)) {
    return caveats.map(readString).filter((value): value is string => value !== null);
  }
  if (isRecord(caveats)) {
    return Object.values(caveats)
      .map(readString)
      .filter((value): value is string => value !== null);
  }
  return [];
}

function parseExpectedUnderNull(
  value: unknown,
): ModelPerformanceReport["expectedUnderNull"] {
  if (!isRecord(value)) return null;
  // A bare {status: ...} payload carries no usable volume metrics.
  if (
    value.eligible_rows === undefined &&
    value.expected_amber_alerts_per_year_under_null === undefined
  ) {
    return null;
  }
  return {
    eligibleRows: toFiniteNumberOrNull(value.eligible_rows),
    weeksObserved: toFiniteNumberOrNull(value.weeks_observed),
    meanEligibleRowsPerWeek: toFiniteNumberOrNull(
      value.mean_eligible_rows_per_week,
    ),
    empiricalAmberFraction: toFiniteNumberOrNull(value.empirical_amber_fraction),
    empiricalRedFraction: toFiniteNumberOrNull(value.empirical_red_fraction),
    expectedAmberPerYear: toFiniteNumberOrNull(
      value.expected_amber_alerts_per_year_under_null,
    ),
    expectedRedPerYear: toFiniteNumberOrNull(
      value.expected_red_alerts_per_year_under_null,
    ),
    note: readString(value.note),
  };
}

function parseScoreSummary(
  value: unknown,
): ModelPerformanceReport["scoreSummary"] {
  if (!isRecord(value)) return null;
  return {
    min: toNumber(value.min),
    median: toNumber(value.median),
    mean: toNumber(value.mean),
    p95: toNumber(value.p95),
    max: toNumber(value.max),
  };
}

function parseBestF1Rows(value: unknown): ModelPerformanceBestF1Row[] {
  const rows = Array.isArray(value)
    ? value
    : isRecord(value) && Array.isArray(value.rows)
      ? value.rows
      : [];
  return rows.filter(isRecord).map((entry) => ({
    claimType: readString(entry.claim_type) ?? readString(entry.claimType) ?? "",
    grain: readString(entry.grain) ?? "",
    signal: readString(entry.signal) ?? "",
    scoreColumn: readString(entry.score_column) ?? readString(entry.scoreColumn) ?? "",
    scoreAggregation:
      readString(entry.score_aggregation) ?? readString(entry.scoreAggregation) ?? "",
    label: readString(entry.label) ?? "",
    horizonWeeks: toNumber(entry.horizon_weeks ?? entry.horizonWeeks),
    decisionRule:
      readString(entry.decision_rule) ?? readString(entry.decisionRule) ?? "",
    alertLevel: readString(entry.alert_level) ?? readString(entry.alertLevel) ?? "",
    labelOptimized: Boolean(entry.label_optimized ?? entry.labelOptimized),
    upperBound: Boolean(entry.upper_bound ?? entry.upperBound),
    f1: toFiniteNumberOrNull(entry.f1),
    precision: toFiniteNumberOrNull(entry.precision),
    recall: toFiniteNumberOrNull(entry.recall),
    alertRate: toFiniteNumberOrNull(entry.alert_rate ?? entry.alertRate),
    threshold: toFiniteNumberOrNull(entry.threshold),
    positiveRows: toNumber(entry.positive_rows ?? entry.positiveRows),
    negativeRows: toNumber(entry.negative_rows ?? entry.negativeRows),
    commonRows: toNumber(entry.common_rows ?? entry.commonRows),
    commonUnits: toFiniteNumberOrNull(entry.common_units ?? entry.commonUnits),
    interpretation: readString(entry.interpretation),
  }));
}

function parseBestF1ByGrainSignal(value: unknown): ModelPerformanceBestF1Row[] {
  if (!isRecord(value) || !Array.isArray(value.best_by_grain_signal)) return [];
  return parseBestF1Rows(value.best_by_grain_signal);
}

/**
 * Parse the persisted modeling validation-report JSON into a frontend-friendly
 * model performance report. Returns null when no report is available or the
 * payload is unparseable so the UI can fall back gracefully.
 */
export function parseModelPerformanceReport(
  rawJson: string | null,
): ModelPerformanceReport | null {
  if (!rawJson) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(rawJson);
  } catch {
    return null;
  }

  if (!isRecord(parsed)) return null;

  const validation = isRecord(parsed.validation)
    ? parsed.validation
    : parsed; // tolerate a report stored without the {validation: ...} envelope
  if (!isRecord(validation)) return null;

  const runId =
    readString(parsed.run_id) ?? readString(validation.run_id) ?? null;
  const generatedAt =
    readString(parsed.generated_at) ??
    readString(validation.generated_at) ??
    null;

  const alertCountsRaw = isRecord(validation.alert_counts)
    ? validation.alert_counts
    : {};
  const monthlyAlignment = isRecord(validation.monthly_alignment)
    ? validation.monthly_alignment
    : {};
  const bestF1Raw = validation.best_f1_leaderboard;

  const weakLabels = isRecord(validation.weak_label_metrics)
    ? parseWeakLabelMetrics(validation.weak_label_metrics)
    : [];
  const { amberSweep, redSweep } = parseThresholdSweep(
    validation.alert_threshold_sweep,
  );

  return {
    available: true,
    unavailableReason: null,
    runId,
    generatedAt,
    reportVersion: readString(validation.report_version),
    rowsScored: toNumber(validation.rows_scored),
    topK: toFiniteNumberOrNull(validation.top_k),
    alertCounts: {
      green: toNumber(alertCountsRaw.green),
      amber: toNumber(alertCountsRaw.amber),
      red: toNumber(alertCountsRaw.red),
    },
    expectedUnderNull: parseExpectedUnderNull(
      validation.expected_alert_volume_under_null,
    ),
    scoreSummary: parseScoreSummary(validation.score_summary),
    weakLabels,
    primaryMortality: parsePrimaryMortality(monthlyAlignment),
    ensemble: parseEnsembleComparison(validation.ensemble_comparison),
    amberSweep,
    redSweep,
    leadLag: parseLeadLag(monthlyAlignment),
    outbreakHitRate: parseOutbreakHitRate(monthlyAlignment),
    negativeControl: parseNegativeControl(
      validation.non_target_species_control,
    ),
    bestF1Leaderboard: parseBestF1Rows(bestF1Raw),
    bestF1ByGrainSignal: parseBestF1ByGrainSignal(bestF1Raw),
    caveats: parseCaveats(validation.caveats),
    spatialValidation: parseSpatialValidation(parsed),
    scoringMode:
      readString(parsed.scoring_mode) ??
      readString(validation.scoring_mode) ??
      null,
  };
}

/** Map an AUROC to a 0..1 "skill above chance" value: max(0, (a - 0.5) * 2). */
export function aurocSkill(auroc: number | null): number | null {
  if (auroc === null) return null;
  return Math.max(0, (auroc - 0.5) * 2);
}

export function formatMetric(value: number | null, digits = 3): string {
  if (value === null) return "n/a";
  return value.toFixed(digits);
}

export function formatPercent(value: number | null, digits = 1): string {
  if (value === null) return "n/a";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatCount(value: number | null): string {
  if (value === null) return "n/a";
  return Math.round(value).toLocaleString("en-GB");
}

/** Classify an AUROC into a qualitative band for colour coding. */
export function aurocBand(
  auroc: number | null,
): "unknown" | "below" | "chance" | "weak" | "strong" {
  if (auroc === null) return "unknown";
  if (auroc < 0.45) return "below";
  if (auroc < 0.55) return "chance";
  if (auroc < 0.65) return "weak";
  return "strong";
}
