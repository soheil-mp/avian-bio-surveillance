import { NextResponse } from "next/server";
import type { GridCellProfileResponse } from "@/lib/earth-engine-overlays";
import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import {
  buildOverlayQuery,
  gridSystemForSize,
  mapOverlaySample,
  resolveGridSizeKm,
  type SupportedGridSizeKm,
} from "@/lib/earth-engine-grid-profiles";

export const dynamic = "force-dynamic";

const CACHE_TTL_MS = 15 * 60 * 1000;
const RESPONSE_HEADERS = {
  "Cache-Control": "public, s-maxage=900, stale-while-revalidate=600",
};

const gridCellProfileCache = new Map<
  string,
  {
    expiresAt: number;
    payload: GridCellProfileResponse;
  }
>();

function cacheKey(gridSizeKm: SupportedGridSizeKm, gridCellId: string): string {
  return `${gridSizeKm}:${gridCellId}`;
}

const handleGet = async (request: Request) => {
  try {
    const { searchParams } = new URL(request.url);
    const gridSizeKm = resolveGridSizeKm(searchParams.get("grid_size_km"));
    const gridSystem = gridSystemForSize(gridSizeKm);
    const gridCellId = searchParams.get("grid_cell_id")?.trim() ?? "";

    if (!gridCellId) {
      return NextResponse.json(
        { error: "grid_cell_id is required" },
        { status: 400 },
      );
    }

    const cached = gridCellProfileCache.get(cacheKey(gridSizeKm, gridCellId));
    if (cached && cached.expiresAt > Date.now()) {
      return NextResponse.json(cached.payload, { headers: RESPONSE_HEADERS });
    }

    const geeTableCheck = await query<{ table_exists: boolean }>(`
      SELECT to_regclass('public.google_earth_engine_tbl') IS NOT NULL AS table_exists
    `);
    const gridTableCheck = await query<{ table_exists: boolean }>(`
      SELECT to_regclass('public.grid_cells_tbl') IS NOT NULL AS table_exists
    `);

    const geeTableExists = Boolean(geeTableCheck.rows[0]?.table_exists);
    const gridTableExists = Boolean(gridTableCheck.rows[0]?.table_exists);

    const gridAvailability = geeTableExists && gridTableExists
      ? await query<{ has_rows: boolean }>(
          `
            SELECT EXISTS(
              SELECT 1
              FROM google_earth_engine_tbl gee
              INNER JOIN grid_cells_tbl gc
                ON gc.grid_cell_id = gee.grid_cell_id
              WHERE gc.grid_system = $1
                AND gee.grid_cell_id = $2
              LIMIT 1
            ) AS has_rows
          `,
          [gridSystem, gridCellId],
        )
      : { rows: [{ has_rows: false }] };

    const useGridCell = Boolean(gridAvailability.rows[0]?.has_rows);
    if (!useGridCell) {
      const emptyPayload: GridCellProfileResponse = {
        generatedAt: new Date().toISOString(),
        samplingMode: "grid",
        gridSizeKm,
        gridCellId,
        sample: null,
      };
      gridCellProfileCache.set(cacheKey(gridSizeKm, gridCellId), {
        expiresAt: Date.now() + CACHE_TTL_MS,
        payload: emptyPayload,
      });
      return NextResponse.json(emptyPayload, { headers: RESPONSE_HEADERS });
    }

    const { sql, params } = buildOverlayQuery(gridSystem, { gridCellId });
    const res = await query<Record<string, unknown>>(sql, params);
    const sample = res.rows[0] ? mapOverlaySample(res.rows[0]) : null;

    const payload: GridCellProfileResponse = {
      generatedAt: new Date().toISOString(),
      samplingMode: "grid",
      gridSizeKm,
      gridCellId,
      sample,
    };

    gridCellProfileCache.set(cacheKey(gridSizeKm, gridCellId), {
      expiresAt: Date.now() + CACHE_TTL_MS,
      payload,
    });

    return NextResponse.json(payload, { headers: RESPONSE_HEADERS });
  } catch (error) {
    console.error("Grid cell profile API error:", error);
    return NextResponse.json(
      { error: "Failed to fetch grid cell profile" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);