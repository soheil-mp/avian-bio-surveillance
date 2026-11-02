-- Add targeted indexes for dashboard freshness, latest-row, and coverage scans.
-- These mirror the canonical baseline indexes so existing databases receive the
-- same performance support without a full rebuild.

CREATE INDEX IF NOT EXISTS idx_bwt_detected_at_desc
    ON birdweather_tbl (detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_bwt_observation_date
    ON birdweather_tbl (observation_date);

CREATE INDEX IF NOT EXISTS idx_knt_observed_at_desc
    ON knmi_tbl (observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_ebt_obs_dt_desc
    ON ebird_tbl (obs_dt DESC);

CREATE INDEX IF NOT EXISTS idx_cdt_observation_month_desc
    ON cds_drought_tbl (observation_month DESC);

CREATE INDEX IF NOT EXISTS idx_dqt_created_at_desc
    ON data_quality_tbl (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_spt_computed_at_desc
    ON species_tracker_tbl (computed_at DESC);
