import type { AlertsPageFilters } from "./types";

export type SearchParamValue = string | string[] | undefined;
export type AlertSort = AlertsPageFilters["sort"];
export type AlertLevel = AlertsPageFilters["alertLevel"];
export type EmbeddingBackend = AlertsPageFilters["backend"];
export type ClusterViewMode = AlertsPageFilters["viewMode"];
export type ClusterScope = AlertsPageFilters["clusterScope"];

export const DEFAULT_ALERTS_WINDOW_DAYS = 14;
export const DEFAULT_ALERTS_WINDOW_WEEKS = 1;
const MAX_CONTIGUOUS_WEEK_GAP_DAYS = 2;

export type AlertsAvailableWeek = {
  periodStart: string;
  periodEnd: string;
};

export type AlertsDefaultDateWindow = {
  latestAvailableDate: string;
  dateFrom: string;
  dateTo: string;
  label: string;
};

export type AlertsArtifactCoverageStatus = {
  trusted: boolean;
  latestArtifactDate: string | null;
  latestDetectedDate: string | null;
  latestEmbeddedDate: string | null;
  reason: string | null;
};

export type AlertsNormalizedDateRange = {
  dateFrom: string;
  dateTo: string;
  dateFromIso: string | null;
  dateToIso: string | null;
  rangeLabel: string;
};

export function readSearchParam(value: SearchParamValue): string {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (typeof candidate !== "string") return "";

  return candidate.trim();
}

export function readDateInputParam(value: SearchParamValue): string {
  const candidate = readSearchParam(value);
  if (!candidate) return "";

  return /^\d{4}-\d{2}-\d{2}$/.test(candidate) ? candidate : "";
}

export function readEnumParam<T extends string>(
  value: SearchParamValue,
  allowed: readonly T[],
  fallback: T,
): T {
  const candidate = readSearchParam(value) as T;
  return allowed.includes(candidate) ? candidate : fallback;
}

export function readConfidenceParam(value: SearchParamValue): string {
  const candidate = readSearchParam(value);
  if (!candidate) return "";

  const numeric = Number(candidate);
  if (!Number.isFinite(numeric)) return "";
  const normalized = numeric > 1 ? numeric / 100 : numeric;
  if (normalized < 0 || normalized > 1) return "";

  return normalized.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

export function formatUtcDateInput(value: Date | string): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";

  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
}

export function shiftUtcDateInput(dateInput: string, deltaDays: number): string {
  const [year, month, day] = dateInput.split("-").map(Number);
  if (![year, month, day].every(Number.isFinite)) return "";

  const shifted = new Date(Date.UTC(year, month - 1, day + deltaDays));
  return formatUtcDateInput(shifted);
}

export function alertsWeekStartDateInput(dateInput: string): string {
  const timestamp = parseUtcDateInput(dateInput);
  if (timestamp == null) return "";

  const weekday = new Date(timestamp).getUTCDay();
  const mondayOffset = (weekday + 6) % 7;
  return shiftUtcDateInput(dateInput, -mondayOffset);
}

export function alertsWeekEndDateInput(dateInput: string): string {
  const weekStart = alertsWeekStartDateInput(dateInput);
  if (!weekStart) return "";

  return shiftUtcDateInput(weekStart, 6);
}

function dayLabel(days: number): string {
  return days === 1 ? "day" : "days";
}

function weekLabel(weeks: number): string {
  return weeks === 1 ? "week" : "weeks";
}

function normalizeAvailableWeeks(
  availableWeeks: readonly AlertsAvailableWeek[],
): AlertsAvailableWeek[] {
  const dedupedWeeks = new Map<string, AlertsAvailableWeek>();

  for (const week of availableWeeks) {
    const periodStart = readDateInputParam(week.periodStart);
    const periodEnd = readDateInputParam(week.periodEnd) || periodStart;
    if (!periodStart || !periodEnd) continue;

    const key = `${periodStart}:${periodEnd}`;
    if (!dedupedWeeks.has(key)) {
      dedupedWeeks.set(key, { periodStart, periodEnd });
    }
  }

  return [...dedupedWeeks.values()].sort(
    (left, right) =>
      left.periodEnd.localeCompare(right.periodEnd) ||
      left.periodStart.localeCompare(right.periodStart),
  );
}

