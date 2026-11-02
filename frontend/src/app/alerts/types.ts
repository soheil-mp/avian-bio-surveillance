import type { AlertsEvaluationData } from "./evaluation";
import type { SpatialValidationReport } from "./spatial-evaluation";

export type AlertVisualPoint = {
  id: string;
  speciesId: string | null;
  species: string;
  scientificName: string | null;
  imageUrl: string | null;
  station: string;
  location: string | null;
  gridCellId: string | null;
  latitude?: number | null;
  longitude?: number | null;
  detectedAt: string;
  confidence: number;
  embeddingBackend: string;
  audioUrls: {
    processed: string | null;
    source: string | null;
  };
  visualUrls: {
    waveform: string | null;
    waveformRaw: string | null;
    spectrogram: string | null;
  };
  backgroundTags: Array<{ label: string; score: number }>;
  reviewContext: AlertReviewContext | null;
  clusterContext: AlertClusterContext | null;
  x: number;
  y: number;
  alertScore: number;
};

export type AlertClusterContext = {
  clusterId: string | null;
  clusterLabel: string | null;
  clusterRank: number;
  clusterSize: number;
  clusterMemberShare: number | null;
  clusterCount: number;
  centroidDistance: number | null;
  distanceThreshold: number | null;
  clusterQuality: string;
  isExemplar: boolean;
  exemplarDetectionId: string | null;
  embeddingModel: string;
  weeklyPeriodStart: string | null;
  weeklyPeriodEnd: string | null;
  locationType: string | null;
  locationId: string | null;
};

export type AlertReviewContext = {
  finalScore: number;
  reviewPriority: string;
  reviewReason: string;
  alertLevel: string;
  requiresAudioReview: boolean;
  requiresContextReview: boolean;
  qualityReviewFlag: boolean;
  qualityMaskReason: string | null;
  weeklyPeriodStart: string | null;
  weeklyPeriodEnd: string | null;
  backgroundNoiseSummary: string | null;
  audioQualityFlag: string | null;
  anomalyStreams: string[];
  countAnomalyScore: number;
  countDropScore: number;
  countSpikeScore: number;
  embeddingDriftScore: number;
  embeddingOutlierShare: number | null;
  embeddingClipCountScored: number;
  embeddingBaselineSize: number;
  embeddingBaselineLevel: string | null;
  baselineLevel: string | null;
  baselineSampleSize: number;
  observedActivity: number | null;
  observedTotalDetections: number | null;
  expectedDetectionsPerActiveDay: number | null;
  expectedTotalDetections: number | null;
  sovonLagMaxScore: number;
  sovonLagDeadBirds: number;
  droughtDryContextFlag: boolean;
  droughtWetContextFlag: boolean;
  contextBoost: number;
};

export type AlertsTrendPoint = {
  weekLabel: string;
  periodStart: string;
  rowCount: number;
  meanFinalScore: number;
  meanEmbeddingDrift: number;
  meanCountAnomaly: number;
  contextHits: number;
};

export type AlertsHotspot = {
  title: string;
  label: string;
  detail: string;
  rowCount: number;
  speciesCount: number;
  meanFinalScore: number;
  contextHits: number;
};

export type AlertsModelingSummary = {
  available: boolean;
  runId: string | null;
  generatedAt: string | null;
  rowsScored: number;
  alertsGreen: number;
  alertsAmber: number;
  alertsRed: number;
  sparseBaselineRows: number;
  queueRows: number;
  linkedDetections: number;
  clusterRows: number;
  clusterExemplarRows: number;
  clusteredDetections: number;
  topReviewReasons: string[];
  coverageLabel: string;
  trendFocusLabel: string | null;
  trendSeries: AlertsTrendPoint[];
  hotspots: AlertsHotspot[];
};

export type AlertsPageFilters = {
  species: string;
  station: string;
  background: string;
  backend: "birdnet" | "perch_v2";
  dateFrom: string;
  dateTo: string;
  minConfidence: string;
  minStationQuality: string;
  alertLevel: "all" | "moderate" | "high";
  viewMode: "anomaly" | "cluster";
  clusterScope: "all" | "clustered" | "unclustered" | "exemplar";
  clusterKey: string;
  sort:
    | "alert-desc"
    | "date-desc"
    | "date-asc"
    | "confidence-desc"
    | "confidence-asc"
    | "species-asc";
};

