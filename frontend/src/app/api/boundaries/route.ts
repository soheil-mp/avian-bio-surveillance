import {
  COUNTRY_WFS_URL,
  MUNICIPALITIES_WFS_URL,
  NETHERLANDS_BOUNDS,
  PROVINCES_WFS_URL,
} from "@/config/map";
import { bboxPolygon } from "@turf/bbox-polygon";
import { mask } from "@turf/mask";
import { simplify } from "@turf/simplify";
import { promises as fs } from "node:fs";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";

import { withMeasuredApiRoute } from "@/lib/performance/server";

export const runtime = "nodejs";

type BoundaryType = "country" | "municipalities" | "provinces";
type BoundaryFeature = GeoJSON.Feature<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;
type BoundaryFeatureCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;
type CountryBoundaryResponse = BoundaryFeatureCollection & {
  countryMask: BoundaryFeature | null;
};
type PolygonBoundaryFeature = GeoJSON.Feature<
  GeoJSON.Polygon | GeoJSON.MultiPolygon,
  GeoJSON.GeoJsonProperties
>;

const BOUNDARY_URLS: Record<BoundaryType, string> = {
  country: COUNTRY_WFS_URL,
  municipalities: MUNICIPALITIES_WFS_URL,
  provinces: PROVINCES_WFS_URL,
};

const CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const cache = new Map<BoundaryType, { body: string; fetchedAt: number }>();

const CACHE_CONTROL_FRESH =
  "public, s-maxage=86400, stale-while-revalidate=604800";
const CACHE_CONTROL_STALE =
  "public, s-maxage=300, stale-while-revalidate=604800";

function isBoundaryType(value: string | null): value is BoundaryType {
  return (
    value === "country" || value === "municipalities" || value === "provinces"
  );
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isBoundaryFeatureCollection(
  value: unknown,
): value is BoundaryFeatureCollection {
  return (
    !!value &&
    typeof value === "object" &&
    Array.isArray((value as { features?: unknown }).features)
  );
}

function isPolygonBoundaryFeature(
  feature: BoundaryFeature | undefined,
): feature is PolygonBoundaryFeature {
  return (
    feature?.geometry?.type === "Polygon" ||
    feature?.geometry?.type === "MultiPolygon"
  );
}

function slimNamedBoundaryFeatures(
  collection: BoundaryFeatureCollection,
): BoundaryFeatureCollection {
  return {
    ...collection,
    features: collection.features.map((feature, index) => {
      const name =
        feature.properties && typeof feature.properties === "object"
          ? (feature.properties as Record<string, unknown>).naam
          : undefined;

      return {
        ...feature,
        id: feature.id ?? index + 1,
        properties: typeof name === "string" ? { naam: name } : {},
      };
    }),
  };
}

async function fetchWithRetry(url: string, retries = 3): Promise<unknown> {
  let lastError: unknown;

  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, {
        cache: "force-cache",
        next: { revalidate: 86_400 },
        headers: {
          Accept: "application/json",
          "User-Agent": "avian-biosurveillance-dashboard/0.1",
        },
      });

      if (!response.ok) {
        throw new Error(`Boundary upstream returned HTTP ${response.status}`);
      }

      return await response.json();
    } catch (error) {
      lastError = error;
      if (attempt < retries) {
        await sleep(250 * attempt);
      }
    }
  }

  throw lastError;
}

/* Heavy geometry transforms (turf simplify + mask) used to run in the
   browser on every map mount. Doing them here lets the 24h server cache
   amortise the cost and keeps the work off the client thread. */
function processBoundary(
  type: BoundaryType,
  raw: unknown,
): BoundaryFeatureCollection | CountryBoundaryResponse | unknown {
  if (!isBoundaryFeatureCollection(raw)) return raw;

  if (type === "provinces" && raw.features.length > 0) {
    const simplified = simplify(raw, {
      tolerance: 0.005,
      highQuality: false,
      mutate: false,
    }) as BoundaryFeatureCollection;
    return slimNamedBoundaryFeatures(simplified);
  }
  if (type === "country" && raw.features.length > 0) {
    // Preserve the full coastline here so the mask does not cut away Zeeland,
    // Wadden islands, or other coastal detail on high-contrast skins.
    const countryGeometry = raw as BoundaryFeatureCollection;
    let countryMask: BoundaryFeature | null = null;
    const countryFeature = countryGeometry.features[0];
    try {
      if (isPolygonBoundaryFeature(countryFeature)) {
        const padded = bboxPolygon([
          NETHERLANDS_BOUNDS[0] - 4,
          NETHERLANDS_BOUNDS[1] - 3,
          NETHERLANDS_BOUNDS[2] + 4,
          NETHERLANDS_BOUNDS[3] + 3,
        ]);
        countryMask = mask(countryFeature, padded) as BoundaryFeature;
      }
    } catch (err) {
      console.warn("Failed to precompute country mask:", err);
    }
    return { ...countryGeometry, countryMask };
  }
  if (type === "municipalities" && raw.features.length > 0) {
    const simplified = simplify(raw, {
      tolerance: 0.002,
      highQuality: false,
      mutate: false,
    }) as BoundaryFeatureCollection;
    return slimNamedBoundaryFeatures(simplified);
  }
  return raw;
}

