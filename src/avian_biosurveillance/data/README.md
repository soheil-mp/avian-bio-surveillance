# Data Layer (`avian_biosurveillance.data`)

Data-quality auditing and station profiling for the Avian Biosurveillance platform.

## Modules

| Module | Purpose |
|--------|---------|
| `quality.py` | Table-level quality audit (6 ISO 25012 dimensions) + per-station profiling |
| `stations.py` | BirdWeather station extraction from JSONL files and metadata export |

## Quality Audit

### Overview

The quality audit evaluates three source tables — `birdweather_tbl`,
`knmi_tbl`, and `sovon_tbl` — across six ISO 25012 /
DAMA DMBOK quality dimensions. Each metric is classified as:

| Status | Meaning | Affects Score |
|--------|---------|---------------|
| **PASS** | Meets or exceeds threshold | Yes |
| **WARN** | Below threshold but not critical | Yes |
| **FAIL** | Critical violation | Yes |
| **INFO** | Diagnostic / descriptive only | No |

Metrics enforced by database constraints (CHECK, PK) or measuring geographic
relevance rather than data quality are reported as INFO-only.

### Dimension Weights (Table-Level Composite Score)

| Dimension | Weight | Rationale |
|-----------|--------|-----------|
| Completeness | 30% | Temporal gaps directly break time-series ML models |
| Timeliness | 25% | Stale data is useless for early warning |
| Accuracy | 20% | Range and consistency checks (geographic checks now INFO) |
| Consistency | 15% | Cross-field and cross-table agreement |
| Uniqueness | 5% | PK uniqueness now INFO; near-dup is only active check |
| Validity | 5% | Most sub-metrics enforced by DB constraints |

### Station-Level Quality Score

Per-station quality is computed by `refresh_station_summary()` in SQL and
stored directly in `birdweather_stations_tbl`. The score blends signal quality,
recent reporting behavior, operational freshness, station maturity, audio
metadata health, and duplicate pressure:

| Weight | Metric | What It Measures |
|--------|--------|------------------|
| 25% | `avg_confidence` | Signal quality, computed as weighted mean BirdNET confidence |
| 20% | `recent_coverage_pct` | Active-day coverage in the latest 90-day window |
| 20% | freshness | Operational health, decaying to 0 over 30 idle days |
| 15% | `station_maturity_pct` | Active-day maturity, capped at 90 reporting days |
| 10% | `audio_metadata_valid_pct` | Recent detections with usable clip metadata |
| 10% | `100 - near_dup_pct` | Penalty for repeated same-station/species near-duplicates |

Diagnostic columns such as `is_within_nl_bbox`, `temporal_coverage_pct`, and
`processed_clip_pct` are still computed and stored for dashboard display but do
not directly affect the composite score. Geographic location is a filtering
concern, not a quality concern — a station outside the Netherlands with perfect
data can still score 100%.

---

## Complete Metric Reference

### 1. Completeness

Measures whether expected data is present — temporal coverage, column
population, species coverage, and volume stability.

#### birdweather_tbl

| Metric | Threshold | Status | Description |
|--------|-----------|--------|-------------|
| `total_rows` | — | INFO | Row count |
| `non_null_pct_{col}` (×6) | 95% | PASS/WARN | NULL rate for `confidence`, `score`, `probability`, `species_id`, `station_id`, `audio_url` |
| `temporal_day_coverage_pct` | 80% | PASS/WARN | % of calendar days (first → last) with ≥ 1 detection |
| `target_species_coverage` | 70% | PASS/WARN | % of 11 target species (SDD §1.2) observed in the data |
| `max_consecutive_gap_days` | — | INFO | Longest gap between detection days |
| `volume_trend_pct` | 50% | PASS/WARN | Last-7-day daily average / preceding-30-day daily average |
| `species_diversity_shannon` | — | INFO | Shannon H′ diversity index across all species |

#### knmi_tbl

| Metric | Threshold | Status | Description |
|--------|-----------|--------|-------------|
| `total_rows` | — | INFO | Row count |
| `non_null_pct_{col}` (×8) | 80% | PASS/WARN | NULL rate for `temperature_c`, `relative_humidity_pct`, `wind_speed_ms`, `wind_direction_deg`, `pressure_hpa`, `precipitation_mm`, `solar_radiation_wm2`, `cloud_cover_okta` |
| `active_station_count` | — | INFO | Number of distinct reporting stations |

#### sovon_tbl