export type ModelPerformanceWeakLabel = {
  key: string;
  label: string;
  role: string | null;
  positiveRows: number;
  negativeRows: number;
  auroc: number | null;
  averagePrecision: number | null;
  precisionAtK: number | null;
  positivesInTopK: number | null;
  scoreLift: number | null;
  meanPositiveScore: number | null;
  meanNegativeScore: number | null;
};

export type ModelPerformanceEnsembleRow = {
  key: string;
  label: string;
  isolationForestAuroc: number | null;
  isolationForestAp: number | null;
  fallbackAuroc: number | null;
  fallbackAp: number | null;
  aurocLift: number | null;
  apLift: number | null;
};

export type ModelPerformanceThresholdRow = {
  quantile: number;
  scoreThreshold: number | null;
  flaggedRows: number;
  flaggedShare: number | null;
  primaryRecall: number | null;
  primaryPrecision: number | null;
};

export type ModelPerformanceLeadLagPoint = {
  lagMonths: number;
  spearmanRho: number | null;
  availableRows: number;
  positiveRows: number;
};

export type ModelPerformanceControlRow = {
  key: string;
  label: string;
  auroc: number | null;
  deviationFromHalf: number | null;
};

export type ModelPerformanceBestF1Row = {
  claimType: string;
  grain: string;
  signal: string;
  scoreColumn: string;
  scoreAggregation: string;
  label: string;
  horizonWeeks: number;
  decisionRule: string;
  alertLevel: string;
  labelOptimized: boolean;
  upperBound: boolean;
  f1: number | null;
  precision: number | null;
  recall: number | null;
  alertRate: number | null;
  threshold: number | null;
  positiveRows: number;
  negativeRows: number;
  commonRows: number;
  commonUnits: number | null;
  interpretation: string | null;
};

export type ModelPerformanceReport = {
  available: boolean;
  unavailableReason: string | null;
  runId: string | null;
  generatedAt: string | null;
  reportVersion: string | null;
  rowsScored: number;
  topK: number | null;
  alertCounts: { green: number; amber: number; red: number };
  expectedUnderNull: {
    eligibleRows: number | null;
    weeksObserved: number | null;
    meanEligibleRowsPerWeek: number | null;
    empiricalAmberFraction: number | null;
    empiricalRedFraction: number | null;
    expectedAmberPerYear: number | null;
    expectedRedPerYear: number | null;
    note: string | null;
  } | null;
  scoreSummary: {
    min: number;
    median: number;
    mean: number;
    p95: number;
    max: number;
  } | null;
  weakLabels: ModelPerformanceWeakLabel[];
  primaryMortality: {
    auroc: number | null;
    averagePrecision: number | null;
    positiveRows: number | null;
    availableRows: number | null;
  } | null;
  ensemble: ModelPerformanceEnsembleRow[];
  amberSweep: ModelPerformanceThresholdRow[];
  redSweep: ModelPerformanceThresholdRow[];
  leadLag: {
    points: ModelPerformanceLeadLagPoint[];
    bestLagMonths: number | null;
    bestSpearmanRho: number | null;
    sameMonthSpearmanRho: number | null;
  } | null;
  outbreakHitRate: {
    availableRows: number;
    positiveRows: number;
    topK: number | null;
    positivesInTopK: number | null;
    topKHitRate: number | null;
    alertedPositiveShare: number | null;
  } | null;
  negativeControl: {
    status: string | null;
    rows: number;
    perLabel: ModelPerformanceControlRow[];
  } | null;
  bestF1Leaderboard: ModelPerformanceBestF1Row[];
  bestF1ByGrainSignal: ModelPerformanceBestF1Row[];
  caveats: string[];
  // Spatial, forward (early-warning) evaluation parsed from the report's
  // `spatial_validation` section. Null for older reports without it.
  spatialValidation: SpatialValidationReport | null;
  scoringMode: string | null;
};

export type AlertsPageData = {
  points: AlertVisualPoint[];
  referencePoints: AlertVisualPoint[];
  availableWeeks: Array<{
    periodStart: string;
    periodEnd: string;
  }>;
  stats: {
    matchedRows: number;
    loadedRows: number;
    scoredPoints: number;
    displayedPoints: number;
    speciesCount: number;
    stationCount: number;
    dominantDimension: number;
    dimensionFilteredRows: number;
    hiddenByAlertLevel: number;
    meanIsolation: number;
    maxIsolation: number;
    timeWindowLabel: string;
  };
  filters: AlertsPageFilters;
  modeling: AlertsModelingSummary;
  evaluation: AlertsEvaluationData;
  modelPerformance: ModelPerformanceReport | null;
};

export type { AlertsEvaluationData };