export function preferAvailableWeeksWithData({
  availableWeeks = [],
  weeksWithData = [],
}: {
  availableWeeks?: readonly AlertsAvailableWeek[];
  weeksWithData?: readonly AlertsAvailableWeek[];
} = {}): AlertsAvailableWeek[] {
  const normalizedAvailableWeeks = normalizeAvailableWeeks(availableWeeks);
  if (normalizedAvailableWeeks.length === 0) {
    return [];
  }

  const normalizedWeeksWithData = normalizeAvailableWeeks(weeksWithData);
  if (normalizedWeeksWithData.length === 0) {
    return normalizedAvailableWeeks;
  }

  const dataKeys = new Set(
    normalizedWeeksWithData.map((week) => `${week.periodStart}:${week.periodEnd}`),
  );
  const overlappingWeeks = normalizedAvailableWeeks.filter((week) =>
    dataKeys.has(`${week.periodStart}:${week.periodEnd}`),
  );

  return overlappingWeeks.length > 0
    ? overlappingWeeks
    : normalizedAvailableWeeks;
}

function parseUtcDateInput(dateInput: string): number | null {
  const [year, month, day] = dateInput.split("-").map(Number);
  if (![year, month, day].every(Number.isFinite)) return null;

  return Date.UTC(year, month - 1, day);
}

function selectRecentAvailableWeeks(
  normalizedWeeks: readonly AlertsAvailableWeek[],
): AlertsAvailableWeek[] {
  if (normalizedWeeks.length === 0) return [];

  const selected = [normalizedWeeks[normalizedWeeks.length - 1]];
  const maxGapMs = MAX_CONTIGUOUS_WEEK_GAP_DAYS * 24 * 60 * 60 * 1000;

  for (
    let index = normalizedWeeks.length - 2;
    index >= 0 && selected.length < DEFAULT_ALERTS_WINDOW_WEEKS;
    index -= 1
  ) {
    const candidate = normalizedWeeks[index];
    const nextWeek = selected[0];
    const candidateEnd = parseUtcDateInput(candidate.periodEnd);
    const nextStart = parseUtcDateInput(nextWeek.periodStart);

    if (candidateEnd == null || nextStart == null) {
      break;
    }

    if (nextStart - candidateEnd <= maxGapMs) {
      selected.unshift(candidate);
      continue;
    }

    break;
  }

  return selected;
}

function buildExplicitDateRangeLabel(dateFrom: string, dateTo: string): string {
  if (dateFrom && dateTo) {
    return `Weekly range · ${dateFrom} to ${dateTo}`;
  }
  if (dateFrom) {
    return `Weekly range · from ${dateFrom}`;
  }
  if (dateTo) {
    return `Weekly range · through ${dateTo}`;
  }
  return "Weekly range";
}

export function selectLatestAlertsCoverageDate({
  latestDetectedAt,
  latestEmbeddedDetectedAt,
}: {
  latestDetectedAt?: Date | string | null;
  latestEmbeddedDetectedAt?: Date | string | null;
} = {}): Date | string | null {
  return latestEmbeddedDetectedAt ?? latestDetectedAt ?? null;
}

export function assessAlertsArtifactCoverage({
  availableWeeks = [],
  latestDetectedAt,
  latestEmbeddedDetectedAt,
}: {
  availableWeeks?: readonly AlertsAvailableWeek[];
  latestDetectedAt?: Date | string | null;
  latestEmbeddedDetectedAt?: Date | string | null;
} = {}): AlertsArtifactCoverageStatus {
  const normalizedWeeks = normalizeAvailableWeeks(availableWeeks);
  const latestWeek = normalizedWeeks[normalizedWeeks.length - 1];
  const latestArtifactDate =
    latestWeek?.periodEnd || latestWeek?.periodStart || null;
  // A weekly bucket extends up to 6 days past its start, so the latest modeled
  // window's *end* routinely sits a few days beyond the newest detection even
  // when the run is perfectly current. Gate "trust" on the week's START instead:
  // the run is only genuinely ahead of coverage if its latest week *began* after
  // our newest detection (i.e. a whole future week with no detections yet).
  const latestArtifactWeekStart = latestWeek?.periodStart || latestArtifactDate;
  const latestDetectedDate = latestDetectedAt
    ? formatUtcDateInput(latestDetectedAt)
    : null;
  const latestEmbeddedDate = latestEmbeddedDetectedAt
    ? formatUtcDateInput(latestEmbeddedDetectedAt)
    : null;

  if (!latestArtifactDate) {
    return {
      trusted: true,
      latestArtifactDate: null,
      latestDetectedDate,
      latestEmbeddedDate,
      reason: null,
    };
  }

  if (!latestDetectedDate) {
    return {
      trusted: false,
      latestArtifactDate,
      latestDetectedDate,
      latestEmbeddedDate,
      reason:
        `Ignored persisted weekly modeling run because the latest modeled window ends ${latestArtifactDate}, ` +
        "but the local BirdWeather table has no processed detections yet.",
    };
  }

  if (latestArtifactWeekStart && latestArtifactWeekStart > latestDetectedDate) {
    return {
      trusted: false,
      latestArtifactDate,
      latestDetectedDate,
      latestEmbeddedDate,
      reason:
        `Ignored persisted weekly modeling run because the latest modeled window ends ${latestArtifactDate}, ` +
        `ahead of local BirdWeather coverage ending ${latestDetectedDate}.`,
    };
  }

  return {
    trusted: true,
    latestArtifactDate,
    latestDetectedDate,
    latestEmbeddedDate,
    reason: null,
  };
}

