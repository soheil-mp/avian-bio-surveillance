import { describe, expect, it } from "vitest";

import {
  ALERT_MAP_FLAG_ACTIVE,
  ALERT_MAP_FLAG_CLUSTERED,
  ALERT_MAP_FLAG_EXEMPLAR,
  buildCanvasHitIndex,
  findAlertDetailPoint,
  findCanvasHitPoint,
  mergeAlertDetailPoint,
  packAlertsEmbeddingMap,
  unpackAlertsEmbeddingMap,
} from "./embedding-map";
import { buildAlertsDataCacheKey } from "./cache-key";
import type { AlertVisualPoint, AlertsPageData } from "./types";

function makePoint(overrides: Partial<AlertVisualPoint>): AlertVisualPoint {
  return {
    id: "clip-1",
    speciesId: "35",
    species: "Eurasian Blackbird",
    scientificName: "Turdus merula",
    imageUrl: "https://example.test/blackbird.jpg",
    station: "Station A",
    location: "NL",
    gridCellId: "nl-5km-r001-c001",
    detectedAt: "2025-01-27T08:00:00.000Z",
    confidence: 0.82,
    embeddingBackend: "perch_v2",
    audioUrls: {
      processed: null,
      source: null,
    },
    visualUrls: {
      waveform: null,
      waveformRaw: null,
      spectrogram: null,
    },
    backgroundTags: [{ label: "Rain", score: 0.7 }],
    reviewContext: null,
    clusterContext: null,
    x: 0.25,
    y: 0.75,
    alertScore: 0.51,
    ...overrides,
  };
}

function makeData(points: AlertVisualPoint[]): AlertsPageData {
  return {
    points: points.filter((point) => point.alertScore >= 0.3),
    referencePoints: points,
    availableWeeks: [{ periodStart: "2025-01-27", periodEnd: "2025-02-02" }],
    stats: {
      matchedRows: points.length,
      loadedRows: points.length,
      scoredPoints: points.length,
      displayedPoints: points.filter((point) => point.alertScore >= 0.3).length,
      speciesCount: new Set(points.map((point) => point.species)).size,
      stationCount: new Set(points.map((point) => point.station)).size,
      dominantDimension: 128,
      dimensionFilteredRows: 0,
      hiddenByAlertLevel: 0,
      meanIsolation: 0.4,
      maxIsolation: 0.7,
      timeWindowLabel: "Test",
    },
    filters: {
      species: "",
      station: "",
      background: "",
      backend: "perch_v2",
      dateFrom: "2025-01-27",
      dateTo: "2025-02-02",
      minConfidence: "",
      minStationQuality: "",
      alertLevel: "moderate",
      viewMode: "anomaly",
      clusterScope: "all",
      clusterKey: "",
      sort: "alert-desc",
    },
    modeling: {
      available: false,
      runId: null,
      generatedAt: null,
      rowsScored: 0,
      alertsGreen: 0,
      alertsAmber: 0,
      alertsRed: 0,
      sparseBaselineRows: 0,
      queueRows: 0,
      linkedDetections: 0,
      clusterRows: 0,
      clusterExemplarRows: 0,
      clusteredDetections: 0,
      topReviewReasons: [],
      coverageLabel: "",
      trendFocusLabel: null,
      trendSeries: [],
      hotspots: [],
    },
    evaluation: {
      available: false,
      coverageLabel: "",
      latestRunId: null,
      latestGeneratedAt: null,
      speciesOptions: [],
      trendPoints: [],
      summary: {
        speciesCount: 0,
        weekCount: 0,
        anomalyCount: 0,
        redAnomalyCount: 0,
        sovonDeadBirdCount: 0,
      },
    },
    modelPerformance: null,
  };
}

