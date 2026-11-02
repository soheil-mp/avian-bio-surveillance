# Database Changelog

## Unreleased

- **Modeling outputs now persist in Postgres instead of runtime CSV/JSON files.**
  Added `db/baseline/160_modeling.sql` with run envelopes, weekly feature rows,
  weekly anomaly score rows, embedding cluster assignments, and tuning sweep
  leaderboard tables. The schema keeps typed columns for current dashboard and
  import paths while preserving complete historical rows in JSONB payloads,
  with indexes for latest complete run lookup, score/context filters,
  detection-id linkage, and embedding-cluster reads.

- **KNMI daily cagg now materializes the full daily station view.**
  `knmi_daily_cagg_tbl` now carries station metadata (`station_name`, lat/lon,
  elevation), bucket/sample timing (`observation_date`, sample counts,
  first/last observation timestamps), and min/max/avg rollups for every numeric
  `knmi_tbl` source column. The aggregate also adds circular daily wind
  direction averages for both meteorological direction and flow direction while
  retaining the older compatibility aliases (`daily_mean_temp_c`,
  `daily_precip_mm`, `daily_max_wind_ms`, ...). Existing databases rebuild the
  cagg via `db/migrations/V003__expand_knmi_daily_cagg.sql`, and the weather
  frontend can now switch entirely onto the daily aggregate instead of mixing in
  raw `knmi_tbl` reads for wind.

- **Alerts embedding-map no longer fetches raw embedding text or runs PCA in
  the API tier.** Added six nullable `REAL` columns to `birdweather_tbl`
  (`birdnet_pc1`, `birdnet_pc2`, `birdnet_alert_score`, `perch_v2_pc1`,
  `perch_v2_pc2`, `perch_v2_alert_score`) that carry the canvas coordinate and
  kNN cosine-isolation alert score for each detection per embedding backend.
  Population happens out-of-band via
  `poetry run python -m avian_biosurveillance.modeling.embedding_projection`,
  which runs a global sklearn PCA + kNN over the full embedding population
  (one fit per backend) and UPSERTs the results back to the row. The frontend
  now SELECTs these columns directly instead of pulling 10-20 KB of stringified
  embedding per row and running a 28-iteration power-iteration PCA + 256×N
  cosine isolation on every request. The baseline DDL carries the column list
  for fresh installs; existing databases pick the columns up via
  `db/migrations/V002__alerts_precomputed_embedding_projections.sql`. Rows
  whose backend has not yet been projected return NULL and are skipped by the
  frontend rather than piled on the canvas centre.

- **Alerts embedding-map query now uses a dedicated partial index.**
  Added `idx_bwt_embedded_species_detected` to `birdweather_tbl`: a partial
  index keyed on `(species_id, detected_at DESC)` restricted to rows with
  `has_processed_clip` and at least one populated embedding column. The
  `/api/alerts/embedding-map` and shell-coverage queries used to perform a
  recent-chunk scan with a residual filter; this index lets the planner serve
  the same predicate as an ordered key-range scan per species. Idempotent
  (`CREATE INDEX IF NOT EXISTS`) in baseline for fresh installs and mirrored in
  `db/migrations/V004__alerts_embedded_species_detected_index.sql` so existing
  databases pick it up even when baseline replay is skipped while application
  clients are connected.

- **Modeling embedding backfills now have an all-species embedded-date index.**
  Added `idx_bwt_embedded_detected_at` to `birdweather_tbl`, a partial
  `(detected_at DESC)` index restricted to processed rows with BirdNET or Perch
  embeddings. The modeling pipeline now filters embedding candidates by
  `detected_at` before station/grid enrichment, so target and all-species
  embedding loads avoid broad `observation_date` scans during historical
  backfills. Existing databases pick this up through
  `db/migrations/V005__modeling_embedded_detected_index.sql`.

- **Dashboard freshness and coverage queries now have targeted temporal support.**
  Added narrow indexes for BirdWeather latest/coverage scans, KNMI/eBird
  freshness windows, CDS monthly coverage, data-quality audit freshness, and
  species-tracker computed timestamps. Existing databases can apply the same
  indexes through `db/migrations/V001__dashboard_temporal_indexes.sql`.

