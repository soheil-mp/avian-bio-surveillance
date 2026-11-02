export type HotspotSource = "bioacoustic";
export type HotspotGrain = "grid" | "admin_area";

export type HotspotSummaryInputRow = {
  location_type?: string | null;
  location_id?: string | null;
  location_name?: string | null;
  admin_level?: string | null;
  grid_cell_id?: string | null;
  period_start?: string | Date | null;
  bioacoustic_score?: number | string | null;
  bioacoustic_alert_rows?: number | string | null;
};

export type HotspotSummaryUnit = {
  location_type: HotspotGrain;
  location_id: string;
  location_name: string;
  admin_level: string | null;
  grid_cell_id: string | null;
  period_start: string;
  score: number;
  bioacoustic_alert_rows: number;
  renderable: boolean;
};

export type HotspotSummaryCoverage = {
  totalRows: number;
  totalUnits: number;
  renderableRows: number;
  renderableUnits: number;
  unrenderableRows: number;
  latestPeriodStart: string | null;
  /**
   * Distinct renderable weeks (period_start) that carry signal for the
   * requested source, newest first. Drives the week picker in the UI.
   */
  availableWeeks: string[];
  /**
   * The single week the response was sliced to, or null when the caller
   * requested the full (all-weeks) view. See the `week` argument of
   * {@link buildHotspotSummaryResponse}.
   */
  selectedWeek: string | null;
};

export const LATEST_WEEK_SENTINEL = "latest";

export type HotspotSummaryResponse = {
  runId: string | null;
  generatedAt: string | null;
  source: HotspotSource;
  grain: HotspotGrain;
  coverage: HotspotSummaryCoverage;
  hotspots: HotspotSummaryUnit[];
};

const HOTSPOT_SOURCES: readonly HotspotSource[] = ["bioacoustic"] as const;
const HOTSPOT_GRAINS: readonly HotspotGrain[] = ["grid", "admin_area"] as const;
const BIOACOUSTIC_THRESHOLD = 0.34;