| Metric | Threshold | Status | Description |
|--------|-----------|--------|-------------|
| `total_rows` | — | INFO | Row count |
| `species_count` | — | INFO | Distinct species |
| `year_coverage_pct` | 80% | PASS/WARN | % of years from min to max that have data |
| `non_null_pct_dead_bird_count` | 90% | PASS/WARN | NULL rate for mortality counts |
| `non_null_pct_n_total` | 90% | PASS/WARN | NULL rate for total counts |
| `target_species_coverage` | 40% | PASS/WARN | % of target species with mortality data |
| `avg_month_coverage_per_species` | — | INFO | Mean distinct months per species (out of 12) |

### 2. Timeliness

Measures data freshness and pipeline health.

| Table | Metric | Threshold | Status | Description |
|-------|--------|-----------|--------|-------------|
| All 3 | `data_freshness_hours` | — | INFO | Hours since most recent event timestamp |
| All 3 | `freshness_score` | 90 | PASS/WARN | Decay from 100 based on staleness vs table-specific threshold (BW: 48h, KNMI: 24h, Sovon: 730h) |
| `birdweather_tbl` | `active_station_pct_7d` | 60% | PASS/WARN | % of stations that reported in the last 7 days |
| `knmi_tbl` | `observation_regularity_pct` | 90% | PASS/WARN | % of consecutive observations that are exactly 10 minutes apart |

### 3. Accuracy

Range checks and coordinate validation.

#### birdweather_tbl

| Metric | Threshold | Status | Description |
|--------|-----------|--------|-------------|
| `confidence_in_range_pct` | — | INFO | % with confidence ∈ [0, 1]. CHECK constraints have been removed in dev — kept as a descriptive metric. |
| `coords_in_netherlands_pct` | — | INFO | % of detections inside NL bounding box (50.75–53.70°N, 3.33–7.22°E). Computed by joining the detection's station and reading the station's coordinates. Geographic relevance, not quality. |
| `confidence_mean` | — | INFO | Descriptive stats: mean, median, p5, p95 |
| `confidence_outlier_pct` | — | INFO | % with \|z-score\| > 3 |

#### knmi_tbl

| Metric | Threshold | Status | Description |
|--------|-----------|--------|-------------|
| `{param}_in_range_pct` (×8) | 98% | PASS/WARN | Physical range checks: temperature (−50–50°C), humidity (0–100%), wind (0–75 m/s), direction (0–360°), pressure (870–1085 hPa), precipitation (0–200 mm), radiation (0–2000 W/m²), cloud cover (0–9 okta) |
| `coords_in_netherlands_pct` | — | INFO | % of stations inside NL bounding box. Geographic relevance, not quality. |
| `temperature_outlier_pct` | — | INFO | % with \|z-score\| > 3 |

#### sovon_tbl

| Metric | Threshold | Status | Description |
|--------|-----------|--------|-------------|
| `non_negative_count_pct` | 99% | PASS/FAIL | % with `dead_bird_count ≥ 0` |
| `year_in_range_pct` | 99% | PASS/WARN | % with year between 2000 and current year |

### 4. Consistency

Cross-field logical checks and cross-table agreement.

| Table | Metric | Threshold | Status | Description |
|-------|--------|-----------|--------|-------------|
| `birdweather_tbl` | `audio_start_before_end_pct` | 99% | PASS/WARN | % where `audio_start_time_s < audio_end_time_s` |
| `birdweather_tbl` | `date_timestamp_match_pct` | 95% | PASS/WARN | % where `observation_date = detected_at::date` |
| Cross-table | `bw_knmi_date_overlap_pct` | 70% | PASS/WARN | % of BirdWeather detection days that also have KNMI weather data |
| Cross-table | `bw_sovon_species_overlap` | — | INFO | Count of species shared between BirdWeather and Sovon |
| Cross-table | `sovon_species_in_bw_pct` | 50% | PASS/WARN | % of Sovon species also detected in BirdWeather |

### 5. Uniqueness

Duplicate detection on natural keys.

| Table | Metric | Threshold | Status | Description |
|-------|--------|-----------|--------|-------------|
| `birdweather_tbl` | `detection_id_uniqueness_pct` | — | INFO | Enforced by PRIMARY KEY — always 100%. |
| `birdweather_tbl` | `near_duplicate_pct_14d` | — | INFO | % of detections within 3s of the same species at the same station (last 14 days) |
| `knmi_tbl` | `natural_key_uniqueness_pct` | 99% | PASS/WARN | Uniqueness of `(station_id, observed_at)` |
| `sovon_tbl` | `natural_key_uniqueness_pct` | 99% | PASS/WARN | Uniqueness of `(species_english, year, month)` |

### 6. Validity

Domain and constraint conformance.

