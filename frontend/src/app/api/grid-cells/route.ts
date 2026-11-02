import { NextResponse } from "next/server";
import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";

export const dynamic = "force-dynamic";

const DEFAULT_GRID_SIZE_KM = 5 as const;
type SupportedGridSizeKm = typeof DEFAULT_GRID_SIZE_KM;

const DEFAULT_GRID_SYSTEM = "nl_geodesic_5km";

const CACHE_TTL_MS = 15 * 60 * 1000;
const RESPONSE_HEADERS = {
  "Cache-Control": "public, s-maxage=900, stale-while-revalidate=600",
};

type GridCellsCacheKey = `${SupportedGridSizeKm}:${"with-counts" | "no-counts"}`;

const gridCellsCache = new Map<
  GridCellsCacheKey,
  {
    expiresAt: number;
    payload: {
      generatedAt: string;
      source: string;
      gridSizeKm: number;
      cells: Array<Record<string, unknown>>;
    };
  }
>();

type GridCellRow = {
  grid_cell_id: string;
  grid_system: string;
  lat: number;
  lon: number;
  bbox_min_lat: number;
  bbox_min_lon: number;
  bbox_max_lat: number;
  bbox_max_lon: number;
  grid_size_km: number;
  cell_geojson: unknown;
  station_count?: number | null;
  total_detections?: number | null;
};

type GridCellPayload = {
  grid_cell_id: string;
  grid_system: string;
  lat: number;
  lon: number;
  bbox_min_lat: number;
  bbox_min_lon: number;
  bbox_max_lat: number;
  bbox_max_lon: number;
  grid_size_km: number;
  cell_geojson: unknown;
  station_count?: number;
  total_detections?: number;
};

function parseCellGeometry(value: unknown) {
  if (!value) return null;
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }
  return value;
}

function resolveGridSizeKm(_raw: string | null): SupportedGridSizeKm {
  void _raw;
  return DEFAULT_GRID_SIZE_KM;
}

function gridSystemForSize(_gridSizeKm: SupportedGridSizeKm): string {
  void _gridSizeKm;
  return DEFAULT_GRID_SYSTEM;
}

function resolveIncludeCounts(raw: string | null): boolean {
  if (raw === null) return true;
  const normalised = raw.trim().toLowerCase();
  if (normalised === "" || normalised === "true" || normalised === "1") return true;
  return false;
}

function buildCacheKey(
  gridSizeKm: SupportedGridSizeKm,
  includeCounts: boolean,
): GridCellsCacheKey {
  return `${gridSizeKm}:${includeCounts ? "with-counts" : "no-counts"}`;
}

