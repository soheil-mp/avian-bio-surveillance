import { NextResponse } from "next/server";
import type { EarthEngineOverlayResponse } from "@/lib/earth-engine-overlays";
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

const earthEngineOverlayCache = new Map<
  SupportedGridSizeKm,
  {
    expiresAt: number;
    payload: EarthEngineOverlayResponse;
  }
>();

const handleGet = async (request: Request) => {
  try {
    const { searchParams } = new URL(request.url);
    const gridSizeKm = resolveGridSizeKm(searchParams.get("grid_size_km"));
    const gridSystem = gridSystemForSize(gridSizeKm);
    const cached = earthEngineOverlayCache.get(gridSizeKm);
    if (cached && cached.expiresAt > Date.now()) {
      return NextResponse.json(cached.payload, { headers: RESPONSE_HEADERS });
    }

    const [geeTableCheck, gridTableCheck] = await Promise.all([
      query(`
        SELECT to_regclass('public.google_earth_engine_tbl') IS NOT NULL AS table_exists
      `),
      query(`
        SELECT to_regclass('public.grid_cells_tbl') IS NOT NULL AS table_exists
      `),
    ]);
    const geeTableExists = Boolean(geeTableCheck.rows[0]?.table_exists);
    const gridTableExists = Boolean(gridTableCheck.rows[0]?.table_exists);

    if (!geeTableExists || !gridTableExists) {
      const emptyPayload = {
        generatedAt: new Date().toISOString(),
        samplingMode: "grid" as const,
        gridSizeKm,
        samples: [],
      };
      earthEngineOverlayCache.set(gridSizeKm, {
        expiresAt: Date.now() + CACHE_TTL_MS,
        payload: emptyPayload,
      });
      return NextResponse.json(
        emptyPayload,
        { headers: RESPONSE_HEADERS },
      );
    }

    const { sql, params } = buildOverlayQuery(gridSystem);
    const res = await query<Record<string, unknown>>(sql, params);
    const samples = res.rows.map((row) => mapOverlaySample(row));

    const payload = {
      generatedAt: new Date().toISOString(),
      samplingMode: "grid" as const,
      gridSizeKm,
      samples,
    };

    earthEngineOverlayCache.set(gridSizeKm, {
      expiresAt: Date.now() + CACHE_TTL_MS,
      payload,
    });

    return NextResponse.json(payload, { headers: RESPONSE_HEADERS });
  } catch (error) {
    console.error("Earth Engine overlays API error:", error);
    return NextResponse.json(
      { error: "Failed to fetch Earth Engine overlay data" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