- **Live BirdWeather species seeding now populates PhyloPic cache columns.**
  The Python species write path now resolves PhyloPic silhouettes while it
  seeds `species_tbl`, instead of leaving `phylopic_svg_url`,
  `phylopic_thumb_url`, attribution, and UUID empty until the frontend lazily
  touches a species. The BirdWeather species upsert now stores those columns,
  and the live collector enriches new species rows before writing them.

- **`refresh_species_tracker()` now stages before live cutover.**
  The tracker rebuild no longer truncates `species_tracker_tbl` before running
  its expensive CTE pipeline. `db/routines/050_refresh_species_tracker.sql`
  now materializes rows into a temp shadow table first, then takes the
  `ACCESS EXCLUSIVE` lock only for the final truncate-and-copy into the live
  table. This keeps long tracker refreshes from blocking readers and
  autovacuum for the full rebuild window.

- **Frontend dashboard hot paths now have dedicated cache/index support.**
  Added indexes for Movebank overlay time-window scans, Earth Engine
  grid-cell/latest-label lookups, and BirdWeather station map scans so the
  Next.js dashboard can serve cached API responses without repeatedly forcing
  full-table work in Postgres.
    - `db/baseline/060_movebank.sql` adds located-fix timestamp/species
      indexes for `/api/movebank-overlays`.
    - `db/baseline/080_google_earth_engine.sql` adds a
      `(grid_cell_id, observation_date DESC)` index for environment overlays.
    - `db/baseline/110_birdweather_stations.sql` adds station map/freshness
      indexes for `/api/stations` and `/api/network-health`.

- **Frontend species tracker now reads precomputed network tracker rows.**
  Added whole-network (`location_type = 'network'`, `location_id = 'nl'`)
  rows to `refresh_species_tracker()` so the home dashboard can load species
  summaries from `species_tracker_tbl` instead of recomputing raw/cagg
  analytics on every request. The tracker now stores hourly detection counts
  in `extras.hourly_detection_counts`, and `db/baseline/150_species_tracker.sql`
  adds a partial index for the frontend network-species query path.

- **`species_tbl` audit cleanup (remove route-shaped cache fields).**
  Tightened the semantics of the canonical species dimension so it stores
  species metadata, the current activity envelope, and cached PhyloPic
  assets, but not request-shaped dashboard payloads.
    - `db/baseline/130_species.sql` removes `detection_count_90d`,
      `list_rank`, `dashboard_raw`, and `dashboard_computed_at`, along
      with the `idx_species_tbl_list_rank` index.
    - `db/routines/040_refresh_species_dashboard_cache.sql` is deleted;
      the old top-N snapshot cache is no longer part of bootstrap.
    - Dagster follow-up flows and asset definitions no longer schedule a
      `refresh_species_dashboard_cache()` step.
    - Frontend linkage hardening: `frontend/src/app/api/species-activity/route.ts`
      now left-joins `species_tbl` and falls back to `Species {species_id}`
      when metadata seeding lags behind detection ingest, avoiding false
      empty states when BirdWeather aggregates exist but `species_tbl` has
      not been populated yet.

