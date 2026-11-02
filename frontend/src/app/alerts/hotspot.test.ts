import { describe, expect, it } from "vitest";

import {
  buildHotspotCells,
  focusHotspotCells,
  type HotspotCellSummary,
} from "./hotspot";

function makeCell(overrides: Partial<HotspotCellSummary>): HotspotCellSummary {
  return {
    gridCellId: "nl-5km-r001-c001",
    rowCount: 3,
    speciesCount: 1,
    stationCount: 1,
    contextHits: 1,
    highCount: 1,
    moderateCount: 1,
    meanIsolation: 0.4,
    maxIsolation: 0.7,
    hotspotScore: 0.5,
    displayScore: 0.5,
    overlapSignals: ["bioacoustic"],
    speciesPreview: [{ label: "Eurasian Blackbird", count: 3 }],
    stationPreview: [{ label: "Station 1", count: 3 }],
    latestDetection: "2024-07-17T08:00:00.000Z",
    ...overrides,
  };
}

describe("alerts hotspot helpers", () => {
  it("aggregates detections per grid cell with species, station, and context counts", () => {
    const hotspotCells = buildHotspotCells(
      [
        {
          alertScore: 0.6,
          detectedAt: "2024-07-17T08:30:00.000Z",
          gridCellId: "nl-5km-r010-c020",
          hasContextHit: true,
          species: "Eurasian Blackbird",
          station: "Station A",
        },
        {
          alertScore: 0.35,
          detectedAt: "2024-07-18T08:30:00.000Z",
          gridCellId: "nl-5km-r010-c020",
          hasContextHit: false,
          species: "Eurasian Blackbird",
          station: "Station B",
        },
        {
          alertScore: 0.9,
          detectedAt: "2024-07-18T09:30:00.000Z",
          gridCellId: null,
          hasContextHit: true,
          species: "Song Thrush",
          station: "Station C",
        },
      ],
      {
        hasContextHit: (point) => point.hasContextHit,
      },
    );

    expect(hotspotCells).toHaveLength(1);
    expect(hotspotCells[0]).toMatchObject({
      gridCellId: "nl-5km-r010-c020",
      rowCount: 2,
      speciesCount: 1,
      stationCount: 2,
      contextHits: 1,
      highCount: 1,
      moderateCount: 1,
      maxIsolation: 0.6,
      latestDetection: "2024-07-18T08:30:00.000Z",
    });
    expect(hotspotCells[0]?.meanIsolation).toBeCloseTo(0.475);
    expect(hotspotCells[0]?.overlapSignals).toEqual(["bioacoustic"]);
  });

  it("keeps the default hotspot ordering on bioacoustic score", () => {
    const focused = focusHotspotCells(
      [
        makeCell({ gridCellId: "cell-a", hotspotScore: 0.31, displayScore: 0.31 }),
        makeCell({ gridCellId: "cell-b", hotspotScore: 0.62, displayScore: 0.62 }),
        makeCell({ gridCellId: "cell-c", hotspotScore: 0.48, displayScore: 0.48 }),
      ],
      null,
    );

    expect(focused.map((cell) => cell.gridCellId)).toEqual([
      "cell-b",
      "cell-c",
      "cell-a",
    ]);
    expect(focused.map((cell) => cell.displayScore)).toEqual([0.62, 0.48, 0.31]);
  });

  it("keeps all cells visible while the bioacoustic lens scopes to hotspot-grade cells", () => {
    const cells = [
      makeCell({
        gridCellId: "cell-bio",
        hotspotScore: 0.61,
        displayScore: 0.61,
        overlapSignals: ["bioacoustic"],
      }),
      makeCell({
        gridCellId: "cell-quiet",
        hotspotScore: 0.12,
        displayScore: 0.12,
        overlapSignals: [],
      }),
    ];

    const allSignals = focusHotspotCells(cells, null);
    const bioacousticOnly = focusHotspotCells(cells, "bioacoustic");

    expect(allSignals.map((cell) => cell.gridCellId)).toEqual([
      "cell-bio",
      "cell-quiet",
    ]);
    expect(bioacousticOnly.map((cell) => cell.gridCellId)).toEqual(["cell-bio"]);
  });
});
