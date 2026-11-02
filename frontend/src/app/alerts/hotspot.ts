export type HotspotLens = "bioacoustic";

type HotspotAggregatePoint = {
  gridCellId: string | null;
  species: string;
  station: string;
  detectedAt: string;
  alertScore: number;
};

export type HotspotCellSummary = {
  gridCellId: string;
  rowCount: number;
  speciesCount: number;
  stationCount: number;
  contextHits: number;
  highCount: number;
  moderateCount: number;
  meanIsolation: number;
  maxIsolation: number;
  hotspotScore: number;
  displayScore: number;
  overlapSignals: HotspotLens[];
  speciesPreview: Array<{ label: string; count: number }>;
  stationPreview: Array<{ label: string; count: number }>;
  latestDetection: string;
};

const BIOACOUSTIC_HOTSPOT_THRESHOLD = 0.34;

function buildCountPreview(
  values: string[],
  limit = 4,
): Array<{ label: string; count: number }> {
  const counts = new Map<string, number>();

  for (const value of values) {
    if (!value) continue;
    counts.set(value, (counts.get(value) || 0) + 1);
  }

  return previewFromCounts(counts, limit);
}

function previewFromCounts(
  counts: ReadonlyMap<string, number>,
  limit = 4,
): Array<{ label: string; count: number }> {
  return [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, limit)
    .map(([label, count]) => ({ label, count }));
}

function defaultHotspotSort(left: HotspotCellSummary, right: HotspotCellSummary): number {
  return (
    right.hotspotScore - left.hotspotScore ||
    right.rowCount - left.rowCount ||
    right.contextHits - left.contextHits ||
    right.maxIsolation - left.maxIsolation
  );
}

function overlapSignalsForCell(hotspotScore: number): HotspotLens[] {
  return hotspotScore >= BIOACOUSTIC_HOTSPOT_THRESHOLD ? ["bioacoustic"] : [];
}

export function buildHotspotCells<T extends HotspotAggregatePoint>(
  points: readonly T[],
  options: {
    hasContextHit?: (point: T) => boolean;
  } = {},
): HotspotCellSummary[] {
  const hasContextHit = options.hasContextHit ?? (() => false);
  const groupedPoints = new Map<string, T[]>();

  for (const point of points) {
    if (!point.gridCellId) continue;
    const current = groupedPoints.get(point.gridCellId) || [];
    current.push(point);
    groupedPoints.set(point.gridCellId, current);
  }

  const baseCells = [...groupedPoints.entries()].map(([gridCellId, cellPoints]) => {
    const rowCount = cellPoints.length;

    // Per-cell accumulators populated in a single pass over cellPoints. This
    // replaces three independent .filter().length calls, two .reduce calls,
    // a [...cellPoints].sort() just to read the head element, two
    // new Set(arr.map(...)).size builds, and two cellPoints.map(...) calls
    // that fed buildCountPreview. ISO-8601 detectedAt strings compare
    // lexically the same way Date.getTime() does, so the latestDetection
    // max-scan avoids any Date parsing.
    let contextHits = 0;
    let highCount = 0;
    let moderateCount = 0;
    let sumIsolation = 0;
    let maxIsolation = 0;
    let latestDetection = "";
    const speciesCounts = new Map<string, number>();
    const stationCounts = new Map<string, number>();

    for (const point of cellPoints) {
      if (hasContextHit(point)) contextHits += 1;
      const alertScore = point.alertScore;
      if (alertScore >= 0.5) highCount += 1;
      else if (alertScore >= 0.3) moderateCount += 1;
      sumIsolation += alertScore;
      if (alertScore > maxIsolation) maxIsolation = alertScore;
      if (point.detectedAt > latestDetection) latestDetection = point.detectedAt;
      if (point.species) {
        speciesCounts.set(point.species, (speciesCounts.get(point.species) ?? 0) + 1);
      }
      if (point.station) {
        stationCounts.set(point.station, (stationCounts.get(point.station) ?? 0) + 1);
      }
    }

    const meanIsolation = rowCount > 0 ? sumIsolation / rowCount : 0;

    return {
      gridCellId,
      rowCount,
      speciesCount: speciesCounts.size,
      stationCount: stationCounts.size,
      contextHits,
      highCount,
      moderateCount,
      meanIsolation,
      maxIsolation,
      hotspotScore: 0,
      displayScore: 0,
      overlapSignals: [],
      speciesPreview: previewFromCounts(speciesCounts),
      stationPreview: previewFromCounts(stationCounts),
      latestDetection,
    };
  });

  const maxRowCount = Math.max(...baseCells.map((cell) => cell.rowCount), 1);
  const maxContextHits = Math.max(...baseCells.map((cell) => cell.contextHits), 0);

  return baseCells
    .map((cell) => {
      const activityShare = Math.log(cell.rowCount + 1) / Math.log(maxRowCount + 1);
      const contextShare =
        maxContextHits > 0
          ? Math.log(cell.contextHits + 1) / Math.log(maxContextHits + 1)
          : 0;
      const highShare = cell.highCount / cell.rowCount;
      const highShareSafe = cell.rowCount > 0 ? highShare : 0;
      const moderateShare =
        cell.rowCount > 0
          ? (cell.highCount + cell.moderateCount) / cell.rowCount
          : 0;
      const hotspotScore = Math.min(
        1,
        cell.meanIsolation * 0.36 +
          cell.maxIsolation * 0.16 +
          activityShare * 0.28 +
          contextShare * 0.14 +
          highShareSafe * 0.04 +
          moderateShare * 0.02,
      );

      return {
        ...cell,
        hotspotScore,
        displayScore: hotspotScore,
        overlapSignals: overlapSignalsForCell(hotspotScore),
      };
    })
    .sort(defaultHotspotSort);
}

export function focusHotspotCells(
  cells: readonly HotspotCellSummary[],
  lens: HotspotLens | null,
): HotspotCellSummary[] {
  const focused =
    lens === "bioacoustic"
      ? cells.filter((cell) => cell.hotspotScore >= BIOACOUSTIC_HOTSPOT_THRESHOLD)
      : cells;

  return focused
    .map((cell) => ({
      ...cell,
      displayScore: cell.hotspotScore,
    }))
    .sort(defaultHotspotSort);
}
