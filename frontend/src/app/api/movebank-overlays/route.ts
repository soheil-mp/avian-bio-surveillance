import { NextResponse } from "next/server";
import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import {
  MOVEBANK_OVERLAY_META,
  isMovebankOverlayId,
  type MovebankOverlayId,
  type MovebankOverlayResponse,
  type MovebankOverlaySummary,
} from "@/config/movebank-overlays";

export const dynamic = "force-dynamic";

const CACHE_TTL_MS = 10 * 60 * 1000;
const MAX_CACHE_ENTRIES = 128;
const RESPONSE_HEADERS = {
  "Cache-Control": "public, s-maxage=600, stale-while-revalidate=600",
};

const movebankOverlayCache = new Map<
  string,
  { expiresAt: number; payload: MovebankOverlayResponse }
>();

const SCOPE_BOUNDS = {
  minLon: 1.25,
  maxLon: 9.45,
  minLat: 49.75,
  maxLat: 54.75,
};

const STORY_BOUNDS = {
  minLon: SCOPE_BOUNDS.minLon - 1.5,
  maxLon: SCOPE_BOUNDS.maxLon + 1.5,
  minLat: SCOPE_BOUNDS.minLat - 1.0,
  maxLat: SCOPE_BOUNDS.maxLat + 1.0,
};

// =============================================================================
// Hierarchical canonical grid configuration
// -----------------------------------------------------------------------------
// All point binning goes through the canonical `nl_geodesic_5km` grid stored
// in `grid_cells_tbl`, then rolls up to a coarser display resolution by
// dividing the (grid_row, grid_col) integer coordinates by an integer factor.
// Coarser bins must be clean integer multiples of 5 km so child cells nest
// exactly inside parent cells (and align with the satellite/CDS grids).
// =============================================================================

const CANONICAL_GRID_SYSTEM = "nl_geodesic_5km";
const CANONICAL_CELL_KM = 5;

/** Default display resolution per overlay (km, must be a multiple of 5). */
const DEFAULT_AGGREGATION_KM: Record<string, number> = {
  corridors: 15,
  stopovers: 10,
  flow_field: 25,
};

/** Resolve the requested aggregation level (km) for an overlay. */
function resolveAggregationKm(overlayId: string, raw: string | null): number {
  const fallback = DEFAULT_AGGREGATION_KM[overlayId] ?? 15;
  const requested = Number(raw);
  if (!Number.isFinite(requested) || requested <= 0) return fallback;
  // Snap to the nearest multiple of the base grid, clamp to a sane range.
  const snapped = Math.max(
    CANONICAL_CELL_KM,
    Math.round(requested / CANONICAL_CELL_KM) * CANONICAL_CELL_KM,
  );
  return Math.min(snapped, 100);
}

const JOURNEY_COLORS = [
  "rgb(52, 211, 153)",
  "rgb(56, 189, 248)",
  "rgb(251, 191, 36)",
  "rgb(244, 114, 182)",
  "rgb(167, 139, 250)",
  "rgb(251, 146, 60)",
];

const DEFAULT_SPECIES_LABEL = "Unclassified";
const SPECIES_LABEL_SQL = `COALESCE(NULLIF(BTRIM(individual_taxon_canonical_name), ''), '${DEFAULT_SPECIES_LABEL}')`;

interface BaseSummaryRow {
  fix_count: number;
  individual_count: number;
  species_count: number;
  latest_ts: string | null;
}