export function buildDefaultAlertsDateWindow({
  availableWeeks = [],
  latestDetectedAt,
  referenceNow = new Date(),
  days = DEFAULT_ALERTS_WINDOW_DAYS,
}: {
  availableWeeks?: readonly AlertsAvailableWeek[];
  latestDetectedAt?: Date | string | null;
  referenceNow?: Date;
  days?: number;
} = {}): AlertsDefaultDateWindow {
  const normalizedWeeks = normalizeAvailableWeeks(availableWeeks);
  if (normalizedWeeks.length > 0) {
    const selectedWeeks = selectRecentAvailableWeeks(normalizedWeeks);
    const earliestWeek = selectedWeeks[0] ?? normalizedWeeks[normalizedWeeks.length - 1];
    const latestWeek = selectedWeeks[selectedWeeks.length - 1] ?? earliestWeek;
    const dateFrom = earliestWeek.periodStart;
    const dateTo = latestWeek.periodEnd || latestWeek.periodStart;

    return {
      latestAvailableDate: dateTo,
      dateFrom,
      dateTo,
      label: `Latest available ${selectedWeeks.length} ${weekLabel(selectedWeeks.length)} · ${dateFrom} to ${dateTo}`,
    };
  }

  const normalizedDays =
    Number.isFinite(days) && days > 0
      ? Math.max(1, Math.floor(days))
      : DEFAULT_ALERTS_WINDOW_DAYS;
  const latestAvailableDate =
    (latestDetectedAt ? formatUtcDateInput(latestDetectedAt) : "") ||
    formatUtcDateInput(referenceNow);
  const dateTo = latestAvailableDate;
  const dateFrom = shiftUtcDateInput(dateTo, -(normalizedDays - 1)) || dateTo;

  return {
    latestAvailableDate,
    dateFrom,
    dateTo,
    label: `Latest available ${normalizedDays} ${dayLabel(normalizedDays)} · ${dateFrom} to ${dateTo}`,
  };
}

export function normalizeAlertsDateRange(
  searchParams: { dateFrom?: SearchParamValue; dateTo?: SearchParamValue } | undefined,
  defaultWindow: AlertsDefaultDateWindow,
  availableWeeks: readonly AlertsAvailableWeek[] = [],
): AlertsNormalizedDateRange {
  const normalizedWeeks = normalizeAvailableWeeks(availableWeeks);
  const explicitDateFrom = readDateInputParam(searchParams?.dateFrom);
  const explicitDateTo = readDateInputParam(searchParams?.dateTo);
  const hasExplicitBounds = Boolean(explicitDateFrom || explicitDateTo);
  let dateFrom = hasExplicitBounds
    ? alertsWeekStartDateInput(explicitDateFrom || explicitDateTo)
    : defaultWindow.dateFrom;
  let dateTo = hasExplicitBounds
    ? alertsWeekEndDateInput(explicitDateTo || explicitDateFrom)
    : defaultWindow.dateTo;

  if (dateFrom && dateTo && dateFrom > dateTo) {
    dateFrom = alertsWeekStartDateInput(explicitDateTo || explicitDateFrom);
    dateTo = alertsWeekEndDateInput(explicitDateFrom || explicitDateTo);
  }

  if (hasExplicitBounds && normalizedWeeks.length > 0) {
    const hasWeekStart = normalizedWeeks.some((week) => week.periodStart === dateFrom);
    const hasWeekEnd = normalizedWeeks.some((week) => week.periodEnd === dateTo);

    if (!hasWeekStart || !hasWeekEnd) {
      return {
        dateFrom: defaultWindow.dateFrom,
        dateTo: defaultWindow.dateTo,
        dateFromIso: defaultWindow.dateFrom
          ? `${defaultWindow.dateFrom}T00:00:00.000Z`
          : null,
        dateToIso: defaultWindow.dateTo
          ? `${defaultWindow.dateTo}T23:59:59.999Z`
          : null,
        rangeLabel: defaultWindow.label,
      };
    }
  }

  return {
    dateFrom,
    dateTo,
    dateFromIso: dateFrom ? `${dateFrom}T00:00:00.000Z` : null,
    dateToIso: dateTo ? `${dateTo}T23:59:59.999Z` : null,
    rangeLabel: hasExplicitBounds
      ? buildExplicitDateRangeLabel(dateFrom, dateTo)
      : defaultWindow.label,
  };
}
