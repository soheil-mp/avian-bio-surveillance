# Roadmap

The platform works end to end: it ingests public data, scores weekly acoustic
anomalies, and serves a dashboard. The main problem is that it is
**over-engineered for what it does**. A read-mostly dashboard with weekly
scoring currently needs 11 Compose services, a database server, an object
store, and a multi-process orchestrator. That makes it heavy to run, slow to
change, and hard for anyone else to reproduce.

This roadmap has two goals:

1. **Simplify the stack** by swapping heavyweight tools for lighter ones.
2. **Make it ready to deploy for a larger user base.**

## Where the weight comes from

| Today | Why it is too much |
|---|---|
| TimescaleDB / PostgreSQL with hypertables, continuous aggregates, and large SQL refresh routines | A database server, credentials, and migrations for data that is written in batches and mostly read |
| MinIO object storage plus a `minio-init` container | A second storage service and credential set for files that could live on disk |
| Dagster webserver, daemon, two gRPC code servers, and Postgres run storage | Four processes to schedule a handful of daily, weekly, and monthly jobs |
| Init and sidecar containers (`timescaledb-init`, `minio-init`, `dagster-home-init`, `autoheal`) | Extra startup ordering and failure modes |
| Two deployment paths (Docker Compose and Singularity) with separate scripts | Every change has to be kept in sync twice |
| Dormant modules and tables left over from the lean refactor | Code that still has to be read, built, and tested |

## Target architecture

Two containers and one data volume:

```
┌──────────────────────────────┐        ┌──────────────────────────┐
│ pipeline                     │        │ web                      │
│ ingest → process → embed →   │  data/ │ Next.js dashboard        │
│ score, on a schedule         │ ─────► │ read-only queries        │
│ the only writer              │        │ stateless, replicable    │
└──────────────────────────────┘        └──────────────────────────┘
          data/ = Parquet lake + DuckDB file + audio and image files
```

| Concern | Planned replacement |
|---|---|
| Tabular storage | **DuckDB over Apache Parquet**, partitioned by source and date |
| Audio, spectrograms, waveforms | **Plain files on the data volume** (no object storage service) |
| Scheduling | A single-process scheduler inside the pipeline container |
| Deployment | The same two images under Docker and Apptainer |

## Phase 0 — Clean up before migrating

- [ ] Finish the lean refactor in `docs/refactor/lean_bioacoustic_migration.md`:
      extract the helpers `lean_pipeline.py` still imports from `pipeline.py`,
      then delete the dormant fusion and population modules and their loaders.
- [ ] Drop the `deprecated_*` tables and the run-envelope machinery they need.
- [ ] Remove frontend panels whose data source no longer exists (cluster view,
      model-performance view, spatial evaluation panel).
- [ ] Pin down a parity baseline (row counts, sample queries, one scoring run)
      to check each later phase against.

## Phase 1 — PostgreSQL → DuckDB + Parquet

- [ ] Define the Parquet layout, for example `data/lake/<source>/year=YYYY/month=MM/`.
- [ ] Port the schema in `db/baseline/`, `db/caggs/`, and `db/routines/` to
      DuckDB SQL, replacing TimescaleDB-only features (hypertables, continuous
      aggregates, `time_bucket`) with views or precomputed Parquet tables.
      About 37 files use TimescaleDB-specific SQL today.
- [ ] Rewrite the Python loaders that use `psycopg2` (about 34 modules) to
      append Parquet partitions and refresh derived tables.
- [ ] Repoint the frontend queries (about 19 files use the `pg` client) at
      DuckDB in read-only mode.
- [ ] Export the existing PostgreSQL data to Parquet once and verify it against
      the parity baseline.
- [ ] Respect DuckDB's single-writer rule: the pipeline writes, and the web
      tier only reads published snapshots.

## Phase 2 — Drop the object storage

- [ ] Make the existing filesystem mode of the storage client
      (`MINIO_LOCAL_ROOT` in `src/avian_biosurveillance/db/minio_client.py`) the default, move the
      buckets to `data/audio/` and `data/features/`, then remove the MinIO
      client.
- [ ] Replace presigned MinIO URLs in the frontend (`lib/minio.ts` and its
      three consumers) with an API route that streams files from the data
      volume.
- [ ] Remove the `minio` and `minio-init` services and their credentials.

## Phase 3 — Squash the stack into two containers

- [ ] **pipeline image:** merge the ingestion, model, and Dagster images into
      one, with an optional GPU build argument for embeddings.
- [ ] **web image:** the existing Next.js standalone build.
- [ ] Replace the Dagster webserver, daemon, and code servers with a
      single-process scheduler (Dagster with SQLite storage, or plain cron);
      decide based on how much of the Dagster UI is still used.
- [ ] Fold the init containers into entrypoint steps and drop `autoheal` in
      favour of container health checks.
- [ ] Collapse `docker-compose.yml` to two services and one volume, and run the
      same images under Apptainer instead of maintaining separate scripts.

## Phase 4 — Ready for a larger user base

The dashboard is read-only, so it can scale horizontally once it no longer
depends on a live database server.

- [ ] **Immutable data snapshots:** each pipeline run publishes a versioned
      snapshot (Parquet + DuckDB) and switches to it atomically, so web
      replicas never read half-written data.
- [ ] **Stateless web tier:** run several web replicas behind a reverse proxy
      or load balancer, with HTTPS terminated at the proxy.
- [ ] **Precompute expensive views:** build alerts, hotspots, and species
      tracker payloads at pipeline time so requests become cheap lookups.
- [ ] **Caching and CDN:** set cache headers on API routes and serve audio
      clips, spectrograms, and waveforms through a CDN or static host.
- [ ] **Protection:** rate limits, request timeouts, and pagination caps on
      every API route; keep the pipeline and scheduler UI off the public
      internet.
- [ ] **Privacy:** never serve clips that the background-sound classifier tags
      as speech.
- [ ] **Observability:** structured logs, request metrics (building on the
      existing performance module), a health endpoint, and error tracking.
- [ ] **Load testing:** set target concurrency and latency budgets, then test
      against them before each release.
- [ ] **CI/CD:** build and test both images on every push, tag releases, and
      deploy from tagged images.

## Open questions

- Is the Dagster UI worth keeping, or is a simpler scheduler with log files
  enough?
- At larger scale, should the dashboard query Parquet in the browser
  (DuckDB-WASM on static hosting) instead of through server routes?
- Where should audio be hosted once traffic grows: the data volume behind a
  CDN, or a static file host?

## Guardrails

- Keep using public data sources only.
- Every phase must reproduce the parity baseline before the old component is
  removed.
- Keep the dashboard usable throughout; migrate one data source at a time.
