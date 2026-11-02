-- Add precomputed PC1/PC2 + alert-score columns to birdweather_tbl so the
-- alerts API can stop fetching raw embedding text and stop running PCA + kNN
-- on every request. The baseline DDL in db/baseline/020_birdweather.sql now
-- carries the same column list for fresh installs; this migration mirrors it
-- for any existing database created before the baseline change landed.
--
-- All adds are idempotent (ADD COLUMN IF NOT EXISTS) and nullable. Population
-- happens out-of-band via
--   poetry run python -m avian_biosurveillance.modeling.embedding_projection

ALTER TABLE birdweather_tbl
    ADD COLUMN IF NOT EXISTS birdnet_pc1 REAL,
    ADD COLUMN IF NOT EXISTS birdnet_pc2 REAL,
    ADD COLUMN IF NOT EXISTS birdnet_alert_score REAL,
    ADD COLUMN IF NOT EXISTS perch_v2_pc1 REAL,
    ADD COLUMN IF NOT EXISTS perch_v2_pc2 REAL,
    ADD COLUMN IF NOT EXISTS perch_v2_alert_score REAL;
