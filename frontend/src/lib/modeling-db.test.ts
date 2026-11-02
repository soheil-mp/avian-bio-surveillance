import { beforeEach, describe, expect, it, vi } from "vitest";

const queryMock = vi.fn();

vi.mock("server-only", () => ({}));
vi.mock("@/lib/db", () => ({
  query: (...args: unknown[]) => queryMock(...args),
}));

import {
  loadFeaturePayloadsForSpecies,
  loadLatestModelPerformanceJson,
  loadLatestModelingRun,
  loadLatestModelingScorePayloads,
  loadWeeklyAnomalyScoreForDetection,
  payloadToStringRecord,
  resetLatestModelingRunCacheForTests,
} from "./modeling-db";

describe("modeling DB helpers", () => {
  beforeEach(() => {
    queryMock.mockReset();
    resetLatestModelingRunCacheForTests();
  });

  it("normalizes payload values into CSV-compatible strings for alerts", () => {
    expect(
      payloadToStringRecord({
        period_start: new Date("2026-03-02T00:00:00Z"),
        top_detection_ids: ["det-1", "det-2"],
        nested: { source: "test" },
        final_score: 0.91,
      }),
    ).toEqual({
      period_start: "2026-03-02",
      top_detection_ids: "det-1,det-2",
      nested: "{\"source\":\"test\"}",
      final_score: "0.91",
    });
  });

  it("loads active score payloads with the latest run envelope", async () => {
    queryMock
      .mockResolvedValueOnce({
        rows: [
          {
            run_id: "run-1",
            run_kind: "anomaly_scoring",
            generated_at: "2026-03-09T00:00:00Z",
            config: { min_observation_days: 3 },
            stats: { rows_scored: 1 },
            source_coverage: {},
            validation_report: null,
            quality_report: null,
          },
        ],
      })
      .mockResolvedValueOnce({
        rows: [
          {
            payload: {
              species_id: "35",
              period_start: "2026-03-02",
              top_detection_ids: ["det-1"],
            },
          },
        ],
      });

    const result = await loadLatestModelingScorePayloads();

    expect(result.run?.run_id).toBe("run-1");
    expect(result.rows).toEqual([
      {
        species_id: "35",
        period_start: "2026-03-02",
        top_detection_ids: "det-1",
      },
    ]);
    // Scores come from the active score layer, so species-specific reruns do
    // not hide other species that were scored by older runs.
    const scoreSql = String(queryMock.mock.calls[1][0]);
    expect(scoreSql).toContain("active_score_rows AS");
    expect(scoreSql).toContain("SELECT DISTINCT ON");
    expect(scoreSql).toContain("modeling_species_week_scores_tbl scores");
    expect(scoreSql).not.toContain("WHERE run_id = $1");
    expect(queryMock.mock.calls[1][1]).toBeUndefined();
  });

  it("loads detection anomaly enrichment from the active score layer", async () => {
    queryMock.mockResolvedValueOnce({ rows: [{ score: "0.82" }] });

    await expect(loadWeeklyAnomalyScoreForDetection("35", "det-1")).resolves.toBe(
      0.82,
    );
    const scoreSql = String(queryMock.mock.calls[0][0]);
    expect(scoreSql).toContain("active_score_rows AS");
    expect(scoreSql).toContain("FROM active_score_rows scores");
    expect(queryMock.mock.calls[0][1]).toEqual(["35", "det-1"]);
  });

  it("resolves the newest scored anomaly run for run-level provenance", async () => {
    queryMock.mockResolvedValueOnce({
      rows: [
        {
          run_id: "run-aggregate-1",
          run_kind: "anomaly_scoring",
          generated_at: "2026-06-02T09:02:21Z",
          config: null,
          stats: { rows_scored: 193 },
          source_coverage: null,
          validation_report: null,
          quality_report: null,
        },
      ],
    });

    await expect(loadLatestModelingRun()).resolves.toMatchObject({
      run_id: "run-aggregate-1",
      run_kind: "anomaly_scoring",
    });
    const sql = String(queryMock.mock.calls[0][0]);
    expect(sql.trim().startsWith("WITH scored_runs AS (")).toBe(true);
    expect(sql).toContain("modeling_species_week_scores_tbl");
    expect(sql).toContain("runs.status = 'complete'");
    expect(sql).toContain("MAX(COALESCE(period_end, period_start)) AS latest_scored_period_end");
    expect(sql).toContain("ORDER BY");
    expect(sql).toContain("scored_runs.latest_scored_period_end DESC NULLS LAST");
    expect(sql).toContain("runs.generated_at DESC");
    expect(sql).not.toContain("anomaly_scoring_window");
  });

  it("loads model performance from the latest run that actually has a validation report", async () => {
    queryMock.mockResolvedValueOnce({
      rows: [
        {
          run_id: "run-validation-1",
          run_kind: "anomaly_scoring",
          generated_at: "2026-06-02T11:02:29Z",
          config: null,
          stats: { rows_scored: 228 },
          source_coverage: null,
          validation_report: { report_version: "weak_validation_v1" },
          quality_report: null,
        },
      ],
    });

    await expect(loadLatestModelPerformanceJson()).resolves.toBe(
      JSON.stringify({ report_version: "weak_validation_v1" }),
    );

    const sql = String(queryMock.mock.calls[0][0]);
    expect(sql).toContain("runs.validation_report IS NOT NULL");
    expect(sql).toContain("modeling_species_week_scores_tbl");
    expect(sql).toContain("scored_runs.latest_scored_period_end DESC NULLS LAST");
    expect(sql).toContain("runs.generated_at DESC");
  });

  it("loads species tracker modeling feature payloads", async () => {
    queryMock
      // loadFeaturePayloadsForSpecies reads the active feature layer directly
      // and selects typed columns (no JSONB payload), so rows come back as-is.
      .mockResolvedValueOnce({
        rows: [{ species_id: "35", total_detections: 12 }],
      });

    await expect(loadFeaturePayloadsForSpecies("35")).resolves.toEqual([
      { species_id: "35", total_detections: 12 },
    ]);

    // The feature query is keyed only by species_id and reads typed columns
    // from the active feature layer.
    const featureSql = String(queryMock.mock.calls[0][0]);
    expect(featureSql).toContain("active_feature_rows AS");
    expect(featureSql).toContain("modeling_species_week_features_tbl");
    expect(featureSql).not.toContain("features.payload");
    expect(queryMock.mock.calls[0][1]).toEqual(["35"]);
  });
});
