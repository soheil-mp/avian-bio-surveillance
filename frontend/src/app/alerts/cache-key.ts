import type { AlertsPageFilters } from "./types";

type AlertsCacheKeyFilters = AlertsPageFilters & {
  minConfidenceValue?: number | null;
  alertThreshold?: number;
  dateFromIso?: string | null;
  dateToIso?: string | null;
  hasExplicitDateRange?: boolean;
  rangeLabel?: string;
  focusDetectionId?: string | null;
};

export function buildAlertsDataCacheKey(
  filters: AlertsCacheKeyFilters,
  modelingArtifactsKey: string,
): string {
  const stableFilters = {
    species: filters.species,
    backend: filters.backend,
    dateFrom: filters.dateFrom,
    dateTo: filters.dateTo,
    minConfidence: filters.minConfidence,
    minStationQuality: filters.minStationQuality,
    alertLevel: filters.alertLevel,
    focusDetectionId: filters.focusDetectionId ?? null,
  };

  return `${modelingArtifactsKey}:${JSON.stringify(stableFilters)}`;
}
