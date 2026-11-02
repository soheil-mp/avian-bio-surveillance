import { describe, expect, it } from "vitest";

import {
  buildAlertsEvaluationTrendData,
  buildEvaluationAlignmentMetrics,
} from "./evaluation";

describe("buildAlertsEvaluationTrendData", () => {
  it("aggregates weekly anomaly counts and SOVON mortality per species", () => {
    const data = buildAlertsEvaluationTrendData({
      anomalyRows: [
        {
          species_id: "35",
          species_english: "Eurasian Blackbird",
          period_start: "2025-07-21",
          alert_level: "red",
          final_score: "0.9",
          sovon_dead_bird_count: "12",
          sovon_mortality_score: "0.8",
        },
        {
          species_id: "35",
          species_english: "Eurasian Blackbird",
          period_start: "2025-07-21",
          alert_level: "amber",
          final_score: "0.7",
          sovon_dead_bird_count: "12",
          sovon_mortality_score: "0.8",
        },
        {
          species_id: "61",
          species_english: "Song Thrush",
          period_start: "2025-07-21",
          alert_level: "green",
          final_score: "0.2",
        },
      ],
    });

    const blackbird = data.trendPoints.find((point) => point.species === "Eurasian Blackbird");
    const songThrush = data.trendPoints.find((point) => point.species === "Song Thrush");

    expect(blackbird?.rowCount).toBe(2);
    expect(blackbird?.anomalyCount).toBe(2);
    expect(blackbird?.redAnomalyCount).toBe(1);
    expect(blackbird?.sovonDeadBirdCount).toBe(12);
    expect(blackbird?.sovonMortalityScore).toBeCloseTo(0.8);
    expect(songThrush?.anomalyCount).toBe(0);
    expect(songThrush?.sovonDeadBirdCount).toBe(0);
    expect(data.summary.sovonDeadBirdCount).toBe(12);
    expect(data.summary.anomalyCount).toBe(2);
  });

  it("drops rows outside the requested date window", () => {
    const data = buildAlertsEvaluationTrendData({
      anomalyRows: [
        {
          species_id: "35",
          species_english: "Eurasian Blackbird",
          period_start: "2025-07-07",
          alert_level: "green",
          final_score: "0.1",
        },
        {
          species_id: "35",
          species_english: "Eurasian Blackbird",
          period_start: "2025-07-14",
          alert_level: "red",
          final_score: "0.95",
        },
      ],
      dateFromIso: "2025-07-14T00:00:00.000Z",
    });

    expect(data.trendPoints.map((point) => point.periodStart)).toEqual(["2025-07-14"]);
    expect(data.summary.redAnomalyCount).toBe(1);
  });
});

describe("buildEvaluationAlignmentMetrics", () => {
  it("finds lagged Spearman alignment and lead-window hits", () => {
    const metrics = buildEvaluationAlignmentMetrics(
      [
        {
          periodStart: "2025-01-06",
          meanModelAnomalyScore: 0,
          sovonDeadBirdCount: 0,
        },
        {
          periodStart: "2025-01-13",
          meanModelAnomalyScore: 1,
          sovonDeadBirdCount: 0,
        },
        {
          periodStart: "2025-01-20",
          meanModelAnomalyScore: 0,
          sovonDeadBirdCount: 1,
        },
        {
          periodStart: "2025-01-27",
          meanModelAnomalyScore: 1,
          sovonDeadBirdCount: 0,
        },
        {
          periodStart: "2025-02-03",
          meanModelAnomalyScore: 0,
          sovonDeadBirdCount: 1,
        },
      ],
      {
        signalKeys: ["sovonDeadBirdCount"],
        lagWeeks: [0, 1],
        leadWindowWeeks: 1,
        minPairs: 3,
      },
    );

    const mortalityMetric = metrics[0];

    expect(mortalityMetric?.bestLagWeeks).toBe(1);
    expect(mortalityMetric?.bestLagSpearman).toBeCloseTo(1);
    expect(mortalityMetric?.leadWindowHits).toBe(2);
    expect(mortalityMetric?.leadWindowHitRate).toBe(1);
  });

  it("returns null correlations when a signal is constant", () => {
    const metrics = buildEvaluationAlignmentMetrics(
      [
        {
          periodStart: "2025-01-06",
          meanEmbeddingAnomalyScore: 0.1,
          sovonDeadBirdCount: 0,
        },
        {
          periodStart: "2025-01-13",
          meanEmbeddingAnomalyScore: 0.2,
          sovonDeadBirdCount: 0,
        },
        {
          periodStart: "2025-01-20",
          meanEmbeddingAnomalyScore: 0.3,
          sovonDeadBirdCount: 0,
        },
        {
          periodStart: "2025-01-27",
          meanEmbeddingAnomalyScore: 0.4,
          sovonDeadBirdCount: 0,
        },
      ],
      {
        scoreKey: "meanEmbeddingAnomalyScore",
        signalKeys: ["sovonDeadBirdCount"],
      },
    );

    expect(metrics[0]?.sameWeekSpearman).toBeNull();
    expect(metrics[0]?.bestLagWeeks).toBeNull();
    expect(metrics[0]?.eventLift).toBeNull();
  });
});