- **Species tracker v2 (richer ecological + audio metrics, JSONB sidecars).**
  Redesigned `species_tracker_tbl` to be the unified analytical surface for
  the 11 target species across station/grid grain and
  day/week/month/season/year periods.
    - `db/baseline/150_species_tracker.sql` adds:
        - Community-level diversity: `pielou_evenness`, `simpson_d`,
          `target_species_richness`, `target_species_share_pct`.
        - Per-species classifier metrics: `confidence_mean/min/max`,
          `confidence_p10/p50/p90`, `high_confidence_detections`,
          `high_confidence_pct`, `processed_clip_count`/`pct`,
          `embedding_count`.
        - Diel + phenology: `first_detection_at`, `last_detection_at`,
          `peak_hour_utc`, `peak_hour_share_pct`, `nocturnal_share_pct`,
          `dawn_share_pct`, `dusk_share_pct`.
        - Acoustic scene mix: `background_top_label`,
          `background_top_share_pct`, `background_label_mix` (JSONB).
        - Station-grain quality projection (NULL for grid rows):
          `station_lifetime_availability_pct`,
          `station_availability_90d_pct`, `station_days_since_last`,
          `station_data_quality_grade` (A/B/C from availability_90d_pct).
        - JSONB sidecars: `species_info` (cached `species_tbl` projection +
          `is_target_species` flag), `audio_artifacts` (capped detection-ID
          lists for processed clips and embeddings, plus top-confidence
          clip), `metric_provenance` (refresh version, source caggs, period
          window, target species count), `extras` (open extension slot).
        - GIN indexes on `species_info` and `audio_artifacts`; BTREE on
          `period_start DESC` for retention queries.
    - `db/routines/050_refresh_species_tracker.sql` rewritten to populate
      the new metrics. Per-detection aggregates (confidence quantiles,
      diel, audio artifacts, background mix) source from `birdweather_tbl`;
      ecological roll-ups continue to source from
      `birdweather_daily_cagg_tbl` to keep the cheap path. Target species
      IDs are inlined from `config/target_species.json` (a future migration
      may move these to a dedicated `target_species_tbl`). Audio artifact
      ID lists are capped at the top 200 by confidence to bound row size.
    - Frontend impact: none — `frontend/` does not yet read
      `species_tracker_tbl` (it queries source tables directly). The
      tracker is positioned as the canonical surface for upcoming
      ecological dashboards and the per-species detail page.

- **BirdWeather audio processing and feature columns.** Added pgvector-backed
  feature storage and a JSONB artifact manifest to `birdweather_tbl` for the
  new Dagster audio-processing and feature-engineering assets.
    - `db/baseline/010_extensions.sql` now enables the `vector` extension.
    - `db/baseline/020_birdweather.sql` adds `birdnet_embedding`,
      `perch_v2_embedding`, and `feature_artifacts` alongside the existing
      `background_sound_clf` and `has_processed_clip` fields.
    - The new pipeline writes cropped FLAC clips to `processed-audio`, writes
      spectrogram and waveform PNGs to the `features` MinIO bucket, and records
      feature metadata back on the BirdWeather row.

- **BirdWeather station-quality redesign.** The hybrid
  `birdweather_stations_tbl` remains the station reference plus summary cache,
  but the metric semantics now match the analytical contract more closely.
    - `db/caggs/010_birdweather_daily.sql` now exposes `confidence_sum` and
      `high_confidence_count`, so station summaries use weighted mean
      confidence and exact high-confidence detection counts instead of
      averaging daily averages or inferring counts from bucket maxima.
    - `db/baseline/110_birdweather_stations.sql` replaced the misleading
      `coords_in_nl_pct` and `audio_valid_pct` fields with
      `is_within_nl_bbox`, `audio_metadata_valid_pct`, `processed_clip_pct`,
      and a simplified availability model centered on `days_since_last`,
      `lifetime_availability_pct`, and `availability_90d_pct`; the dev-phase
      cleanup later removed the recent-window counters, metadata provenance
      fields, `summary_refreshed_at`, and table-level `CHECK` constraints.
    - `db/routines/010_refresh_station_summary.sql` now uses
      `INSERT ... ON CONFLICT DO UPDATE`, preserving seeded station metadata
      while ensuring detected stations still appear if summary refresh runs
      before metadata seeding. The composite score is now 25% weighted
      confidence, 20% recent 90-day coverage, 20% freshness, 15% maturity,
      10% audio metadata validity, and 10% duplicate quality.

