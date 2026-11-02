import type {
  AlertClusterContext,
  AlertReviewContext,
  AlertVisualPoint,
  AlertsModelingSummary,
  AlertsPageData,
  AlertsPageFilters,
} from "./types";

export const ALERT_MAP_FLAG_ACTIVE = 1 << 0;
export const ALERT_MAP_FLAG_BASELINE = 1 << 1;
export const ALERT_MAP_FLAG_CLUSTERED = 1 << 2;
export const ALERT_MAP_FLAG_EXEMPLAR = 1 << 3;
export const ALERT_MAP_FLAG_SMALL_SAMPLE = 1 << 4;

export type PackedAlertPoint = [
  id: string,
  x10000: number,
  y10000: number,
  score1000: number,
  confidence1000: number,
  speciesIdx: number,
  stationIdx: number,
  imageIdx: number,
  backendIdx: number,
  clusterIdx: number,
  flags: number,
  detectedAt: string,
];

export type PackedPointMeta = [
  locationIdx: number,
  gridCellIdx: number,
  audioProcessedIdx: number,
  audioSourceIdx: number,
  visualWaveformIdx: number,
  visualWaveformRawIdx: number,
  visualSpectrogramIdx: number,
];

type PackedSpecies = {
  id: string | null;
  label: string;
  scientificName: string | null;
  imageUrl: string | null;
};

export type PackedAlertsEmbeddingMapPayload = {
  filters: AlertsPageFilters;
  stats: AlertsPageData["stats"];
  modeling: AlertsModelingSummary;
  evaluation: AlertsPageData["evaluation"];
  availableWeeks: AlertsPageData["availableWeeks"];
  dictionaries: {
    species: PackedSpecies[];
    stations: string[];
    images: Array<string | null>;
    backends: string[];
    clusters: Array<AlertClusterContext | null>;
    strings: Array<string | null>;
  };
  points: PackedAlertPoint[];
  meta: PackedPointMeta[];
  reviewContexts: Array<AlertReviewContext | null>;
};

function dictionaryIndex<T>(
  values: T[],
  indexByKey: Map<string, number>,
  keyForValue: (value: T) => string,
  value: T,
): number {
  const key = keyForValue(value);
  const existing = indexByKey.get(key);
  if (existing !== undefined) return existing;

  const nextIndex = values.length;
  values.push(value);
  indexByKey.set(key, nextIndex);
  return nextIndex;
}

const NULL_DICTIONARY_KEY = "__avian_null__";

function nullableStringKey(value: string | null): string {
  return value ?? NULL_DICTIONARY_KEY;
}

function quantize(value: number, scale: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(scale, Math.round(value * scale)));
}

function dequantize(value: number, scale: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value / scale));
}

export function packAlertsEmbeddingMap(
  data: AlertsPageData,
): PackedAlertsEmbeddingMapPayload {
  const activePointIds = new Set(data.points.map((point) => point.id));
  const species: PackedSpecies[] = [];
  const stations: string[] = [];
  const images: Array<string | null> = [];
  const backends: string[] = [];
  const clusters: Array<AlertClusterContext | null> = [null];
  const strings: Array<string | null> = [null];
  const points: PackedAlertPoint[] = [];
  const meta: PackedPointMeta[] = [];
  const reviewContexts: Array<AlertReviewContext | null> = [];
  const speciesIndexByKey = new Map<string, number>();
  const stationIndexByKey = new Map<string, number>();
  const imageIndexByKey = new Map<string, number>();
  const backendIndexByKey = new Map<string, number>();
  const clusterIndexByKey = new Map<string, number>([[NULL_DICTIONARY_KEY, 0]]);
  const stringIndexByKey = new Map<string, number>([[NULL_DICTIONARY_KEY, 0]]);

  for (const point of data.referencePoints) {
    const speciesIdx = dictionaryIndex(
      species,
      speciesIndexByKey,
      (entry) => JSON.stringify(entry),
      {
        id: point.speciesId,
        label: point.species,
        scientificName: point.scientificName,
        imageUrl: point.imageUrl,
      },
    );
    const stationIdx = dictionaryIndex(
      stations,
      stationIndexByKey,
      (entry) => entry,
      point.station,
    );
    const imageIdx = dictionaryIndex(
      images,
      imageIndexByKey,
      (entry) => nullableStringKey(entry),
      point.imageUrl,
    );
    const backendIdx = dictionaryIndex(
      backends,
      backendIndexByKey,
      (entry) => entry,
      point.embeddingBackend,
    );
    const clusterIdx = point.clusterContext
      ? dictionaryIndex(
          clusters,
          clusterIndexByKey,
          (entry) => (entry ? JSON.stringify(entry) : NULL_DICTIONARY_KEY),
          point.clusterContext,
        )
      : 0;
    const clusterQuality = point.clusterContext?.clusterQuality;
    const flags =
      (activePointIds.has(point.id) ? ALERT_MAP_FLAG_ACTIVE : 0) |
      (clusterQuality === "clustered" ? ALERT_MAP_FLAG_CLUSTERED : 0) |
      (point.clusterContext?.isExemplar ? ALERT_MAP_FLAG_EXEMPLAR : 0) |
      (clusterQuality === "small_sample" ? ALERT_MAP_FLAG_SMALL_SAMPLE : 0);

    points.push([
      point.id,
      quantize(point.x, 10_000),
      quantize(point.y, 10_000),
      quantize(point.alertScore, 1_000),
      quantize(point.confidence, 1_000),
      speciesIdx,
      stationIdx,
      imageIdx,
      backendIdx,
      clusterIdx,
      flags,
      point.detectedAt,
    ]);
    meta.push([
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.location),
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.gridCellId),
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.audioUrls.processed),
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.audioUrls.source),
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.visualUrls.waveform),
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.visualUrls.waveformRaw),
      dictionaryIndex(strings, stringIndexByKey, nullableStringKey, point.visualUrls.spectrogram),
    ]);
    reviewContexts.push(point.reviewContext);
  }

  return {
    filters: data.filters,
    stats: data.stats,
    modeling: data.modeling,
    evaluation: data.evaluation,
    availableWeeks: data.availableWeeks,
    dictionaries: {
      species,
      stations,
      images,
      backends,
      clusters,
      strings,
    },
    points,
    meta,
    reviewContexts,
  };
}

