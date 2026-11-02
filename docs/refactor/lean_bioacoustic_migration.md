# Lean bioacoustic-primary refactor

Status: **cutover complete & build-verified** (Phases 0–4 done; Phase 5 partial —
old tables retired to `deprecated_*`, final drop + dead-module deletion pending).
This supersedes the original fusion-based architecture. Decisions are settled by the 2-year
`retro_full_2024_2025_v3` Phase-1 evaluation (`evaluation_runs/phase1/retro_full_2024_2025_v3/`).

## Why
The 2-year evaluation showed:
- **Fusion (IsolationForest blend) adds no value** and underperforms plain
  bioacoustic drift everywhere; it is also the source of run-to-run variance.
- **Population is real but redundant** with bioacoustic (same species, same
  lags, weaker on mortality: best ~0.34 vs bioacoustic's 0.75) — and the fusion
  that blends them does worse than bioacoustic alone.
- **Bioacoustic drift (top-decile) is the strongest signal** and the one to
  build around. (Honest caveat retained: the mortality lead is largely
  *seasonal* — it shows in the control species too — so this is an acoustic
  activity-anomaly tracker that leads regional bird mortality by ~2 months, not a
  validated cause-specific early warning.)
- The **`run_id` concept is overkill** for a single-scoring system.

## Target schema (shipped)
One lean weekly table, no `run_id`:

    species_week_anomaly_tbl
      (anomaly_row_id, species_id, species_english, location_type, location_id,
       grid_cell_id, period_start, period_end, drift_score, outlier_share,
       clip_count, alert_level, top_detection_ids, payload, computed_at)
      UNIQUE (species_id, location_id, period_start)

`alert_level` uses the **outlier-share rule with a clip guardrail** (not fusion
thresholds, and not raw drift quantiles — see findings): amber `outlier_share>=0.50`,
red `>=0.90`, only when `clip_count>=5`.

Mortality evidence is **reference data**, decoupled from runs:
- `sovon_tbl` — SOVON mortality (was already a permanent reference; survives).

Per-clip embedding map: the dormant `birdweather_tbl` columns
(`perch_v2_pc1/pc2/perch_v2_alert_score`) are now **populated** for the recent
window (see Phase 4).

## What shipped (verified)
- **Lean table backfilled** from `v3` (106,377 rows, exact 1:1, no dupes/gaps).
- **Parity verified**: the lean scoring pipeline (`modeling/lean_pipeline.py`,
  reusing `_compute_embedding_stage_streamed` verbatim) is **bit-identical** to
  v3 on the bioacoustic columns (Aug-2024 spot check: 0 drift/outlier/clip
  mismatches), and the lean table reproduces v3 with **0 mismatches** for
  July-2025 (5,537 cell-weeks).
- **Embedding projection** (`modeling/embedding_projection.py`): rewrote the
  fetch to parse into a preallocated `float32` matrix (the old list-of-lists
  OOM'd on a single month at dim 1536) and added a `--since` window bound. Ran
  `--backend perch_v2 --since 2025-11-01`: **963,942 coordinates** written
  (PCA+kNN in ~40s). Full-history projection still needs a chunked/incremental
  transform (the corpus is ~10M vectors); the recent window is what the map shows.
- **Frontend cutover**: `lib/modeling-db.ts` rewritten (no `run_id`/`latestRun`
  machinery; reads `species_week_anomaly_tbl` directly; clusters
  → empty; model-performance → static-artifact fallback). `activeScoreRowsCte`
  keeps compatibility aliases so old score names map to `drift_score` and
  population columns surface as NULL. All raw-SQL consumers repointed
  (`alerts/hotspots`, `grid-cell/overview`, `species-tracker`,
  `home-live-detections`, `alerts/embedding-detail`). The alerts `payload` now
  has population keys stripped and fusion names mapped to drift.
- **Verification**: `tsc --noEmit` clean; **full `next build` clean** (compiled +
  23/23 static pages generated, which executes the repointed queries for
  pre-rendered pages). Zero references to the old tables remain in the frontend.

## Dropped / retired
- **Tables** renamed to `deprecated_*` (reversible; drop after final UI QA):
  `modeling_species_week_scores_tbl`, `modeling_runs_tbl`,
  `modeling_species_week_features_tbl`, `modeling_embedding_clusters_tbl`.
  Final drop:

      DROP TABLE deprecated_modeling_species_week_scores_tbl,
        deprecated_modeling_runs_tbl, deprecated_modeling_species_week_features_tbl,
        deprecated_modeling_embedding_clusters_tbl;

- **Pipeline stages**: IsolationForest fusion (`anomaly_model`), population
  baseline (`count_baseline`), embedding clusters, scene control — no longer on
  the lean path.
- The `run_id` parameter / "latest run" machinery.

## Phased plan
- [x] **Phase 0 — recon** (schema, read contract, settings).
- [x] **Phase 1 — additive lean table + backfill** (`V008` DDL +
      `scripts/ops/backfill_lean_anomaly.py`). Validated.
- [x] **Phase 2 — eval/parity on the lean table** (0-mismatch vs v3).
- [x] **Phase 3 — lean scoring pipeline** (`modeling/lean_pipeline.py`,
      parity-validated bit-identical to v3).
- [x] **Phase 4 — frontend cutover** (`modeling-db.ts` + routes repointed; tsc +
      full build clean) and per-clip embedding projection populated.
- [~] **Phase 5 — drop old tables + dead modules + docs.** Tables retired to
      `deprecated_*`; this doc updated. **Remaining:** see below.

## Remaining work
1. **UI lean-polish (needs dev server).** The dropped signals are still rendered
   by deeply interwoven components — `AlertsClient.tsx` (~4,600 lines; clusters
   are a whole *view mode* with grouping/indexing/palette, eval defaults to
   `"fusion"`) and `SpeciesTracker.tsx` (~3,800 lines). The data layer already
   feeds these null/empty (defensively typed, so most should self-hide), so this
   is **cosmetic**. Do it against `npm run dev` (visual verification), not blind.
   Candidate first cuts: drop the `cluster` view-mode entry point; default the
   evaluation selector to a bioacoustic signal instead of `"fusion"`; remove the
   model-performance view (its data source is now null).
2. **Dead Python module deletion — needs a decouple first.** `lean_pipeline.py`
   imports `_compute_embedding_stage_streamed` / `_coerce_date` /
   `_effective_target_species_ids` from `pipeline.py`, which top-level-imports the
   dead `anomaly_model` / `count_baseline`. Extract those 3 helpers (+ their
   internal deps) into a standalone module, repoint `lean_pipeline`, re-run the
   1-month parity to confirm bit-identical, *then* delete `pipeline.py`,
   `anomaly_model.py`, `count_baseline.py`, `db/loaders/modeling.py`,
   `ops/run_modeling_backfill.py`. (Deferred deliberately: the modules are
   dormant; the extraction risks the parity-validated core for no functional gain
   until done carefully + re-validated.)
3. **`lean_pipeline` context attachment (future-only).** Future re-scorings
   produce a slim payload; the live dashboard reads mortality context from the
   (rich) backfilled payload. Before any re-score, have `lean_pipeline` merge
   SOVON context into its payload (or switch the dashboard to join context at
   read time).
4. **Finalize Phase 5.** After UI QA: run the `DROP TABLE deprecated_*` above and
   delete the `.bak` files (`lib/modeling-db.ts.bak`,
   `modeling/embedding_projection.py.bak`).

## Phase 1 findings (informing the alert rule)
- **`embedding_drift_score` is near-binary/saturated**: a mass at 0 (37% of
  rows) and most non-zero values pinned near 1.0 (median 0.965 among drift>0;
  p95=p99=1.0). It cannot grade alerts — a calibration limitation noted honestly;
  deeper recalibration is out of refactor scope.
- **`outlier_share` is the graded magnitude** and is what alerts/ranking use —
  but it also saturates for sparse cell-weeks (one anomalous clip ⇒ 1.0), so a
  **minimum clip-count guardrail is mandatory**.
- Implemented rule (`backfill_lean_anomaly.py` and `lean_pipeline.py`): amber if
  `outlier_share>=0.50`, red if `>=0.90`, only when `clip_count>=5`. ~1.5% alert
  rate (amber 1462 / red 162 / green 104753 on v3).

## Risks / notes
- Frontend depends heavily on `payload` JSONB; the lean table carries the full
  backfilled `payload` so the cutover is column-compatible (the alerts loader
  overlays canonical bioacoustic columns and strips dead population keys).
- Alert recomputation changes alert counts vs the pre-refactor numbers (expected).
- Per-species alert thresholds (not pooled) — consistent with how v3 was scored.
