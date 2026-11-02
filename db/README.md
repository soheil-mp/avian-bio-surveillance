# Database — Avian Biosurveillance

All SQL, schema migrations, role provisioning, and data-model assets for the
TimescaleDB / PostgreSQL database live here. The folder is the **single source
of truth**: both Docker (`infrastructure/docker/docker-compose.yml`) and
Singularity (`infrastructure/singularity/start.sh`) bootstrap the database by
running `bootstrap/init_database.sh` against this tree.

> Companion documents
> - [`CHANGELOG.md`](CHANGELOG.md) — human-readable schema history
> - [`permissions.md`](permissions.md) — role matrix + grant rationale
> - [`Makefile`](Makefile) — `make bootstrap | erd | lint | psql`

---

## Folder Layout

| Folder | Purpose | Load order |
|---|---|---|
| [`bootstrap/`](bootstrap/) | Bootstrap entry points: `init_database.sh`, the `ensure_aux_roles.sh` connection wrapper, and SQL for aux-DB provisioning / legacy-role cleanup. | step 1 |
| [`functions/`](functions/) | Helper functions such as `fn_point_to_cell`. The current function set is loaded **after** `baseline/` because it depends on `grid_cells_tbl`. | step 3 |
| [`baseline/`](baseline/) | Canonical, idempotent DDL split by dependency/domain: extensions first, then source tables, operational tables, and derived/cache tables. | step 2 |
| [`migrations/`](migrations/) | Versioned, one-way migrations reserved for data backfills or destructive changes that cannot safely live in idempotent baseline DDL. Tracked in `schema_migrations_tbl`; each file is applied at most once per database. See [`migrations/README.md`](migrations/README.md). | step 4 |
| [`triggers/`](triggers/) | Cross-table triggers — function + binding co-located in one file per trigger. | step 5 |
| [`routines/`](routines/) | **Stateful refresh procedures** called by Dagster (`refresh_station_summary`, `refresh_species_dim`, `refresh_movebank_tracks`, `refresh_species_tracker`). Distinct from `functions/`: these are jobs, not reusable helpers. | step 6 |
| [`caggs/`](caggs/) | TimescaleDB continuous aggregates + their refresh / retention policies. | step 7 |
| [`views/`](views/) | Read-only convenience views. | step 8 |
| [`grants/`](grants/) | Reserved home for object-level grants; currently a no-op because the local/dev stack uses one owning login role. | step 9 |
| [`seeds/`](seeds/) | Opt-in deterministic SQL seed/reference data. Not part of default bootstrap. | manual |
| [`erd/`](erd/) | SQL-generated ERD (`data_model.drawio`) plus its stdlib generator (`generate_erd.py`). Regenerate with `make erd`. |  — |

The bootstrap orchestrator is **the** load-order document; the table above
mirrors it.

---

## Bootstrap

### Single source of truth

`bootstrap/init_database.sh` is run by:

| Caller | When |
|---|---|
| `infrastructure/docker/docker-compose.yml` → service `timescaledb-init` | every container boot |
| `infrastructure/singularity/start.sh` → `run_init` | every `start.sh` invocation |

The script is fully idempotent (`CREATE … IF NOT EXISTS`, `CREATE OR REPLACE`,
`schema_migrations_tbl` for one-shot migrations). The `timescaledb` service /
SIF intentionally has **no** `/docker-entrypoint-initdb.d/` mounts — the
sidecar is the only writer of schema. This eliminates the previous
fresh-volume vs replayed-volume drift.

### Migrations

A migration is any one-way change that should run **once per database** and
does not fit cleanly in the always-replayed baseline files:

1. Add a new file `db/migrations/V<NNN>__short_name.sql` (zero-padded, sortable).
2. Make the SQL idempotent if possible (`ADD COLUMN IF NOT EXISTS`, …) so a
   replayed apply during testing is harmless.
3. The bootstrap runs the migration and the ledger insert in one transaction,
   then records it in `schema_migrations_tbl` (`version`, `applied_at`,
   `filename`).
4. Update [`CHANGELOG.md`](CHANGELOG.md).