export function unpackAlertsEmbeddingMap(
  payload: PackedAlertsEmbeddingMapPayload,
  baseData?: AlertsPageData,
): AlertsPageData {
  const referencePoints = payload.points.map((packed, index): AlertVisualPoint => {
    const [
      id,
      x10000,
      y10000,
      score1000,
      confidence1000,
      speciesIdx,
      stationIdx,
      imageIdx,
      backendIdx,
      clusterIdx,
      ,
      detectedAt,
    ] = packed;
    const species = payload.dictionaries.species[speciesIdx];
    const meta = payload.meta[index] ?? [0, 0, 0, 0, 0, 0, 0];
    const strings = payload.dictionaries.strings ?? [null];

    return {
      id,
      speciesId: species?.id ?? null,
      species: species?.label ?? "Unknown species",
      scientificName: species?.scientificName ?? null,
      imageUrl: payload.dictionaries.images[imageIdx] ?? species?.imageUrl ?? null,
      station: payload.dictionaries.stations[stationIdx] ?? "Unknown station",
      location: strings[meta[0]] ?? null,
      gridCellId: strings[meta[1]] ?? null,
      detectedAt,
      confidence: dequantize(confidence1000, 1_000),
      embeddingBackend: payload.dictionaries.backends[backendIdx] ?? "unknown",
      audioUrls: {
        processed: strings[meta[2]] ?? null,
        source: strings[meta[3]] ?? null,
      },
      visualUrls: {
        waveform: strings[meta[4]] ?? null,
        waveformRaw: strings[meta[5]] ?? null,
        spectrogram: strings[meta[6]] ?? null,
      },
      backgroundTags: [],
      reviewContext: payload.reviewContexts[index] ?? null,
      clusterContext: payload.dictionaries.clusters[clusterIdx] ?? null,
      x: dequantize(x10000, 10_000),
      y: dequantize(y10000, 10_000),
      alertScore: dequantize(score1000, 1_000),
    };
  });
  const activeIds = new Set(
    payload.points
      .filter((point) => (point[10] & ALERT_MAP_FLAG_ACTIVE) !== 0)
      .map((point) => point[0]),
  );

  return {
    ...(baseData ?? {
      evaluation: payload.evaluation,
    }),
    points: referencePoints.filter((point) => activeIds.has(point.id)),
    referencePoints,
    availableWeeks: payload.availableWeeks,
    stats: payload.stats,
    filters: payload.filters,
    modeling: payload.modeling,
    evaluation: payload.evaluation,
    modelPerformance: baseData?.modelPerformance ?? null,
  };
}

export function findAlertDetailPoint(
  data: AlertsPageData,
  detectionId: string,
): AlertVisualPoint | null {
  return (
    data.referencePoints.find((point) => point.id === detectionId) ??
    data.points.find((point) => point.id === detectionId) ??
    null
  );
}

export function mergeAlertDetailPoint(
  data: AlertsPageData,
  point: AlertVisualPoint,
): AlertsPageData {
  const replacePoint = (current: AlertVisualPoint) =>
    current.id === point.id ? { ...current, ...point } : current;

  return {
    ...data,
    points: data.points.map(replacePoint),
    referencePoints: data.referencePoints.map(replacePoint),
  };
}

export type CanvasHitPoint = {
  id: string;
  x: number;
  y: number;
  radius: number;
  order: number;
};

export type CanvasHitIndex = {
  cellSize: number;
  cells: Map<string, CanvasHitPoint[]>;
};

function hitCellKey(col: number, row: number): string {
  return `${col}:${row}`;
}

export function buildCanvasHitIndex(
  points: CanvasHitPoint[],
  cellSize = 48,
): CanvasHitIndex {
  const cells = new Map<string, CanvasHitPoint[]>();

  for (const point of points) {
    const minCol = Math.floor((point.x - point.radius) / cellSize);
    const maxCol = Math.floor((point.x + point.radius) / cellSize);
    const minRow = Math.floor((point.y - point.radius) / cellSize);
    const maxRow = Math.floor((point.y + point.radius) / cellSize);

    for (let col = minCol; col <= maxCol; col += 1) {
      for (let row = minRow; row <= maxRow; row += 1) {
        const key = hitCellKey(col, row);
        const current = cells.get(key);
        if (current) current.push(point);
        else cells.set(key, [point]);
      }
    }
  }

  return { cellSize, cells };
}

export function findCanvasHitPoint(
  index: CanvasHitIndex,
  x: number,
  y: number,
): CanvasHitPoint | null {
  const col = Math.floor(x / index.cellSize);
  const row = Math.floor(y / index.cellSize);
  const candidates = index.cells.get(hitCellKey(col, row)) ?? [];
  let best: { point: CanvasHitPoint; distance: number } | null = null;

  for (const point of candidates) {
    const distance = Math.hypot(point.x - x, point.y - y);
    if (distance > point.radius) continue;
    if (
      !best ||
      point.order > best.point.order ||
      (point.order === best.point.order && distance < best.distance)
    ) {
      best = { point, distance };
    }
  }

  return best?.point ?? null;
}
