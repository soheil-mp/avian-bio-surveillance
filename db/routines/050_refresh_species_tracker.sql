-- =========================================================================
-- FUNCTION: refresh_species_tracker()
-- =========================================================================
-- Rebuilds species_tracker_tbl from BirdWeather detections plus station-,
-- grid-, and network-aligned environmental sources.
--
-- v2 metrics added:
--   * Pielou evenness, Simpson D, target-species richness/share.
--   * Per-species classifier confidence stats (mean/min/max/p10/p50/p90),
--     high-confidence counts, processed-clip and embedding counts.
--   * Diel and phenology: first/last detection timestamp, peak hour,
--     nocturnal share, dawn/dusk shares.
--   * Background acoustic scene mix (top label + JSONB distribution)
--     derived from birdweather_tbl.background_sound_clf top-tag-per-detection.
--   * Station-grain quality projection from birdweather_stations_tbl.
--   * JSONB sidecars: species_info (taxonomy + cached species_tbl fields),
--     audio_artifacts (cheap clip/embedding counts only),
--     metric_provenance (refresh-time audit trail), extras (extension slot).
--
-- Behaviour:
--   1. Maps stations to canonical grid cells.
--   2. Materializes only the tracker slices currently consumed by the route
--      and modeling path: network day/week/month/year, station year, and
--      grid week. Uses birdweather_daily_cagg_tbl for ecology and
--      birdweather_tbl for the needed week/year audio/diel/scene metrics.
--      When refresh_scope = 'modeling', restricts the rebuild to the grid
--      week slice and pads reads only to the overlapping week boundaries.
--   3. Computes species rows.
--   4. Joins KNMI, GEE, CDS drought, seismic, and station-quality sources.
--
-- Depends: birdweather_tbl, birdweather_daily_cagg_tbl, birdweather_stations_tbl,
--          knmi_daily_cagg_tbl, google_earth_engine_tbl, cds_drought_tbl,
--          seismic_tbl, grid_cells_tbl, species_tbl, fn_point_to_cell().
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION refresh_species_tracker(
    p_window_start date,
    p_window_end date,
    p_refresh_scope text
)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    v_inserted bigint := 0;
    v_refresh_version constant text := '2';
    v_scope_mode text := lower(COALESCE(p_refresh_scope, 'full'));
    v_is_modeling_scope boolean := FALSE;
    v_scope_start date := NULL;
    v_scope_end date := NULL;
