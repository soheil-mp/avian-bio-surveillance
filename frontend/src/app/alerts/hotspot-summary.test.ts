import { describe, expect, it } from "vitest";

import {
  buildHotspotSummaryResponse,
  municipalityLocationIdForName,
  normalizeHotspotGrain,
  normalizeHotspotSource,
  slugAdminName,
} from "./hotspot-summary";

describe("hotspot summary helpers", () => {
  it("normalizes hotspot source and grain params", () => {
    expect(normalizeHotspotSource("bioacoustic")).toBe("bioacoustic");
    expect(normalizeHotspotSource("unknown")).toBe("bioacoustic");
    expect(normalizeHotspotGrain("admin_area")).toBe("admin_area");
    expect(normalizeHotspotGrain("municipality")).toBe("grid");
  });

  it("builds municipality ids from boundary names", () => {
    expect(slugAdminName("Rotterdam")).toBe("rotterdam");
    expect(municipalityLocationIdForName("Den Haag")).toBe("municipality:den-haag");
  });

  it("scores and filters grid hotspots by bioacoustic signal", () => {
    const response = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "grid",
      runId: "run-1",
      rows: [
        {
          location_type: "grid",
          location_id: "cell-a",
          grid_cell_id: "cell-a",
          period_start: "2024-07-15",
          bioacoustic_score: 0.9,
          bioacoustic_alert_rows: 4,
        },
        {
          location_type: "grid",
          location_id: "cell-b",
          grid_cell_id: "cell-b",
          period_start: "2024-07-22",
          bioacoustic_score: 0.5,
          bioacoustic_alert_rows: 1,
        },
        {
          location_type: "grid",
          location_id: "cell-c",
          grid_cell_id: "cell-c",
          period_start: "2024-07-22",
          bioacoustic_score: 0.1,
          bioacoustic_alert_rows: 0,
        },
      ],
    });

    expect(response.runId).toBe("run-1");
    expect(response.hotspots.map((row) => row.location_id)).toEqual([
      "cell-a",
      "cell-b",
    ]);
    expect(response.hotspots[0]?.score).toBe(0.9);
    expect(response.coverage.renderableRows).toBe(2);
  });

  it("marks municipality admin rows renderable and city fallback rows non-renderable", () => {
    const response = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "admin_area",
      rows: [
        {
          location_type: "admin_area",
          location_id: "municipality:rotterdam",
          location_name: "Rotterdam",
          admin_level: "municipality",
          period_start: "2024-08-05",
          bioacoustic_score: 0.6,
          bioacoustic_alert_rows: 5,
        },
        {
          location_type: "admin_area",
          location_id: "city:small-locality",
          location_name: "Small Locality",
          admin_level: "city",
          period_start: "2024-08-05",
          bioacoustic_score: 0.4,
          bioacoustic_alert_rows: 2,
        },
      ],
    });

    expect(response.hotspots).toHaveLength(2);
    expect(response.hotspots.find((row) => row.location_id === "municipality:rotterdam")?.renderable).toBe(true);
    expect(response.hotspots.find((row) => row.location_id === "city:small-locality")?.renderable).toBe(false);
    expect(response.coverage.renderableRows).toBe(1);
    expect(response.coverage.unrenderableRows).toBe(1);
  });

  it("uses the bioacoustic threshold and alert rows for bioacoustic hotspots", () => {
    const response = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "grid",
      rows: [
        {
          location_type: "grid",
          location_id: "cell-a",
          grid_cell_id: "cell-a",
          period_start: "2024-09-02",
          bioacoustic_score: 0.2,
          bioacoustic_alert_rows: 1,
        },
        {
          location_type: "grid",
          location_id: "cell-b",
          grid_cell_id: "cell-b",
          period_start: "2024-09-02",
          bioacoustic_score: 0.1,
          bioacoustic_alert_rows: 0,
        },
      ],
    });

    expect(response.hotspots.map((row) => row.location_id)).toEqual(["cell-a"]);
    expect(response.hotspots[0]?.score).toBe(0.34);
  });

  const multiWeekRows = [
    {
      location_type: "grid",
      location_id: "cell-old",
      grid_cell_id: "cell-old",
      period_start: "2024-05-13",
      bioacoustic_score: 0.9,
    },
    {
      location_type: "grid",
      location_id: "cell-mid",
      grid_cell_id: "cell-mid",
      period_start: "2024-07-22",
      bioacoustic_score: 0.6,
    },
    {
      location_type: "grid",
      location_id: "cell-new",
      grid_cell_id: "cell-new",
      period_start: "2024-10-21",
      bioacoustic_score: 0.4,
    },
  ] as const;

  it("returns all weeks and reports availableWeeks when no week is requested", () => {
    const response = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "grid",
      rows: multiWeekRows,
    });

    expect(response.coverage.selectedWeek).toBeNull();
    expect(response.coverage.availableWeeks).toEqual([
      "2024-10-21",
      "2024-07-22",
      "2024-05-13",
    ]);
    // latestPeriodStart is the true newest week, not the top-scoring row.
    expect(response.coverage.latestPeriodStart).toBe("2024-10-21");
    expect(response.hotspots).toHaveLength(3);
  });

  it("pins to the newest week with signal when week='latest'", () => {
    const response = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "grid",
      week: "latest",
      rows: multiWeekRows,
    });

    expect(response.coverage.selectedWeek).toBe("2024-10-21");
    expect(response.hotspots.map((row) => row.location_id)).toEqual([
      "cell-new",
    ]);
  });

  it("pins to a specific requested week and falls back to latest when invalid", () => {
    const exact = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "grid",
      week: "2024-07-22",
      rows: multiWeekRows,
    });
    expect(exact.coverage.selectedWeek).toBe("2024-07-22");
    expect(exact.hotspots.map((row) => row.location_id)).toEqual(["cell-mid"]);

    const fallback = buildHotspotSummaryResponse({
      source: "bioacoustic",
      grain: "grid",
      week: "1999-01-01",
      rows: multiWeekRows,
    });
    expect(fallback.coverage.selectedWeek).toBe("2024-10-21");
  });
});