- **`birdweather_tbl`: drop redundant per-event columns
  (`detection_lat`, `detection_lon`, `audio_duration_s`).**
  Dev-phase normalisation pass driven by the observation that all three
  columns were either pure duplicates of upstream tables or semantically
  wrong. Existing dev databases must be wiped to converge.
    - **Per-event coordinates removed.** `detection_lat` / `detection_lon`
      duplicated `birdweather_stations_tbl.lat / lon` for every credible
      (fixed PUC) station — they only diverged for mobile/app stations
      which are excluded from the credible-tier analytical path anyway.
      The detection's coordinates are now joined from the station row
      via `station_id`. Storage win: `2 × DOUBLE PRECISION NOT NULL =
      16 bytes/row` of fixed payload removed from every detection.
    - **Soundscape-duration column removed.** `audio_duration_s` was
      the duration of the *whole soundscape* (typically ~25 s), not of
      the clip. The frontend explore page was reading it as
      `clipDuration`, which was a semantic bug — per-clip duration is
      derived as `audio_end_time_s - audio_start_time_s` and the
      frontend now computes it that way.
    - **Coordinated consumer rewrites** so nothing reads the dropped
      columns:
        - `db/routines/010_refresh_station_summary.sql`: geographic relevance
          is derived directly from the station's own coords, and recent audio
          health is based on clip metadata rather than the removed duration
          column.
        - `data/quality.py`:
            - Removed `detection_lat` / `detection_lon` /
              `audio_duration_s` from `_EXPECTED_COLUMNS["birdweather_tbl"]`
              and the lat/lon entries from the `non_null_pct_{col}` loop.
            - Rewrote `coords_in_netherlands_pct` to JOIN
              `birdweather_stations_tbl` and filter on `s.lat / s.lon`.
            - **Removed `detection_station_coord_agreement_pct` entirely**
              — it was a tautology after the per-event coords disappeared.
            - **Removed `positive_audio_duration_pct` entirely** — its
              source column is gone.
            - Rewrote the per-station drill-down query so `in_nl` filters
              on `s.lat / s.lon` and `audio_ok` no longer references
              `b.audio_duration_s`.
        - `frontend/src/app/explore/page.tsx`: dropped `b.audio_duration_s`
          from the SELECT list and changed `clipDuration` to compute
          `Number(row.audio_end_time_s) - Number(row.audio_start_time_s)`.
        - `db/models/birdweather.py`: dropped the three fields from the
          dataclass, dropped the `coords = raw.get("coords") or {}`
          extraction in `from_jsonl`, and updated the docstring +
          `pandas_dtypes` map.
        - `db/loaders/birdweather.py`: dropped `detection_lat`,
          `detection_lon`, `audio_duration_s` from `_BWD_COLUMNS`.
        - `data/README.md`: updated the `non_null_pct_{col}` count
          (8 → 6), removed the dropped quality metrics from the table,
          and clarified that `coords_in_netherlands_pct` is now joined
          from the station row.
        - `docs/reference/data_dictionary.md`: dropped the three column
          rows, added "No per-event coordinates" and "No clip duration
          column" bullets explaining the rationale, and updated the FK
          description to mention the station as the source of coords.
    - Tests updated: `tests/db/test_models_birdweather.py` adds
      `assert not hasattr(det, ...)` guards for all three dropped fields,
      removes the lat/lon assertions, drops the `coords` block from the
      minimal raw fixture, and updates the soundscape-fields test to
      assert `audio_start_time_s` / `audio_end_time_s` instead of
      `audio_duration_s`. `tests/db/test_loader.py` drops the lat/lon
      defaults from `_make_bwd`.
    - `db/erd/data_model.drawio` regenerated; `birdweather_tbl` now
      shows **22 columns** (was 25 in the previous round, 35 before
      species normalisation, 43 originally). Bootstrap-paths suite
      (15/15) still passes.
