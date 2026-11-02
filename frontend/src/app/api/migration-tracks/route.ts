import { NextResponse } from "next/server";
import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";

/**
 * GET /api/migration-tracks?species=Ciconia+ciconia&windowDays=365
 *
 * Returns a GeoJSON FeatureCollection of pre-decimated per-individual GPS
 * tracks read from `movebank_tracks_tbl`.  Accepts optional query params:
 *   - `species`     (repeatable) — filter by scientific name
 *   - `windowDays`  — only return tracks whose `end_ts` is within N days
 *
 * Decimation is performed once at ingestion time by `refresh_movebank_tracks()`
 * so this route is a single parameterised SELECT — no N+1 scans of the raw
 * hypertable.
 */

export const dynamic = "force-dynamic";

interface TrackBbox {
    west: number;
    south: number;
    east: number;
    north: number;
}

interface TrackPoint {
    timestamp: string;
    coordinates: [number, number];
}

interface TrackRow {
    individual_key: string;
    species: string | null;
    study_id: string;
    raw_point_count: number;
    point_count: number;
    bbox: TrackBbox;
    start_ts: string;
    start_point: [number, number];
    end_ts: string;
    end_point: [number, number];
    track_points: TrackPoint[];
}

const handleGet = async (request: Request) => {
    try {
        const { searchParams } = new URL(request.url);
        const speciesFilter = searchParams.getAll("species").filter(Boolean);
        const windowDaysStr = searchParams.get("windowDays");
        const windowDays = windowDaysStr ? parseInt(windowDaysStr, 10) : null;

        const conditions: string[] = [];
        const params: unknown[] = [];

        if (speciesFilter.length > 0) {
            params.push(speciesFilter);
            conditions.push(`species = ANY($${params.length}::text[])`);
        }
        if (windowDays != null && windowDays > 0) {
            params.push(windowDays);
            conditions.push(
                `end_ts >= (SELECT MAX(end_ts) FROM movebank_tracks_tbl) - make_interval(days => $${params.length}::int)`,
            );
        }

        const whereClause =
            conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

        const sql = `
            SELECT individual_key,
                   species,
                   study_id::text         AS study_id,
                   raw_point_count,
                   point_count,
                   bbox,
                   start_ts,
                   start_point,
                   end_ts,
                   end_point,
                   track_points
            FROM   movebank_tracks_tbl
            ${whereClause}
            ORDER  BY raw_point_count DESC, point_count DESC
        `;

        const res = await query<TrackRow>(sql, params);

        const features = res.rows.map((row) => ({
            type: "Feature" as const,
            geometry: {
                type: "LineString" as const,
                coordinates: row.track_points.map((point) => point.coordinates),
            },
            properties: {
                individual: row.individual_key,
                species: row.species ?? "Unknown",
                study_id: row.study_id,
                raw_point_count: row.raw_point_count,
                point_count: row.point_count,
                bbox: row.bbox,
                start_ts: row.start_ts,
                start_point: row.start_point,
                end_ts: row.end_ts,
                end_point: row.end_point,
            },
        }));

        const payload = { type: "FeatureCollection" as const, features };

        return NextResponse.json(payload, {
            headers: {
                "Cache-Control":
                    "public, max-age=300, s-maxage=900, stale-while-revalidate=900",
            },
        });
    } catch (err) {
        console.error("Migration tracks API error:", err);
        return NextResponse.json(
            { error: "Failed to fetch migration tracks" },
            { status: 500 },
        );
    }
};

export const GET = withMeasuredApiRoute(handleGet);