Prefer `baseline/` for schema that should exist on every fresh install and can
be expressed idempotently (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD
COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`). Reserve `migrations/`
for legacy catch-up, data backfills, or destructive one-way DDL.

Files that change current schema **shape** (add/rename/remove a table or
column) also need an ERD regeneration: `make erd`.

---

### Seeds and Reference Data

Default bootstrap creates schema only. Use [`seeds/`](seeds/) for small,
deterministic SQL reference/demo rows that are safe to load explicitly and
re-run. Keep production-like observational data in ingestion or repair scripts;
if a data fix must be ledger-backed, place it in [`migrations/`](migrations/)
instead.

---

## Conventions

### Naming

| Object kind | Suffix | Example |
|---|---|---|
| Base / hyper table | `_tbl` | `birdweather_tbl`, `cds_drought_tbl` |
| Continuous aggregate (TimescaleDB) | `_cagg_tbl` | `birdweather_daily_cagg_tbl` |
| Read-only convenience view | `_v` | `cds_drought_v` |
| Index | `idx_<3-letter-prefix>_*` | `idx_bwt_station_id`, `idx_gct_system_row_col` |

**Index prefix is mandatory.** Stable 3-letter prefixes: `birdweather_tbl` →
`bwt`, `knmi_tbl` → `knt`, `grid_cells_tbl` → `gct`,
`google_earth_engine_tbl` → `geeg`, `cds_drought_tbl` → `cdt`,
`data_quality_tbl` → `dqt`, …

**Exception:** `google_earth_engine_grid_tbl` is a *view* but keeps the `_tbl`
suffix for backward compatibility with ~50 callers across SQL/Python/TS. New
views should use `_v`.

### Idempotency

Every file in `bootstrap/`, `functions/`, `baseline/`, `triggers/`,
`routines/`, `caggs/`, `views/`, and `grants/` must be safe to re-apply. Use
`IF NOT EXISTS`, `CREATE OR REPLACE`, `DROP … IF EXISTS` + `CREATE`, or
`ALTER … IF NOT EXISTS`. Files in `migrations/` may use one-way DDL
(`UPDATE … SET …`, `DROP COLUMN`) because they apply at most once.

### File numeric prefixes

Inside each subfolder, `0NN_*.sql` enforces local apply order. Prefixes are
**folder-local** — `010` in `functions/` runs in step 2, while `010` in
`caggs/` runs in step 7. The folder is the unit of order.

Current `baseline/` order (one table per file):

| File | Scope | Class |
|---|---|---|
| `010_extensions.sql` | TimescaleDB extension | infra |
| `020_birdweather.sql` | BirdWeather source hypertable | source |
| `030_knmi.sql` | KNMI weather hypertable | source |
| `040_sovon.sql` | Sovon mortality table | source |
| `050_ebird.sql` | eBird observation hypertable | source |
| `060_movebank.sql` | Movebank telemetry hypertable | source |
| `070_grid_cells.sql` | Canonical grid registry | source |
| `080_google_earth_engine.sql` | Canonical grid-cell GEE facts | source |
| `090_cds_drought.sql` | CDS drought hypertable | source |
| `100_seismic.sql` | KNMI FDSN earthquake catalogue | operational |
| `110_birdweather_stations.sql` | BirdWeather station reference + summary | operational |
| `120_data_quality.sql` | Quality audit log (summary + metric rows) | operational |
| `130_species.sql` | Canonical species reference + dashboard cache | derived |
| `140_movebank_tracks.sql` | Pre-decimated per-individual GPS tracks | derived |
| `150_species_tracker.sql` | Unified species tracker analytics surface | derived |
| `160_modeling.sql` | Persisted modeling runs, scores, clusters, and tuning results | derived |

---

## Tables

| Table | Type | Grain |
|---|---|---|
| `birdweather_tbl` | Hypertable (1-day chunks) | 1 acoustic detection |
| `knmi_tbl` | Hypertable (1-day chunks) | 1 station × 10-min interval |
| `sovon_tbl` | Regular | 1 species × year × month |
| `ebird_tbl` | Hypertable (1-day chunks) | 1 eBird checklist observation (with merged hotspot data) |
| `movebank_tbl` | Hypertable (7-day chunks) | 1 GPS tracking event (with merged individual/tag/deployment data) |
| `google_earth_engine_tbl` | Regular | 1 canonical grid cell × date satellite profile |
| `grid_cells_tbl` | Regular | 1 canonical spatial grid cell (tessellation registry) |
| `google_earth_engine_grid_tbl` | **View** (legacy `_tbl` suffix) | Convenience join of facts × grid_cells |
| `cds_drought_tbl` | Hypertable (yearly chunks) | 1 0.25° grid cell × month × dataset/product variant |
| `cds_drought_v` | View | `cds_drought_tbl` enriched with the canonical cell polygon |
| `seismic_tbl` | Regular | 1 seismic event from KNMI catalog |
| `birdweather_stations_tbl` | Regular | 1 station (pre-aggregated stats + inline quality metrics) |
| `data_quality_tbl` | Regular | Quality audit (summary + metric rows per run) |
| `species_tbl` | Regular | Unified species reference, tracker cache, and PhyloPic cache |
| `species_tracker_tbl` | Regular (derived) | Unified period/location/species/community tracker surface with environmental covariates |
| `movebank_tracks_tbl` | Regular (derived) | Pre-computed Movebank track polylines |
| `modeling_runs_tbl` | Regular (derived) | 1 persisted modeling/evaluation run envelope |
| `modeling_species_week_features_tbl` | Regular (derived) | 1 weekly species/location feature row per modeling run |
| `modeling_species_week_scores_tbl` | Regular (derived) | 1 weekly species/location anomaly-score row per modeling run |
| `modeling_embedding_clusters_tbl` | Regular (derived) | 1 detection-level embedding cluster assignment per modeling run |
| `modeling_tuning_runs_tbl` | Regular (derived) | 1 modeling parameter sweep |
| `modeling_tuning_leaderboard_rows_tbl` | Regular (derived) | 1 sweep candidate leaderboard row |
| `schema_migrations_tbl` | Regular | Records which `migrations/V*.sql` files have been applied |

## Continuous Aggregates

| View | Refresh | Source |
|---|---|---|
| `birdweather_daily_cagg_tbl` | Hourly | `birdweather_tbl` |
| `knmi_daily_cagg_tbl` | Hourly | `knmi_tbl` |

## Retention & Compression

In the current development phase **no TimescaleDB compression or retention
policies are installed** on hypertables or continuous aggregates. Data
lifecycle is managed manually so the schema can evolve freely without
fighting compressed chunks. The previous 15-year retention horizon and
per-table compression segment-by/order-by knobs will be reintroduced
once the ingestion contract stabilises for production.

## Architecture Notes

- **No foreign keys between source tables** — joined spatially/temporally at query time, keeping ingestion decoupled.
- **Reference data merged inline (v9)** — Movebank reference data and eBird hotspot data are merged as flat columns; no separate reference tables.
- **Single local DB login** — Docker/Singularity, the ETL worker, the frontend, and Dagster all connect as `root` in the current local/dev setup.
- **`REVOKE CREATE ON SCHEMA public FROM PUBLIC`** — no role can create objects without explicit permission.
- **`birdweather_stations_tbl` uses UPSERT** — eliminates the zero-row window during refresh.
- **`birdweather_stations_tbl` includes trust and health metrics** — `station_trust_score` and `station_operational_health_score` are computed separately; `data_quality_score` is retained as a legacy alias to trust score.
- **`birdweather_stations_tbl.credibility_tier`** — `credible` / `provisional` / `unrated` based on detection volume and active days; only credible stations feed downstream anomaly scoring.
- **`GENERATED ALWAYS AS IDENTITY`** — modern identity columns (PG 10+) instead of SERIAL/BIGSERIAL.
- **No CHECK constraints in the baseline DDL** — value-range, enum, and coordinate-bound validation lives in the `data_quality` pipeline rather than as table-level constraints, so refresh routines can write transient/quarantined values without fighting hard schema rules.
- **Minimal, justified indexes** — only PK + read-path-justified secondary indexes are kept; chunk-time pruning replaces explicit time-axis indexes on hypertables.

See [`permissions.md`](permissions.md) for the full role and grant matrix.

## Audio Feature Columns

`birdweather_tbl` stores downstream audio enrichments produced after the daily
quality audit:

- `has_processed_clip` marks that the cropped FLAC clip exists in the
   `processed-audio` MinIO bucket.
- `birdnet_embedding` and `perch_v2_embedding` are pgvector columns populated
   by the feature-engineering asset when real embedding providers are
   configured.
- `background_sound_clf` stores the background-sound classification JSONB
   envelope.
- `feature_artifacts` records the MinIO object paths for spectrogram and
   waveform PNGs under the `features` bucket.

---

## Data-Model Diagram

`erd/data_model.drawio` is auto-generated by `erd/generate_erd.py`. The
generator parses the SQL files under `baseline/`, `caggs/`, and `views/`,
then derives layout groups from file order and query dependencies. Regenerate
after any schema change:

```bash
make erd          # or:  python db/erd/generate_erd.py
```

The generator is stdlib-only and writes `db/erd/data_model.drawio`. Commit the
regenerated file alongside the schema change in the same PR.