const handleGet = async (request: Request) => {
  try {
    const { searchParams } = new URL(request.url);
    const gridSizeKm = resolveGridSizeKm(searchParams.get("grid_size_km"));
    const gridSystem = gridSystemForSize(gridSizeKm);
    const includeCounts = resolveIncludeCounts(searchParams.get("include_counts"));
    const cacheKey = buildCacheKey(gridSizeKm, includeCounts);
    const cached = gridCellsCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now()) {
      return NextResponse.json(cached.payload, { headers: RESPONSE_HEADERS });
    }

    const gridTableCheck = await query(`
      SELECT to_regclass('public.grid_cells_tbl') IS NOT NULL AS table_exists
    `);
    const gridTableExists = Boolean(gridTableCheck.rows[0]?.table_exists);
    const canonicalGridCheck = gridTableExists
      ? await query<{ has_rows: boolean }>(
          `
            SELECT EXISTS(
              SELECT 1
              FROM grid_cells_tbl gc
              WHERE gc.grid_system = $1
              LIMIT 1
            ) AS has_rows
          `,
          [gridSystem],
        )
      : { rows: [{ has_rows: false }] as Array<{ has_rows: boolean }> };
    const canonicalGridExists = Boolean(canonicalGridCheck.rows[0]?.has_rows);
    let hasLegacyFallback = false;

    if (!canonicalGridExists) {
      const earthEngineGridTableCheck = await query(`
        SELECT to_regclass('public.google_earth_engine_grid_tbl') IS NOT NULL AS table_exists
      `);
      const earthEngineGridTableExists = Boolean(earthEngineGridTableCheck.rows[0]?.table_exists);
      const legacyFallbackCheck = earthEngineGridTableExists
        ? await query<{ has_rows: boolean }>(
            `
              SELECT EXISTS(
                SELECT 1
                FROM google_earth_engine_grid_tbl gee
                WHERE gee.grid_size_km = $1
                LIMIT 1
              ) AS has_rows
            `,
            [gridSizeKm],
          )
        : { rows: [{ has_rows: false }] as Array<{ has_rows: boolean }> };
      hasLegacyFallback = Boolean(legacyFallbackCheck.rows[0]?.has_rows);
    }

    if (!canonicalGridExists && !hasLegacyFallback) {
      const emptyPayload = {
        generatedAt: new Date().toISOString(),
        source: "none",
        gridSizeKm,
        geometryClippedToCountry: true,
        cells: [],
      };
      gridCellsCache.set(cacheKey, {
        expiresAt: Date.now() + CACHE_TTL_MS,
        payload: emptyPayload,
      });
      return NextResponse.json(
        emptyPayload,
        { headers: RESPONSE_HEADERS },
      );
    }

    // The cheap path drops the LEFT JOIN against birdweather_stations_tbl
    // and the COUNT/SUM aggregation entirely. Both are unused by the alerts
    // hotspot consumer; SatelliteMap opts back into the full path by leaving
    // include_counts at its default (true). The full path is unchanged.
    const stationCountSelect = includeCounts
      ? `,
          COUNT(rs.station_id)::int AS station_count,
          COALESCE(SUM(rs.total_detections), 0)::bigint AS total_detections`
      : "";
    const canonicalStationJoin = includeCounts
      ? `LEFT JOIN birdweather_stations_tbl rs
          ON rs.lat BETWEEN gc.bbox_min_lat AND gc.bbox_max_lat
         AND rs.lon BETWEEN gc.bbox_min_lon AND gc.bbox_max_lon`
      : "";
    const canonicalGroupBy = includeCounts
      ? `GROUP BY
          gc.grid_cell_id,
          gc.grid_system,
          gc.lat,
          gc.lon,
          gc.bbox_min_lat,
          gc.bbox_min_lon,
          gc.bbox_max_lat,
          gc.bbox_max_lon,
          gc.grid_size_km,
          gc.cell_geojson`
      : "";
    const legacyStationJoin = includeCounts
      ? `LEFT JOIN birdweather_stations_tbl rs
          ON rs.lat BETWEEN dc.bbox_min_lat AND dc.bbox_max_lat
         AND rs.lon BETWEEN dc.bbox_min_lon AND dc.bbox_max_lon`
      : "";
    const legacyGroupBy = includeCounts
      ? `GROUP BY
          dc.grid_cell_id,
          dc.grid_system,
          dc.lat,
          dc.lon,
          dc.bbox_min_lat,
          dc.bbox_min_lon,
          dc.bbox_max_lat,
          dc.bbox_max_lon,
          dc.grid_size_km,
          dc.cell_geojson`
      : "";

    const sql = canonicalGridExists
      ? `
        SELECT
          gc.grid_cell_id,
          gc.grid_system,
          gc.lat,
          gc.lon,
          gc.bbox_min_lat,
          gc.bbox_min_lon,
          gc.bbox_max_lat,
          gc.bbox_max_lon,
          gc.grid_size_km,
          gc.cell_geojson${stationCountSelect}
        FROM grid_cells_tbl gc
        ${canonicalStationJoin}
        WHERE gc.grid_system = $1
        ${canonicalGroupBy}
        ORDER BY gc.grid_cell_id ASC
      `
      : `
        WITH distinct_cells AS (
          SELECT DISTINCT ON (gee.grid_cell_id)
            gee.grid_cell_id,
            $1::text AS grid_system,
            gee.lat,
            gee.lon,
            gee.bbox_min_lat,
            gee.bbox_min_lon,
            gee.bbox_max_lat,
            gee.bbox_max_lon,
            gee.grid_size_km,
            NULL::jsonb AS cell_geojson
          FROM google_earth_engine_grid_tbl gee
          WHERE gee.grid_size_km = $2
          ORDER BY gee.grid_cell_id, gee.observation_date DESC
        )
        SELECT
          dc.grid_cell_id,
          dc.grid_system,
          dc.lat,
          dc.lon,
          dc.bbox_min_lat,
          dc.bbox_min_lon,
          dc.bbox_max_lat,
          dc.bbox_max_lon,
          dc.grid_size_km,
          dc.cell_geojson${stationCountSelect}
        FROM distinct_cells dc
        ${legacyStationJoin}
        ${legacyGroupBy}
        ORDER BY dc.grid_cell_id ASC
      `;

    const sqlParams = canonicalGridExists
      ? [gridSystem]
      : [gridSystem, gridSizeKm];

    const res = await query<GridCellRow>(sql, sqlParams);
    const cells: GridCellPayload[] = res.rows.map((row) => {
      const base: GridCellPayload = {
        grid_cell_id: row.grid_cell_id,
        grid_system: row.grid_system,
        lat: Number(row.lat),
        lon: Number(row.lon),
        bbox_min_lat: Number(row.bbox_min_lat),
        bbox_min_lon: Number(row.bbox_min_lon),
        bbox_max_lat: Number(row.bbox_max_lat),
        bbox_max_lon: Number(row.bbox_max_lon),
        grid_size_km: Number(row.grid_size_km),
        cell_geojson: parseCellGeometry(row.cell_geojson),
      };
      if (includeCounts) {
        base.station_count = Number(row.station_count ?? 0);
        base.total_detections = Number(row.total_detections ?? 0);
      }
      return base;
    });

    const payload = {
      generatedAt: new Date().toISOString(),
      source: canonicalGridExists ? "grid_cells_tbl" : "google_earth_engine_grid_tbl",
      gridSizeKm,
      // The client will apply a lightweight country-aware filter to hide cells
      // outside the Netherlands without blocking this API route on heavy
      // geometry clipping work.
      geometryClippedToCountry: true,
      cells,
    };

    gridCellsCache.set(cacheKey, {
      expiresAt: Date.now() + CACHE_TTL_MS,
      payload,
    });

    return NextResponse.json(payload, { headers: RESPONSE_HEADERS });
  } catch (error) {
    console.error("Grid cells API error:", error);
    return NextResponse.json({ error: "Failed to fetch grid cells" }, { status: 500 });
  }
};

export const GET = withMeasuredApiRoute(handleGet);