BEGIN
    IF v_scope_mode NOT IN ('full', 'modeling') THEN
        RAISE EXCEPTION
            'p_refresh_scope (%) must be one of: full, modeling',
            p_refresh_scope;
    END IF;

    v_is_modeling_scope := v_scope_mode = 'modeling';

    IF (p_window_start IS NULL) <> (p_window_end IS NULL) THEN
        RAISE EXCEPTION
            'p_window_start and p_window_end must both be null or both be non-null';
    END IF;

    IF p_window_start IS NOT NULL AND p_window_end < p_window_start THEN
        RAISE EXCEPTION
            'p_window_end (%) must be >= p_window_start (%)',
            p_window_end,
            p_window_start;
    END IF;

    IF p_window_start IS NOT NULL THEN
        IF v_is_modeling_scope THEN
            v_scope_start := date_trunc('week', p_window_start::timestamp)::date;
            v_scope_end := (date_trunc('week', p_window_end::timestamp)::date + 6);
        ELSE
            v_scope_start := date_trunc('year', p_window_start::timestamp)::date;
            v_scope_end := (
                date_trunc('year', p_window_end::timestamp)
                + INTERVAL '1 year - 1 day'
            )::date;
        END IF;
    END IF;

    -- Stage the rebuild in a temp shadow table so the expensive CTE work does
    -- not hold an exclusive lock on the live tracker table.
    CREATE TEMP TABLE IF NOT EXISTS species_tracker_tbl
    ON COMMIT DROP AS
    SELECT * FROM public.species_tracker_tbl WITH NO DATA;

    TRUNCATE TABLE species_tracker_tbl;

    WITH
    -- ---------------------------------------------------------------------
    -- Reference sets
    -- ---------------------------------------------------------------------
    target_species(species_id) AS (
        -- Source: config/target_species.json (11 tracked species).
        -- Keep aligned with the JSON registry; a future migration may move
        -- this to a target_species_tbl reference table.
        VALUES ('35'), ('61'), ('44'), ('5'), ('6'),
               ('62'), ('81'), ('25'), ('76'), ('407'), ('66')
    ),
    grains(period_grain) AS (
        SELECT g.period_grain
        FROM (
            VALUES ('day'), ('week'), ('month'), ('year')
        ) AS g(period_grain)
        WHERE NOT v_is_modeling_scope
           OR g.period_grain = 'week'
    ),
    raw_metric_grains(period_grain) AS (
        SELECT g.period_grain
        FROM (
            VALUES ('week'), ('year')
        ) AS g(period_grain)
        WHERE NOT v_is_modeling_scope
           OR g.period_grain = 'week'
    ),
    refresh_days(observation_date) AS (
        SELECT gs::date
        FROM generate_series(
            p_window_start::timestamp,
            p_window_end::timestamp,
            INTERVAL '1 day'
        ) AS gs
    ),
    refresh_periods AS (
        SELECT DISTINCT
            g.period_grain,
            CASE g.period_grain
                WHEN 'day' THEN rd.observation_date
                WHEN 'week' THEN date_trunc('week', rd.observation_date::timestamp)::date
                WHEN 'month' THEN date_trunc('month', rd.observation_date::timestamp)::date
                WHEN 'year' THEN date_trunc('year', rd.observation_date::timestamp)::date
            END AS period_start,
            CASE g.period_grain
                WHEN 'day' THEN rd.observation_date
                WHEN 'week' THEN (date_trunc('week', rd.observation_date::timestamp)::date + 6)
                WHEN 'month' THEN (
                    date_trunc('month', rd.observation_date::timestamp)
                    + INTERVAL '1 month - 1 day'
                )::date
                WHEN 'year' THEN (
                    date_trunc('year', rd.observation_date::timestamp)
                    + INTERVAL '1 year - 1 day'
                )::date
            END AS period_end
        FROM refresh_days rd
        CROSS JOIN grains g
    ),
    station_cells AS (
        SELECT
            s.station_id,
            s.lat,
            s.lon,
            CASE
                WHEN s.lat IS NOT NULL AND s.lon IS NOT NULL
                THEN fn_point_to_cell(s.lat, s.lon, 'nl_geodesic_5km')
                ELSE NULL
            END AS grid_cell_id
        FROM birdweather_stations_tbl s
    ),
    -- ---------------------------------------------------------------------
    -- Daily station rollup (cheap path; drives ecological metrics).
    -- ---------------------------------------------------------------------
    daily_station AS (
        SELECT
            c.bucket::date AS observation_date,
            c.station_id,
            sc.grid_cell_id,
            c.species_id,
            SUM(c.detection_count)::bigint AS detection_count
        FROM birdweather_daily_cagg_tbl c
        LEFT JOIN station_cells sc ON sc.station_id = c.station_id
        WHERE c.species_id IS NOT NULL
            AND (
                v_scope_start IS NULL
                OR c.bucket::date BETWEEN v_scope_start AND v_scope_end
            )
        GROUP BY c.bucket::date, c.station_id, sc.grid_cell_id, c.species_id
    ),
    periodized AS (
        SELECT
            g.period_grain,
            CASE g.period_grain
                WHEN 'day' THEN d.observation_date
                WHEN 'week' THEN date_trunc('week', d.observation_date::timestamp)::date
                WHEN 'month' THEN date_trunc('month', d.observation_date::timestamp)::date
                WHEN 'season' THEN
                    CASE
                        WHEN EXTRACT(MONTH FROM d.observation_date) = 12
                            THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int, 12, 1)
                        WHEN EXTRACT(MONTH FROM d.observation_date) IN (1, 2)
                            THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int - 1, 12, 1)
                        WHEN EXTRACT(MONTH FROM d.observation_date) IN (3, 4, 5)
                            THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int, 3, 1)
                        WHEN EXTRACT(MONTH FROM d.observation_date) IN (6, 7, 8)
                            THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int, 6, 1)
                        ELSE make_date(EXTRACT(YEAR FROM d.observation_date)::int, 9, 1)
                    END
                WHEN 'year' THEN date_trunc('year', d.observation_date::timestamp)::date
            END AS period_start,
            CASE g.period_grain
                WHEN 'day' THEN d.observation_date
                WHEN 'week' THEN (date_trunc('week', d.observation_date::timestamp)::date + 6)
                WHEN 'month' THEN (date_trunc('month', d.observation_date::timestamp) + INTERVAL '1 month - 1 day')::date
                WHEN 'season' THEN
                    (
                        CASE
                            WHEN EXTRACT(MONTH FROM d.observation_date) = 12
                                THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int + 1, 3, 1)
                            WHEN EXTRACT(MONTH FROM d.observation_date) IN (1, 2)
                                THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int, 3, 1)
                            WHEN EXTRACT(MONTH FROM d.observation_date) IN (3, 4, 5)
                                THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int, 6, 1)
                            WHEN EXTRACT(MONTH FROM d.observation_date) IN (6, 7, 8)
                                THEN make_date(EXTRACT(YEAR FROM d.observation_date)::int, 9, 1)
                            ELSE make_date(EXTRACT(YEAR FROM d.observation_date)::int, 12, 1)
                        END - 1
                    )
                WHEN 'year' THEN (date_trunc('year', d.observation_date::timestamp) + INTERVAL '1 year - 1 day')::date
            END AS period_end,
            d.observation_date,
            d.station_id,
            d.grid_cell_id,
            d.species_id,
            d.detection_count
        FROM daily_station d
        CROSS JOIN grains g
    ),
    scoped_periodized AS (
        SELECT p.*
        FROM periodized p
        WHERE p_window_start IS NULL
           OR EXISTS (
                SELECT 1
                FROM refresh_periods rp
                WHERE rp.period_grain = p.period_grain
                  AND rp.period_start = p.period_start
                  AND rp.period_end = p.period_end
           )
    ),
    location_detections AS (
        SELECT
            p.period_grain,
            p.period_start,
            p.period_end,
            'station'::text AS location_type,
            p.station_id AS location_id,
            p.station_id,
            p.grid_cell_id,
            p.station_id AS member_station_id,
            p.observation_date,
            p.species_id,
            p.detection_count
        FROM scoped_periodized p
                WHERE NOT v_is_modeling_scope
                    AND p.station_id IS NOT NULL
                    -- Station rows are emitted at every grain so the Top-stations
                    -- duration selector (24h / week / month / season / year / total)
                    -- can be served straight from this aggregate. 'day' is the
                    -- heaviest contributor (stations x days x species); drop it from
                    -- this list to reclaim rows/refresh-time at the cost of the 24h window.
                    AND p.period_grain IN ('day', 'week', 'month', 'year')

        UNION ALL

        SELECT
            p.period_grain,
            p.period_start,
            p.period_end,
            'grid'::text AS location_type,
            p.grid_cell_id AS location_id,
            NULL::text AS station_id,
            p.grid_cell_id,
            p.station_id AS member_station_id,
            p.observation_date,
            p.species_id,
            p.detection_count
        FROM scoped_periodized p
                WHERE p.grid_cell_id IS NOT NULL
                    AND p.period_grain = 'week'

        UNION ALL

        SELECT
            p.period_grain,
            p.period_start,
            p.period_end,
            'network'::text AS location_type,
            'nl'::text AS location_id,
            NULL::text AS station_id,
            NULL::text AS grid_cell_id,
            p.station_id AS member_station_id,
            p.observation_date,
            p.species_id,
            p.detection_count
        FROM scoped_periodized p
                WHERE NOT v_is_modeling_scope
                    AND p.station_id IS NOT NULL
                    AND p.period_grain IN ('day', 'week', 'month', 'year')
    ),
    location_totals AS (
        SELECT
            ld.period_grain,
            ld.period_start,
            ld.period_end,
            GREATEST(1, (LEAST(ld.period_end, CURRENT_DATE) - ld.period_start) + 1)::int AS period_day_count,
            ld.location_type,
            ld.location_id,
            MAX(ld.station_id) AS station_id,
            MAX(ld.grid_cell_id) AS grid_cell_id,
            SUM(ld.detection_count)::bigint AS total_detections,
            COUNT(DISTINCT ld.observation_date)::int AS active_days,
            COUNT(DISTINCT ld.member_station_id)::int AS active_station_count,
            COUNT(DISTINCT ld.species_id)::int AS species_richness,
            COUNT(DISTINCT ld.species_id) FILTER (
                WHERE ld.species_id IN (SELECT species_id FROM target_species)
            )::int AS target_species_richness,
            SUM(ld.detection_count) FILTER (
                WHERE ld.species_id IN (SELECT species_id FROM target_species)
            )::bigint AS target_species_detections
        FROM location_detections ld
        GROUP BY
            ld.period_grain, ld.period_start, ld.period_end,
            ld.location_type, ld.location_id
    ),
    species_metrics AS (
        SELECT
            ld.period_grain,
            ld.period_start,
            ld.period_end,
            ld.location_type,
            ld.location_id,
            MAX(ld.station_id) AS station_id,
            MAX(ld.grid_cell_id) AS grid_cell_id,
            ld.species_id,
            SUM(ld.detection_count)::bigint AS total_detections,
            COUNT(DISTINCT ld.observation_date)::int AS active_days,
            COUNT(DISTINCT ld.member_station_id)::int AS species_station_count
        FROM location_detections ld
        GROUP BY
            ld.period_grain, ld.period_start, ld.period_end,
            ld.location_type, ld.location_id, ld.species_id
    ),
    community_shares AS (
        SELECT
            sm.period_grain,
            sm.period_start,
            sm.period_end,
            sm.location_type,
            sm.location_id,
            sm.species_id,
            sm.total_detections,
            lt.total_detections AS location_total,
            CASE
                WHEN lt.total_detections > 0
                THEN sm.total_detections::numeric / lt.total_detections
                ELSE 0
            END AS p_share
        FROM species_metrics sm
        JOIN location_totals lt
          ON lt.period_grain = sm.period_grain
         AND lt.period_start = sm.period_start
         AND lt.period_end = sm.period_end
         AND lt.location_type = sm.location_type
         AND lt.location_id = sm.location_id
    ),
    community_metrics AS (
        SELECT
            cs.period_grain,
            cs.period_start,
            cs.period_end,
            cs.location_type,
            cs.location_id,
            ROUND(COALESCE(-SUM(cs.p_share * LN(GREATEST(cs.p_share, 1e-10))), 0)::numeric, 4)::double precision AS shannon_h,
            ROUND(
                COALESCE(MAX(cs.total_detections)::numeric * 100.0 / NULLIF(MAX(cs.location_total), 0), 0),
                2
            )::double precision AS dominance_index,
            ROUND((1.0 - COALESCE(SUM(cs.p_share * cs.p_share), 0))::numeric, 4)::double precision AS simpson_d
        FROM community_shares cs
        GROUP BY
            cs.period_grain, cs.period_start, cs.period_end,
            cs.location_type, cs.location_id
    ),
    -- ---------------------------------------------------------------------
    -- Per-detection raw view (drives week/year audio / diel / scene metrics).
    -- ---------------------------------------------------------------------
    raw_detections AS (
        SELECT
            d.detection_id,
            d.species_id,
            d.station_id,
            sc.grid_cell_id,
            d.detected_at,
            d.observation_date,
            d.confidence,
            d.eclipse,
            EXTRACT(HOUR FROM d.detected_at)::int AS hour_utc,
            d.has_processed_clip,
            (d.birdnet_embedding IS NOT NULL) AS has_embedding,
            d.background_sound_clf
        FROM birdweather_tbl d
        LEFT JOIN station_cells sc ON sc.station_id = d.station_id
        WHERE d.species_id IS NOT NULL
          AND (
                v_scope_start IS NULL
                OR d.observation_date BETWEEN v_scope_start AND v_scope_end
          )
    ),
    periodized_raw AS (
        SELECT
            g.period_grain,
            CASE g.period_grain
                WHEN 'day' THEN rd.observation_date
                WHEN 'week' THEN date_trunc('week', rd.observation_date::timestamp)::date
                WHEN 'month' THEN date_trunc('month', rd.observation_date::timestamp)::date
                WHEN 'season' THEN
                    CASE
                        WHEN EXTRACT(MONTH FROM rd.observation_date) = 12
                            THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 12, 1)
                        WHEN EXTRACT(MONTH FROM rd.observation_date) IN (1, 2)
                            THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int - 1, 12, 1)
                        WHEN EXTRACT(MONTH FROM rd.observation_date) IN (3, 4, 5)
                            THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 3, 1)
                        WHEN EXTRACT(MONTH FROM rd.observation_date) IN (6, 7, 8)
                            THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 6, 1)
                        ELSE make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 9, 1)
                    END
                WHEN 'year' THEN date_trunc('year', rd.observation_date::timestamp)::date
            END AS period_start,
            CASE g.period_grain
                WHEN 'day' THEN rd.observation_date
                WHEN 'week' THEN (date_trunc('week', rd.observation_date::timestamp)::date + 6)
                WHEN 'month' THEN (
                    date_trunc('month', rd.observation_date::timestamp)
                    + INTERVAL '1 month - 1 day'
                )::date
                WHEN 'season' THEN
                    (
                        CASE
                            WHEN EXTRACT(MONTH FROM rd.observation_date) = 12
                                THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int + 1, 3, 1)
                            WHEN EXTRACT(MONTH FROM rd.observation_date) IN (1, 2)
                                THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 3, 1)
                            WHEN EXTRACT(MONTH FROM rd.observation_date) IN (3, 4, 5)
                                THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 6, 1)
                            WHEN EXTRACT(MONTH FROM rd.observation_date) IN (6, 7, 8)
                                THEN make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 9, 1)
                            ELSE make_date(EXTRACT(YEAR FROM rd.observation_date)::int, 12, 1)
                        END - 1
                    )
                WHEN 'year' THEN (
                    date_trunc('year', rd.observation_date::timestamp)
                    + INTERVAL '1 year - 1 day'
                )::date
            END AS period_end,
            rd.detection_id,
            rd.species_id,
            rd.station_id,
            rd.grid_cell_id,
            rd.detected_at,
            rd.confidence,
            rd.eclipse,
            rd.hour_utc,
            rd.has_processed_clip,
            rd.has_embedding,
            rd.background_sound_clf
        FROM raw_detections rd
        CROSS JOIN raw_metric_grains g
    ),
    scoped_periodized_raw AS (
        SELECT pr.*
        FROM periodized_raw pr
        WHERE p_window_start IS NULL
           OR EXISTS (
                SELECT 1
                FROM refresh_periods rp
                WHERE rp.period_grain = pr.period_grain
                  AND rp.period_start = pr.period_start
                  AND rp.period_end = pr.period_end
           )
    ),
    location_raw AS (
        SELECT
            pr.period_grain,
            pr.period_start,
            'station'::text AS location_type,
            pr.station_id AS location_id,
            pr.species_id,
            pr.detection_id,
            pr.detected_at,
            pr.confidence,
            pr.eclipse,
            pr.hour_utc,
            pr.has_processed_clip,
            pr.has_embedding,
            pr.background_sound_clf
        FROM scoped_periodized_raw pr
                WHERE NOT v_is_modeling_scope
                    AND pr.station_id IS NOT NULL
                    AND pr.period_grain = 'year'

        UNION ALL

        SELECT
            pr.period_grain,
            pr.period_start,
            'grid'::text AS location_type,
            pr.grid_cell_id AS location_id,
            pr.species_id,
            pr.detection_id,
            pr.detected_at,
            pr.confidence,
            pr.eclipse,
            pr.hour_utc,
            pr.has_processed_clip,
            pr.has_embedding,
            pr.background_sound_clf
        FROM scoped_periodized_raw pr
                WHERE pr.grid_cell_id IS NOT NULL
                    AND pr.period_grain = 'week'

        UNION ALL

        SELECT
            pr.period_grain,
            pr.period_start,
            'network'::text AS location_type,
            'nl'::text AS location_id,
            pr.species_id,
            pr.detection_id,
            pr.detected_at,
            pr.confidence,
            pr.eclipse,
            pr.hour_utc,
            pr.has_processed_clip,
            pr.has_embedding,
            pr.background_sound_clf
        FROM scoped_periodized_raw pr
                WHERE NOT v_is_modeling_scope
                    AND pr.station_id IS NOT NULL
                    AND pr.period_grain = 'year'
    ),
    -- Top background tag per detection (NULL if no tags / no clf).
    detection_top_bg AS (
        SELECT
            lr.period_grain,
            lr.period_start,
            lr.location_type,
            lr.location_id,
            lr.species_id,
            lr.detection_id,
            top_tag.label
        FROM location_raw lr
        CROSS JOIN LATERAL (
            SELECT t.value->>'label' AS label
            FROM jsonb_array_elements(
                COALESCE(lr.background_sound_clf->'tags', '[]'::jsonb)
            ) AS t
            WHERE jsonb_typeof(COALESCE(lr.background_sound_clf->'tags', '[]'::jsonb)) = 'array'
            ORDER BY (t.value->>'score')::numeric DESC NULLS LAST
            LIMIT 1
        ) AS top_tag
        WHERE top_tag.label IS NOT NULL
    ),
    bg_label_counts AS (
        SELECT
            dtb.period_grain,
            dtb.period_start,
            dtb.location_type,
            dtb.location_id,
            dtb.species_id,
            dtb.label,
            COUNT(*)::bigint AS label_count
        FROM detection_top_bg dtb
        GROUP BY
            dtb.period_grain,
            dtb.period_start,
            dtb.location_type,
            dtb.location_id,
            dtb.species_id,
            dtb.label
    ),
    bg_totals AS (
        SELECT
            blc.period_grain,
            blc.period_start,
            blc.location_type,
            blc.location_id,
            blc.species_id,
            SUM(blc.label_count)::bigint AS total_label_count
        FROM bg_label_counts blc
        GROUP BY
            blc.period_grain,
            blc.period_start,
            blc.location_type,
            blc.location_id,
            blc.species_id
    ),
    -- Background label distribution per (location, period, species), expressed
    -- as JSONB {label: share_pct}.
    bg_mix AS (
        SELECT
            blc.period_grain,
            blc.period_start,
            blc.location_type,
            blc.location_id,
            blc.species_id,
            jsonb_object_agg(
                blc.label,
                ROUND((blc.label_count::numeric * 100.0 / NULLIF(bt.total_label_count, 0)), 2)
            ) AS background_label_mix
        FROM bg_label_counts blc
        JOIN bg_totals bt
          ON bt.period_grain = blc.period_grain
         AND bt.period_start = blc.period_start
         AND bt.location_type = blc.location_type
         AND bt.location_id = blc.location_id
         AND bt.species_id = blc.species_id
        GROUP BY
            blc.period_grain,
            blc.period_start,
            blc.location_type,
            blc.location_id,
            blc.species_id
    ),
    bg_top AS (
        SELECT DISTINCT ON (
            blc.period_grain, blc.period_start,
            blc.location_type, blc.location_id, blc.species_id
        )
            blc.period_grain,
            blc.period_start,
            blc.location_type,
            blc.location_id,
            blc.species_id,
            blc.label AS background_top_label,
            ROUND(
                (blc.label_count::numeric * 100.0 / NULLIF(bt.total_label_count, 0)),
                2
            )::double precision AS background_top_share_pct
        FROM bg_label_counts blc
        JOIN bg_totals bt
          ON bt.period_grain = blc.period_grain
         AND bt.period_start = blc.period_start
         AND bt.location_type = blc.location_type
         AND bt.location_id = blc.location_id
         AND bt.species_id = blc.species_id
        ORDER BY
            blc.period_grain,
            blc.period_start,
            blc.location_type,
            blc.location_id,
            blc.species_id,
            blc.label_count DESC,
            blc.label
    ),
    bg_combined AS (
        SELECT
            COALESCE(m.period_grain, t.period_grain) AS period_grain,
            COALESCE(m.period_start, t.period_start) AS period_start,
            COALESCE(m.location_type, t.location_type) AS location_type,
            COALESCE(m.location_id, t.location_id) AS location_id,
            COALESCE(m.species_id, t.species_id) AS species_id,
            t.background_top_label,
            t.background_top_share_pct,
            m.background_label_mix
        FROM bg_mix m
        FULL OUTER JOIN bg_top t
          ON t.period_grain = m.period_grain
         AND t.period_start = m.period_start
         AND t.location_type = m.location_type
         AND t.location_id = m.location_id
         AND t.species_id = m.species_id
    ),
    -- Hour-of-day distribution for diel peak.
    hour_counts AS (
        SELECT
            lr.period_grain,
            lr.period_start,
            lr.location_type,
            lr.location_id,
            lr.species_id,
            lr.hour_utc,
            COUNT(*)::bigint AS hour_detections
        FROM location_raw lr
        GROUP BY lr.period_grain, lr.period_start, lr.location_type,
                 lr.location_id, lr.species_id, lr.hour_utc
    ),
    peak_hour AS (
        SELECT DISTINCT ON (
            hc.period_grain, hc.period_start,
            hc.location_type, hc.location_id, hc.species_id
        )
            hc.period_grain,
            hc.period_start,
            hc.location_type,
            hc.location_id,
            hc.species_id,
            hc.hour_utc AS peak_hour_utc,
            ROUND(
                (hc.hour_detections::numeric * 100.0
                 / NULLIF(SUM(hc.hour_detections) OVER (
                     PARTITION BY hc.period_grain, hc.period_start,
                                  hc.location_type, hc.location_id, hc.species_id
                 ), 0)),
                2
            )::double precision AS peak_hour_share_pct
        FROM hour_counts hc
        ORDER BY hc.period_grain, hc.period_start,
                 hc.location_type, hc.location_id, hc.species_id,
                 hc.hour_detections DESC, hc.hour_utc ASC
    ),
    hour_mix AS (
        SELECT
            hc.period_grain,
            hc.period_start,
            hc.location_type,
            hc.location_id,
            hc.species_id,
            jsonb_object_agg(
                hc.hour_utc::text,
                hc.hour_detections
                ORDER BY hc.hour_utc
            ) AS hourly_detection_counts
        FROM hour_counts hc
        GROUP BY hc.period_grain, hc.period_start, hc.location_type,
                 hc.location_id, hc.species_id
    ),
    -- Per-species audio / diel / phenology aggregates.
    species_audio_metrics AS (
        SELECT
            lr.period_grain,
            lr.period_start,
            lr.location_type,
            lr.location_id,
            lr.species_id,
            ROUND(AVG(lr.confidence)::numeric, 4)::double precision AS confidence_mean,
            ROUND(MIN(lr.confidence)::numeric, 4)::double precision AS confidence_min,
            ROUND(MAX(lr.confidence)::numeric, 4)::double precision AS confidence_max,
            ROUND(percentile_cont(0.10) WITHIN GROUP (ORDER BY lr.confidence)::numeric, 4)::double precision AS confidence_p10,
            ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY lr.confidence)::numeric, 4)::double precision AS confidence_p50,
            ROUND(percentile_cont(0.90) WITHIN GROUP (ORDER BY lr.confidence)::numeric, 4)::double precision AS confidence_p90,
            COUNT(*) FILTER (WHERE lr.confidence >= 0.8)::bigint AS high_confidence_detections,
            ROUND((COUNT(*) FILTER (WHERE lr.confidence >= 0.8)::numeric * 100.0
                   / NULLIF(COUNT(*), 0)), 2)::double precision AS high_confidence_pct,
            COUNT(*) FILTER (WHERE lr.has_processed_clip)::bigint AS processed_clip_count,
            ROUND((COUNT(*) FILTER (WHERE lr.has_processed_clip)::numeric * 100.0
                   / NULLIF(COUNT(*), 0)), 2)::double precision AS processed_clip_pct,
            COUNT(*) FILTER (WHERE lr.has_embedding)::bigint AS embedding_count,
            MIN(lr.detected_at) AS first_detection_at,
            MAX(lr.detected_at) AS last_detection_at,
            ROUND((COUNT(*) FILTER (WHERE lr.eclipse)::numeric * 100.0
                   / NULLIF(COUNT(*), 0)), 2)::double precision AS nocturnal_share_pct,
            ROUND((COUNT(*) FILTER (WHERE lr.hour_utc BETWEEN 4 AND 7)::numeric * 100.0
                   / NULLIF(COUNT(*), 0)), 2)::double precision AS dawn_share_pct,
            ROUND((COUNT(*) FILTER (WHERE lr.hour_utc BETWEEN 17 AND 20)::numeric * 100.0
                   / NULLIF(COUNT(*), 0)), 2)::double precision AS dusk_share_pct
        FROM location_raw lr
        GROUP BY lr.period_grain, lr.period_start,
                 lr.location_type, lr.location_id, lr.species_id
    ),
    -- ---------------------------------------------------------------------
    -- Period-level support tables for environmental joins.
    -- ---------------------------------------------------------------------
    period_station_membership AS (
        SELECT DISTINCT
            ld.period_grain, ld.period_start, ld.period_end,
            ld.location_type, ld.location_id, ld.member_station_id
        FROM location_detections ld
        WHERE ld.member_station_id IS NOT NULL
    ),
    period_locations AS (
        SELECT
            lt.period_grain,
            lt.period_start,
            lt.period_end,
            lt.period_day_count,
            lt.location_type,
            lt.location_id,
            lt.station_id,
            lt.grid_cell_id,
            'nl_geodesic_5km'::text AS grid_system,
            COALESCE(st.lat, gc.lat) AS lat,
            COALESCE(st.lon, gc.lon) AS lon
        FROM location_totals lt
        LEFT JOIN birdweather_stations_tbl st
            ON lt.location_type = 'station'
           AND st.station_id = lt.station_id
        LEFT JOIN grid_cells_tbl gc
            ON gc.grid_cell_id = lt.grid_cell_id
    ),
    -- Distinct KNMI weather-station coordinates (from raw knmi_tbl; the daily
    -- cagg carries no coordinates). KNMI uses WMO station ids
    -- ('0-20000-0-06###'), which are unrelated to BirdWeather station ids, so
    -- the previous join on member_station_id matched zero rows and left every
    -- knmi_* column NULL. Bridge by nearest weather station instead.
    knmi_stations AS (
        SELECT
            station_id,
            AVG(station_lat) AS station_lat,
            AVG(station_lon) AS station_lon
        FROM knmi_tbl
        WHERE station_lat IS NOT NULL
          AND station_lon IS NOT NULL
        GROUP BY station_id
    ),
    -- Nearest KNMI station per DISTINCT location (time-invariant: a location's
    -- nearest weather station does not change week to week, so resolve it once
    -- over the small distinct-location set rather than per location-week. This
    -- keeps the haversine cross-join at (distinct locations x ~72 stations).
    distinct_locations AS (
        SELECT DISTINCT
            pl.location_type, pl.location_id, pl.lat, pl.lon
        FROM period_locations pl
        WHERE pl.lat IS NOT NULL
          AND pl.lon IS NOT NULL
    ),
    location_nearest_knmi AS (
        SELECT DISTINCT ON (dl.location_type, dl.location_id)
            dl.location_type,
            dl.location_id,
            ks.station_id AS knmi_station_id
        FROM distinct_locations dl
        JOIN knmi_stations ks ON TRUE
        ORDER BY
            dl.location_type, dl.location_id,
            (
                6371.0 * 2.0 * ASIN(
                    SQRT(
                        POWER(SIN(RADIANS((ks.station_lat - dl.lat) / 2.0)), 2) +
                        COS(RADIANS(dl.lat)) * COS(RADIANS(ks.station_lat)) *
                        POWER(SIN(RADIANS((ks.station_lon - dl.lon) / 2.0)), 2)
                    )
                )
            ) ASC
    ),
    location_knmi_station AS (
        SELECT
            pl.period_grain, pl.period_start, pl.period_end,
            pl.location_type, pl.location_id,
            lnk.knmi_station_id
        FROM period_locations pl
        JOIN location_nearest_knmi lnk
            ON lnk.location_type = pl.location_type
           AND lnk.location_id = pl.location_id
    ),
    knmi_metrics AS (
        SELECT
            lks.period_grain, lks.period_start, lks.period_end,
            lks.location_type, lks.location_id,
            ROUND(AVG(k.daily_mean_temp_c)::numeric, 2)::double precision AS knmi_temp_mean_c,
            ROUND(MIN(k.daily_min_temp_c)::numeric, 2)::double precision AS knmi_temp_min_c,
            ROUND(MAX(k.daily_max_temp_c)::numeric, 2)::double precision AS knmi_temp_max_c,
            ROUND(SUM(COALESCE(k.daily_precip_mm, 0))::numeric, 2)::double precision AS knmi_precip_mm,
            ROUND(AVG(k.daily_mean_rh_pct)::numeric, 2)::double precision AS knmi_humidity_mean_pct,
            ROUND(MAX(k.daily_max_wind_ms)::numeric, 2)::double precision AS knmi_wind_max_ms,
            ROUND(AVG(NULLIF(k.daily_mean_solar_wm2, 0))::numeric, 2)::double precision AS knmi_solar_mean_wm2
        FROM location_knmi_station lks
        JOIN knmi_daily_cagg_tbl k
            ON k.station_id = lks.knmi_station_id
           AND k.bucket::date >= lks.period_start
           AND k.bucket::date <= LEAST(lks.period_end, CURRENT_DATE)
        GROUP BY lks.period_grain, lks.period_start, lks.period_end,
                 lks.location_type, lks.location_id
    ),
    gee_metrics AS (
        SELECT
            pl.period_grain, pl.period_start, pl.period_end,
            pl.location_type, pl.location_id,
            ROUND(AVG(g.ndvi)::numeric, 4)::double precision AS gee_ndvi_mean,
            ROUND(AVG(g.temperature_2m_c)::numeric, 2)::double precision AS gee_temp2m_mean_c,
            ROUND(AVG(g.soil_moisture)::numeric, 4)::double precision AS gee_soil_moisture_mean,
            ROUND(AVG(g.total_precipitation_mm)::numeric, 2)::double precision AS gee_precip_mean_mm,
            ROUND(AVG(g.cloud_cover_pct)::numeric, 2)::double precision AS gee_cloud_cover_mean_pct
        FROM period_locations pl
        JOIN google_earth_engine_tbl g
            ON g.grid_cell_id = pl.grid_cell_id
           AND g.observation_date >= pl.period_start
           AND g.observation_date <= LEAST(pl.period_end, CURRENT_DATE)
        GROUP BY pl.period_grain, pl.period_start, pl.period_end,
                 pl.location_type, pl.location_id
    ),
    cds_metrics AS (
        -- CDS drought is published on a 0.25-degree lat/lon grid
        -- (grid_system = 'cds_latlon_0p25deg', ids like
        -- 'cds_0.25deg_lat_..._lon_...'), which does NOT share identifiers with
        -- the canonical 5 km cell ids ('nl-5km-r###-c###') used by
        -- period_locations. Joining on grid_cell_id therefore matched zero rows
        -- and left every cds_* column NULL. Bridge spatially instead: floor the
        -- location centroid (lat/lon) to its containing 0.25-degree cell anchor
        -- and match the CDS cell whose (lat, lon) anchor equals it. The CDS
        -- lat/lon are the cell's grid-aligned lower-left anchor, so a tolerance
        -- well under one cell (0.05 deg) is an exact-anchor match without
        -- relying on floating-point equality.
        SELECT
            pl.period_grain, pl.period_start, pl.period_end,
            pl.location_type, pl.location_id,
            ROUND(AVG(d.spi_1m)::numeric, 4)::double precision AS cds_spi_1m_mean,
            ROUND(AVG(d.spi_3m)::numeric, 4)::double precision AS cds_spi_3m_mean,
            ROUND(AVG(d.spei_3m)::numeric, 4)::double precision AS cds_spei_3m_mean,
            ROUND(AVG(d.spei_12m)::numeric, 4)::double precision AS cds_spei_12m_mean
        FROM period_locations pl
        JOIN cds_drought_tbl d
            ON d.ensemble_member = 0
           AND pl.lat IS NOT NULL
           AND pl.lon IS NOT NULL
           AND ABS(d.lat - (FLOOR(pl.lat / 0.25) * 0.25)) < 0.05
           AND ABS(d.lon - (FLOOR(pl.lon / 0.25) * 0.25)) < 0.05
           AND d.observation_month >= date_trunc('month', pl.period_start)::date
           AND d.observation_month <= date_trunc('month', LEAST(pl.period_end, CURRENT_DATE))::date
        GROUP BY pl.period_grain, pl.period_start, pl.period_end,
                 pl.location_type, pl.location_id
    ),
    seismic_metrics AS (
        SELECT
            pl.period_grain, pl.period_start, pl.period_end,
            pl.location_type, pl.location_id,
            COUNT(*) FILTER (WHERE s.dist_km <= 50)::int AS seismic_event_count,
            ROUND(MAX(s.magnitude) FILTER (WHERE s.dist_km <= 50)::numeric, 2)::double precision AS seismic_max_magnitude,
            ROUND(AVG(s.magnitude) FILTER (WHERE s.dist_km <= 50)::numeric, 2)::double precision AS seismic_mean_magnitude,
            ROUND(MIN(s.dist_km)::numeric, 2)::double precision AS seismic_nearest_event_km
        FROM period_locations pl
        LEFT JOIN LATERAL (
            SELECT
                e.magnitude,
                6371.0 * 2.0 * ASIN(
                    SQRT(
                        POWER(SIN(RADIANS((e.latitude - pl.lat) / 2.0)), 2) +
                        COS(RADIANS(pl.lat)) * COS(RADIANS(e.latitude)) *
                        POWER(SIN(RADIANS((e.longitude - pl.lon) / 2.0)), 2)
                    )
                ) AS dist_km
            FROM seismic_tbl e
            WHERE pl.lat IS NOT NULL
              AND pl.lon IS NOT NULL
              AND e.event_time::date >= pl.period_start
              AND e.event_time::date <= LEAST(pl.period_end, CURRENT_DATE)
        ) s ON TRUE
        GROUP BY pl.period_grain, pl.period_start, pl.period_end,
                 pl.location_type, pl.location_id
    ),
    -- Station-grain quality projection (NULL on grid rows).
    station_quality AS (
        SELECT
            lt.period_grain, lt.period_start, lt.period_end,
            lt.location_type, lt.location_id,
            st.lifetime_availability_pct AS station_lifetime_availability_pct,
            st.availability_90d_pct AS station_availability_90d_pct,
            st.days_since_last AS station_days_since_last,
            CASE
                WHEN st.availability_90d_pct >= 90 THEN 'A'
                WHEN st.availability_90d_pct >= 60 THEN 'B'
                WHEN st.availability_90d_pct IS NOT NULL THEN 'C'
                ELSE NULL
            END AS station_data_quality_grade
        FROM location_totals lt
        JOIN birdweather_stations_tbl st
            ON st.station_id = lt.station_id
        WHERE lt.location_type = 'station'
    ),
    enriched_locations AS (
        SELECT
            lt.period_grain, lt.period_start, lt.period_end, lt.period_day_count,
            lt.location_type, lt.location_id, lt.station_id, lt.grid_cell_id,
            pl.grid_system,
            lt.total_detections, lt.active_days, lt.active_station_count,
            lt.species_richness, lt.target_species_richness,
            ROUND((lt.target_species_detections::numeric * 100.0
                   / NULLIF(lt.total_detections, 0)), 2)::double precision AS target_species_share_pct,
            cm.shannon_h, cm.dominance_index, cm.simpson_d,
            CASE
                WHEN lt.species_richness > 1
                THEN ROUND((cm.shannon_h / LN(lt.species_richness))::numeric, 4)::double precision
                ELSE NULL
            END AS pielou_evenness,
            km.knmi_temp_mean_c, km.knmi_temp_min_c, km.knmi_temp_max_c,
            km.knmi_precip_mm, km.knmi_humidity_mean_pct,
            km.knmi_wind_max_ms, km.knmi_solar_mean_wm2,
            gm.gee_ndvi_mean, gm.gee_temp2m_mean_c, gm.gee_soil_moisture_mean,
            gm.gee_precip_mean_mm, gm.gee_cloud_cover_mean_pct,
            dm.cds_spi_1m_mean, dm.cds_spi_3m_mean,
            dm.cds_spei_3m_mean, dm.cds_spei_12m_mean,
            smx.seismic_event_count, smx.seismic_max_magnitude,
            smx.seismic_mean_magnitude, smx.seismic_nearest_event_km,
            sq.station_lifetime_availability_pct,
            sq.station_availability_90d_pct,
            sq.station_days_since_last,
            sq.station_data_quality_grade
        FROM location_totals lt
        LEFT JOIN period_locations pl USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN community_metrics cm USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN knmi_metrics km USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN gee_metrics gm USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN cds_metrics dm USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN seismic_metrics smx USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN station_quality sq USING (period_grain, period_start, period_end, location_type, location_id)
    ),
    -- Cached per-species reference projection (one row per species_id).
    species_info_proj AS (
        SELECT
            sp.species_id,
            jsonb_build_object(
                'common_name', sp.common_name,
                'scientific_name', sp.scientific_name,
                'alpha_code', sp.alpha_code,
                'alpha6_code', sp.alpha6_code,
                'ebird_code', sp.ebird_code,
                'species_color', sp.species_color,
                'thumbnail_url', sp.species_thumbnail_url,
                'image_url', sp.species_image_url,
                'wikipedia_url', sp.wikipedia_url,
                'birdweather_url', sp.birdweather_url,
                'first_seen_global', sp.first_seen,
                'last_seen_global', sp.last_seen,
                'is_target_species', (sp.species_id IN (SELECT species_id FROM target_species)),
                'phylopic_thumb_url', NULLIF(sp.phylopic_thumb_url, '')
            ) AS species_info
        FROM species_tbl sp
    ),
    final_rows AS (
        -- Species rows
        SELECT
            'species'::text AS row_kind,
            el.period_grain, el.period_start, el.period_end, el.period_day_count,
            el.location_type, el.location_id, el.station_id, el.grid_cell_id,
            el.grid_system,
            sm.species_id,
            sm.total_detections, sm.active_days, el.active_station_count,
            sm.species_station_count,
            ROUND((sm.total_detections::numeric / NULLIF(sm.active_days, 0)), 2)::double precision AS detections_per_active_day,
            ROUND((sm.active_days::numeric * 100.0 / NULLIF(el.period_day_count, 0)), 2)::double precision AS temporal_occupancy_pct,
            ROUND((sm.total_detections::numeric * 100.0 / NULLIF(el.total_detections, 0)), 2)::double precision AS relative_abundance_pct,
            ROUND((sm.species_station_count::numeric * 100.0 / NULLIF(el.active_station_count, 0)), 2)::double precision AS station_presence_pct,
            el.species_richness, el.shannon_h, el.dominance_index,
            el.pielou_evenness, el.simpson_d,
            el.target_species_richness, el.target_species_share_pct,
            sam.confidence_mean, sam.confidence_min, sam.confidence_max,
            sam.confidence_p10, sam.confidence_p50, sam.confidence_p90,
            sam.high_confidence_detections, sam.high_confidence_pct,
            sam.processed_clip_count, sam.processed_clip_pct, sam.embedding_count,
            sam.first_detection_at, sam.last_detection_at,
            ph.peak_hour_utc, ph.peak_hour_share_pct,
            sam.nocturnal_share_pct, sam.dawn_share_pct, sam.dusk_share_pct,
            bg.background_top_label, bg.background_top_share_pct, bg.background_label_mix,
            el.knmi_temp_mean_c, el.knmi_temp_min_c, el.knmi_temp_max_c,
            el.knmi_precip_mm, el.knmi_humidity_mean_pct,
            el.knmi_wind_max_ms, el.knmi_solar_mean_wm2,
            el.gee_ndvi_mean, el.gee_temp2m_mean_c, el.gee_soil_moisture_mean,
            el.gee_precip_mean_mm, el.gee_cloud_cover_mean_pct,
            el.cds_spi_1m_mean, el.cds_spi_3m_mean,
            el.cds_spei_3m_mean, el.cds_spei_12m_mean,
            el.seismic_event_count, el.seismic_max_magnitude,
            el.seismic_mean_magnitude, el.seismic_nearest_event_km,
            el.station_lifetime_availability_pct,
            el.station_availability_90d_pct,
            el.station_days_since_last,
            el.station_data_quality_grade,
            COALESCE(sip.species_info, '{}'::jsonb) AS species_info,
            jsonb_build_object(
                'clip_count', sam.processed_clip_count,
                'embedding_count', sam.embedding_count
            ) AS audio_artifacts,
            jsonb_build_object(
                'refresh_version', v_refresh_version,
                'refresh_scope', v_scope_mode,
                'source_caggs', jsonb_build_array('birdweather_daily_cagg_tbl', 'knmi_daily_cagg_tbl'),
                'raw_source', 'birdweather_tbl',
                'period_window', jsonb_build_object(
                    'period_start', el.period_start,
                    'period_end', el.period_end,
                    'period_day_count', el.period_day_count
                ),
                'target_species_count', (SELECT COUNT(*) FROM target_species)
            ) AS metric_provenance,
            jsonb_build_object(
                'hourly_detection_counts', COALESCE(hm.hourly_detection_counts, '{}'::jsonb)
            ) AS extras,
            NOW() AS computed_at
        FROM enriched_locations el
        JOIN species_metrics sm USING (period_grain, period_start, period_end, location_type, location_id)
        LEFT JOIN species_audio_metrics sam
            ON sam.period_grain = sm.period_grain
           AND sam.period_start = sm.period_start
           AND sam.location_type = sm.location_type
           AND sam.location_id = sm.location_id
           AND sam.species_id = sm.species_id
        LEFT JOIN peak_hour ph
            ON ph.period_grain = sm.period_grain
           AND ph.period_start = sm.period_start
           AND ph.location_type = sm.location_type
           AND ph.location_id = sm.location_id
           AND ph.species_id = sm.species_id
        LEFT JOIN bg_combined bg
            ON bg.period_grain = sm.period_grain
           AND bg.period_start = sm.period_start
           AND bg.location_type = sm.location_type
           AND bg.location_id = sm.location_id
           AND bg.species_id = sm.species_id
        LEFT JOIN hour_mix hm
            ON hm.period_grain = sm.period_grain
           AND hm.period_start = sm.period_start
           AND hm.location_type = sm.location_type
           AND hm.location_id = sm.location_id
           AND hm.species_id = sm.species_id
        LEFT JOIN species_info_proj sip ON sip.species_id = sm.species_id
    )
    INSERT INTO species_tracker_tbl (
        row_kind, period_grain, period_start, period_end, period_day_count,
        location_type, location_id, station_id, grid_cell_id, grid_system,
        species_id,
        total_detections, active_days, active_station_count, species_station_count,
        detections_per_active_day, temporal_occupancy_pct, relative_abundance_pct,
        station_presence_pct, species_richness, shannon_h, dominance_index,
        pielou_evenness, simpson_d, target_species_richness, target_species_share_pct,
        confidence_mean, confidence_min, confidence_max,
        confidence_p10, confidence_p50, confidence_p90,
        high_confidence_detections, high_confidence_pct,
        processed_clip_count, processed_clip_pct, embedding_count,
        first_detection_at, last_detection_at,
        peak_hour_utc, peak_hour_share_pct,
        nocturnal_share_pct, dawn_share_pct, dusk_share_pct,
        background_top_label, background_top_share_pct, background_label_mix,
        knmi_temp_mean_c, knmi_temp_min_c, knmi_temp_max_c, knmi_precip_mm,
        knmi_humidity_mean_pct, knmi_wind_max_ms, knmi_solar_mean_wm2,
        gee_ndvi_mean, gee_temp2m_mean_c, gee_soil_moisture_mean,
        gee_precip_mean_mm, gee_cloud_cover_mean_pct,
        cds_spi_1m_mean, cds_spi_3m_mean, cds_spei_3m_mean, cds_spei_12m_mean,
        seismic_event_count, seismic_max_magnitude,
        seismic_mean_magnitude, seismic_nearest_event_km,
        station_lifetime_availability_pct, station_availability_90d_pct,
        station_days_since_last, station_data_quality_grade,
        species_info, audio_artifacts, metric_provenance, extras,
        computed_at
    )
    SELECT
        row_kind, period_grain, period_start, period_end, period_day_count,
        location_type, location_id, station_id, grid_cell_id, grid_system,
        species_id,
        total_detections, active_days, active_station_count, species_station_count,
        detections_per_active_day, temporal_occupancy_pct, relative_abundance_pct,
        station_presence_pct, species_richness, shannon_h, dominance_index,
        pielou_evenness, simpson_d, target_species_richness, target_species_share_pct,
        confidence_mean, confidence_min, confidence_max,
        confidence_p10, confidence_p50, confidence_p90,
        high_confidence_detections, high_confidence_pct,
        processed_clip_count, processed_clip_pct, embedding_count,
        first_detection_at, last_detection_at,
        peak_hour_utc, peak_hour_share_pct,
        nocturnal_share_pct, dawn_share_pct, dusk_share_pct,
        background_top_label, background_top_share_pct, background_label_mix,
        knmi_temp_mean_c, knmi_temp_min_c, knmi_temp_max_c, knmi_precip_mm,
        knmi_humidity_mean_pct, knmi_wind_max_ms, knmi_solar_mean_wm2,
        gee_ndvi_mean, gee_temp2m_mean_c, gee_soil_moisture_mean,
        gee_precip_mean_mm, gee_cloud_cover_mean_pct,
        cds_spi_1m_mean, cds_spi_3m_mean, cds_spei_3m_mean, cds_spei_12m_mean,
        seismic_event_count, seismic_max_magnitude,
        seismic_mean_magnitude, seismic_nearest_event_km,
        station_lifetime_availability_pct, station_availability_90d_pct,
        station_days_since_last, station_data_quality_grade,
        species_info, audio_artifacts, metric_provenance, extras,
        computed_at
    FROM final_rows;

    LOCK TABLE public.species_tracker_tbl IN ACCESS EXCLUSIVE MODE;

    IF p_window_start IS NULL THEN
        IF v_is_modeling_scope THEN
            DELETE FROM public.species_tracker_tbl
            WHERE period_grain = 'week'
              AND location_type = 'grid';
        ELSE
            TRUNCATE TABLE public.species_tracker_tbl RESTART IDENTITY;
        END IF;
    ELSE
        DELETE FROM public.species_tracker_tbl
        WHERE period_end >= p_window_start
          AND period_start <= p_window_end
          AND (
              NOT v_is_modeling_scope
              OR (
                  period_grain = 'week'
                  AND location_type = 'grid'
              )
          );
    END IF;

    INSERT INTO public.species_tracker_tbl (
        row_kind, period_grain, period_start, period_end, period_day_count,
        location_type, location_id, station_id, grid_cell_id, grid_system,
        species_id,
        total_detections, active_days, active_station_count, species_station_count,
        detections_per_active_day, temporal_occupancy_pct, relative_abundance_pct,
        station_presence_pct, species_richness, shannon_h, dominance_index,
        pielou_evenness, simpson_d, target_species_richness, target_species_share_pct,
        confidence_mean, confidence_min, confidence_max,
        confidence_p10, confidence_p50, confidence_p90,
        high_confidence_detections, high_confidence_pct,
        processed_clip_count, processed_clip_pct, embedding_count,
        first_detection_at, last_detection_at,
        peak_hour_utc, peak_hour_share_pct,
        nocturnal_share_pct, dawn_share_pct, dusk_share_pct,
        background_top_label, background_top_share_pct, background_label_mix,
        knmi_temp_mean_c, knmi_temp_min_c, knmi_temp_max_c, knmi_precip_mm,
        knmi_humidity_mean_pct, knmi_wind_max_ms, knmi_solar_mean_wm2,
        gee_ndvi_mean, gee_temp2m_mean_c, gee_soil_moisture_mean,
        gee_precip_mean_mm, gee_cloud_cover_mean_pct,
        cds_spi_1m_mean, cds_spi_3m_mean, cds_spei_3m_mean, cds_spei_12m_mean,
        seismic_event_count, seismic_max_magnitude,
        seismic_mean_magnitude, seismic_nearest_event_km,
        station_lifetime_availability_pct, station_availability_90d_pct,
        station_days_since_last, station_data_quality_grade,
        species_info, audio_artifacts, metric_provenance, extras,
        computed_at
    )
    SELECT
        row_kind, period_grain, period_start, period_end, period_day_count,
        location_type, location_id, station_id, grid_cell_id, grid_system,
        species_id,
        total_detections, active_days, active_station_count, species_station_count,
        detections_per_active_day, temporal_occupancy_pct, relative_abundance_pct,
        station_presence_pct, species_richness, shannon_h, dominance_index,
        pielou_evenness, simpson_d, target_species_richness, target_species_share_pct,
        confidence_mean, confidence_min, confidence_max,
        confidence_p10, confidence_p50, confidence_p90,
        high_confidence_detections, high_confidence_pct,
        processed_clip_count, processed_clip_pct, embedding_count,
        first_detection_at, last_detection_at,
        peak_hour_utc, peak_hour_share_pct,
        nocturnal_share_pct, dawn_share_pct, dusk_share_pct,
        background_top_label, background_top_share_pct, background_label_mix,
        knmi_temp_mean_c, knmi_temp_min_c, knmi_temp_max_c, knmi_precip_mm,
        knmi_humidity_mean_pct, knmi_wind_max_ms, knmi_solar_mean_wm2,
        gee_ndvi_mean, gee_temp2m_mean_c, gee_soil_moisture_mean,
        gee_precip_mean_mm, gee_cloud_cover_mean_pct,
        cds_spi_1m_mean, cds_spi_3m_mean, cds_spei_3m_mean, cds_spei_12m_mean,
        seismic_event_count, seismic_max_magnitude,
        seismic_mean_magnitude, seismic_nearest_event_km,
        station_lifetime_availability_pct, station_availability_90d_pct,
        station_days_since_last, station_data_quality_grade,
        species_info, audio_artifacts, metric_provenance, extras,
        computed_at
    FROM species_tracker_tbl;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$;

CREATE OR REPLACE FUNCTION refresh_species_tracker(
    p_window_start date,
    p_window_end date
)
RETURNS bigint LANGUAGE plpgsql AS $$
BEGIN
    RETURN refresh_species_tracker(p_window_start, p_window_end, 'full');
END;
$$;

CREATE OR REPLACE FUNCTION refresh_species_tracker()
RETURNS bigint LANGUAGE plpgsql AS $$
BEGIN
    RETURN refresh_species_tracker(NULL::date, NULL::date);
END;
$$;

COMMENT ON FUNCTION refresh_species_tracker(date, date, text) IS
    'Rebuilds species_tracker_tbl (v2) from birdweather_tbl + birdweather_daily_cagg_tbl '
    'plus KNMI, Google Earth Engine, CDS drought, seismic, and station-quality '
    'sources across the tracker slices currently consumed by the route and modeling paths. '
    'When refresh_scope = modeling, recomputes only week/grid rows and pads reads to the '
    'overlapping week boundaries instead of the enclosing year. '
    'Populates ecological metrics (Shannon, Pielou, Simpson, target-species '
    'richness/share, dominance), classifier metrics (confidence quantiles, '
    'high-confidence/processed-clip/embedding counts on week/year rows), diel/phenology (peak hour, '
    'nocturnal/dawn/dusk shares, first/last detection), background acoustic scene '
    'mix, and JSONB sidecars (species_info, cheap audio_artifacts counts, metric_provenance, extras).';

COMMENT ON FUNCTION refresh_species_tracker(date, date) IS
    'Backward-compatible full-scope wrapper around refresh_species_tracker(date, date, text).';

COMMENT ON FUNCTION refresh_species_tracker() IS
    'Full all-history wrapper around refresh_species_tracker(date, date).';

COMMIT;