- **Cross-table dev-phase cleanup (rest of the baseline)**. The same
  policy applied to `birdweather_tbl` last round was extended to every
  other baseline file and to both continuous aggregates: drop value-range
  and enum CHECK constraints (validation moves to the `data_quality_tbl`
  pipeline), drop `add_compression_policy` / `add_retention_policy` on
  hypertables and caggs, delete legacy idempotent shims (the schema now
  describes the intended end-state directly), and trim each table's
  index set down to a justified minimum. Existing dev databases must be
  wiped to converge.
    - **`030_knmi.sql`.** Dropped six value-range CHECKs (rh, wind_speed,
      wind_direction, precipitation, solar_radiation, cloud_cover),
      compression segment-by/order-by + 60-day compression policy, and
      the 15-year retention policy. Removed all four secondary indexes:
      the PK `(station_id, observed_at)` already covers per-station and
      per-station-time queries, chunk-time pruning handles cross-station
      time scans, and the ~50-station coordinate index was useless.
    - **`040_sovon.sql`.** Dropped four value-range CHECKs (year, month,
      dead_bird_count, n_total) and the legacy `DO $$ ... ADD COLUMN
      observation_date` shim — the generated column is declared inline
      in the canonical CREATE TABLE. Trimmed from six to one index
      (`idx_svt_observation_date`); the unique constraint already
      indexes `(species_english, year, month)`, so the
      species/year/month composites and partials were
      redundant.
    - **`050_ebird.sql`.** Dropped three value-range CHECKs (how_many,
      lat, lng), 90-day compression, and 15-year retention. Trimmed
      from five to three indexes: kept `idx_ebt_species_code`,
      `idx_ebt_loc_id`, and the partial `idx_ebt_country_code`; dropped
      `idx_ebt_observation_date` (chunks) and `idx_ebt_coords` (no
      PostGIS).
    - **`060_movebank.sql`.** Dropped ten value-range CHECKs across
      lat/lon/heading/ground_speed/gps_hdop/gps_satellite_count plus
      the birth-and-mortality lat/lon/year ranges. Removed compression
      and retention. Trimmed from eleven to five biosurveillance-justified
      indexes: kept `idx_mbt_study_id`, `idx_mbt_individual_id`,
      `idx_mbt_species`, `idx_mbt_mortality`, and `idx_mbt_behaviour`;
      dropped `idx_mbt_sensor_type`, `idx_mbt_visible`, `idx_mbt_coords`,
      `idx_mbt_individual_sex`, and `idx_mbt_deployment_id`.
    - **`070_grid_cells.sql`.** Dropped seven value-range CHECKs
      (centroid lat/lon, four bbox bounds, grid_size_km > 0) and the
      legacy `ALTER TABLE ... ADD COLUMN IF NOT EXISTS grid_row/grid_col`
      + `UPDATE … regexp_match(grid_cell_id …)` backfill shim — the
      columns are declared inline in the canonical CREATE TABLE. Trimmed
      from five to two indexes: kept `idx_gct_grid_system` and the
      partial `idx_gct_system_row_col`; dropped `idx_gct_coords`,
      `idx_gct_bbox`, and the GIN on `cell_geojson` (none of these
      have a current read path).
    - **`080_google_earth_engine.sql`.** Full rewrite. Removed both
      legacy `DO $$ ... $$` blocks: the legacy-table cleanup
      (`google_earth_engine_station_legacy_tbl%`, the station-id
      detection branch, and the `google_earth_engine_grid_facts_tbl`
      rename), and the ~80-column `ALTER TABLE ... ADD COLUMN IF NOT
      EXISTS` ladder + `section_availability` NOT-NULL fixup. The
      foreign key to `grid_cells_tbl` is now declared inline as a
      regular `FOREIGN KEY` clause inside CREATE TABLE (no
      `NOT VALID` + `VALIDATE` two-step). Dropped the partial index
      `idx_geeg_ndvi`; kept `observation_date`, `grid_cell_id`, and
      both JSONB GINs (`profile_data`, `section_availability`).
    - **`090_cds_drought.sql`.** Dropped seven value-range CHECKs (lat,
      lon, four bbox bounds, resolution_degrees > 0) and three enum
      CHECKs (`dataset_type IN (...)`, `product_type IN (...)`,
      `ensemble_member BETWEEN 0 AND 9`). Removed 180-day compression
      and 15-year retention. Trimmed from five indexes to one
      (`idx_cdt_dataset_product`); the PK
      `(grid_cell_id, observation_month, dataset_type, product_type,
      ensemble_member, data_version)` already covers per-cell time-axis
      lookups and chunk pruning handles range scans, so the explicit
      `idx_cdt_observation_month`, `idx_cdt_coords`, `idx_cdt_spi_1m`,
      and `idx_cdt_spei_1m` were redundant.
    - **`100_seismic.sql`.** Dropped two coordinate CHECKs. Trimmed
      from three to one index — kept `idx_set_event_time` (the only
      index with a routine read path); `idx_set_magnitude` and
      `idx_set_coords` had no consumer.
    - **`110_birdweather_stations.sql`.** Dropped the two enum CHECKs
      on `freshness_status` and `credibility_tier` — domain enforcement
      moves to the data-quality pipeline so refresh routines can write
      transient values without fighting hard schema rules.
    - **`120_data_quality.sql`.** Dropped the
      `kind IN ('summary', 'metric')` CHECK. Trimmed from five to four
      indexes: dropped `idx_dqt_run_id` because it is a strict prefix
      of the kept `idx_dqt_run_kind (run_id, kind)`. Kept the two
      partial indexes for summary/metric drill-downs and the
      `metric_status` failure index.
    - **`130_species.sql`.** Dropped the legacy `DO $$ ... ALTER TABLE
      species_dim_tbl RENAME TO species_tbl` rename shim and the
      trailing `DROP VIEW IF EXISTS species_dim_tbl` cleanup. Trimmed
      from five to three indexes: kept the case-insensitive expression
      indexes `idx_species_tbl_common_name_ci`,
      `idx_species_tbl_scientific_name_ci`, and the partial
      `idx_species_tbl_list_rank`; dropped the duplicate non-CI
      `idx_species_dim_common_name` and `idx_species_dim_scientific`.
    - **`caggs/010_birdweather_daily.sql` and `caggs/020_knmi_daily.sql`.**
      Dropped the 15-year retention policies from both continuous
      aggregates. The continuous-aggregate refresh policies (3-day
      look-back, hourly schedule) are kept — those are the actual
      refresh contract, not lifecycle.
    - **Documentation.** `db/README.md` "Retention" section was
      replaced by a "Retention & Compression" note explaining that
      no policies are installed in dev, and a new architecture-notes
      bullet flags that the baseline carries no CHECK constraints.
      `docs/reference/data_dictionary.md` had every `CHECK (...)`
      stripped from the Constraints column across all tables (~50
      rows touched).
    - `db/erd/data_model.drawio` was regenerated; the bootstrap-paths
      test suite (15/15) still passes.
