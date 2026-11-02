// Typed parser for the `spatial_validation` section of a stored validation
// report. Current `build_validation_report` output no longer includes it, so the
// parser returns null for those reports. This is the
// data contract the evaluation UI consumes: forward (early-warning) ranking,
// within-week case-control lift, lead-time, and acoustic coverage, for each
// score variant (fusion / bioacoustic / population).
//
// It is intentionally self-contained (only depends on the report JSON shape) so
// it can be unit-tested without the rest of the alerts data layer.

export type SpatialRankingMetric = {
  status: string;
  positiveRows: number;
  negativeRows: number;
  evaluableRows: number;
  scoreLift: number | null;
  precisionAtK: number | null;
  auroc: number | null;
  averagePrecision: number | null;
};

export type SpatialCaseControl = {
  status: string;
  contrastWeeks: number;
  caseRows: number;
  meanWeekLift: number | null;
  permutationPValue: number | null;
};

export type SpatialLeadTime = {
  events: number;
  detectedEvents: number;
  detectionRate: number | null;
  meanLeadWeeks: number | null;
  medianLeadWeeks: number | null;
  maxLeadWeeks: number | null;
};

export type SpatialVariant = {
  variant: string;
  scoreColumn: string;
  // label -> horizon(weeks, string) -> metric
  forwardRanking: Record<string, Record<string, SpatialRankingMetric>>;
  caseControl: Record<string, Record<string, SpatialCaseControl>>;
  // event -> horizon -> lead time
  leadTime: Record<string, Record<string, SpatialLeadTime>>;
};

export type SpatialCoverage = {
  spatialGrain: string;
  unitLabel: string;
  totalUnits: number | null;
  totalGridCells: number | null;
  acousticFilterApplied: boolean;
  acousticCoverageColumns: string[];
  acousticCoveredShare: number | null;
  acousticEligibilityShare: number | null;
  adminLabelUnits: number | null;
  adminScoreUnits: number | null;
  unmatchedAdminLabelUnits: number | null;
  crosswalkSource: string | null;
};

// Operating-point scorecard at the amber/red decision (fusion alert_level).
// recall is the headline early-warning number; alertRate is its ungameable cost;
// maskedPrecision is valid only inside the evaluable set.
export type SpatialOperatingPointHorizon = {
  status: string;
  nPositive: number;
  nDetected: number;
  recall: number | null;
  maskedPrecision: number | null;
};

export type SpatialOperatingPointEntry = {
  nEligible: number;
  nAlert: number;
  alertRate: number | null;
  byHorizon: Record<string, SpatialOperatingPointHorizon>;
};

export type SpatialOperatingPoint = {
  status: string;
  decisionColumn: string;
  // operating point name (amber_red | red_only) -> label -> entry
  operatingPoints: Record<string, Record<string, SpatialOperatingPointEntry>>;
};

export type SpatialEvaluationTableRow = {
  status: string;
  spatialGrain: string;
  spatialUnitLabel: string;
  anomalyType: string;
  scoreColumn: string;
  groundTruthKey: string;
  horizonWeeks: number;
  alertLevel: string;
  decisionRule: string;
  groundTruthScope: string;
  spatiallyResolved: boolean;
  alertRateCap: number | null;
  threshold: number | null;
  totalUnits: number | null;
  totalGridCells: number | null;
  commonUnits: number | null;
  commonGridCells: number | null;
  scoreCoveredUnits: number | null;
  scoreCoveredGridCells: number | null;
  groundTruthCoveredUnits: number | null;
  groundTruthCoveredGridCells: number | null;
  scoreCoveredRows: number;
  groundTruthCoveredRows: number;
  commonRows: number;
  positiveRows: number;
  negativeRows: number;
  alertRows: number;
  truePositiveRows: number;
  falsePositiveRows: number;
  trueNegativeRows: number;
  falseNegativeRows: number;
  alertRate: number | null;
  recall: number | null;
  precision: number | null;
  f1: number | null;
  specificity: number | null;
  balancedAccuracy: number | null;
};

export type SpatialEvaluationTableFilters = {
  anomalyType: string;
  alertLevel: string;
  horizonWeeks: number;
};

// Run-context for the evaluation header strip. scoring_mode and run_id live in
// the report *wrapper* (not the spatial_validation section); the scoring window
// is encoded in the run id (…_YYYYMMDD_YYYYMMDD_…) as there is no explicit field.
export type SpatialRunContext = {
  runId: string | null;
  scoringMode: string | null;
  rowsScored: number | null;
  windowStart: string | null;
  windowEnd: string | null;
};