function asNumber(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function asNullableNumber(value: unknown): number | null {
  if (value == null) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function asText(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim().length > 0 ? value : fallback;
}

function withAlpha(rgb: string, alpha: number): string {
  return rgb.replace(/^rgb\(/, "rgba(").replace(/\)$/, `, ${Math.max(0, Math.min(1, alpha))})`);
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function buildFeatureCollection(features: GeoJSON.Feature[]): GeoJSON.FeatureCollection | null {
  return features.length > 0 ? { type: "FeatureCollection", features } : null;
}

function buildCacheKey(
  overlay: MovebankOverlayId,
  species: string[],
  windowDays: number,
  aggregationKm: number,
): string {
  return JSON.stringify({
    overlay,
    species: [...species].sort(),
    windowDays,
    aggregationKm,
  });
}

function rememberPayload(key: string, payload: MovebankOverlayResponse) {
  if (movebankOverlayCache.size >= MAX_CACHE_ENTRIES) {
    const oldestKey = movebankOverlayCache.keys().next().value;
    if (oldestKey) movebankOverlayCache.delete(oldestKey);
  }
  movebankOverlayCache.set(key, {
    expiresAt: Date.now() + CACHE_TTL_MS,
    payload,
  });
}

/**
 * Build a rectangular polygon from min/max bbox coordinates of a roll-up block.
 * Used to render the outline of a coarsened canonical-grid cell aggregation.
 */
function buildBboxPolygon(
  minLon: number,
  minLat: number,
  maxLon: number,
  maxLat: number,
): GeoJSON.Position[][] {
  return [[
    [minLon, minLat],
    [maxLon, minLat],
    [maxLon, maxLat],
    [minLon, maxLat],
    [minLon, minLat],
  ]];
}

function buildLineFromBearing(lon: number, lat: number, bearingDeg: number, lengthDeg: number): number[][] {
  const theta = (bearingDeg * Math.PI) / 180;
  const dLon = Math.sin(theta) * lengthDeg;
  const dLat = Math.cos(theta) * lengthDeg * 0.72;
  return [
    [lon, lat],
    [lon + dLon, lat + dLat],
  ];
}

function distanceKm(lon1: number, lat1: number, lon2: number, lat2: number): number {
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const earthRadiusKm = 6371;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return earthRadiusKm * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function baseFilterConditions({
  restrictToScope,
  requireHeading = false,
  requireDeployment = false,
}: {
  restrictToScope: boolean;
  requireHeading?: boolean;
  requireDeployment?: boolean;
}) {
  return `
    location_lat IS NOT NULL
    AND location_long IS NOT NULL
    ${restrictToScope ? `
    AND location_lat BETWEEN ${SCOPE_BOUNDS.minLat} AND ${SCOPE_BOUNDS.maxLat}
    AND location_long BETWEEN ${SCOPE_BOUNDS.minLon} AND ${SCOPE_BOUNDS.maxLon}
    ` : ""}
    ${requireHeading ? "AND heading IS NOT NULL AND COALESCE(ground_speed, 0) >= 1" : ""}
    ${requireDeployment ? "AND deploy_on_latitude IS NOT NULL AND deploy_on_longitude IS NOT NULL" : ""}
    AND ($1::text[] IS NULL OR individual_taxon_canonical_name = ANY($1::text[]))
  `;
}

function baseWhereClause({
  restrictToScope,
  requireHeading = false,
  requireDeployment = false,
}: {
  restrictToScope: boolean;
  requireHeading?: boolean;
  requireDeployment?: boolean;
}) {
  const conditions = baseFilterConditions({ restrictToScope, requireHeading, requireDeployment });
  return `
    WHERE ${conditions}
      AND timestamp >= (
        SELECT MAX(anchor.timestamp) - make_interval(days => $2::int)
        FROM movebank_tbl AS anchor
        WHERE ${conditions}
      )
  `;
}

async function getBaseSummary(
  species: string[],
  windowDays: number,
  restrictToScope: boolean,
): Promise<BaseSummaryRow> {
  const speciesParam = species.length > 0 ? species : null;
  const result = await query(
    `
      SELECT
        COUNT(*)::int AS fix_count,
        COUNT(DISTINCT COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text)))::int AS individual_count,
        COUNT(DISTINCT ${SPECIES_LABEL_SQL})::int AS species_count,
        MAX(timestamp)::text AS latest_ts
      FROM movebank_tbl
      ${baseWhereClause({ restrictToScope })}
    `,
    [speciesParam, windowDays],
  );

  const row = result.rows[0] ?? {};
  return {
    fix_count: asNumber(row.fix_count),
    individual_count: asNumber(row.individual_count),
    species_count: asNumber(row.species_count),
    latest_ts: typeof row.latest_ts === "string" ? row.latest_ts : null,
  };
}

function buildSummary(base: BaseSummaryRow, windowDays: number, featureCount: number, metricLabel?: string, metricRange?: string): MovebankOverlaySummary {
  return {
    featureCount,
    individualCount: base.individual_count,
    speciesCount: base.species_count,
    latestTimestamp: base.latest_ts,
    windowDays,
    metricLabel,
    metricRange,
  };
}

async function buildCorridors(
  species: string[],
  windowDays: number,
  aggregationKm: number,
): Promise<MovebankOverlayResponse> {
  const meta = MOVEBANK_OVERLAY_META.corridors;
  const summaryBase = await getBaseSummary(species, windowDays, true);
  const speciesParam = species.length > 0 ? species : null;
  const factor = aggregationKm / CANONICAL_CELL_KM;
  const result = await query(
    `
      WITH filtered AS (
        SELECT
          fn_point_to_cell(location_lat, location_long, $3) AS cell_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text)) AS individual_key,
          ${SPECIES_LABEL_SQL} AS species_name,
          timestamp AS ts
        FROM movebank_tbl
        ${baseWhereClause({ restrictToScope: true })}
      ),
      joined AS (
        SELECT
          (g.grid_row / $4)::int AS rblock,
          (g.grid_col / $4)::int AS cblock,
          g.bbox_min_lat,
          g.bbox_min_lon,
          g.bbox_max_lat,
          g.bbox_max_lon,
          f.individual_key,
          f.species_name,
          f.ts
        FROM filtered f
        JOIN grid_cells_tbl g
          ON g.grid_cell_id = f.cell_id
         AND g.grid_system  = $3
         AND g.grid_row IS NOT NULL
      )
      SELECT
        rblock,
        cblock,
        MIN(bbox_min_lon)::float8 AS min_lon,
        MIN(bbox_min_lat)::float8 AS min_lat,
        MAX(bbox_max_lon)::float8 AS max_lon,
        MAX(bbox_max_lat)::float8 AS max_lat,
        COUNT(*)::int AS fixes,
        COUNT(DISTINCT individual_key)::int AS individuals,
        COUNT(DISTINCT species_name)::int AS species_count,
        MAX(ts)::text AS latest_ts
      FROM joined
      GROUP BY rblock, cblock
      HAVING COUNT(*) >= 4
      ORDER BY individuals DESC, fixes DESC
      LIMIT 160
    `,
    [speciesParam, windowDays, CANONICAL_GRID_SYSTEM, factor],
  );

  const rows = result.rows;
  const maxIndividuals = Math.max(...rows.map((row) => asNumber(row.individuals)), 1);
  const maxFixes = Math.max(...rows.map((row) => asNumber(row.fixes)), 1);

  const polygons: GeoJSON.Feature[] = rows.map((row) => {
    const individuals = asNumber(row.individuals);
    const fixes = asNumber(row.fixes);
    const intensity = clamp(Math.max(individuals / maxIndividuals, fixes / maxFixes), 0, 1);
    return {
      type: "Feature",
      properties: {
        fixes,
        individuals,
        species_count: asNumber(row.species_count),
        latest_ts: row.latest_ts,
        fill_color: withAlpha(meta.accent, 0.10 + intensity * 0.32),
        fill_opacity: 0.12 + intensity * 0.28,
        line_color: withAlpha(meta.accent, 0.24 + intensity * 0.44),
        line_opacity: 0.28 + intensity * 0.42,
        glow_opacity: 0.06 + intensity * 0.18,
      },
      geometry: {
        type: "Polygon",
        coordinates: buildBboxPolygon(
          asNumber(row.min_lon),
          asNumber(row.min_lat),
          asNumber(row.max_lon),
          asNumber(row.max_lat),
        ),
      },
    };
  });

  const labels: GeoJSON.Feature[] = rows.slice(0, 14).map((row) => {
    const lon = (asNumber(row.min_lon) + asNumber(row.max_lon)) / 2;
    const lat = (asNumber(row.min_lat) + asNumber(row.max_lat)) / 2;
    const individuals = asNumber(row.individuals);
    return {
      type: "Feature",
      properties: {
        text: `${individuals} inds`,
        text_color: withAlpha(meta.accent, 0.7),
      },
      geometry: { type: "Point", coordinates: [lon, lat] },
    };
  });

  return {
    overlay: meta.id,
    generatedAt: new Date().toISOString(),
    summary: buildSummary(summaryBase, windowDays, polygons.length, `traffic intensity (${aggregationKm} km bins)`, `1–${maxIndividuals} inds/cell`),
    polygons: buildFeatureCollection(polygons),
    labels: buildFeatureCollection(labels),
  };
}

async function buildStopovers(
  species: string[],
  windowDays: number,
  aggregationKm: number,
): Promise<MovebankOverlayResponse> {
  const meta = MOVEBANK_OVERLAY_META.stopovers;
  const summaryBase = await getBaseSummary(species, windowDays, true);
  const speciesParam = species.length > 0 ? species : null;
  const factor = aggregationKm / CANONICAL_CELL_KM;
  const result = await query(
    `
      WITH filtered AS (
        SELECT
          fn_point_to_cell(location_lat, location_long, $3) AS cell_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text)) AS individual_key,
          ${SPECIES_LABEL_SQL} AS species_name,
          location_long AS lon,
          location_lat AS lat,
          timestamp AS ts
        FROM movebank_tbl
        ${baseWhereClause({ restrictToScope: true })}
      ),
      joined AS (
        SELECT
          (g.grid_row / $4)::int AS rblock,
          (g.grid_col / $4)::int AS cblock,
          g.bbox_min_lat,
          g.bbox_min_lon,
          g.bbox_max_lat,
          g.bbox_max_lon,
          f.individual_key,
          f.species_name,
          f.lon,
          f.lat,
          f.ts
        FROM filtered f
        JOIN grid_cells_tbl g
          ON g.grid_cell_id = f.cell_id
         AND g.grid_system  = $3
         AND g.grid_row IS NOT NULL
      ),
      individual_cells AS (
        SELECT
          rblock,
          cblock,
          MIN(bbox_min_lon) AS bbox_min_lon,
          MIN(bbox_min_lat) AS bbox_min_lat,
          MAX(bbox_max_lon) AS bbox_max_lon,
          MAX(bbox_max_lat) AS bbox_max_lat,
          individual_key,
          MIN(species_name) AS species_name,
          AVG(lon)::float8 AS lon,
          AVG(lat)::float8 AS lat,
          COUNT(*)::int AS fix_count,
          EXTRACT(EPOCH FROM MAX(ts) - MIN(ts)) / 3600.0 AS dwell_hours,
          MAX(ts)::text AS latest_ts
        FROM joined
        GROUP BY rblock, cblock, individual_key
        HAVING COUNT(*) >= 4
           AND MAX(ts) - MIN(ts) >= INTERVAL '6 hours'
      )
      SELECT
        rblock,
        cblock,
        AVG(lon)::float8 AS lon,
        AVG(lat)::float8 AS lat,
        MIN(bbox_min_lon)::float8 AS min_lon,
        MIN(bbox_min_lat)::float8 AS min_lat,
        MAX(bbox_max_lon)::float8 AS max_lon,
        MAX(bbox_max_lat)::float8 AS max_lat,
        COUNT(*)::int AS stop_events,
        COUNT(DISTINCT individual_key)::int AS individuals,
        ROUND(AVG(dwell_hours)::numeric, 1)::float8 AS mean_dwell_hours,
        ROUND(MAX(dwell_hours)::numeric, 1)::float8 AS max_dwell_hours,
        MAX(latest_ts)::text AS latest_ts
      FROM individual_cells
      GROUP BY rblock, cblock
      ORDER BY individuals DESC, stop_events DESC, mean_dwell_hours DESC
      LIMIT 80
    `,
    [speciesParam, windowDays, CANONICAL_GRID_SYSTEM, factor],
  );

  const rows = result.rows;
  const maxIndividuals = Math.max(...rows.map((row) => asNumber(row.individuals)), 1);
  const maxDwell = Math.max(...rows.map((row) => asNumber(row.mean_dwell_hours)), 1);

  const points: GeoJSON.Feature[] = rows.map((row) => {
    const individuals = asNumber(row.individuals);
    const dwell = asNumber(row.mean_dwell_hours);
    const intensity = clamp(Math.max(individuals / maxIndividuals, dwell / maxDwell), 0, 1);
    return {
      type: "Feature",
      properties: {
        stop_events: asNumber(row.stop_events),
        individuals,
        dwell_hours: dwell,
        latest_ts: row.latest_ts,
        point_color: withAlpha(meta.accent, 0.42 + intensity * 0.5),
        point_opacity: 0.78,
        point_radius: 4 + intensity * 9,
        glow_color: withAlpha(meta.accent, 0.2 + intensity * 0.28),
        glow_radius: 12 + intensity * 16,
        glow_opacity: 0.16 + intensity * 0.18,
        stroke_color: withAlpha("rgb(255, 255, 255)", 0.18),
      },
      geometry: {
        type: "Point",
        coordinates: [asNumber(row.lon), asNumber(row.lat)],
      },
    };
  });

  const labels: GeoJSON.Feature[] = rows.slice(0, 10).map((row) => ({
    type: "Feature",
    properties: {
      text: `${Math.round(asNumber(row.mean_dwell_hours))}h dwell`,
      text_color: withAlpha(meta.accent, 0.76),
    },
    geometry: {
      type: "Point",
      coordinates: [asNumber(row.lon), asNumber(row.lat)],
    },
  }));

  return {
    overlay: meta.id,
    generatedAt: new Date().toISOString(),
    summary: buildSummary(summaryBase, windowDays, points.length, `dwell duration (${aggregationKm} km bins)`, `1–${Math.round(maxDwell)}h mean dwell`),
    points: buildFeatureCollection(points),
    labels: buildFeatureCollection(labels),
  };
}

async function buildRecentFixes(species: string[], windowDays: number): Promise<MovebankOverlayResponse> {
  const meta = MOVEBANK_OVERLAY_META.recent_fixes;
  const summaryBase = await getBaseSummary(species, windowDays, true);
  const speciesParam = species.length > 0 ? species : null;
  const recentFixConditions = baseFilterConditions({ restrictToScope: true });
  const result = await query(
    `
      WITH filtered AS (
        SELECT
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text)) AS individual_key,
          ${SPECIES_LABEL_SQL} AS species_name,
          study_id,
          location_long AS lon,
          location_lat AS lat,
          timestamp AS ts,
          ground_speed,
          heading,
          MAX(timestamp) OVER () AS anchor_ts
        FROM movebank_tbl
        WHERE ${recentFixConditions}
          AND timestamp >= (
            SELECT MAX(anchor.timestamp) - make_interval(days => $2::int)
            FROM movebank_tbl AS anchor
            WHERE ${recentFixConditions}
          )
      ),
      ranked AS (
        SELECT
          individual_key,
          species_name,
          study_id,
          lon,
          lat,
          ts,
          ground_speed,
          heading,
          anchor_ts,
          ROW_NUMBER() OVER (
            PARTITION BY study_id, individual_key
            ORDER BY ts DESC
          ) AS rn
        FROM filtered
      )
      SELECT
        individual_key,
        species_name,
        study_id,
        lon,
        lat,
        ts::text AS ts,
        ground_speed,
        heading,
        EXTRACT(EPOCH FROM anchor_ts - ts) / 3600.0 AS age_hours
      FROM ranked
      WHERE rn = 1
      ORDER BY ts DESC
      LIMIT 180
    `,
    [speciesParam, windowDays],
  );

  const rows = result.rows;
  const maxAge = Math.max(...rows.map((row) => asNumber(row.age_hours)), 1);

  const points: GeoJSON.Feature[] = rows.map((row) => {
    const ageHours = asNumber(row.age_hours);
    const freshness = clamp(1 - ageHours / maxAge, 0.1, 1);
    return {
      type: "Feature",
      properties: {
        individual: asText(row.individual_key, "Unknown"),
        species: asText(row.species_name, "Unknown"),
        study_id: asNumber(row.study_id),
        ts: row.ts,
        age_hours: ageHours,
        point_color: withAlpha(meta.accent, 0.28 + freshness * 0.62),
        point_opacity: 0.72 + freshness * 0.2,
        point_radius: 3 + freshness * 5,
        glow_color: withAlpha(meta.accent, 0.12 + freshness * 0.24),
        glow_radius: 10 + freshness * 8,
        glow_opacity: 0.10 + freshness * 0.12,
        stroke_color: withAlpha("rgb(255, 255, 255)", 0.22),
      },
      geometry: {
        type: "Point",
        coordinates: [asNumber(row.lon), asNumber(row.lat)],
      },
    };
  });

  const labels: GeoJSON.Feature[] = rows.slice(0, 12).map((row) => {
    const speciesName = asText(row.species_name, "Unknown");
    const shortSpecies = speciesName === DEFAULT_SPECIES_LABEL
      ? "Tagged bird"
      : speciesName.split(" ").slice(-1)[0] || speciesName;
    return {
      type: "Feature",
      properties: {
        text: `${shortSpecies} · ${Math.round(asNumber(row.age_hours))}h`,
        text_color: withAlpha(meta.accent, 0.72),
      },
      geometry: {
        type: "Point",
        coordinates: [asNumber(row.lon), asNumber(row.lat)],
      },
    };
  });

  return {
    overlay: meta.id,
    generatedAt: new Date().toISOString(),
    summary: buildSummary(summaryBase, windowDays, points.length, "fix freshness", `0–${Math.round(maxAge)}h age`),
    points: buildFeatureCollection(points),
    labels: buildFeatureCollection(labels),
  };
}

async function buildFlowField(
  species: string[],
  windowDays: number,
  aggregationKm: number,
): Promise<MovebankOverlayResponse> {
  const meta = MOVEBANK_OVERLAY_META.flow_field;
  const summaryBase = await getBaseSummary(species, windowDays, true);
  const speciesParam = species.length > 0 ? species : null;
  const factor = aggregationKm / CANONICAL_CELL_KM;
  const result = await query(
    `
      WITH filtered AS (
        SELECT
          study_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text)) AS individual_key,
          location_long AS lon,
          location_lat AS lat,
          timestamp AS ts,
          LAG(location_long) OVER (
            PARTITION BY study_id, COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text))
            ORDER BY timestamp
          ) AS prev_lon,
          LAG(location_lat) OVER (
            PARTITION BY study_id, COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text))
            ORDER BY timestamp
          ) AS prev_lat,
          LAG(timestamp) OVER (
            PARTITION BY study_id, COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text))
            ORDER BY timestamp
          ) AS prev_ts
        FROM movebank_tbl
        ${baseWhereClause({ restrictToScope: true })}
      ),
      segments AS (
        SELECT
          ((lon + prev_lon) / 2.0)::float8 AS mid_lon,
          ((lat + prev_lat) / 2.0)::float8 AS mid_lat,
          fn_point_to_cell(
            ((lat + prev_lat) / 2.0),
            ((lon + prev_lon) / 2.0),
            $3
          ) AS cell_id,
          ts,
          individual_key,
          MOD((DEGREES(ATAN2(lon - prev_lon, lat - prev_lat)) + 360.0)::numeric, 360::numeric)::float8 AS bearing_deg,
          (
            SQRT(
              POWER((lon - prev_lon) * 111320.0 * COS(RADIANS(((lat + prev_lat) / 2.0))), 2) +
              POWER((lat - prev_lat) * 110540.0, 2)
            ) / NULLIF(EXTRACT(EPOCH FROM ts - prev_ts), 0)
          )::float8 AS derived_speed
        FROM filtered
        WHERE prev_lon IS NOT NULL
          AND prev_lat IS NOT NULL
          AND prev_ts IS NOT NULL
          AND ts > prev_ts
      ),
      usable AS (
        SELECT s.*, g.grid_row, g.grid_col
        FROM segments s
        JOIN grid_cells_tbl g
          ON g.grid_cell_id = s.cell_id
         AND g.grid_system  = $3
         AND g.grid_row IS NOT NULL
        WHERE s.derived_speed BETWEEN 0.5 AND 35
      )
      SELECT
        (grid_row / $4)::int AS rblock,
        (grid_col / $4)::int AS cblock,
        AVG(mid_lon)::float8 AS lon,
        AVG(mid_lat)::float8 AS lat,
        COUNT(*)::int AS sample_count,
        COUNT(DISTINCT individual_key)::int AS individuals,
        ROUND(AVG(derived_speed)::numeric, 1)::float8 AS avg_speed,
        MOD(
          (DEGREES(ATAN2(AVG(SIN(RADIANS(bearing_deg))), AVG(COS(RADIANS(bearing_deg))))) + 360.0)::numeric,
          360::numeric
        )::float8 AS bearing_deg,
        SQRT(
          POWER(AVG(SIN(RADIANS(bearing_deg))), 2) +
          POWER(AVG(COS(RADIANS(bearing_deg))), 2)
        )::float8 AS coherence,
        MAX(ts)::text AS latest_ts
      FROM usable
      GROUP BY rblock, cblock
      HAVING COUNT(*) >= 5
      ORDER BY coherence DESC, sample_count DESC
      LIMIT 90
    `,
    [speciesParam, windowDays, CANONICAL_GRID_SYSTEM, factor],
  );

  const rows = result.rows;
  const maxSpeed = Math.max(...rows.map((row) => asNumber(row.avg_speed)), 1);

  const vectors: GeoJSON.Feature[] = rows.map((row) => {
    const coherence = clamp(asNumber(row.coherence), 0, 1);
    const avgSpeed = asNumber(row.avg_speed);
    const size = 12 + (avgSpeed / maxSpeed) * 10 + coherence * 8;
    return {
      type: "Feature",
      properties: {
        angle: asNumber(row.bearing_deg),
        glyph: "↑",
        vector_size: size,
        vector_color: withAlpha(meta.accent, 0.38 + coherence * 0.56),
        vector_opacity: 0.5 + coherence * 0.45,
        avg_speed: avgSpeed,
        coherence,
        sample_count: asNumber(row.sample_count),
        individuals: asNumber(row.individuals),
        text_color: withAlpha(meta.accent, 0.74),
      },
      geometry: {
        type: "Point",
        coordinates: [asNumber(row.lon), asNumber(row.lat)],
      },
    };
  });

  const labels: GeoJSON.Feature[] = rows.slice(0, 12).map((row) => ({
    type: "Feature",
    properties: {
      text: `${asNumber(row.avg_speed).toFixed(1)} m/s`,
      text_color: withAlpha(meta.accent, 0.68),
    },
    geometry: {
      type: "Point",
      coordinates: [asNumber(row.lon), asNumber(row.lat)],
    },
  }));

  return {
    overlay: meta.id,
    generatedAt: new Date().toISOString(),
    summary: buildSummary(summaryBase, windowDays, vectors.length, `flow coherence (${aggregationKm} km bins)`, `≤ ${Math.round(maxSpeed)} m/s mean speed`),
    vectors: buildFeatureCollection(vectors),
    labels: buildFeatureCollection(labels),
  };
}

async function buildJourneyStories(species: string[], windowDays: number): Promise<MovebankOverlayResponse> {
  const meta = MOVEBANK_OVERLAY_META.journey_stories;
  const speciesParam = species.length > 0 ? species : null;
  const result = await query(
    `
      WITH filtered AS (
        SELECT
          study_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, CONCAT('study-', study_id::text, '-event-', event_id::text)) AS individual_key,
          COALESCE(NULLIF(individual_local_identifier, ''), CONCAT('IND-', COALESCE(individual_id::text, deployment_id::text, event_id::text))) AS individual_label,
          ${SPECIES_LABEL_SQL} AS species_name,
          location_long AS lon,
          location_lat AS lat,
          timestamp AS ts,
          (
            location_lat BETWEEN ${STORY_BOUNDS.minLat} AND ${STORY_BOUNDS.maxLat}
            AND location_long BETWEEN ${STORY_BOUNDS.minLon} AND ${STORY_BOUNDS.maxLon}
          ) AS in_scope
        FROM movebank_tbl
        ${baseWhereClause({ restrictToScope: false })}
      ),
      ranked AS (
        SELECT
          study_id,
          individual_key,
          MAX(individual_label) AS individual_label,
          MIN(species_name) AS species_name,
          COUNT(*)::int AS fix_count,
          SUM(CASE WHEN in_scope THEN 1 ELSE 0 END)::int AS in_scope_fix_count,
          MIN(ts) AS first_ts,
          MAX(ts) AS last_ts
        FROM filtered
        GROUP BY study_id, individual_key
        HAVING COUNT(*) >= 6
            AND SUM(CASE WHEN in_scope THEN 1 ELSE 0 END) >= 1
        ORDER BY in_scope_fix_count DESC, COUNT(*) DESC, MAX(ts) DESC
        LIMIT 6
      ),
      sampled AS (
        SELECT
          f.study_id,
          f.individual_key,
          r.individual_label,
          r.species_name,
          r.fix_count,
          f.lon,
          f.lat,
          f.ts,
          ROW_NUMBER() OVER (PARTITION BY f.study_id, f.individual_key ORDER BY f.ts) AS rn,
          COUNT(*) OVER (PARTITION BY f.study_id, f.individual_key) AS total_points
        FROM filtered f
        INNER JOIN ranked r
          ON r.study_id = f.study_id
         AND r.individual_key = f.individual_key
        WHERE f.lon BETWEEN ${STORY_BOUNDS.minLon} AND ${STORY_BOUNDS.maxLon}
          AND f.lat BETWEEN ${STORY_BOUNDS.minLat} AND ${STORY_BOUNDS.maxLat}
      )
      SELECT
        study_id,
        individual_key,
        individual_label,
        species_name,
        fix_count,
        lon,
        lat,
        ts::text AS ts,
        rn,
        total_points
      FROM sampled
      WHERE rn = 1
         OR rn = total_points
         OR rn % GREATEST(1, FLOOR(total_points / 90.0)::int) = 0
      ORDER BY species_name, individual_label, ts
    `,
    [speciesParam, windowDays],
  );

  const grouped = new Map<string, {
    studyId: number;
    individual: string;
    label: string;
    species: string;
    fixCount: number;
    points: number[][];
    lastPoint: number[] | null;
    lastTs: string | null;
  }>();

  for (const row of result.rows) {
    const key = `${asNumber(row.study_id)}::${asText(row.individual_key, "Unknown")}`;
    const entry = grouped.get(key) ?? {
      studyId: asNumber(row.study_id),
      individual: asText(row.individual_key, "Unknown"),
      label: asText(row.individual_label, "Unknown"),
      species: asText(row.species_name, "Unknown"),
      fixCount: asNumber(row.fix_count),
      points: [],
      lastPoint: null,
      lastTs: null,
    };
    const point = [asNumber(row.lon), asNumber(row.lat)];
    entry.points.push(point);
    entry.lastPoint = point;
    entry.lastTs = typeof row.ts === "string" ? row.ts : null;
    grouped.set(key, entry);
  }

  const journeys = Array.from(grouped.values()).filter((entry) => entry.points.length >= 2);
  const lines: GeoJSON.Feature[] = [];
  const points: GeoJSON.Feature[] = [];
  const labels: GeoJSON.Feature[] = [];

  journeys.forEach((entry, index) => {
    const color = JOURNEY_COLORS[index % JOURNEY_COLORS.length];
    lines.push({
      type: "Feature",
      properties: {
        individual: entry.individual,
        label: entry.label,
        species: entry.species,
        line_color: withAlpha(color, 0.72),
        line_opacity: 0.74,
        line_width: 2.2,
        glow_color: withAlpha(color, 0.24),
        glow_width: 9,
        glow_opacity: 0.22,
      },
      geometry: {
        type: "LineString",
        coordinates: entry.points,
      },
    });

    if (entry.lastPoint) {
      points.push({
        type: "Feature",
        properties: {
          species: entry.species,
          individual: entry.individual,
          point_color: withAlpha(color, 0.9),
          point_opacity: 0.9,
          point_radius: 5.5,
          glow_color: withAlpha(color, 0.24),
          glow_radius: 14,
          glow_opacity: 0.16,
          stroke_color: withAlpha("rgb(255, 255, 255)", 0.22),
        },
        geometry: {
          type: "Point",
          coordinates: entry.lastPoint,
        },
      });

      labels.push({
        type: "Feature",
        properties: {
          text: entry.species === DEFAULT_SPECIES_LABEL
            ? "Tagged bird"
            : entry.species.split(" ").slice(-1)[0] || entry.label,
          text_color: withAlpha(color, 0.82),
        },
        geometry: {
          type: "Point",
          coordinates: entry.lastPoint,
        },
      });
    }
  });

  const latestTs = journeys
    .map((entry) => entry.lastTs)
    .filter((value): value is string => typeof value === "string")
    .sort()
    .slice(-1)[0] ?? null;

  const speciesCount = new Set(journeys.map((entry) => entry.species)).size;
  const summary: MovebankOverlaySummary = {
    featureCount: lines.length,
    individualCount: journeys.length,
    speciesCount,
    latestTimestamp: latestTs,
    windowDays,
    metricLabel: "narrative tracks",
    metricRange: `top ${journeys.length} individuals`,
  };

  return {
    overlay: meta.id,
    generatedAt: new Date().toISOString(),
    summary,
    lines: buildFeatureCollection(lines),
    points: buildFeatureCollection(points),
    labels: buildFeatureCollection(labels),
  };
}

async function buildDeploymentOrigins(species: string[], windowDays: number): Promise<MovebankOverlayResponse> {
  const meta = MOVEBANK_OVERLAY_META.deployment_origins;
  const speciesParam = species.length > 0 ? species : null;
  const result = await query(
    `
      WITH ranked AS (
        SELECT DISTINCT ON (
          study_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, COALESCE(deployment_id::text, event_id::text))
        )
          study_id,
          deployment_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, COALESCE(deployment_id::text, event_id::text)) AS individual_key,
          ${SPECIES_LABEL_SQL} AS species_name,
          deploy_on_longitude AS origin_lon,
          deploy_on_latitude AS origin_lat,
          location_long AS latest_lon,
          location_lat AS latest_lat,
          timestamp::text AS latest_ts
        FROM movebank_tbl
        ${baseWhereClause({ restrictToScope: true, requireDeployment: true })}
        ORDER BY
          study_id,
          COALESCE(NULLIF(individual_local_identifier, ''), individual_id::text, COALESCE(deployment_id::text, event_id::text)),
          timestamp DESC
      )
      SELECT *
      FROM ranked
      LIMIT 60
    `,
    [speciesParam, windowDays],
  );

  const rows = result.rows;
  const distances = rows.map((row) => distanceKm(
    asNumber(row.origin_lon),
    asNumber(row.origin_lat),
    asNumber(row.latest_lon),
    asNumber(row.latest_lat),
  ));
  const maxDistance = Math.max(...distances, 1);

  const lines: GeoJSON.Feature[] = [];
  const points: GeoJSON.Feature[] = [];
  const labels: GeoJSON.Feature[] = [];

  rows.forEach((row, index) => {
    const originLon = asNumber(row.origin_lon);
    const originLat = asNumber(row.origin_lat);
    const latestLon = asNumber(row.latest_lon);
    const latestLat = asNumber(row.latest_lat);
    const distance = distanceKm(originLon, originLat, latestLon, latestLat);
    const intensity = clamp(distance / maxDistance, 0.1, 1);
    const color = withAlpha(meta.accent, 0.28 + intensity * 0.5);

    lines.push({
      type: "Feature",
      properties: {
        species: asText(row.species_name, "Unknown"),
        individual: asText(row.individual_key, "Unknown"),
        distance_km: Math.round(distance),
        line_color: color,
        line_opacity: 0.62,
        line_width: 1.8 + intensity * 1.2,
        glow_color: withAlpha(meta.accent, 0.18 + intensity * 0.18),
        glow_width: 7 + intensity * 5,
        glow_opacity: 0.14 + intensity * 0.12,
      },
      geometry: {
        type: "LineString",
        coordinates: [[originLon, originLat], [latestLon, latestLat]],
      },
    });

    points.push({
      type: "Feature",
      properties: {
        role: "origin",
        point_color: withAlpha("rgb(255, 255, 255)", 0.68),
        point_opacity: 0.56,
        point_radius: 2.8,
        glow_color: withAlpha(meta.accent, 0.08),
        glow_radius: 8,
        glow_opacity: 0.08,
        stroke_color: withAlpha(meta.accent, 0.12),
      },
      geometry: { type: "Point", coordinates: [originLon, originLat] },
    });

    points.push({
      type: "Feature",
      properties: {
        role: "latest",
        point_color: withAlpha(meta.accent, 0.88),
        point_opacity: 0.84,
        point_radius: 4.6 + intensity * 2.4,
        glow_color: withAlpha(meta.accent, 0.16 + intensity * 0.12),
        glow_radius: 12 + intensity * 6,
        glow_opacity: 0.14 + intensity * 0.08,
        stroke_color: withAlpha("rgb(255, 255, 255)", 0.18),
      },
      geometry: { type: "Point", coordinates: [latestLon, latestLat] },
    });

    if (index < 10) {
      labels.push({
        type: "Feature",
        properties: {
          text: `${Math.round(distance)} km`,
          text_color: withAlpha(meta.accent, 0.74),
        },
        geometry: {
          type: "Point",
          coordinates: [latestLon, latestLat],
        },
      });
    }
  });

  const latestTs = rows
    .map((row) => (typeof row.latest_ts === "string" ? row.latest_ts : null))
    .filter((value): value is string => value !== null)
    .sort()
    .slice(-1)[0] ?? null;

  const summary: MovebankOverlaySummary = {
    featureCount: lines.length,
    individualCount: rows.length,
    speciesCount: new Set(rows.map((row) => asText(row.species_name, "Unknown"))).size,
    latestTimestamp: latestTs,
    windowDays,
    metricLabel: "deployment displacement",
    metricRange: `${Math.round(maxDistance)} km max displacement`,
  };

  return {
    overlay: meta.id,
    generatedAt: new Date().toISOString(),
    summary,
    lines: buildFeatureCollection(lines),
    points: buildFeatureCollection(points),
    labels: buildFeatureCollection(labels),
  };
}

const handleGet = async (request: Request) => {
  try {
    const { searchParams } = new URL(request.url);
    const overlayParam = searchParams.get("overlay");

    if (!isMovebankOverlayId(overlayParam)) {
      return NextResponse.json(
        { error: "A valid Movebank overlay id is required." },
        { status: 400 },
      );
    }

    const overlay: MovebankOverlayId = overlayParam;
    const meta = MOVEBANK_OVERLAY_META[overlay];
    const species = searchParams.getAll("species").filter((value) => value.trim().length > 0);
    const requestedWindowDays = Number(searchParams.get("window_days"));
    const windowDays = Number.isFinite(requestedWindowDays) && requestedWindowDays > 0
      ? Math.min(Math.round(requestedWindowDays), 3650)
      : meta.defaultWindowDays;

    const aggregationKm = resolveAggregationKm(overlay, searchParams.get("aggregation_km"));
    const cacheKey = buildCacheKey(overlay, species, windowDays, aggregationKm);
    const cached = movebankOverlayCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now()) {
      return NextResponse.json(cached.payload, { headers: RESPONSE_HEADERS });
    }

    let payload: MovebankOverlayResponse;
    switch (overlay) {
      case "corridors":
        payload = await buildCorridors(species, windowDays, aggregationKm);
        break;
      case "stopovers":
        payload = await buildStopovers(species, windowDays, aggregationKm);
        break;
      case "recent_fixes":
        payload = await buildRecentFixes(species, windowDays);
        break;
      case "flow_field":
        payload = await buildFlowField(species, windowDays, aggregationKm);
        break;
      case "journey_stories":
        payload = await buildJourneyStories(species, windowDays);
        break;
      case "deployment_origins":
        payload = await buildDeploymentOrigins(species, windowDays);
        break;
    }

    rememberPayload(cacheKey, payload);

    return NextResponse.json(payload, {
      headers: RESPONSE_HEADERS,
    });
  } catch (error) {
    console.error("Movebank overlays API error:", error);
    return NextResponse.json(
      { error: "Failed to fetch Movebank overlay data" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