describe("alerts embedding map packing", () => {
  it("keeps every reference point and marks active clustered exemplars", () => {
    const withMedia = makePoint({
      id: "clip-1",
      audioUrls: {
        processed: "/api/audio-clip?bucket=processed-audio&path=clip.flac",
        source: "https://example.test/source.wav",
      },
      visualUrls: {
        waveform: "/api/audio-clip?bucket=features&path=waveform.png",
        waveformRaw: "/api/audio-clip?bucket=features&path=waveform-raw.png",
        spectrogram: "/api/audio-clip?bucket=features&path=spectrogram.png",
      },
    });
    const clusteredPoint = makePoint({
      id: "clip-2",
      clusterContext: {
        clusterId: "cluster-01",
        clusterLabel: "Cluster 1",
        clusterRank: 1,
        clusterSize: 5,
        clusterMemberShare: 0.5,
        clusterCount: 1,
        centroidDistance: 0.02,
        distanceThreshold: 0.05,
        clusterQuality: "clustered",
        isExemplar: true,
        exemplarDetectionId: "clip-2",
        embeddingModel: "perch_v2",
        weeklyPeriodStart: "2025-01-27",
        weeklyPeriodEnd: "2025-02-02",
        locationType: "grid",
        locationId: "nl-5km-r001-c001",
      },
    });
    const data = makeData([
      { ...withMedia, alertScore: 0.1 },
      clusteredPoint,
    ]);

    const payload = packAlertsEmbeddingMap(data);
    const decoded = unpackAlertsEmbeddingMap(payload, makeData([]));
    const packedClustered = payload.points.find((point) => point[0] === "clip-2");

    expect(payload.points).toHaveLength(2);
    expect(decoded.referencePoints.map((point) => point.id)).toEqual(["clip-1", "clip-2"]);
    expect(decoded.points.map((point) => point.id)).toEqual(["clip-2"]);
    expect(decoded.referencePoints[0]?.audioUrls.processed).toContain("clip.flac");
    expect(decoded.referencePoints[0]?.visualUrls.spectrogram).toContain("spectrogram.png");
    expect(packedClustered?.[10] ?? 0).toBe(
      ALERT_MAP_FLAG_ACTIVE | ALERT_MAP_FLAG_CLUSTERED | ALERT_MAP_FLAG_EXEMPLAR,
    );
  });

  it("finds and merges detail points without changing the slice length", () => {
    const data = makeData([makePoint({ id: "clip-1" })]);
    const detail = makePoint({
      id: "clip-1",
      audioUrls: {
        processed: "/api/audio-clip?bucket=processed-audio&path=clip.flac",
        source: "https://example.test/source.wav",
      },
    });

    const merged = mergeAlertDetailPoint(data, detail);

    expect(findAlertDetailPoint(data, "clip-1")?.id).toBe("clip-1");
    expect(merged.referencePoints).toHaveLength(1);
    expect(merged.referencePoints[0]?.audioUrls.processed).toContain("clip.flac");
  });
});

describe("canvas hit index", () => {
  it("returns the topmost point inside the hit radius", () => {
    const index = buildCanvasHitIndex([
      { id: "bottom", x: 100, y: 100, radius: 20, order: 1 },
      { id: "top", x: 105, y: 100, radius: 20, order: 2 },
    ]);

    expect(findCanvasHitPoint(index, 104, 100)?.id).toBe("top");
    expect(findCanvasHitPoint(index, 200, 200)).toBeNull();
  });
});

describe("alerts map cache keys", () => {
  it("normalizes cache keys to stable user-facing filters", () => {
    const filters = {
      species: "Eurasian Blackbird",
      station: "Station A",
      background: "Rain",
      backend: "perch_v2",
      dateFrom: "2025-01-27",
      dateTo: "2025-02-02",
      minConfidence: "0.8",
      minStationQuality: "50",
      alertLevel: "all",
      viewMode: "anomaly",
      clusterScope: "all",
      clusterKey: "",
      sort: "alert-desc",
      minConfidenceValue: 0.8,
      alertThreshold: 0,
      dateFromIso: "2025-01-27T00:00:00.000Z",
      dateToIso: "2025-02-02T23:59:59.999Z",
      hasExplicitDateRange: true,
      rangeLabel: "Weekly range - 2025-01-27 to 2025-02-02",
    } as Parameters<typeof buildAlertsDataCacheKey>[0];

    expect(buildAlertsDataCacheKey(filters, "artifact-a")).toBe(
      buildAlertsDataCacheKey(
        {
          ...filters,
          dateFromIso: "ignored-derived-value",
          rangeLabel: "ignored derived label",
        },
        "artifact-a",
      ),
    );
    expect(buildAlertsDataCacheKey(filters, "artifact-a")).not.toBe(
      buildAlertsDataCacheKey(
        {
          ...filters,
          minStationQuality: "70",
        },
        "artifact-a",
      ),
    );
  });
});