function cleanString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function toNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function toDateInput(value: string | Date | null | undefined): string {
  if (!value) return "";
  if (value instanceof Date) {
    return value.toISOString().slice(0, 10);
  }
  const trimmed = value.trim();
  return /^\d{4}-\d{2}-\d{2}/.test(trimmed) ? trimmed.slice(0, 10) : trimmed;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function bioacousticSignalValue(row: HotspotSummaryUnit): number {
  return Math.max(row.score, row.bioacoustic_alert_rows > 0 ? BIOACOUSTIC_THRESHOLD : 0);
}

export function normalizeHotspotSource(value: string | null | undefined): HotspotSource {
  const candidate = cleanString(value).toLowerCase();
  return HOTSPOT_SOURCES.includes(candidate as HotspotSource)
    ? (candidate as HotspotSource)
    : "bioacoustic";
}

export function normalizeHotspotGrain(value: string | null | undefined): HotspotGrain {
  const candidate = cleanString(value).toLowerCase();
  return HOTSPOT_GRAINS.includes(candidate as HotspotGrain)
    ? (candidate as HotspotGrain)
    : "grid";
}

export function slugAdminName(value: unknown): string {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function municipalityLocationIdForName(name: unknown): string {
  const slug = slugAdminName(name);
  return slug ? `municipality:${slug}` : "";
}

export function isRenderableHotspotLocation({
  grain,
  locationId,
  adminLevel,
  gridCellId,
}: {
  grain: HotspotGrain;
  locationId: string;
  adminLevel: string | null;
  gridCellId: string | null;
}): boolean {
  if (grain === "grid") return Boolean(gridCellId || locationId);
  return locationId.startsWith("municipality:") && adminLevel !== "city";
}

export function buildHotspotSummaryResponse({
  rows,
  source,
  grain,
  runId = null,
  generatedAt = null,
  week = null,
}: {
  rows: readonly HotspotSummaryInputRow[];
  source: HotspotSource;
  grain: HotspotGrain;
  runId?: string | null;
  generatedAt?: string | null;
  /**
   * Optional single-week slice. Pass an ISO `YYYY-MM-DD` date to pin the
   * response to that week, or {@link LATEST_WEEK_SENTINEL} ("latest") to pin
   * it to the newest week that has renderable signal for this source. When
   * omitted (null/undefined) the full all-weeks view is returned unchanged —
   * this preserves the legacy contract used by the alerts page date-range
   * filter.
   */
  week?: string | null;
}): HotspotSummaryResponse {
  const normalizedRows = rows
    .map((row): HotspotSummaryUnit | null => {
      const locationType = normalizeHotspotGrain(row.location_type);
      if (locationType !== grain) return null;

      const locationId = cleanString(row.location_id);
      if (!locationId) return null;

      const gridCellId = cleanString(row.grid_cell_id) || (grain === "grid" ? locationId : null);
      const adminLevel = cleanString(row.admin_level) || null;
      const bioacousticScore = clamp01(toNumber(row.bioacoustic_score));
      const unit: HotspotSummaryUnit = {
        location_type: grain,
        location_id: locationId,
        location_name:
          cleanString(row.location_name) ||
          (grain === "grid" ? locationId : locationId.replace(/^[^:]+:/, "")),
        admin_level: adminLevel,
        grid_cell_id: gridCellId,
        period_start: toDateInput(row.period_start),
        score: bioacousticScore,
        bioacoustic_alert_rows: toNumber(row.bioacoustic_alert_rows),
        renderable: false,
      };
      unit.renderable = isRenderableHotspotLocation({
        grain,
        locationId,
        adminLevel,
        gridCellId,
      });
      return unit;
    })
    .filter((row): row is HotspotSummaryUnit => row !== null)
    .filter(
      (row) => row.bioacoustic_alert_rows > 0 || row.score >= BIOACOUSTIC_THRESHOLD,
    );

  const scoredHotspots = normalizedRows
    .map((row) => ({
      ...row,
      score: clamp01(bioacousticSignalValue(row)),
    }))
    .sort(
      (left, right) =>
        right.score - left.score ||
        right.bioacoustic_alert_rows - left.bioacoustic_alert_rows ||
        right.period_start.localeCompare(left.period_start) ||
        left.location_name.localeCompare(right.location_name),
    );

  // Weeks that actually carry renderable signal for this source, newest
  // first. This is the menu the UI offers and the basis for "latest".
  const availableWeeks = Array.from(
    new Set(
      scoredHotspots
        .filter((row) => row.renderable && row.period_start)
        .map((row) => row.period_start),
    ),
  ).sort((left, right) => right.localeCompare(left));

  const requestedWeek = cleanString(week);
  let selectedWeek: string | null = null;
  if (requestedWeek) {
    selectedWeek =
      requestedWeek.toLowerCase() === LATEST_WEEK_SENTINEL
        ? availableWeeks[0] ?? null
        : availableWeeks.includes(requestedWeek)
          ? requestedWeek
          : availableWeeks[0] ?? null;
  }

  const hotspots = selectedWeek
    ? scoredHotspots.filter((row) => row.period_start === selectedWeek)
    : scoredHotspots;

  const renderableRows = hotspots.filter((row) => row.renderable);
  return {
    runId,
    generatedAt,
    source,
    grain,
    coverage: {
      totalRows: hotspots.length,
      totalUnits: new Set(hotspots.map((row) => row.location_id)).size,
      renderableRows: renderableRows.length,
      renderableUnits: new Set(renderableRows.map((row) => row.location_id)).size,
      unrenderableRows: hotspots.length - renderableRows.length,
      // True newest renderable week (independent of score ordering).
      latestPeriodStart: availableWeeks[0] ?? null,
      availableWeeks,
      selectedWeek,
    },
    hotspots,
  };
}
