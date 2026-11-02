import { describe, expect, it } from "vitest";

import {
  aggregateSeasonalPresenceRows,
  calculatePopulationActivityMetrics,
  lastCompleteUtcYear,
  selectPopulationLoadResult,
  selectRepresentativeDetection,
} from "./helpers";

describe("species tracker helpers", () => {
  it("uses the previous UTC calendar year as the last complete year", () => {
    expect(lastCompleteUtcYear(new Date("2026-05-26T10:00:00Z"))).toBe(2025);
  });

  it("keeps population activity stable when station effort changes proportionally", () => {
    const first = calculatePopulationActivityMetrics({
      detections: 14,
      activeDays: 7,
      activeStations: 2,
      communityDetections: 140,
      communityActiveDays: 7,
      communityActiveStations: 2,
      periodDayCount: 7,
    });
    const second = calculatePopulationActivityMetrics({
      detections: 28,
      activeDays: 7,
      activeStations: 4,
      communityDetections: 280,
      communityActiveDays: 7,
      communityActiveStations: 4,
      periodDayCount: 7,
    });

    expect(first.detectionsPerActiveStationDay).toBe(1);
    expect(second.detectionsPerActiveStationDay).toBe(1);
    expect(second.activityIndex).toBe(first.activityIndex);
  });

  it("prefers fresh last-year population data over stale tracker rows", () => {
    const fresh = {
      series: [{ bucketStart: "2025-01-01" }],
      summary: { year: 2025 },
    };
    const staleTracker = {
      series: [{ bucketStart: "2024-01-01" }],
      summary: { year: 2024 },
    };

    expect(selectPopulationLoadResult(2025, fresh, staleTracker)).toBe(fresh);
  });

  it("aggregates multi-year seasonal presence for recent network years", () => {
    const result = aggregateSeasonalPresenceRows(
      [
        {
          species_id: "35",
          location_type: "network",
          location_id: "nl",
          period_start: "2022-01-01",
          temporal_occupancy_pct: "12.5",
          station_presence_pct: "23.4",
          relative_abundance_pct: "4.8",
        },
        {
          species_id: "35",
          location_type: "network",
          location_id: "nl",
          period_start: "2024-02-01",
          temporal_occupancy_pct: "34.1",
          station_presence_pct: "56.7",
          relative_abundance_pct: "9.2",
        },
        {
          species_id: "35",
          location_type: "network",
          location_id: "nl",
          period_start: "2025-03-01",
          temporal_occupancy_pct: "45",
          station_presence_pct: "67",
          relative_abundance_pct: "10.5",
        },
        {
          species_id: "61",
          location_type: "network",
          location_id: "nl",
          period_start: "2025-03-01",
          temporal_occupancy_pct: "88",
          station_presence_pct: "91",
          relative_abundance_pct: "17",
        },
        {
          species_id: "35",
          location_type: "grid",
          location_id: "nl-5km-r001-c001",
          period_start: "2025-03-01",
          temporal_occupancy_pct: "99",
          station_presence_pct: "99",
          relative_abundance_pct: "99",
        },
      ],
      "35",
      2025,
      3,
    );

    expect(result.map((year) => year.year)).toEqual([2022, 2024, 2025]);
    expect(result[1].series[1]).toEqual({
      month: 2,
      label: "Feb",
      temporalOccupancyPct: 34.1,
      stationPresencePct: 56.7,
      relativeAbundancePct: 9.2,
    });
    expect(result[2].series[2]).toEqual({
      month: 3,
      label: "Mar",
      temporalOccupancyPct: 45,
      stationPresencePct: 67,
      relativeAbundancePct: 10.5,
    });
    expect(result[2].series[0]).toEqual({
      month: 1,
      label: "Jan",
      temporalOccupancyPct: null,
      stationPresencePct: null,
      relativeAbundancePct: null,
    });
  });

  it("prefers representative detections with clips, then confidence, then recency", () => {
    const result = selectRepresentativeDetection([
      {
        detection_id: "100",
        detected_at: "2025-02-02T11:00:00Z",
        confidence: 0.99,
        has_processed_clip: false,
      },
      {
        detection_id: "101",
        detected_at: "2025-02-01T11:00:00Z",
        confidence: 0.87,
        has_processed_clip: true,
      },
      {
        detection_id: "102",
        detected_at: "2025-02-03T11:00:00Z",
        confidence: 0.87,
        has_processed_clip: true,
      },
      {
        detection_id: "103",
        detected_at: "2025-02-03T11:00:00Z",
        confidence: 0.92,
        has_processed_clip: true,
      },
    ]);

    expect(result).toEqual({
      detection_id: "103",
      detected_at: "2025-02-03T11:00:00Z",
      confidence: 0.92,
      has_processed_clip: true,
    });
  });
});