function jsonResponse(body: string, headers?: HeadersInit) {
  return new Response(body, {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...headers,
    },
  });
}

async function loadBoundaryBodyUncached(type: BoundaryType): Promise<string> {
  const data = await fetchWithRetry(BOUNDARY_URLS[type]);
  const processed = processBoundary(type, data);
  return JSON.stringify(processed);
}

/* Persistent disk cache for processed boundary bodies. The country layer
   (~2.8MB, full coastline preserved) exceeds Next's 2MB fetch-cache limit and
   so cannot use unstable_cache; without persistence every cold process
   (e.g. each `next dev` restart) re-fetched it from the PDOK WFS (~5s). A
   plain JSON file keyed by type, with the same 24h TTL, survives restarts and
   has no size limit. Processing is unchanged, so output is byte-identical. */
const DISK_CACHE_DIR = path.join(process.cwd(), ".cache", "boundaries");

async function readDiskCache(type: BoundaryType): Promise<string | null> {
  try {
    const file = path.join(DISK_CACHE_DIR, `${type}.json`);
    const stat = await fs.stat(file);
    if (Date.now() - stat.mtimeMs > CACHE_TTL_MS) return null;
    return await fs.readFile(file, "utf8");
  } catch {
    return null;
  }
}

async function writeDiskCache(type: BoundaryType, body: string): Promise<void> {
  try {
    await fs.mkdir(DISK_CACHE_DIR, { recursive: true });
    const file = path.join(DISK_CACHE_DIR, `${type}.json`);
    const tmp = `${file}.${process.pid}.tmp`;
    await fs.writeFile(tmp, body, "utf8");
    await fs.rename(tmp, file);
  } catch (error) {
    console.warn(`Failed to persist ${type} boundary disk cache:`, error);
  }
}

/* Coalesce concurrent cold misses per type so a cold map mount across multiple
   tabs hits each upstream WFS layer at most once instead of stampeding it. */
const inFlightBoundary = new Map<BoundaryType, Promise<string>>();

async function loadBoundaryBody(type: BoundaryType): Promise<string> {
  const fromDisk = await readDiskCache(type);
  if (fromDisk !== null) return fromDisk;

  const existing = inFlightBoundary.get(type);
  if (existing) return existing;

  const pending = (async () => {
    const body = await loadBoundaryBodyUncached(type);
    await writeDiskCache(type, body);
    return body;
  })().finally(() => {
    inFlightBoundary.delete(type);
  });

  inFlightBoundary.set(type, pending);
  return pending;
}

const handleGet = async (request: NextRequest) => {
  const typeParam = request.nextUrl.searchParams.get("type");
  if (!isBoundaryType(typeParam)) {
    return NextResponse.json(
      { error: "Invalid boundary type" },
      { status: 400 },
    );
  }

  const cached = cache.get(typeParam);
  const now = Date.now();
  if (cached && now - cached.fetchedAt < CACHE_TTL_MS) {
    return jsonResponse(cached.body, {
      "Cache-Control": CACHE_CONTROL_FRESH,
    });
  }

  try {
    const body = await loadBoundaryBody(typeParam);
    cache.set(typeParam, { body, fetchedAt: now });

    return jsonResponse(body, {
      "Cache-Control": CACHE_CONTROL_FRESH,
    });
  } catch (error) {
    if (cached) {
      console.warn(
        `Using stale cached ${typeParam} boundary data after upstream failure.`,
        error,
      );
      return jsonResponse(cached.body, {
        "Cache-Control": CACHE_CONTROL_STALE,
        "X-Boundary-Cache": "stale",
      });
    }

    console.error(`Failed to fetch ${typeParam} boundary data:`, error);
    return NextResponse.json(
      { error: `Failed to fetch ${typeParam} boundary data` },
      { status: 502 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