- **BirdWeather schema cleanup (dev-phase rewrite of `birdweather_tbl`)**.
  We are still in the development phase, so legacy catch-up shims and
  ALTER ladders are no longer paying for themselves; the baseline file
  was rewritten to match the intended end-state and existing dev databases
  must be wiped to converge.
    - **Normalised station reference.** Dropped six denormalised station
      columns (`station_name`, `station_type`, `station_country`,
      `station_state`, `station_lat`, `station_lon`) from `birdweather_tbl`;
      `station_id` remains as the foreign key into `birdweather_stations_tbl`
      (renamed from `recording_stations_tbl` for clarity).
    - **Renamed `recording_stations_tbl` → `birdweather_stations_tbl`**
      across every SQL file, Python module, frontend route, README, notebook,
      and config (~31 production files touched). The file was also renamed
      `db/baseline/110_recording_stations.sql` → `db/baseline/110_birdweather_stations.sql`.
    - **Combined background-sound classification columns.** `bg_classification_status`
      and `bg_classification_tags` were merged into a single JSONB column
      `background_sound_clf` with envelope shape
      `{"status": "...", "tags": [{"label": ..., "score": ...}, ...]}`.
      Loader, model, ERD, and all consumers (frontend explore page, ops
      precompute script) updated to read from the merged column.
    - **Dropped useless interaction URLs.** Removed `favorite_url`,
      `flag_url`, `vote_url` (BirdWeather web-app links with no analytical
      value) from the table, the dataclass, and the loader column list.
    - **Removed all CHECK constraints** on `confidence`, `score`,
      `probability`, `detection_lat`, `detection_lon`, `audio_duration_s`,
      and `audio_filesize_bytes`. Value-range validation is performed by
      the `data_quality_tbl` pipeline; insert-time CHECKs were duplicating
      that work and silently rejecting rows with no audit trail.
    - **Dropped TimescaleDB compression + retention policies** from
      `birdweather_tbl` (`add_compression_policy`, `add_retention_policy`,
      `compress_segmentby`, `compress_orderby`). The table is still a
      hypertable (1-day chunks), but is kept uncompressed and indefinitely
      until volume justifies re-introducing the operational complexity.
    - **Trimmed indices** to a justified, minimal set: kept the PK,
      `idx_bwt_species_id`, `idx_bwt_station_id`, the composite
      `idx_bwt_station_species_id`, and the partial
      `idx_bwt_has_processed_clip`. Dropped `idx_bwt_observation_date`
      (covered by chunk-time pruning), `idx_bwt_species_date` and
      `idx_bwt_station_obs_date` (composites redundant with chunks +
      single-col), `idx_bwt_confidence` (rarely the right access path on
      a continuous numeric), and `idx_bwt_coords` (no PostGIS yet, so a
      btree on `(lat, lon)` adds little value).
    - **Removed all idempotent catch-up shims** from the BirdWeather DDL:
      no more `ALTER ... ADD COLUMN IF NOT EXISTS` for `has_processed_clip`,
      no more `DO $$ ... DROP MATERIALIZED VIEW ...` for the legacy cagg
      shape, no more `ALTER TABLE ... DROP COLUMN IF EXISTS` ladder for
      the species denormalisation, no more `DROP INDEX IF EXISTS
      idx_bwt_station_common_name`. The CREATE TABLE statement now
      describes the intended end-state directly.
    - Deleted `db/baseline/150_legacy_audit_cleanup.sql` (the catch-up
      file folded in from the now-deleted `V002` migration). With the
      legacy-cleanup-by-shim policy retired in dev, there is nothing for
      this file to do on a fresh database.
    - **Refactored `refresh_station_summary()`** to a pure
      detection-derived summary `UPDATE` against pre-existing rows in
      `birdweather_stations_tbl`. The legacy `meta` CTE that recovered
      station_name/type/etc. from `birdweather_tbl` is gone; reference
      attributes are now owned exclusively by the API loader
      `upsert_birdweather_stations`.
    - **Coordinated consumer updates** so every reader joins
      `birdweather_stations_tbl` (or `species_tbl`) instead of reading
      the dropped columns from `birdweather_tbl`:
        - SQL: `db/routines/040_refresh_species_dashboard_cache.sql`
          (hotspots CTE).
        - Python: `data/quality.py` (schema-compliance set, detection-vs-station
          drift, `station_country_netherlands_pct`, `_assess_station_quality`,
          target-species coverage, Shannon diversity, BW↔Sovon overlap),
          `data/stations.py` (DB-availability flag), `scripts/ops/precompute_explore.py`
          (species options + bg-tag palette).
        - Frontend: `app/page.tsx`, `app/explore/page.tsx`,
          `/api/species-tracker` (hotspots).
    - Tests updated: `tests/db/test_models_birdweather.py` now asserts
      the dropped fields are absent and verifies the new
      `background_sound_clf` envelope shape; `tests/db/test_bootstrap_paths.py`
      drops `150_legacy_audit_cleanup.sql` from the expected baseline
      and adds the renamed `110_birdweather_stations.sql` (with the old
      `110_recording_stations.sql` filename added to the legacy-resurface
      guard).
    - **Storage impact** on the per-detection row: 6 station TEXT/DOUBLE
      columns + 3 URL TEXT columns + 1 redundant TEXT column → roughly
      80–150 bytes/row removed pre-compression. The merged JSONB
      envelope is slightly larger than the two old columns combined when
      both sides are populated, but most rows have no AST tags so the
      net is still negative.
