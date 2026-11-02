import {
  TARGET_SPECIES,
  TARGET_SPECIES_BY_BIRDWEATHER_ID,
  TARGET_SPECIES_LIST,
} from "../../config/target-species";

export type AlertsEvaluationTrendPoint = {
  species: string;
  periodStart: string;
  weekLabel: string;
  rowCount: number;
  anomalyCount: number;
  amberAnomalyCount: number;
  redAnomalyCount: number;
  meanModelAnomalyScore: number;
  meanPopulationAnomalyScore: number;
  meanEmbeddingAnomalyScore: number;
  sovonDeadBirdCount: number;
  sovonMortalityScore: number | null;
};

export type AlertsEvaluationSummary = {
  speciesCount: number;
  weekCount: number;
  anomalyCount: number;
  redAnomalyCount: number;
  sovonDeadBirdCount: number;
};

export type AlertsEvaluationData = {
  available: boolean;
  coverageLabel: string;
  latestRunId: string | null;
  latestGeneratedAt: string | null;
  speciesOptions: string[];
  trendPoints: AlertsEvaluationTrendPoint[];
  summary: AlertsEvaluationSummary;
};

export type EvaluationAlignmentScoreKey =
  | "meanModelAnomalyScore"
  | "meanPopulationAnomalyScore"
  | "meanEmbeddingAnomalyScore";

export type EvaluationAlignmentSignalKey = "sovonDeadBirdCount";

export type EvaluationAlignmentPoint = {
  periodStart: string;
} & Partial<Record<EvaluationAlignmentScoreKey | EvaluationAlignmentSignalKey, number | null>>;

export type EvaluationAlignmentMetric = {
  signalKey: EvaluationAlignmentSignalKey;
  signalLabel: string;
  scoreKey: EvaluationAlignmentScoreKey;
  scoreLabel: string;
  sameWeekSpearman: number | null;
  sameWeekPairCount: number;
  bestLagWeeks: number | null;
  bestLagSpearman: number | null;
  bestLagPairCount: number;
  eventWeeks: number;
  eventMeanScore: number | null;
  quietMeanScore: number | null;
  eventLift: number | null;
  eventLiftRatio: number | null;
  leadWindowWeeks: number;
  leadWindowHits: number;
  leadWindowEvents: number;
  leadWindowHitRate: number | null;
};

export const EVALUATION_ALIGNMENT_SCORE_OPTIONS: Array<{
  key: EvaluationAlignmentScoreKey;
  label: string;
}> = [
  { key: "meanModelAnomalyScore", label: "Overall anomaly" },
  { key: "meanPopulationAnomalyScore", label: "Population anomaly" },
  { key: "meanEmbeddingAnomalyScore", label: "Embedding anomaly" },
];

const EVALUATION_ALIGNMENT_SIGNAL_OPTIONS: Array<{
  key: EvaluationAlignmentSignalKey;
  label: string;
}> = [
  { key: "sovonDeadBirdCount", label: "SOVON mortality" },
];

export type WeeklyAnomalyEvaluationRow = {
  species_english?: string;
  species_id?: string;
  period_start?: string;
  alert_level?: string;
  model_anomaly_score?: string;
  final_score?: string;
  population_anomaly_score?: string;
  count_anomaly_score?: string;
  embedding_anomaly_score?: string;
  embedding_drift_score?: string;
  sovon_dead_bird_count?: string;
  sovon_mortality_score?: string;
};

type TrendAccumulator = {
  speciesKey: string;
  species: string;
  periodStart: string;
  rowCount: number;
  anomalyCount: number;
  amberAnomalyCount: number;
  redAnomalyCount: number;
  modelScoreSum: number;
  populationScoreSum: number;
  embeddingScoreSum: number;
  sovonDeadBirdCount: number;
  sovonMortalityScore: number | null;
};

type SpeciesReferenceRow = {
  species_english?: string;
  species_id?: string;
};

const TARGET_SPECIES_ORDER = new Map(
  TARGET_SPECIES_LIST.map((species, index) => [species.english, index]),
);

function toNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function cleanString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function speciesLabel(row: SpeciesReferenceRow): string {
  const speciesId = cleanString(row.species_id);
  const targetSpecies = speciesId
    ? TARGET_SPECIES_BY_BIRDWEATHER_ID.get(speciesId)
    : undefined;

  return targetSpecies?.english || cleanString(row.species_english) || speciesId || "Unknown species";
}

