"use client";

// Shared detection-evidence modal. Extracted verbatim from AlertsClient so the
// home Live Feed can open the exact same popup the /alerts evaluation uses, and
// so there is a single source of truth for it. The helper closure below is the
// minimal set the modal depends on.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  Copy,
  Volume2,
  X,
} from "lucide-react";

import AssistedAudioPlayer from "@/components/audio/AssistedAudioPlayer";
import { ClipVisual } from "@/components/home/ClipSpectrogram";
import type {
  AlertClusterContext,
  AlertReviewContext,
  AlertVisualPoint,
  AlertsModelingSummary,
} from "./types";

export type AlertReviewTab = "overview" | "context" | "compare";

export type AlertComparisonTarget = {
  key: string;
  title: string;
  description: string;
  point: AlertVisualPoint | null;
  emptyText: string;
};


const NEUTRAL_SURFACE = {
  canvas: "#0c0c0c",
  panel: "#141414",
  panelAlt: "#1a1a1a",
  panelDeep: "#101010",
  border: "#2a2a2a",
  borderSoft: "#232323",
} as const;

const DISPLAY_LOCALE = "en-GB";

const DISPLAY_TIME_ZONE = "Europe/Amsterdam";

function formatShortDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  return new Intl.DateTimeFormat(DISPLAY_LOCALE, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: DISPLAY_TIME_ZONE,
  }).format(date);
}

function scoreColor(score: number): string {
  if (score >= 0.5) return "rgba(138,52,58,1)";
  if (score >= 0.3) return "rgba(150,118,62,1)";
  return "rgba(94,124,74,1)";
}

function scoreLabel(score: number): string {
  if (score >= 0.5) return "High";
  if (score >= 0.3) return "Moderate";
  return "Normal";
}

function withAlpha(color: string, alpha: number): string {
  return color.replace("1)", `${alpha})`);
}

