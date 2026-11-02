export interface PopulationActivityInput {
  detections: number;
  activeDays: number;
  activeStations: number;
  communityDetections: number;
  communityActiveDays: number;
  communityActiveStations: number;
  periodDayCount: number;
  stationDayEffort?: number;
}

export interface PopulationActivityMetrics {
  stationDayEffort: number;
  detectionsPerActiveStationDay: number | null;
  activityIndex: number;
}

export interface PopulationLoadResultLike {
  series: unknown[];
  summary: {
    year: number | null;
  };
}

export interface SeasonalPresenceRow {
  species_id?: string;
  location_type?: string;
  location_id?: string;
  period_start?: string;
  temporal_occupancy_pct?: string | number;
  station_presence_pct?: string | number;
  relative_abundance_pct?: string | number;
}

export interface SeasonalPresencePoint {
  month: number;
  label: string;
  temporalOccupancyPct: number | null;
  stationPresencePct: number | null;
  relativeAbundancePct: number | null;
}

export interface SeasonalPresenceYear {
  year: number;
  series: SeasonalPresencePoint[];
}

export interface RepresentativeDetectionCandidate {
  detection_id?: string | number;
  detected_at?: string | null;
  confidence?: string | number | null;
  has_processed_clip?: string | number | boolean | null;
}

export interface AggregatedMortalityPoint {
  month: number;
  label: string;
  count: number;
  historicalAverageCount: number | null;
}

export interface MortalityYearSeries {
  year: number;
  series: AggregatedMortalityPoint[];
}

export interface AggregatedMortalityResult {
  year: number | null;
  series: AggregatedMortalityPoint[];
  availableYears: number[];
  byYear: MortalityYearSeries[];
}

const MONTH_LABELS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function finiteNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function optionalNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function clampUnit(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function safeRatio(numerator: number, denominator: number): number {
  return denominator > 0 ? numerator / denominator : 0;
}

function roundOne(value: number): number {
  return Math.round(value * 10) / 10;
}

export function lastCompleteUtcYear(referenceDate = new Date()): number {
  return referenceDate.getUTCFullYear() - 1;
}

export function calculatePopulationActivityMetrics(
  input: PopulationActivityInput,
): PopulationActivityMetrics {
  const detections = Math.max(0, finiteNumber(input.detections));
  const activeDays = Math.max(0, finiteNumber(input.activeDays));
  const activeStations = Math.max(0, finiteNumber(input.activeStations));
  const communityDetections = Math.max(0, finiteNumber(input.communityDetections));
  const communityActiveDays = Math.max(0, finiteNumber(input.communityActiveDays));
  const communityActiveStations = Math.max(0, finiteNumber(input.communityActiveStations));
  const periodDayCount = Math.max(0, finiteNumber(input.periodDayCount));
  const stationDayEffort = Math.max(
    0,
    finiteNumber(input.stationDayEffort ?? communityActiveDays * communityActiveStations),
  );
  const detectionsPerActiveStationDay =
    stationDayEffort > 0 ? detections / stationDayEffort : null;
  const effortIntensity =
    detectionsPerActiveStationDay !== null
      ? detectionsPerActiveStationDay / (detectionsPerActiveStationDay + 1)
      : 0;
  const components = [
    clampUnit(safeRatio(detections, communityDetections)),
    clampUnit(safeRatio(activeDays, periodDayCount)),
    clampUnit(safeRatio(activeStations, communityActiveStations)),
    clampUnit(effortIntensity),
  ];
  const activityIndex =
    components.reduce((sum, value) => sum + value, 0) / components.length;

  return {
    stationDayEffort,
    detectionsPerActiveStationDay:
      detectionsPerActiveStationDay === null ? null : roundOne(detectionsPerActiveStationDay),
    activityIndex: roundOne(activityIndex * 100),
  };
}

export function selectPopulationLoadResult<T extends PopulationLoadResultLike>(
  preferredYear: number,
  freshResult: T,
  trackerResult: T,
): T {
  if (freshResult.summary.year === preferredYear && freshResult.series.length > 0) {
    return freshResult;
  }
  if (trackerResult.summary.year === preferredYear && trackerResult.series.length > 0) {
    return trackerResult;
  }
  if (freshResult.series.length > 0) {
    return freshResult;
  }
  return trackerResult;
}

function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let current = "";
  let inQuotes = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];

    if (char === '"') {
      if (inQuotes && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }

    if (char === "," && !inQuotes) {
      values.push(current);
      current = "";
      continue;
    }

    current += char;
  }

  values.push(current);
  return values;
}

export function parseCsvRows(text: string): Array<Record<string, string>> {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length === 0) return [];

  const headers = parseCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = parseCsvLine(line);
    return headers.reduce<Record<string, string>>((row, header, index) => {
      row[header] = values[index] ?? "";
      return row;
    }, {});
  });
}

function yearFromIsoDate(value: string | undefined): number | null {
  if (!value || value.length < 4) return null;
  const year = Number(value.slice(0, 4));
  return Number.isFinite(year) ? year : null;
}

function monthFromIsoDate(value: string | undefined): number | null {
  if (!value || value.length < 7) return null;
  const month = Number(value.slice(5, 7));
  return Number.isFinite(month) && month >= 1 && month <= 12 ? month : null;
}

function truthyBoolean(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  const normalized = String(value ?? "").trim().toLowerCase();
  return normalized === "true" || normalized === "1" || normalized === "yes";
}