function isTargetSpecies(row: SpeciesReferenceRow): boolean {
  const speciesId = cleanString(row.species_id);
  if (speciesId) {
    return TARGET_SPECIES_BY_BIRDWEATHER_ID.has(speciesId);
  }

  const label = cleanString(row.species_english);
  return label ? TARGET_SPECIES.has(label) : false;
}

function speciesKey(row: SpeciesReferenceRow): string {
  const speciesId = cleanString(row.species_id);
  if (speciesId) return `id:${speciesId}`;

  const label = speciesLabel(row).trim().toLowerCase();
  return `label:${label}`;
}

function weekLabel(periodStart: string): string {
  const parsed = new Date(`${periodStart}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return periodStart;

  return new Intl.DateTimeFormat("en-GB", {
    month: "short",
    day: "2-digit",
  }).format(parsed);
}

function emptySummary(): AlertsEvaluationSummary {
  return {
    speciesCount: 0,
    weekCount: 0,
    anomalyCount: 0,
    redAnomalyCount: 0,
    sovonDeadBirdCount: 0,
  };
}

function dateInRange(periodStart: string, dateFromIso?: string | null, dateToIso?: string | null): boolean {
  if (!periodStart) return false;
  if (dateFromIso && periodStart < dateFromIso.slice(0, 10)) return false;
  if (dateToIso && periodStart > dateToIso.slice(0, 10)) return false;
  return true;
}

function maxKnown(...values: unknown[]): number {
  return Math.max(0, ...values.map(toNumber));
}

function addUtcWeeks(isoDate: string, weeks: number) {
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  parsed.setUTCDate(parsed.getUTCDate() + weeks * 7);
  return parsed.toISOString().slice(0, 10);
}

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function quantile(values: number[], percentile: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil(sorted.length * percentile) - 1),
  );
  return sorted[index] ?? null;
}

function ranks(values: number[]): number[] {
  const indexed = values
    .map((value, index) => ({ value, index }))
    .sort((left, right) => left.value - right.value || left.index - right.index);
  const ranked = Array.from<number>({ length: values.length });
  let cursor = 0;

  while (cursor < indexed.length) {
    let end = cursor + 1;
    while (end < indexed.length && indexed[end]?.value === indexed[cursor]?.value) {
      end += 1;
    }

    const averageRank = (cursor + 1 + end) / 2;
    for (let index = cursor; index < end; index += 1) {
      ranked[indexed[index].index] = averageRank;
    }
    cursor = end;
  }

  return ranked;
}

function pearsonCorrelation(pairs: Array<[number, number]>, minPairs: number): number | null {
  if (pairs.length < minPairs) return null;

  const leftMean = mean(pairs.map(([left]) => left));
  const rightMean = mean(pairs.map(([, right]) => right));
  if (leftMean === null || rightMean === null) return null;

  let numerator = 0;
  let leftSumSq = 0;
  let rightSumSq = 0;
  for (const [left, right] of pairs) {
    const leftDelta = left - leftMean;
    const rightDelta = right - rightMean;
    numerator += leftDelta * rightDelta;
    leftSumSq += leftDelta ** 2;
    rightSumSq += rightDelta ** 2;
  }

  const denominator = Math.sqrt(leftSumSq * rightSumSq);
  if (denominator === 0) return null;
  return numerator / denominator;
}

function spearmanCorrelation(pairs: Array<[number, number]>, minPairs: number): number | null {
  if (pairs.length < minPairs) return null;

  const leftRanks = ranks(pairs.map(([left]) => left));
  const rightRanks = ranks(pairs.map(([, right]) => right));
  return pearsonCorrelation(
    pairs.map((_, index) => [leftRanks[index], rightRanks[index]]),
    minPairs,
  );
}

function finiteMetricValue(
  point: EvaluationAlignmentPoint | undefined,
  key: EvaluationAlignmentScoreKey | EvaluationAlignmentSignalKey,
): number {
  const value = point?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function upsertTrend(
  accumulators: Map<string, TrendAccumulator>,
  speciesKey: string,
  species: string,
  periodStart: string,
): TrendAccumulator {
  const key = `${speciesKey}::${periodStart}`;
  const current = accumulators.get(key);
  if (current) return current;

  const next: TrendAccumulator = {
    speciesKey,
    species,
    periodStart,
    rowCount: 0,
    anomalyCount: 0,
    amberAnomalyCount: 0,
    redAnomalyCount: 0,
    modelScoreSum: 0,
    populationScoreSum: 0,
    embeddingScoreSum: 0,
    sovonDeadBirdCount: 0,
    sovonMortalityScore: null,
  };
  accumulators.set(key, next);
  return next;
}

export function buildEvaluationAlignmentMetrics(
  points: readonly EvaluationAlignmentPoint[],
  {
    scoreKey = "meanModelAnomalyScore",
    signalKeys = EVALUATION_ALIGNMENT_SIGNAL_OPTIONS.map((option) => option.key),
    lagWeeks = [0, 1, 3, 5],
    leadWindowWeeks = 5,
    scoreThreshold = null,
    minPairs = 4,
  }: {
    scoreKey?: EvaluationAlignmentScoreKey;
    signalKeys?: readonly EvaluationAlignmentSignalKey[];
    lagWeeks?: readonly number[];
    leadWindowWeeks?: number;
    scoreThreshold?: number | null;
    minPairs?: number;
  } = {},
): EvaluationAlignmentMetric[] {
  const sorted = [...points]
    .filter((point) => point.periodStart)
    .sort((left, right) => left.periodStart.localeCompare(right.periodStart));
  const byPeriod = new Map(sorted.map((point) => [point.periodStart, point]));
  const scoreLabel =
    EVALUATION_ALIGNMENT_SCORE_OPTIONS.find((option) => option.key === scoreKey)?.label ??
    scoreKey;
  const threshold =
    scoreThreshold ??
    Math.max(
      0.000001,
      quantile(sorted.map((point) => finiteMetricValue(point, scoreKey)), 0.75) ?? 0,
    );

  return signalKeys.map((signalKey) => {
    const signalLabel =
      EVALUATION_ALIGNMENT_SIGNAL_OPTIONS.find((option) => option.key === signalKey)?.label ??
      signalKey;
    const lagResults = lagWeeks.map((lag) => {
      const pairs: Array<[number, number]> = [];
      for (const point of sorted) {
        const signalPoint = byPeriod.get(addUtcWeeks(point.periodStart, lag));
        if (!signalPoint) continue;
        pairs.push([
          finiteMetricValue(point, scoreKey),
          finiteMetricValue(signalPoint, signalKey),
        ]);
      }

      return {
        lag,
        pairs: pairs.length,
        spearman: spearmanCorrelation(pairs, minPairs),
      };
    });
    const sameWeek = lagResults.find((result) => result.lag === 0);
    const bestLag = [...lagResults]
      .filter((result) => result.spearman !== null)
      .sort(
        (left, right) =>
          Math.abs(right.spearman ?? 0) - Math.abs(left.spearman ?? 0) ||
          left.lag - right.lag,
      )[0];
    const eventPoints = sorted.filter((point) => finiteMetricValue(point, signalKey) > 0);
    const quietPoints = sorted.filter((point) => finiteMetricValue(point, signalKey) <= 0);
    const eventMeanScore = mean(eventPoints.map((point) => finiteMetricValue(point, scoreKey)));
    const quietMeanScore = mean(quietPoints.map((point) => finiteMetricValue(point, scoreKey)));
    const eventLift =
      eventMeanScore !== null && quietMeanScore !== null
        ? eventMeanScore - quietMeanScore
        : null;
    const eventLiftRatio =
      eventMeanScore !== null && quietMeanScore !== null && quietMeanScore > 0
        ? eventMeanScore / quietMeanScore
        : null;

    let leadWindowHits = 0;
    for (const eventPoint of eventPoints) {
      let hit = false;
      for (let lag = 1; lag <= leadWindowWeeks; lag += 1) {
        const leadPoint = byPeriod.get(addUtcWeeks(eventPoint.periodStart, -lag));
        if (finiteMetricValue(leadPoint, scoreKey) >= threshold) {
          hit = true;
          break;
        }
      }
      if (hit) {
        leadWindowHits += 1;
      }
    }

    return {
      signalKey,
      signalLabel,
      scoreKey,
      scoreLabel,
      sameWeekSpearman: sameWeek?.spearman ?? null,
      sameWeekPairCount: sameWeek?.pairs ?? 0,
      bestLagWeeks: bestLag?.lag ?? null,
      bestLagSpearman: bestLag?.spearman ?? null,
      bestLagPairCount: bestLag?.pairs ?? 0,
      eventWeeks: eventPoints.length,
      eventMeanScore,
      quietMeanScore,
      eventLift,
      eventLiftRatio,
      leadWindowWeeks,
      leadWindowHits,
      leadWindowEvents: eventPoints.length,
      leadWindowHitRate:
        eventPoints.length > 0 ? leadWindowHits / eventPoints.length : null,
    };
  });
}

export function buildAlertsEvaluationTrendData({
  anomalyRows,
  dateFromIso = null,
  dateToIso = null,
  latestRunId = null,
  latestGeneratedAt = null,
}: {
  anomalyRows: readonly WeeklyAnomalyEvaluationRow[];
  dateFromIso?: string | null;
  dateToIso?: string | null;
  latestRunId?: string | null;
  latestGeneratedAt?: string | null;
}): AlertsEvaluationData {
  const accumulators = new Map<string, TrendAccumulator>();
  const speciesLabelsByKey = new Map<string, string>();

  for (const row of anomalyRows) {
    if (!isTargetSpecies(row)) continue;

    const speciesIdKey = speciesKey(row);
    const species = speciesLabel(row);
    if (!speciesLabelsByKey.has(speciesIdKey) && species !== "Unknown species") {
      speciesLabelsByKey.set(speciesIdKey, species);
    }
    const periodStart = row.period_start?.trim() || "";
    if (!dateInRange(periodStart, dateFromIso, dateToIso)) continue;

    const current = upsertTrend(
      accumulators,
      speciesIdKey,
      speciesLabelsByKey.get(speciesIdKey) ?? species,
      periodStart,
    );
    const alertLevel = row.alert_level?.trim().toLowerCase();
    current.rowCount += 1;
    current.modelScoreSum += maxKnown(row.model_anomaly_score, row.final_score);
    current.populationScoreSum += maxKnown(row.population_anomaly_score, row.count_anomaly_score);
    current.embeddingScoreSum += maxKnown(row.embedding_anomaly_score, row.embedding_drift_score);
    current.sovonDeadBirdCount = Math.max(current.sovonDeadBirdCount, toNumber(row.sovon_dead_bird_count));
    current.sovonMortalityScore = Math.max(
      current.sovonMortalityScore ?? Number.NEGATIVE_INFINITY,
      toNumber(row.sovon_mortality_score),
    );

    if (alertLevel === "amber" || alertLevel === "red") {
      current.anomalyCount += 1;
    }
    if (alertLevel === "amber") {
      current.amberAnomalyCount += 1;
    }
    if (alertLevel === "red") {
      current.redAnomalyCount += 1;
    }
  }

  const trendPoints = [...accumulators.values()]
    .map((item) => ({
      species: speciesLabelsByKey.get(item.speciesKey) ?? item.species,
      periodStart: item.periodStart,
      weekLabel: weekLabel(item.periodStart),
      rowCount: item.rowCount,
      anomalyCount: item.anomalyCount,
      amberAnomalyCount: item.amberAnomalyCount,
      redAnomalyCount: item.redAnomalyCount,
      meanModelAnomalyScore:
        item.rowCount > 0 ? item.modelScoreSum / item.rowCount : 0,
      meanPopulationAnomalyScore:
        item.rowCount > 0 ? item.populationScoreSum / item.rowCount : 0,
      meanEmbeddingAnomalyScore:
        item.rowCount > 0 ? item.embeddingScoreSum / item.rowCount : 0,
      sovonDeadBirdCount: item.sovonDeadBirdCount,
      sovonMortalityScore:
        item.sovonMortalityScore === Number.NEGATIVE_INFINITY
          ? null
          : item.sovonMortalityScore,
    }))
    .sort(
      (left, right) =>
        left.periodStart.localeCompare(right.periodStart) ||
        (TARGET_SPECIES_ORDER.get(left.species) ?? Number.MAX_SAFE_INTEGER) -
          (TARGET_SPECIES_ORDER.get(right.species) ?? Number.MAX_SAFE_INTEGER) ||
        left.species.localeCompare(right.species),
    );

  const availableSpecies = new Set(trendPoints.map((point) => point.species));
  const speciesOptions = TARGET_SPECIES_LIST.map((species) => species.english).filter((species) =>
    availableSpecies.has(species),
  );
  const weekCount = new Set(trendPoints.map((point) => point.periodStart)).size;
  const summary = trendPoints.reduce<AlertsEvaluationSummary>((current, point) => ({
    speciesCount: speciesOptions.length,
    weekCount,
    anomalyCount: current.anomalyCount + point.anomalyCount,
    redAnomalyCount: current.redAnomalyCount + point.redAnomalyCount,
    sovonDeadBirdCount: current.sovonDeadBirdCount + point.sovonDeadBirdCount,
  }), emptySummary());
  const available = trendPoints.length > 0;

  return {
    available,
    coverageLabel: available
      ? `Trend evaluation uses ${trendPoints.length.toLocaleString()} species-week points with SOVON mortality context from the score artifacts.`
      : "No anomaly rows are available for trend evaluation yet.",
    latestRunId,
    latestGeneratedAt,
    speciesOptions,
    trendPoints,
    summary,
  };
}