- Normalised the BirdWeather species reference. Dropped the eight
  per-detection species columns (`common_name`, `scientific_name`,
  `alpha_code`, `alpha6_code`, `ebird_code`, `species_color`,
  `species_image_url`, `species_thumbnail_url`) from `birdweather_tbl`;
  `species_id` remains as the foreign key into the canonical
  `species_tbl`. Storage saving is roughly 30–50 bytes/row on disk
  (TimescaleDB-compressed), with the URL columns dominating the win.
  Coordinated changes:
    - `db/baseline/020_birdweather.sql` carries an idempotent catch-up
      shim that drops the legacy `birdweather_daily_cagg_tbl` (whose
      `GROUP BY` referenced `common_name`) before dropping the columns,
      so old dev databases converge on the next bootstrap.
    - `db/caggs/010_birdweather_daily.sql` now groups by
      `(bucket, species_id, station_id)` only — the `common_name` is
      functionally determined by `species_id` and lived in the GROUP BY
      purely as a denormalised display field.
    - `db/routines/020_refresh_species_dim.sql` no longer sources names
      from detections via `DISTINCT ON`; it just refreshes the
      activity envelope (`first_seen`, `last_seen`,
      `total_detections`, `station_count`) from the cagg. Reference
      attributes are seeded exclusively by the Python loader
      `upsert_birdweather_species`.
    - `db/routines/010_refresh_station_summary.sql` joins `species_tbl`
      for top-5 species names + thumbnails (replacing the
      `DISTINCT ON (species_id) FROM birdweather_tbl` thumbnail CTE).
    - `db/routines/040_refresh_species_dashboard_cache.sql` is now a
      cache-only writer: it `INNER JOIN`s `species_tbl` for eligibility
      and `UPDATE`s only the cache columns (`detection_count_90d`,
      `list_rank`, `dashboard_raw`, `dashboard_computed_at`) — no
      reference attributes are ever overwritten.
    - Python: `BirdWeatherDetection`, `_BWD_COLUMNS`, `from_jsonl`,
      and `pandas_dtypes` lost the eight fields. The orchestration
      already calls `upsert_birdweather_species` before
      `upsert_birdweather_detections` (see `scripts/ops/seed_db.py`).
    - Frontend: `/api/species-tracker`, `/api/species-activity`,
      `/api/mortality`, `app/page.tsx`, and `app/explore/page.tsx`
      now `JOIN species_tbl ON species_id` for display attributes.
      `/api/phylopic` already read from `species_tbl` and is unchanged.