| Table | Metric | Threshold | Status | Description |
|-------|--------|-----------|--------|-------------|
| `sovon_tbl` | `valid_month_pct` | — | INFO | Enforced by CHECK constraint — always 100%. |
| `birdweather_tbl` | `station_country_netherlands_pct` | — | INFO | % with `station_country = 'Netherlands'`. Geographic relevance, not quality. |
| `birdweather_tbl` | `eclipse_populated_pct` | — | INFO | % of rows with eclipse flag populated |
| `knmi_tbl` | `date_timestamp_match_pct` | 95% | PASS/WARN | % where `observation_date = observed_at::date` |
| All 3 | `schema_column_match_pct` | 100% | PASS/FAIL | Schema drift — all expected columns exist |

---

## Per-Station Profiling (CSV Export)

The audit also produces a per-station CSV (`data_quality_stations.csv`) with
detailed metrics for every BirdWeather and KNMI station:

**BirdWeather stations:** `total_detections`, `days_active`, `days_span`,
`temporal_coverage_pct`, `species_count`, `species_diversity` (Shannon H′),
`mean_confidence`, `median_confidence`, `is_within_nl_bbox`,
`audio_metadata_valid_pct`, `processed_clip_pct`, `near_dup_pct`,
`days_since_last`, `freshness_status`.

**KNMI stations:** `total_detections`, `days_active`, `days_span`,
`temporal_coverage_pct`, `completeness_pct` (avg non-null rate across key met
params), `days_since_last`, `freshness_status`.

## CLI Usage

```bash
# Run full audit (writes to DB + exports CSVs)
python scripts/ops/data_quality.py

# Custom output directory
python scripts/ops/data_quality.py --output data/quality

# Quality gate — exit non-zero if any table < 70
python scripts/ops/data_quality.py --fail-under 70

# Limit display to one table
python scripts/ops/data_quality.py --table birdweather_tbl

# Custom target species config
python scripts/ops/data_quality.py --species-file config/target_species.json

# Skip database persistence (CSV only)
python scripts/ops/data_quality.py --no-store

# Verbose logging
python scripts/ops/data_quality.py -v
```

## Programmatic API

```python
from avian_biosurveillance.data.quality import (
    run_audit,
    store_results,
    QualityReport,
    StationMetrics,
)
from avian_biosurveillance.db import get_pool

pool = get_pool()

# Run the audit
report, station_metrics = run_audit(pool)

# Inspect results
for table in ["birdweather_tbl", "knmi_tbl", "sovon_tbl"]:
    print(f"{table}: {report.composite_score(table):.1f}")

# Store to database (writes to data_quality_tbl)
run_id = store_results(pool, report, station_metrics)
```

## CSV Outputs

| File | Content |
|------|---------|
| `data_quality_summary.csv` | Per-table dimension scores (0–100) + composite |
| `data_quality_details.csv` | Every metric with value, threshold, status, detail |
| `data_quality_issues.csv` | FAIL + WARN items only |
| `data_quality_stations.csv` | Per-station quality profile |

## Design Decisions

1. **INFO-only for constraint-enforced checks** — Metrics like
   `confidence_in_range_pct` (CHECK constraint), `detection_id_uniqueness_pct`
   (PRIMARY KEY), and `valid_month_pct` (CHECK constraint) will always be 100%.
   They still run as smoke tests but don't inflate dimension scores.

2. **INFO-only for geographic checks** — `coords_in_netherlands_pct` and
   `station_country_netherlands_pct` measure geographic relevance, not data
   quality. A station in Germany with perfect data should not be penalized.
   Filtering by study area is done downstream, not in the quality layer.

3. **Station quality in SQL, not Python** — Per-station composite scores are
   computed by `refresh_station_summary()` using only three metrics (temporal
   coverage, confidence, freshness) that directly affect ML pipeline
   reliability. The Python module still runs detailed per-station profiling
   for CSV export.

4. **Weighted composite with skip** — If a dimension has zero scoreable
   metrics for a table, it's excluded from the weighted average rather than
   dragging the score to zero. This prevents sparse tables (e.g., Sovon with
   no accuracy checks) from being unfairly penalized.

5. **Centralised species registry** — Target species are defined once in
   `config/target_species.json` with all cross-system identifiers (English,
   Dutch, scientific, Sovon ID, BirdWeather ID). The shared loader at
   `avian_biosurveillance.config.species` provides typed accessors. No
   module should hardcode its own species list.



---

Refresh station data inside docker:

```bash
./infrastructure/docker/compose.sh exec -T timescaledb bash -lc \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
CALL refresh_continuous_aggregate('birdweather_daily_cagg_tbl', NULL, NULL);
SELECT refresh_station_summary();
SQL
```