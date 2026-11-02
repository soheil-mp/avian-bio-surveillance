-- =========================================================================
-- FUNCTION: refresh_movebank_tracks()
-- =========================================================================
-- Rebuilds ``movebank_tracks_tbl`` from ``movebank_tbl``.  Replaces the
-- N+1 windowed queries previously executed on every ``/api/migration-tracks``
-- request:
--
--   for each individual:
--       SELECT lon, lat, ts FROM (
--           SELECT *, ROW_NUMBER() OVER (...) FROM movebank_tbl WHERE ...
--       ) WHERE rn % $nth = 1
--
-- Output : One row per (study_id, individual_key) with a JSONB
--          ``track_points`` array ordered by timestamp plus raw point count,
--          bbox, and start/end summaries derived from the full source track.
-- Refresh: Truncate-and-reinsert is correct here because the source set
--          (individuals × visible fixes) is fully derivable from
--          ``movebank_tbl``.  Wrapped in a single transaction so readers
--          never see a partial state.
-- Depends: movebank_tbl, movebank_tracks_tbl
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION refresh_movebank_tracks(
    max_points INTEGER DEFAULT 500
)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    -- Replace the table contents atomically.  TRUNCATE keeps row visibility
    -- consistent for any concurrent readers; this is safe because the
    -- /api/migration-tracks consumer is read-only.
    TRUNCATE TABLE movebank_tracks_tbl;

    INSERT INTO movebank_tracks_tbl (
        study_id, individual_key, species,
        raw_point_count, point_count, bbox,
        start_ts, start_point, end_ts, end_point,
        track_points
    )
    WITH visible AS (
        SELECT
            study_id,
            event_id,
            COALESCE(
                NULLIF(individual_local_identifier, ''),
                individual_id::text,
                CONCAT('study-', study_id::text, '-event-', event_id::text)
            )                                                       AS individual_key,
            individual_taxon_canonical_name                         AS species,
            location_long                                           AS lon,
            location_lat                                            AS lat,
            timestamp                                               AS ts
        FROM movebank_tbl
        WHERE location_lat  IS NOT NULL
          AND location_long IS NOT NULL
          AND individual_local_identifier IS NOT NULL
          AND individual_local_identifier <> ''
          -- visible=FALSE marks Movebank-flagged GPS outliers; NULL means the
          -- study does not populate the field (common), so we keep NULL rows.
          AND (visible IS NULL OR visible = TRUE)
    ),
    raw_stats AS (
        SELECT
            study_id, individual_key,
            COUNT(*)::int AS raw_point_count,
            jsonb_build_object(
                'west', MIN(lon),
                'south', MIN(lat),
                'east', MAX(lon),
                'north', MAX(lat)
            ) AS bbox
        FROM visible
        GROUP BY study_id, individual_key
    ),
    ranked AS (
        SELECT
            v.study_id,
            v.event_id,
            v.individual_key,
            v.species,
            v.lon,
            v.lat,
            v.ts,
            ROW_NUMBER() OVER (
                PARTITION BY v.study_id, v.individual_key
                ORDER BY v.ts, v.event_id
            ) AS rn,
            rs.raw_point_count,
            GREATEST(
                1,
                CEIL(
                    GREATEST(rs.raw_point_count - 2, 0)::numeric /
                    GREATEST(max_points - 2, 1)
                )::int
            ) AS nth
        FROM visible v
        JOIN raw_stats rs USING (study_id, individual_key)
    ),
    decimated AS (
        SELECT
            study_id,
            event_id,
            individual_key,
            species,
            lon, lat, ts
        FROM ranked
        WHERE raw_point_count <= 2
           OR rn = 1
           OR rn = raw_point_count
           OR (
                rn > 1
            AND rn < raw_point_count
            AND MOD(rn - 2, nth) = 0
           )
    ),
    latest_species AS (
        SELECT DISTINCT ON (study_id, individual_key)
            study_id,
            individual_key,
            species
        FROM visible
        WHERE species IS NOT NULL
        ORDER BY study_id, individual_key, ts DESC, event_id DESC
    ),
    start_points AS (
        SELECT DISTINCT ON (study_id, individual_key)
            study_id,
            individual_key,
            ts AS start_ts,
            jsonb_build_array(lon, lat) AS start_point
        FROM visible
        ORDER BY study_id, individual_key, ts, event_id
    ),
    end_points AS (
        SELECT DISTINCT ON (study_id, individual_key)
            study_id,
            individual_key,
            ts AS end_ts,
            jsonb_build_array(lon, lat) AS end_point
        FROM visible
        ORDER BY study_id, individual_key, ts DESC, event_id DESC
    )
    SELECT
        d.study_id,
        d.individual_key,
        ls.species                                                    AS species,
        rs.raw_point_count                                            AS raw_point_count,
        COUNT(*)::int                                                AS point_count,
        rs.bbox                                                       AS bbox,
        sp.start_ts                                                   AS start_ts,
        sp.start_point                                                AS start_point,
        ep.end_ts                                                     AS end_ts,
        ep.end_point                                                  AS end_point,
        jsonb_agg(
            jsonb_build_object(
                'timestamp', d.ts,
                'coordinates', jsonb_build_array(d.lon, d.lat)
            )
            ORDER BY d.ts, d.event_id
        )                                                            AS track_points
    FROM decimated d
    JOIN raw_stats rs USING (study_id, individual_key)
    JOIN start_points sp USING (study_id, individual_key)
    JOIN end_points ep USING (study_id, individual_key)
    LEFT JOIN latest_species ls USING (study_id, individual_key)
    GROUP BY d.study_id, d.individual_key
           , ls.species, rs.raw_point_count, rs.bbox
           , sp.start_ts, sp.start_point
           , ep.end_ts, ep.end_point
    HAVING COUNT(*) >= 2;
END;
$$;

COMMENT ON FUNCTION refresh_movebank_tracks(INTEGER) IS
    'Rebuilds movebank_tracks_tbl with per-individual decimated track points '
    'plus raw point count, bbox, and start/end summaries. '
    'Called by Dagster after each '
    'Movebank ingest.';

COMMIT;