- Removed `db/migrations/V002__drop_redundant_audit_timestamps.sql`. We are
  still in the development phase, so the migration ledger is not the right
  home for what is now part of the canonical schema shape. Its destructive
  cleanup (drop legacy `created_at` / `updated_at` / `collected_at` columns,
  drop `trg_*_updated_at` triggers, drop `update_modified_column()`) was
  folded into a single idempotent baseline file
  `baseline/150_legacy_audit_cleanup.sql` so legacy dev databases converge
  to the canonical shape on the next bootstrap without requiring a volume
  wipe (every statement is `IF EXISTS`-guarded and is a fast no-op on a
  fresh database).
- Moved `grid_cells_5km_v` out of `baseline/` into
  `db/views/012_grid_cells_5km.sql`, where it joins
  `010_google_earth_engine_grid.sql` and `011_cds_drought.sql`. Convenience
  views now live exclusively under `db/views/`, matching the project
  taxonomy in `db/README.md`.
- Completed the per-table split of `baseline/`: every file now defines a
  single table. `100_operational_tables.sql` was split into
  `100_seismic.sql`, `110_recording_stations.sql`, and `120_data_quality.sql`;
  `110_derived_tables.sql` was split into `130_species.sql`,
  `140_movebank_tracks.sql`, and (originally) `150_grid_cells_5km_v.sql`
  which has since moved to `db/views/`.
- Co-located every `COMMENT ON TABLE` with its table definition. The
  centralized comment block previously parked at the bottom of
  `110_derived_tables.sql` is gone; comments for `birdweather_tbl`,
  `knmi_tbl`, `sovon_tbl`, `ebird_tbl`, `movebank_tbl`,
  `google_earth_engine_tbl`, `grid_cells_tbl`, and `cds_drought_tbl` now
  live alongside their respective `CREATE TABLE` statements.
- Moved auxiliary database provisioning and legacy role cleanup into
  `bootstrap/010_aux_databases_and_roles.sql`, with `ensure_aux_roles.sh`
  retained as the multi-database connection wrapper.
- Folded legacy Google Earth Engine grid-table promotion and schema-shape
  catch-up into canonical baseline DDL; removed the obsolete `V999*`
  migration files.
- Removed the retired station-level Google Earth Engine table preservation path;
  stale station-level tables are dropped during baseline catch-up.
- Split the former monolithic baseline into ordered domain files under
  `baseline/` (now one table per file end-to-end).
- Updated DB documentation and ERD generation to describe
  `google_earth_engine_tbl` as the canonical grid-cell facts table.
