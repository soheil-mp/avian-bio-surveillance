-- Existing databases created before the alerts embedding-map index was added
-- to the baseline need the same partial index without relying on a full
-- baseline replay. The bootstrap applies migrations in a transaction, so this
-- uses regular CREATE INDEX. Timescale hypertables reject CREATE INDEX
-- CONCURRENTLY here, so schedule this migration during a low-traffic window on
-- large existing deployments.

CREATE INDEX IF NOT EXISTS idx_bwt_embedded_species_detected
    ON birdweather_tbl (species_id, detected_at DESC)
    WHERE has_processed_clip
      AND (birdnet_embedding IS NOT null OR perch_v2_embedding IS NOT null);