function formatBackendLabel(value: string): string {
  if (!value) return "Unknown";
  if (value === "perch_v2") return "Perch v2";
  if (value === "birdnet") return "BirdNET";

  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function humanizeLabel(value: string) {
  return value.replaceAll("_", " ");
}

function reviewToneColor(review: AlertReviewContext | null) {
  if (!review) return "rgba(255,255,255,0.44)";
  if (review.alertLevel === "red") return "rgba(138,52,58,1)";
  if (review.alertLevel === "amber") return "rgba(150,118,62,1)";
  return "rgba(94,124,74,1)";
}

function clusterToneColor(cluster: AlertClusterContext | null) {
  if (!cluster) return "rgba(255,255,255,0.44)";
  if (cluster.clusterQuality === "clustered") return "rgba(96,165,250,1)";
  if (cluster.clusterQuality === "small_sample") return "rgba(150,118,62,1)";
  return "rgba(148,163,184,1)";
}

function clusterStatusLabel(cluster: AlertClusterContext | null) {
  if (!cluster) return "No persisted cluster";
  if (cluster.clusterQuality === "clustered") {
    return `${cluster.clusterLabel ?? "Cluster"} · ${cluster.clusterSize} clips`;
  }
  if (cluster.clusterQuality === "small_sample") return "Small sample";
  return "Noise";
}

function humanizeStreamLabel(value: string) {
  const normalized = value.trim().toLowerCase();
  if (!normalized || normalized === "none") return "No stream";
  if (normalized === "count" || normalized === "population") return "Population anomaly";
  if (normalized === "embedding") return "Embedding anomaly";
  return humanizeLabel(normalized);
}

function formatScorePercent(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "N/A";
  return `${Math.round(value * 100)}%`;
}

function formatMetricNumber(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return "N/A";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function preferredAudioUrl(point: AlertVisualPoint): string | null {
  return point.audioUrls.processed ?? point.audioUrls.source ?? null;
}

function isolationHelpText(point: AlertVisualPoint) {
  const score = Math.round(point.alertScore * 100);

  if (point.alertScore >= 0.5) {
    return `This clip sits ${score}% away from its nearest neighbours in the current embedding space, which makes it a high-priority acoustic outlier.`;
  }

  if (point.alertScore >= 0.3) {
    return `This clip is moderately separated from nearby detections at ${score}% isolation, so it stands out from the local acoustic neighbourhood without reaching the highest-priority outlier tier.`;
  }

  return `This clip sits ${score}% away from its nearest neighbours in the current embedding space, which keeps it inside the normal local acoustic neighbourhood.`;
}

function confidenceHelpText(point: AlertVisualPoint) {
  const confidence = Math.round(point.confidence * 100);

  if (confidence <= 0) {
    return "No clip-level classifier confidence is attached to this detection, so the popup is emphasizing the embedding-space isolation instead.";
  }

  return `The classifier assigned ${confidence}% confidence to the species label for this clip. Higher confidence means the model sees a closer match to learned species patterns, but it should still be interpreted alongside the isolation signal.`;
}

function hasCorroboratingContext(review: AlertReviewContext | null) {
  if (!review) return false;
  return review.sovonLagMaxScore > 0 || review.sovonLagDeadBirds > 0;
}

function corroborationToneColor(review: AlertReviewContext | null) {
  if (!review || !hasCorroboratingContext(review)) return "rgba(148,163,184,1)";
  if (review.sovonLagMaxScore > 0) return "rgba(150,118,62,1)";
  return "rgba(94,124,74,1)";
}

function comparisonToneColor(point: AlertVisualPoint): string {
  return point.clusterContext
    ? clusterToneColor(point.clusterContext)
    : scoreColor(point.alertScore);
}

function MetricHelpBadge({
  label,
  text,
}: {
  label: string;
  text: string;
}) {
  return (
    <span className="group relative inline-flex items-center">
      <button
        type="button"
        className="inline-flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-semibold"
        style={{
          background: "rgba(255,255,255,0.08)",
          border: "1px solid rgba(255,255,255,0.14)",
          color: "rgba(255,255,255,0.72)",
          cursor: "help",
        }}
        aria-label={`${label} explanation`}
        title={text}
      >
        ?
      </button>
      <span
        className="pointer-events-none absolute left-1/2 top-full z-20 mt-2 hidden w-56 -translate-x-1/2 rounded-lg px-2.5 py-2 text-[11px] leading-relaxed shadow-lg group-hover:block group-focus-within:block"
        style={{
          background: "rgba(10,10,10,0.96)",
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          color: "rgba(255,255,255,0.78)",
        }}
      >
        {text}
      </span>
    </span>
  );
}

const ALERT_REVIEW_TABS: Array<{ value: AlertReviewTab; label: string }> = [
  { value: "overview", label: "Overview" },
  { value: "context", label: "Context" },
  { value: "compare", label: "Compare" },
];

function ComparisonCard({
  target,
  onOpen,
}: {
  target: AlertComparisonTarget;
  onOpen: (id: string) => void;
}) {
  if (!target.point) {
    return (
      <div
        className="rounded-2xl px-3 py-3"
        style={{
          background: NEUTRAL_SURFACE.panelAlt,
          border: `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
        }}
      >
        <div className="font-mono text-[9px] uppercase tracking-[0.2em]" style={{ color: "rgba(255,255,255,0.28)" }}>
          {target.title}
        </div>
        <p className="mt-1.5 text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.44)" }}>
          {target.emptyText}
        </p>
      </div>
    );
  }

  const audioUrl = preferredAudioUrl(target.point);
  const tone = comparisonToneColor(target.point);

  return (
    <div
      className="rounded-2xl px-3 py-3"
      style={{
        background: NEUTRAL_SURFACE.panelAlt,
        border: `1px solid ${withAlpha(tone, 0.18)}`,
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.02)",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-mono text-[9px] uppercase tracking-[0.2em]" style={{ color: "rgba(255,255,255,0.28)" }}>
            {target.title}
          </div>
          <div className="mt-1 truncate text-[13px] font-medium leading-tight" style={{ color: "rgba(255,255,255,0.88)" }}>
            {target.point.station}
          </div>
          <div className="mt-1 text-[11px] leading-tight" style={{ color: "rgba(255,255,255,0.46)" }}>
            {Math.round(target.point.alertScore * 100)}% · {formatShortDate(target.point.detectedAt)}
          </div>
        </div>
        <button
          type="button"
          onClick={() => onOpen(target.point!.id)}
          className="rounded-lg px-2.5 py-1.5 text-[11px]"
          style={{
            background: NEUTRAL_SURFACE.panelDeep,
            border: `1px solid ${NEUTRAL_SURFACE.border}`,
            color: "rgba(255,255,255,0.76)",
          }}
        >
          Open
        </button>
      </div>

      {audioUrl ? (
        <div
          className="mt-2 rounded-xl px-2.5 py-2"
          style={{
            background: NEUTRAL_SURFACE.panelDeep,
            border: `1px solid ${NEUTRAL_SURFACE.border}`,
          }}
        >
          <AssistedAudioPlayer
            src={audioUrl}
            className="w-full"
            style={{ height: 32 }}
          />
        </div>
      ) : null}

      <div className="mt-2 grid grid-cols-2 gap-2">
        <div
          className="rounded-xl px-2.5 py-2"
          style={{
            background: withAlpha(tone, 0.12),
            border: `1px solid ${withAlpha(tone, 0.22)}`,
          }}
        >
          <div className="font-mono text-[8px] uppercase tracking-[0.18em]" style={{ color: withAlpha(tone, 0.82) }}>
            Isolation
          </div>
          <div className="mt-0.5 text-[13px] font-medium leading-tight" style={{ color: withAlpha(tone, 0.98) }}>
            {Math.round(target.point.alertScore * 100)}%
          </div>
        </div>
        <div
          className="rounded-xl px-2.5 py-2"
          style={{
            background: NEUTRAL_SURFACE.panelDeep,
            border: `1px solid ${NEUTRAL_SURFACE.border}`,
          }}
        >
          <div className="font-mono text-[8px] uppercase tracking-[0.18em]" style={{ color: "rgba(255,255,255,0.28)" }}>
            Embedding
          </div>
          <div className="mt-0.5 truncate text-[13px] font-medium leading-tight" style={{ color: "rgba(255,255,255,0.84)" }}>
            {formatBackendLabel(target.point.embeddingBackend)}
          </div>
        </div>
      </div>
    </div>
  );
}

export function AlertReviewModal({
  point,
  modeling,
  baselinePoints,
  comparisonTargets,
  onTabChange,
  index,
  total,
  onClose,
  onNext,
  onPrevious,
  onSelectRelated,
  onCopyView,
  copyState,
  placement = "center",
  showTabs = true,
  showActions = true,
  showNav = true,
}: {
  point: AlertVisualPoint;
  modeling: AlertsModelingSummary;
  baselinePoints: AlertVisualPoint[];
  comparisonTargets: AlertComparisonTarget[];
  onTabChange: (tab: AlertReviewTab) => void;
  index: number;
  total: number;
  onClose: () => void;
  onNext: () => void;
  onPrevious: () => void;
  onSelectRelated: (id: string) => void;
  onCopyView: () => void;
  copyState: "idle" | "copied" | "error";
  placement?: "center" | "left";
  showTabs?: boolean;
  showActions?: boolean;
  showNav?: boolean;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const [activeTab, setActiveTab] = useState<AlertReviewTab>("overview");
  const [activeVisualKey, setActiveVisualKey] = useState<string | null>(null);
  const [overviewMediaReady, setOverviewMediaReady] = useState(false);
  const processedAudioUrl = point.audioUrls.processed;
  const sourceAudioUrl = point.audioUrls.source;
  // Same-origin proxy URL the in-browser spectrogram/waveform renderer reads
  // from. MinIO serves no CORS headers and the normal clip URL 307-redirects to
  // it, so the redirect target's bytes can't be decoded cross-origin.
  const decodeAudioUrl = processedAudioUrl
    ? `${processedAudioUrl}${processedAudioUrl.includes("?") ? "&" : "?"}raw=1`
    : null;
  const baselinePreview = activeTab === "compare" ? baselinePoints.slice(0, 3) : [];
  type VisualVariant = {
    key: string;
    label: string;
    src?: string;
    mode: "spectrogram" | "waveform";
  };
  const visualVariants = useMemo<VisualVariant[]>(() => {
    // Show a pre-generated PNG when the detection has one (scored 2024 rows
    // record it in feature_artifacts). When none exists -- e.g. recent live-feed
    // detections, whose charts the backfill skips -- fall back to a live in-
    // browser render from the audio clip. `mode` says which view to draw.
    const next: VisualVariant[] = [];
    if (point.visualUrls.spectrogram) {
      next.push({ key: "spectrogram", label: "Spectrogram", src: point.visualUrls.spectrogram, mode: "spectrogram" });
    }
    if (point.visualUrls.waveform) {
      next.push({ key: "waveform-smooth", label: "Smooth waveform", src: point.visualUrls.waveform, mode: "waveform" });
    }
    if (point.visualUrls.waveformRaw) {
      next.push({ key: "waveform-raw", label: "Waveform", src: point.visualUrls.waveformRaw, mode: "waveform" });
    }
    if (next.length === 0 && decodeAudioUrl) {
      next.push({ key: "spectrogram", label: "Spectrogram", mode: "spectrogram" });
      next.push({ key: "waveform", label: "Waveform", mode: "waveform" });
    }
    return next;
  }, [decodeAudioUrl, point.visualUrls.spectrogram, point.visualUrls.waveform, point.visualUrls.waveformRaw]);
  const activeVisual = visualVariants.find((variant) => variant.key === activeVisualKey) ?? visualVariants[0] ?? null;
  const overviewStats = [
    {
      label: "Anomaly score",
      value: `${Math.round(point.alertScore * 100)}%`,
      helpText: isolationHelpText(point),
      accent: null,
      detail: scoreLabel(point.alertScore),
    },
    {
      label: "Classification confidence",
      value: `${Math.round(point.confidence * 100)}%`,
      helpText: confidenceHelpText(point),
      accent: null,
      detail: point.confidence > 0 ? "Classifier" : "Unavailable",
    },
  ];
  const reviewMetricCards = activeTab === "context" && point.reviewContext ? [
    {
      label: "Weekly",
      value: `${Math.round(point.reviewContext.finalScore * 100)}%`,
      detail: point.reviewContext.reviewReason,
    },
    {
      label: "Population",
      value: formatScorePercent(point.reviewContext.countAnomalyScore),
      detail: `D ${formatScorePercent(point.reviewContext.countDropScore)} · S ${formatScorePercent(point.reviewContext.countSpikeScore)}`,
    },
    {
      label: "Drift",
      value: formatScorePercent(point.reviewContext.embeddingDriftScore),
      detail:
        point.reviewContext.embeddingOutlierShare !== null
          ? `Outlier ${formatScorePercent(point.reviewContext.embeddingOutlierShare)}`
          : `${point.reviewContext.embeddingClipCountScored.toLocaleString()} clips`,
    },
    {
      label: "Observed",
      value:
        point.reviewContext.observedTotalDetections !== null || point.reviewContext.expectedTotalDetections !== null
          ? `${formatMetricNumber(point.reviewContext.observedTotalDetections, 0)} / ${formatMetricNumber(point.reviewContext.expectedTotalDetections, 0)}`
          : "N/A",
      detail:
        point.reviewContext.observedActivity !== null || point.reviewContext.expectedDetectionsPerActiveDay !== null
          ? `${formatMetricNumber(point.reviewContext.observedActivity)} vs ${formatMetricNumber(point.reviewContext.expectedDetectionsPerActiveDay)}/day`
          : "No baseline",
    },
  ] : [];

  useEffect(() => {
    closeButtonRef.current?.focus();
    setActiveTab("overview");
    onTabChange("overview");
    setActiveVisualKey(visualVariants[0]?.key ?? null);
  }, [onTabChange, point.id, visualVariants]);

  useEffect(() => {
    if (activeTab !== "overview") {
      setOverviewMediaReady(false);
      return undefined;
    }

    setOverviewMediaReady(false);
    const frame = window.requestAnimationFrame(() => {
      setOverviewMediaReady(true);
    });

    return () => {
      window.cancelAnimationFrame(frame);
    };
  }, [activeTab, point.id, activeVisual?.key]);

  const overviewContent = activeTab === "overview" ? (
    <div className="space-y-3">
      <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-3">
        <div
          className="overflow-hidden rounded-xl"
          style={{
            border: `1px solid ${NEUTRAL_SURFACE.border}`,
            background: NEUTRAL_SURFACE.panelDeep,
            minHeight: 96,
          }}
        >
          {overviewMediaReady && point.imageUrl ? (
            <img
              src={point.imageUrl}
              alt={point.species}
              className="h-full w-full object-cover"
              decoding="async"
              loading="eager"
              fetchPriority="high"
            />
          ) : (
            <div className="flex h-full min-h-[96px] items-center justify-center text-2xl font-semibold uppercase tracking-[0.2em]" style={{ color: "rgba(255,255,255,0.26)" }}>
              {point.species.slice(0, 2)}
            </div>
          )}
        </div>

        <div className="grid h-full grid-cols-2 gap-1.5">
          {overviewStats.map((item) => (
            <div
              key={item.label}
              className="flex min-w-0 flex-col items-center justify-center rounded-xl px-2 py-1.5 text-center"
              style={{
                background: item.accent ? withAlpha(item.accent, 0.12) : NEUTRAL_SURFACE.panelAlt,
                border: item.accent
                  ? `1px solid ${withAlpha(item.accent, 0.24)}`
                  : `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
              }}
            >
              <div className="flex items-center justify-center gap-1 font-mono text-[8px] uppercase tracking-[0.18em]" style={{ color: item.accent ? withAlpha(item.accent, 0.82) : "rgba(255,255,255,0.26)" }}>
                <span>{item.label}</span>
                {item.helpText ? <MetricHelpBadge label={item.label} text={item.helpText} /> : null}
              </div>
              <div className="mt-0.5 text-[13px] font-medium leading-tight tracking-tight" style={{ color: item.accent ? withAlpha(item.accent, 0.98) : "rgba(255,255,255,0.84)" }}>
                {item.value}
              </div>
              <div className="mt-0.5 text-[10px] leading-tight" style={{ color: item.accent ? withAlpha(item.accent, 0.72) : "rgba(255,255,255,0.42)" }}>
                {item.detail}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-xl p-3" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}` }}>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(255,255,255,0.26)" }}>
            <Volume2 size={12} />
            Audio
          </div>
          {visualVariants.length > 1 ? (
            <div className="flex flex-wrap gap-1.5">
              {visualVariants.map((variant) => {
                const active = activeVisual?.key === variant.key;

                return (
                  <button
                    key={variant.key}
                    type="button"
                    onClick={() => setActiveVisualKey(variant.key)}
                    className="rounded-full px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                    style={{
                      background: active ? "rgba(94,124,74,0.14)" : NEUTRAL_SURFACE.panelDeep,
                      border: active ? "1px solid rgba(94,124,74,0.24)" : `1px solid ${NEUTRAL_SURFACE.border}`,
                      color: active ? "rgba(225,255,241,0.92)" : "rgba(255,255,255,0.64)",
                    }}
                  >
                    {variant.label}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>

        {activeVisual && overviewMediaReady ? (
          <ClipVisual
            pngUrl={activeVisual.src ?? null}
            audioUrl={decodeAudioUrl}
            mode={activeVisual.mode}
            height={178}
            label={activeVisual.label}
          />
        ) : activeVisual ? (
          <div
            className="flex w-full items-center justify-center rounded-lg text-sm"
            style={{
              minHeight: 178,
              border: `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
              background: NEUTRAL_SURFACE.panelDeep,
              color: "rgba(255,255,255,0.38)",
            }}
          >
            Loading media preview...
          </div>
        ) : (
          <p className="text-sm" style={{ color: "rgba(255,255,255,0.44)" }}>
            No waveform or spectrogram is attached to this clip.
          </p>
        )}

        <div className="mt-3 rounded-lg px-2.5 py-2" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}` }}>
          {processedAudioUrl && overviewMediaReady ? (
            <AssistedAudioPlayer src={processedAudioUrl} className="w-full" style={{ height: 36 }} />
          ) : processedAudioUrl ? (
            <p className="text-sm" style={{ color: "rgba(255,255,255,0.44)" }}>
              Loading audio...
            </p>
          ) : (
            <p className="text-sm" style={{ color: "rgba(255,255,255,0.44)" }}>
              No processed clip is available.
            </p>
          )}
        </div>
      </div>
    </div>
  ) : null;

  const contextContent = activeTab === "context" ? (
    <div className="space-y-3">
      <div className="rounded-xl p-3" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}` }}>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(255,255,255,0.26)" }}>
          Weekly model evidence
        </div>
        {point.reviewContext ? (
          <>
            <div className="flex flex-wrap gap-1.5">
              <span
                className="rounded-full px-2 py-1 text-[10px]"
                style={{
                  background: withAlpha(reviewToneColor(point.reviewContext), 0.12),
                  border: `1px solid ${withAlpha(reviewToneColor(point.reviewContext), 0.24)}`,
                  color: withAlpha(reviewToneColor(point.reviewContext), 0.95),
                }}
              >
                Weekly {Math.round(point.reviewContext.finalScore * 100)}%
              </span>
              <span className="rounded-full px-2 py-1 text-[10px]" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.68)" }}>
                {point.reviewContext.reviewReason}
              </span>
              {point.reviewContext.requiresAudioReview ? (
                <span className="rounded-full px-2 py-1 text-[10px]" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.68)" }}>
                  Audio evidence
                </span>
              ) : null}
              {point.reviewContext.requiresContextReview ? (
                <span className="rounded-full px-2 py-1 text-[10px]" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.68)" }}>
                  Context evidence
                </span>
              ) : null}
            </div>

            {point.reviewContext.anomalyStreams.length > 0 ? (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {point.reviewContext.anomalyStreams.map((stream) => (
                  <span
                    key={stream}
                    className="rounded-full px-2 py-1 text-[10px]"
                    style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.62)" }}
                  >
                    {humanizeStreamLabel(stream)}
                  </span>
                ))}
              </div>
            ) : null}

            <div className="mt-3 grid grid-cols-2 gap-2">
              {reviewMetricCards.map((item) => (
                <div key={item.label} className="rounded-lg px-2.5 py-2" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}` }}>
                  <div className="font-mono text-[9px] uppercase tracking-[0.16em]" style={{ color: "rgba(255,255,255,0.26)" }}>
                    {item.label}
                  </div>
                  <div className="mt-1 text-sm" style={{ color: "rgba(255,255,255,0.86)" }}>
                    {item.value}
                  </div>
                  <div className="mt-1 text-[10px] leading-relaxed" style={{ color: "rgba(255,255,255,0.46)" }}>
                    {item.detail}
                  </div>
                </div>
              ))}
            </div>
          </>
        ) : (
          <p className="text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.58)" }}>
            {modeling.coverageLabel}
            {modeling.generatedAt ? ` Latest run ${formatShortDate(modeling.generatedAt)}.` : ""}
          </p>
        )}
      </div>

      <div className="rounded-xl p-3" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}` }}>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(255,255,255,0.26)" }}>
          Cluster
        </div>
        {point.clusterContext ? (
          <>
            <div className="flex flex-wrap gap-1.5">
              <span
                className="rounded-full px-2 py-1 text-[10px]"
                style={{
                  background: withAlpha(clusterToneColor(point.clusterContext), 0.12),
                  border: `1px solid ${withAlpha(clusterToneColor(point.clusterContext), 0.22)}`,
                  color: withAlpha(clusterToneColor(point.clusterContext), 0.95),
                }}
              >
                {clusterStatusLabel(point.clusterContext)}
              </span>
              <span className="rounded-full px-2 py-1 text-[10px]" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.68)" }}>
                {point.clusterContext.clusterCount} groups
              </span>
              {point.clusterContext.isExemplar ? (
                <span className="rounded-full px-2 py-1 text-[10px]" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.68)" }}>
                  Exemplar
                </span>
              ) : null}
            </div>
            <div className="mt-3 grid gap-1.5 text-[11px]" style={{ color: "rgba(255,255,255,0.64)" }}>
              <div>Window: {point.clusterContext.weeklyPeriodStart ?? "unknown"} to {point.clusterContext.weeklyPeriodEnd ?? "unknown"}</div>
              {point.clusterContext.clusterMemberShare !== null ? (
                <div>Member share: {Math.round(point.clusterContext.clusterMemberShare * 100)}%</div>
              ) : null}
              {point.clusterContext.centroidDistance !== null ? (
                <div>Centroid distance: {Math.round(point.clusterContext.centroidDistance * 100)}%</div>
              ) : null}
            </div>
          </>
        ) : (
          <p className="text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.58)" }}>
            No persisted cluster assignment for this embedding backend.
          </p>
        )}
      </div>

      <div className="rounded-xl p-3" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}` }}>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(255,255,255,0.26)" }}>
          Corroboration
        </div>
        {point.reviewContext ? (
          hasCorroboratingContext(point.reviewContext) ? (
            <div className="flex flex-wrap gap-1.5">
              {(point.reviewContext.sovonLagMaxScore > 0 || point.reviewContext.sovonLagDeadBirds > 0) ? (
                <span className="rounded-full px-2 py-1 text-[10px]" style={{ background: withAlpha(corroborationToneColor(point.reviewContext), 0.12), border: `1px solid ${withAlpha(corroborationToneColor(point.reviewContext), 0.24)}`, color: withAlpha(corroborationToneColor(point.reviewContext), 0.95) }}>
                  SOVON {formatScorePercent(point.reviewContext.sovonLagMaxScore)}
                </span>
              ) : null}
            </div>
          ) : (
            <p className="text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.58)" }}>
              No external corroboration attached to this weekly row.
            </p>
          )
        ) : (
          <p className="text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.58)" }}>
            Corroboration appears when this clip links to the latest weekly modeling artifacts.
          </p>
        )}
      </div>
    </div>
  ) : null;

  const compareContent = activeTab === "compare" ? (
    <div className="space-y-3">
      <div className="rounded-xl p-3" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}` }}>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(255,255,255,0.26)" }}>
          Comparison clips
        </div>
        <div className="space-y-2">
          {comparisonTargets.map((target) => (
            <ComparisonCard key={target.key} target={target} onOpen={onSelectRelated} />
          ))}
        </div>
      </div>

      <div className="rounded-xl p-3" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}` }}>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(255,255,255,0.26)" }}>
          Baselines
        </div>
        {baselinePreview.length > 0 ? (
          <div className="space-y-2">
            {baselinePreview.map((baseline) => {
              const baselineAudio = preferredAudioUrl(baseline);

              return (
                <div key={baseline.id} className="rounded-xl px-3 py-2.5" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}` }}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm" style={{ color: "rgba(255,255,255,0.82)" }}>
                        {baseline.station}
                      </div>
                      <div className="truncate text-[11px]" style={{ color: "rgba(255,255,255,0.46)" }}>
                        {formatShortDate(baseline.detectedAt)} · Baseline {Math.max(0, Math.round((1 - baseline.alertScore) * 100))}%
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => onSelectRelated(baseline.id)}
                      className="rounded-full px-2.5 py-1 text-[11px]"
                      style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.70)" }}
                    >
                      Open
                    </button>
                  </div>

                  {baselineAudio ? (
                    <AssistedAudioPlayer src={baselineAudio} className="mt-2 w-full" style={{ height: 32 }} />
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.44)" }}>
            No same-species baseline clips with playable audio are loaded.
          </p>
        )}
      </div>
    </div>
  ) : null;

  const activeContent = overviewContent ?? contextContent ?? compareContent;

  // Centered over the viewport by default; the home Live Feed pins it to the
  // left, alongside the feed panel, instead of floating over the map.
  const placementClass =
    placement === "left"
      ? "fixed inset-0 z-50 flex items-center justify-start p-3"
      : "fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4";
  const placementStyle =
    placement === "left"
      ? { paddingLeft: "calc(1.25rem + 340px + 0.75rem)" }
      : undefined;

  return (
    <div className={placementClass} style={placementStyle}>
      <button
        type="button"
        className="absolute inset-0"
        style={{ background: "transparent" }}
        onClick={onClose}
        aria-label="Close evidence modal"
      />

      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="alerts-review-title"
        className={`relative z-10 w-full ${showNav ? "max-w-[460px]" : "max-w-[400px]"} overflow-hidden rounded-2xl`}
        style={{
          background: NEUTRAL_SURFACE.panelDeep,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          boxShadow: "0 22px 80px rgba(0,0,0,0.58)",
        }}
      >
        <div className="border-b px-3.5 py-3" style={{ borderColor: NEUTRAL_SURFACE.borderSoft }}>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              {showNav ? (
                <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.18em]" style={{ color: "rgba(255,255,255,0.30)" }}>
                  {String(index + 1).padStart(2, "0")} / {String(total).padStart(2, "0")}
                </div>
              ) : null}
              <h2 id="alerts-review-title" className="truncate text-lg font-semibold tracking-tight" style={{ color: "rgba(255,255,255,0.94)" }}>
                {point.species}
              </h2>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px]" style={{ color: "rgba(255,255,255,0.56)" }}>
                <span>{formatShortDate(point.detectedAt)}</span>
                <span>{point.station}</span>
                {typeof point.latitude === "number" && typeof point.longitude === "number" ? (
                  <span className="font-mono" style={{ color: "rgba(255,255,255,0.46)" }}>
                    {point.latitude.toFixed(4)}, {point.longitude.toFixed(4)}
                  </span>
                ) : null}
              </div>
            </div>

            <div className="flex items-center gap-1.5">
              {showNav ? (
                <>
                  <button
                    type="button"
                    onClick={onPrevious}
                    className="flex h-8 w-8 items-center justify-center rounded-full transition-colors"
                    style={{
                      border: `1px solid ${NEUTRAL_SURFACE.border}`,
                      background: NEUTRAL_SURFACE.panelAlt,
                      color: "rgba(255,255,255,0.68)",
                    }}
                    aria-label="Previous detection"
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <button
                    type="button"
                    onClick={onNext}
                    className="flex h-8 w-8 items-center justify-center rounded-full transition-colors"
                    style={{
                      border: `1px solid ${NEUTRAL_SURFACE.border}`,
                      background: NEUTRAL_SURFACE.panelAlt,
                      color: "rgba(255,255,255,0.68)",
                    }}
                    aria-label="Next detection"
                  >
                    <ChevronRight size={16} />
                  </button>
                </>
              ) : null}
              <button
                ref={closeButtonRef}
                type="button"
                onClick={onClose}
                className="flex h-8 w-8 items-center justify-center rounded-full transition-colors"
                style={{
                  border: `1px solid ${NEUTRAL_SURFACE.border}`,
                  background: NEUTRAL_SURFACE.panelAlt,
                  color: "rgba(255,255,255,0.68)",
                }}
                aria-label="Close evidence modal"
              >
                <X size={16} />
              </button>
            </div>
          </div>
        </div>

        {showTabs ? (
        <div className="border-b px-3 py-2.5" style={{ borderColor: NEUTRAL_SURFACE.borderSoft }}>
          <div
            className="grid grid-cols-3 gap-1 rounded-xl p-1"
            style={{
              background: NEUTRAL_SURFACE.panelAlt,
              border: `1px solid ${NEUTRAL_SURFACE.border}`,
            }}
          >
            {ALERT_REVIEW_TABS.map((tab) => {
              const active = activeTab === tab.value;

              return (
                <button
                  key={tab.value}
                  type="button"
                  onClick={() => {
                    setActiveTab(tab.value);
                    onTabChange(tab.value);
                  }}
                  className="rounded-lg px-2.5 py-2 text-center transition-colors"
                  style={{
                    background: active ? NEUTRAL_SURFACE.panelDeep : "transparent",
                    border: active ? "1px solid rgba(94,124,74,0.24)" : "1px solid transparent",
                    boxShadow: active ? "inset 0 1px 0 rgba(255,255,255,0.04)" : "none",
                    color: active ? "rgba(225,255,241,0.92)" : "rgba(255,255,255,0.56)",
                  }}
                >
                  <div className="font-mono text-[9px] uppercase tracking-[0.18em]">{tab.label}</div>
                </button>
              );
            })}
          </div>
        </div>
        ) : null}

        <div className="sb-scroll max-h-[68vh] overflow-y-auto p-3">
          {activeContent}
        </div>

        {showActions ? (
        <div className="border-t px-3 py-2.5" style={{ borderColor: NEUTRAL_SURFACE.borderSoft }}>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onCopyView}
              className="inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium"
              style={{
                background: NEUTRAL_SURFACE.panelAlt,
                border: `1px solid ${NEUTRAL_SURFACE.border}`,
                color: "rgba(255,255,255,0.72)",
              }}
            >
              <Copy size={12} />
              {copyState === "copied"
                ? "Copied"
                : copyState === "error"
                  ? "Copy failed"
                  : "Copy"}
            </button>
            {sourceAudioUrl ? (
              <a
                href={sourceAudioUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium"
                style={{
                  background: NEUTRAL_SURFACE.panelAlt,
                  border: `1px solid ${NEUTRAL_SURFACE.border}`,
                  color: "rgba(255,255,255,0.72)",
                }}
              >
                Source
                <ArrowUpRight size={12} />
              </a>
            ) : null}
          </div>
        </div>
        ) : null}
      </div>
    </div>
  );
}