// Builds a per-year mortality series (each year carries a trailing 5-year
// baseline) plus the default (latest / preferred) year, from a
// year -> month -> count map, exposing every available year to the UI's year
// picker.
export function buildMortalityByYear(
  monthlyByYear: Map<number, Map<number, number>>,
  preferredYear: number | null,
): AggregatedMortalityResult {
  if (monthlyByYear.size === 0) {
    return { year: null, series: [], availableYears: [], byYear: [] };
  }
  const availableYears = Array.from(monthlyByYear.keys()).sort((a, b) => a - b);
  const seriesForYear = (target: number): AggregatedMortalityPoint[] => {
    const current = monthlyByYear.get(target) ?? new Map<number, number>();
    const baselineYears = availableYears.filter(
      (candidate) => candidate >= target - 5 && candidate < target,
    );
    return MONTH_LABELS.map((label, index) => {
      const month = index + 1;
      const baselineValues = baselineYears.map(
        (candidate) => monthlyByYear.get(candidate)?.get(month) ?? 0,
      );
      const historicalAverageCount =
        baselineValues.length > 0
          ? roundOne(
              baselineValues.reduce((sum, value) => sum + value, 0) /
                baselineValues.length,
            )
          : null;
      return {
        month,
        label,
        count: Math.trunc(current.get(month) ?? 0),
        historicalAverageCount,
      };
    });
  };
  const year =
    preferredYear !== null && monthlyByYear.has(preferredYear)
      ? preferredYear
      : (preferredYear !== null
          ? availableYears.filter((candidate) => candidate <= preferredYear).at(-1)
          : undefined) ?? availableYears.at(-1)!;
  const byYear = availableYears.map((candidate) => ({
    year: candidate,
    series: seriesForYear(candidate),
  }));
  return {
    year,
    series:
      byYear.find((entry) => entry.year === year)?.series ?? seriesForYear(year),
    availableYears,
    byYear,
  };
}

export function aggregateSeasonalPresenceRows(
  rows: SeasonalPresenceRow[],
  speciesId: string,
  preferredYear: number,
  yearCount = 4,
): SeasonalPresenceYear[] {
  const monthlyByYear = new Map<number, Map<number, SeasonalPresencePoint>>();

  for (const row of rows) {
    if (String(row.species_id ?? "") !== speciesId) continue;
    if (row.location_type !== "network" || row.location_id !== "nl") continue;

    const year = yearFromIsoDate(row.period_start);
    const month = monthFromIsoDate(row.period_start);
    if (year === null || month === null) continue;

    if (!monthlyByYear.has(year)) {
      monthlyByYear.set(year, new Map());
    }

    monthlyByYear.get(year)!.set(month, {
      month,
      label: MONTH_LABELS[month - 1],
      temporalOccupancyPct: optionalNumber(row.temporal_occupancy_pct),
      stationPresencePct: optionalNumber(row.station_presence_pct),
      relativeAbundancePct: optionalNumber(row.relative_abundance_pct),
    });
  }

  const availableYears = Array.from(monthlyByYear.keys()).sort((a, b) => a - b);
  if (availableYears.length === 0) return [];

  const pivotYear = monthlyByYear.has(preferredYear)
    ? preferredYear
    : availableYears.filter((candidate) => candidate <= preferredYear).at(-1) ?? availableYears.at(-1)!;
  const selectedYears = availableYears.filter((candidate) => candidate <= pivotYear).slice(-Math.max(1, yearCount));

  return selectedYears.map((year) => {
    const yearMap = monthlyByYear.get(year) ?? new Map<number, SeasonalPresencePoint>();
    return {
      year,
      series: MONTH_LABELS.map((label, index) => {
        const month = index + 1;
        return yearMap.get(month) ?? {
          month,
          label,
          temporalOccupancyPct: null,
          stationPresencePct: null,
          relativeAbundancePct: null,
        };
      }),
    };
  });
}

export function selectRepresentativeDetection<T extends RepresentativeDetectionCandidate>(
  rows: T[],
): T | null {
  let best: T | null = null;

  for (const candidate of rows) {
    if (!candidate) continue;
    if (!best) {
      best = candidate;
      continue;
    }

    const candidateHasClip = truthyBoolean(candidate.has_processed_clip);
    const bestHasClip = truthyBoolean(best.has_processed_clip);
    if (candidateHasClip !== bestHasClip) {
      if (candidateHasClip) {
        best = candidate;
      }
      continue;
    }

    const candidateConfidence = optionalNumber(candidate.confidence) ?? -1;
    const bestConfidence = optionalNumber(best.confidence) ?? -1;
    if (candidateConfidence !== bestConfidence) {
      if (candidateConfidence > bestConfidence) {
        best = candidate;
      }
      continue;
    }

    const candidateDetectedAt = Date.parse(String(candidate.detected_at ?? ""));
    const bestDetectedAt = Date.parse(String(best.detected_at ?? ""));
    const candidateDetectedAtMs = Number.isFinite(candidateDetectedAt) ? candidateDetectedAt : -1;
    const bestDetectedAtMs = Number.isFinite(bestDetectedAt) ? bestDetectedAt : -1;
    if (candidateDetectedAtMs !== bestDetectedAtMs) {
      if (candidateDetectedAtMs > bestDetectedAtMs) {
        best = candidate;
      }
      continue;
    }

    if (String(candidate.detection_id ?? "") > String(best.detection_id ?? "")) {
      best = candidate;
    }
  }

  return best;
}
