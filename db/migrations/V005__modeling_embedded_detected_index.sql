-- Existing databases need the all-species embedded-detection time index used by
-- modeling backfills and scene-control reads. This complements the species-keyed
-- alerts index from V004. Bootstrap applies migrations in a transaction, so this
-- uses regular CREATE INDEX; schedule on a quiet window for large deployments.

CREATE INDEX IF NOT EXISTS idx_bwt_embedded_detected_at
    ON birdweather_tbl (detected_at DESC)
    WHERE has_processed_clip
      AND (birdnet_embedding IS NOT null OR perch_v2_embedding IS NOT null);