export type SpatialNationalContext = {
  status: string;
  spatiallyResolved: boolean;
  groundTruthScope: string | null;
  sovonContextRows: number | null;
  note: string | null;
};

export type SpatialValidationReport = {
  status: string;
  spatialGrain: string;
  unitLabel: string;
  scoreVariants: string[];
  horizonsWeeks: number[];
  labelDirection: string;
  runContext: SpatialRunContext;
  coverage: SpatialCoverage;
  evaluationTable: SpatialEvaluationTableRow[];
  operatingPoint: SpatialOperatingPoint | null;
  variants: SpatialVariant[];
  nationalContext: SpatialNationalContext | null;
  grainReports: SpatialValidationReport[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toNumOrNull(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toNum(value: unknown, fallback = 0): number {
  return toNumOrNull(value) ?? fallback;
}

function toStr(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function parseRankingMetric(raw: unknown): SpatialRankingMetric {
  const r = isRecord(raw) ? raw : {};
  return {
    status: toStr(r.status, "unknown"),
    positiveRows: toNum(r.positive_rows),
    negativeRows: toNum(r.negative_rows),
    evaluableRows: toNum(r.evaluable_rows),
    scoreLift: toNumOrNull(r.score_lift),
    precisionAtK: toNumOrNull(r.precision_at_k),
    auroc: toNumOrNull(r.auroc),
    averagePrecision: toNumOrNull(r.average_precision),
  };
}

function parseCaseControl(raw: unknown): SpatialCaseControl {
  const r = isRecord(raw) ? raw : {};
  return {
    status: toStr(r.status, "unknown"),
    contrastWeeks: toNum(r.contrast_weeks),
    caseRows: toNum(r.case_rows),
    meanWeekLift: toNumOrNull(r.mean_week_lift),
    permutationPValue: toNumOrNull(r.permutation_p_value),
  };
}

function parseLeadTime(raw: unknown): SpatialLeadTime {
  const r = isRecord(raw) ? raw : {};
  return {
    events: toNum(r.events),
    detectedEvents: toNum(r.detected_events),
    detectionRate: toNumOrNull(r.detection_rate),
    meanLeadWeeks: toNumOrNull(r.mean_lead_weeks),
    medianLeadWeeks: toNumOrNull(r.median_lead_weeks),
    maxLeadWeeks: toNumOrNull(r.max_lead_weeks),
  };
}

function parseNestedByHorizon<T>(
  raw: unknown,
  parse: (value: unknown) => T,
): Record<string, Record<string, T>> {
  const out: Record<string, Record<string, T>> = {};
  if (!isRecord(raw)) return out;
  for (const [labelKey, byHorizon] of Object.entries(raw)) {
    if (!isRecord(byHorizon)) continue;
    const inner: Record<string, T> = {};
    for (const [horizon, metric] of Object.entries(byHorizon)) {
      // Skip scalar bookkeeping keys (e.g. lead_time's "alert_quantile").
      if (!isRecord(metric)) continue;
      inner[horizon] = parse(metric);
    }
    out[labelKey] = inner;
  }
  return out;
}

function parseVariant(name: string, raw: unknown): SpatialVariant {
  const r = isRecord(raw) ? raw : {};
  return {
    variant: name,
    scoreColumn: toStr(r.score_column),
    forwardRanking: parseNestedByHorizon(r.forward_ranking, parseRankingMetric),
    caseControl: parseNestedByHorizon(r.within_week_case_control, parseCaseControl),
    leadTime: parseNestedByHorizon(r.lead_time, parseLeadTime),
  };
}

function parseCoverage(raw: unknown): SpatialCoverage {
  const r = isRecord(raw) ? raw : {};
  return {
    spatialGrain: toStr(r.spatial_grain, "grid"),
    unitLabel: toStr(r.unit_label, "grid cells"),
    totalUnits: toNumOrNull(r.total_units ?? r.total_grid_cells),
    totalGridCells: toNumOrNull(r.total_grid_cells),
    acousticFilterApplied: r.acoustic_filter_applied === true,
    acousticCoverageColumns: Array.isArray(r.acoustic_coverage_columns)
      ? r.acoustic_coverage_columns.map((c) => String(c))
      : [],
    acousticCoveredShare: toNumOrNull(r.acoustic_covered_share),
    acousticEligibilityShare: toNumOrNull(r.acoustic_eligibility_share),
    adminLabelUnits: toNumOrNull(r.admin_label_units),
    adminScoreUnits: toNumOrNull(r.admin_score_units),
    unmatchedAdminLabelUnits: toNumOrNull(r.unmatched_admin_label_units),
    crosswalkSource:
      typeof r.crosswalk_source === "string" ? r.crosswalk_source : null,
  };
}

function parseOperatingPointHorizon(raw: unknown): SpatialOperatingPointHorizon {
  const r = isRecord(raw) ? raw : {};
  return {
    status: toStr(r.status, "unknown"),
    nPositive: toNum(r.n_positive),
    nDetected: toNum(r.n_detected),
    recall: toNumOrNull(r.recall),
    maskedPrecision: toNumOrNull(r.masked_precision),
  };
}

function parseOperatingPointEntry(raw: unknown): SpatialOperatingPointEntry {
  const r = isRecord(raw) ? raw : {};
  const byHorizon: Record<string, SpatialOperatingPointHorizon> = {};
  if (isRecord(r.by_horizon)) {
    for (const [horizon, metric] of Object.entries(r.by_horizon)) {
      if (!isRecord(metric)) continue;
      byHorizon[horizon] = parseOperatingPointHorizon(metric);
    }
  }
  return {
    nEligible: toNum(r.n_eligible),
    nAlert: toNum(r.n_alert),
    alertRate: toNumOrNull(r.alert_rate),
    byHorizon,
  };
}

function parseOperatingPoint(raw: unknown): SpatialOperatingPoint | null {
  if (!isRecord(raw)) return null;
  const status = toStr(raw.status, "unknown");
  if (status !== "ok") return null;
  const operatingPoints: Record<
    string,
    Record<string, SpatialOperatingPointEntry>
  > = {};
  if (isRecord(raw.operating_points)) {
    for (const [opName, byLabel] of Object.entries(raw.operating_points)) {
      if (!isRecord(byLabel)) continue;
      const labels: Record<string, SpatialOperatingPointEntry> = {};
      for (const [label, entry] of Object.entries(byLabel)) {
        if (!isRecord(entry)) continue;
        labels[label] = parseOperatingPointEntry(entry);
      }
      operatingPoints[opName] = labels;
    }
  }
  return {
    status,
    decisionColumn: toStr(raw.decision_column, "alert_level"),
    operatingPoints,
  };
}

function parseEvaluationTableRow(raw: unknown): SpatialEvaluationTableRow | null {
  if (!isRecord(raw)) return null;
  const recall = toNumOrNull(raw.recall);
  const precision = toNumOrNull(raw.precision);
  const rawF1 = toNumOrNull(raw.f1);
  const f1 =
    rawF1 ??
    (recall !== null && precision !== null
      ? recall + precision > 0
        ? (2 * recall * precision) / (recall + precision)
        : 0
      : null);

  return {
    status: toStr(raw.status, "unknown"),
    spatialGrain: toStr(raw.spatial_grain, "grid"),
    spatialUnitLabel: toStr(raw.spatial_unit_label, "grid cells"),
    anomalyType: toStr(raw.anomaly_type),
    scoreColumn: toStr(raw.score_column),
    groundTruthKey: toStr(raw.ground_truth_key),
    horizonWeeks: toNum(raw.horizon_weeks),
    alertLevel: toStr(raw.alert_level),
    decisionRule: toStr(raw.decision_rule),
    groundTruthScope: toStr(raw.ground_truth_scope, "grid_week"),
    spatiallyResolved: raw.spatially_resolved !== false,
    alertRateCap: toNumOrNull(raw.alert_rate_cap),
    threshold: toNumOrNull(raw.threshold),
    totalUnits: toNumOrNull(raw.total_units ?? raw.total_grid_cells),
    totalGridCells: toNumOrNull(raw.total_grid_cells),
    commonUnits: toNumOrNull(raw.common_units ?? raw.common_grid_cells),
    commonGridCells: toNumOrNull(raw.common_grid_cells),
    scoreCoveredUnits: toNumOrNull(
      raw.score_covered_units ?? raw.score_covered_grid_cells,
    ),
    scoreCoveredGridCells: toNumOrNull(raw.score_covered_grid_cells),
    groundTruthCoveredUnits: toNumOrNull(
      raw.ground_truth_covered_units ?? raw.ground_truth_covered_grid_cells,
    ),
    groundTruthCoveredGridCells: toNumOrNull(raw.ground_truth_covered_grid_cells),
    scoreCoveredRows: toNum(raw.score_covered_rows),
    groundTruthCoveredRows: toNum(raw.ground_truth_covered_rows),
    commonRows: toNum(raw.common_rows),
    positiveRows: toNum(raw.positive_rows),
    negativeRows: toNum(raw.negative_rows),
    alertRows: toNum(raw.alert_rows),
    truePositiveRows: toNum(raw.true_positive_rows),
    falsePositiveRows: toNum(raw.false_positive_rows),
    trueNegativeRows: toNum(raw.true_negative_rows),
    falseNegativeRows: toNum(raw.false_negative_rows),
    alertRate: toNumOrNull(raw.alert_rate),
    recall,
    precision,
    f1,
    specificity: toNumOrNull(raw.specificity),
    balancedAccuracy: toNumOrNull(raw.balanced_accuracy),
  };
}

function parseEvaluationTable(raw: unknown): SpatialEvaluationTableRow[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map(parseEvaluationTableRow)
    .filter((row): row is SpatialEvaluationTableRow => row !== null);
}

function parseNationalContext(raw: unknown): SpatialNationalContext | null {
  if (!isRecord(raw)) return null;
  return {
    status: toStr(raw.status, "unknown"),
    spatiallyResolved: raw.spatially_resolved === true,
    groundTruthScope:
      typeof raw.ground_truth_scope === "string" ? raw.ground_truth_scope : null,
    sovonContextRows: toNumOrNull(raw.sovon_context_rows),
    note: typeof raw.note === "string" ? raw.note : null,
  };
}

function deriveRunWindow(runId: string | null): {
  start: string | null;
  end: string | null;
} {
  if (!runId) return { start: null, end: null };
  const m = runId.match(/(\d{4})(\d{2})(\d{2})_(\d{4})(\d{2})(\d{2})/);
  if (!m) return { start: null, end: null };
  return { start: `${m[1]}-${m[2]}-${m[3]}`, end: `${m[4]}-${m[5]}-${m[6]}` };
}

function parseRunContext(wrapper: Record<string, unknown>): SpatialRunContext {
  const runId = typeof wrapper.run_id === "string" ? wrapper.run_id : null;
  const scoringMode =
    typeof wrapper.scoring_mode === "string" ? wrapper.scoring_mode : null;
  const validation = isRecord(wrapper.validation) ? wrapper.validation : null;
  const rowsScored = validation ? toNumOrNull(validation.rows_scored) : null;
  const explicitWindow =
    (isRecord(wrapper.scored_window) ? wrapper.scored_window : null) ??
    (validation && isRecord(validation.scored_window)
      ? (validation.scored_window as Record<string, unknown>)
      : null);
  const window =
    explicitWindow && typeof explicitWindow.start === "string"
      ? {
          start: explicitWindow.start as string,
          end:
            typeof explicitWindow.end === "string"
              ? (explicitWindow.end as string)
              : null,
        }
      : deriveRunWindow(runId);
  return {
    runId,
    scoringMode,
    rowsScored,
    windowStart: window.start,
    windowEnd: window.end,
  };
}

/**
 * Classify a tier's operating-point result into a single verdict the matrix can
 * colour on. This is the only place the skill semantics live:
 *  - "uncalibrated": the decision flags an implausible share of the eligible set
 *    (>50%), so recall is meaningless. This is the degenerate signature the
 *    alert-calibration guard now suppresses upstream.
 *  - "inconclusive": no ground-truth positives in the evaluable set (or the metric
 *    did not compute), so there is nothing to measure recall against.
 *  - "skill": the decision recovers positives at a higher rate than its own
 *    alert base rate (recall > alert_rate) -- better than flagging at random.
 *  - "no_skill": recall does not beat the alert base rate.
 */
export type SpatialVerdict = "skill" | "no_skill" | "inconclusive" | "uncalibrated";

export function spatialVerdict(
  entry: SpatialOperatingPointEntry,
  horizon: number,
): SpatialVerdict {
  if (entry.alertRate !== null && entry.alertRate > 0.5) return "uncalibrated";
  const horizonMetric = entry.byHorizon[String(horizon)];
  if (
    !horizonMetric ||
    horizonMetric.status !== "ok" ||
    horizonMetric.nPositive <= 0 ||
    horizonMetric.recall === null ||
    entry.alertRate === null
  ) {
    return "inconclusive";
  }
  return horizonMetric.recall > entry.alertRate ? "skill" : "no_skill";
}

/**
 * Extract the spatial validation section from a model performance report.
 *
 * Accepts either the full wrapper (`{ validation: { spatial_validation } }`),
 * the report object (`{ spatial_validation }`), or the section itself. Returns
 * `null` when no usable spatial section is present (e.g. an older report).
 */
export function parseSpatialValidation(raw: unknown): SpatialValidationReport | null {
  if (!isRecord(raw)) return null;

  // Unwrap common nestings.
  let section: unknown = raw;
  if (isRecord(raw.validation) && isRecord(raw.validation.spatial_validation)) {
    section = raw.validation.spatial_validation;
  } else if (isRecord(raw.spatial_validation)) {
    section = raw.spatial_validation;
  }
  if (!isRecord(section)) return null;

  const runContext = parseRunContext(raw);
  return parseSpatialSection(section, runContext, true);
}

function parseSpatialSection(
  section: Record<string, unknown>,
  runContext: SpatialRunContext,
  includeGrains: boolean,
): SpatialValidationReport | null {
  const status = toStr(section.status, "unknown");
  if (status !== "ok") {
    // Present but empty (no spatial rows / older schema): nothing to render.
    return null;
  }

  const perVariant = isRecord(section.per_variant) ? section.per_variant : {};
  const variants: SpatialVariant[] = Object.entries(perVariant).map(
    ([name, value]) => parseVariant(name, value),
  );

  return {
    status,
    scoreVariants: Array.isArray(section.score_variants)
      ? section.score_variants.map((v) => String(v))
      : variants.map((v) => v.variant),
    horizonsWeeks: Array.isArray(section.horizons_weeks)
      ? section.horizons_weeks.map((h) => toNum(h)).filter((h) => Number.isFinite(h))
      : [],
    labelDirection: toStr(section.label_direction, "unknown"),
    spatialGrain: toStr(section.spatial_grain, "grid"),
    unitLabel: toStr(section.unit_label, "grid cells"),
    runContext,
    coverage: parseCoverage(section.coverage),
    evaluationTable: parseEvaluationTable(section.evaluation_table),
    operatingPoint: parseOperatingPoint(section.operating_point),
    variants,
    nationalContext: parseNationalContext(section.national_context),
    grainReports: includeGrains
      ? parseGrainReports(section, runContext)
      : [],
  };
}

function parseGrainReports(
  section: Record<string, unknown>,
  runContext: SpatialRunContext,
): SpatialValidationReport[] {
  const reports: SpatialValidationReport[] = [];
  for (const key of ["admin_area", "grid"]) {
    const candidate = section[key];
    if (!isRecord(candidate)) continue;
    const parsed = parseSpatialSection(candidate, runContext, false);
    if (parsed && parsed.evaluationTable.length > 0) {
      reports.push(parsed);
    }
  }
  return reports;
}

export const EVALUATION_LABEL_TAXONOMY = ["sovon_mortality_spike"] as const;

export function listSpatialValidationLabels(report: SpatialValidationReport): string[] {
  const present = new Set<string>();
  for (const row of report.evaluationTable) {
    present.add(row.groundTruthKey);
  }
  for (const variant of report.variants) {
    for (const label of Object.keys(variant.forwardRanking)) {
      present.add(label);
    }
  }

  const ordered = EVALUATION_LABEL_TAXONOMY.filter(
    (label) => present.has(label) || label in SPATIAL_LABEL_NAMES,
  );
  const extras = [...present].filter(
    (label) =>
      !EVALUATION_LABEL_TAXONOMY.includes(
        label as (typeof EVALUATION_LABEL_TAXONOMY)[number],
      ),
  );
  return [...ordered, ...extras];
}

export function hasSpatialEvaluationContrast(
  report: SpatialValidationReport,
  labelKey: string,
  horizon: number,
): boolean {
  const horizonKey = String(horizon);
  return report.variants.some(
    (variant) => variant.forwardRanking[labelKey]?.[horizonKey]?.status === "ok",
  );
}

export function filterSpatialEvaluationTableRows(
  report: SpatialValidationReport,
  filters: SpatialEvaluationTableFilters,
): SpatialEvaluationTableRow[] {
  return report.evaluationTable.filter(
    (row) =>
      row.anomalyType === filters.anomalyType &&
      row.alertLevel === filters.alertLevel &&
      row.horizonWeeks === filters.horizonWeeks,
  );
}

/** Human-friendly labels for the ground-truth tiers used in the UI. */
export const SPATIAL_LABEL_NAMES: Record<string, string> = {
  sovon_mortality_spike: "SOVON mortality context (species-month)",
};

export const SPATIAL_VARIANT_NAMES: Record<string, string> = {
  fusion: "Fusion (operational)",
  embedding: "Bioacoustic only",
  bioacoustic_excess: "Bioacoustic excess",
  bioacoustic_outlier: "Bioacoustic outlier share",
  population: "Population only",
};

export const SPATIAL_ALERT_LEVEL_NAMES: Record<string, string> = {
  amber: "Amber",
  red: "Red",
  optimized: "Optimized",
};
