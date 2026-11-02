"use client";

import { useMemo, type ReactNode } from "react";

import {
  filterSpatialEvaluationTableRows,
  SPATIAL_LABEL_NAMES,
  SPATIAL_ALERT_LEVEL_NAMES,
  SPATIAL_VARIANT_NAMES,
  type SpatialEvaluationTableRow,
  type SpatialValidationReport,
} from "./spatial-evaluation";
import type { ModelPerformanceBestF1Row } from "./types";

const S = {
  panelAlt: "#1a1a1a",
  panelDeep: "#101010",
  border: "#2a2a2a",
  borderSoft: "#232323",
} as const;

type GrainKind = "grid" | "admin_area" | "national";

// Each ground-truth source is shown once, at its own native spatial resolution.
// SOVON mortality is scored nationwide (it has no coordinates).
const SOURCE_PLAN: ReadonlyArray<{
  key: string;
  label: string;
  description: string;
  grain: GrainKind;
  unit: string;
}> = [
  {
    key: "sovon_mortality_spike",
    label: "SOVON mortality",
    description:
      "SOVON species-month mortality spikes. No coordinates, so this is tracked as nationwide context, not scored per cell.",
    grain: "national",
    unit: "Nationwide",
  },
];

const UNIT_HINT =
  "Each ground truth is scored at its own native resolution: SOVON mortality is scored nationwide.";

const METRIC_HINTS = {
  positives:
    "Ground-truth positives in the evaluable set — the sample size the rates rest on.",
  hits: "True-positive alerts over all alerts at this operating point.",
  recall: "Share of positives the alert caught within the forward window.",
  precision:
    "Share of alerts that were real positives. Valid only inside the jointly-covered evaluable set.",
  f1: "Harmonic mean of precision and recall.",
  balancedAccuracy:
    "Mean of recall and specificity. This can sit near 50% when recall is 0 but specificity is high.",
  alertRate:
    "Share of eligible cell-weeks the model flagged — the ungameable cost of recall.",
  threshold: "Score cutoff used for this operating point.",
} as const;

function fmtPct(value: number | null, digits = 1): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function fmtPctAdaptive(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const pct = value * 100;
  if (pct === 0) return "0%";
  if (Math.abs(pct) < 0.1) return `${pct.toFixed(3)}%`;
  if (Math.abs(pct) < 1) return `${pct.toFixed(2)}%`;
  return `${pct.toFixed(1)}%`;
}

function fmtCount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-GB");
}

function fmtHitCount(row: SpatialEvaluationTableRow | null): string {
  if (!row) return "—";
  return `${fmtCount(row.truePositiveRows)} / ${fmtCount(row.alertRows)}`;
}

function fmtThreshold(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 100) return value.toPrecision(4);
  if (Number.isInteger(value)) return value.toFixed(0);
  return value.toFixed(3);
}

function variantFor(key: string): string {
  return SPATIAL_VARIANT_NAMES[key] ?? key;
}

function alertLevelFor(key: string): string {
  return SPATIAL_ALERT_LEVEL_NAMES[key] ?? key;
}

function formatHorizon(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "same week";
  return value === 1 ? "1 week" : `${value} weeks`;
}

// Small "?" affordance: explanation on hover (native tooltip), no extra chrome.
function InfoDot({ text }: { text: string }) {
  return (
    <span
      title={text}
      aria-label={text}
      className="ml-1 inline-flex h-3.5 w-3.5 shrink-0 cursor-help items-center justify-center rounded-full align-middle text-[9px] font-semibold leading-none"
      style={{ background: "rgba(255,255,255,0.08)", color: "rgba(255,255,255,0.5)" }}
    >
      ?
    </span>
  );
}

function StatusChip({
  row,
  isNational,
}: {
  row: SpatialEvaluationTableRow | null;
  isNational: boolean;
}) {
  let label: string;
  let background: string;
  let color: string;
  if (isNational) {
    label = "Context";
    background = "rgba(96,165,250,0.12)";
    color = "rgba(147,197,253,0.95)";
  } else if (row?.status === "ok") {
    label = "Evaluable";
    background = "rgba(74,222,128,0.12)";
    color = "rgba(134,239,172,0.95)";
  } else if (row?.status === "no_common_coverage") {
    label = "No overlap";
    background = "rgba(248,113,113,0.12)";
    color = "rgba(252,165,165,0.95)";
  } else if (row?.status === "no_positives") {
    label = "No positives";
    background = "rgba(251,191,36,0.12)";
    color = "rgba(253,224,71,0.95)";
  } else {
    label = "Not evaluable";
    background = "rgba(251,191,36,0.12)";
    color = "rgba(253,224,71,0.95)";
  }
  return (
    <span
      className="inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium"
      style={{ background, color }}
    >
      {label}
    </span>
  );
}

function MetricCell({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <td
      className="px-2 py-2 text-right tabular-nums"
      style={{ color: ok ? "rgba(255,255,255,0.84)" : "rgba(255,255,255,0.34)" }}
    >
      {ok ? children : "—"}
    </td>
  );
}

function HeaderCell({
  label,
  hint,
  align = "right",
}: {
  label: string;
  hint?: string;
  align?: "left" | "right";
}) {
  return (
    <th className={`px-2 py-2 text-xs font-medium ${align === "left" ? "text-left" : "text-right"}`}>
      <span className={`inline-flex items-center ${align === "right" ? "justify-end" : ""}`}>
        {label}
        {hint ? <InfoDot text={hint} /> : null}
      </span>
    </th>
  );
}

function RunStrip({ report }: { report: SpatialValidationReport }) {
  const ctx = report.runContext;
  const window =
    ctx.windowStart && ctx.windowEnd ? `${ctx.windowStart} → ${ctx.windowEnd}` : "—";
  return (
    <div
      className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl px-4 py-2 text-xs"
      style={{
        background: S.panelDeep,
        border: `1px solid ${S.borderSoft}`,
        color: "rgba(255,255,255,0.6)",
      }}
      title={ctx.runId ?? undefined}
    >
      <span className="min-w-0">
        Scored window{" "}
        <span style={{ color: "rgba(255,255,255,0.82)" }}>{window}</span>
      </span>
      <span aria-hidden="true" style={{ color: "rgba(255,255,255,0.28)" }}>
        ·
      </span>
      <span className="shrink-0">
        {ctx.rowsScored !== null ? `${fmtCount(ctx.rowsScored)} rows` : "—"}
      </span>
    </div>
  );
}

function grainLabel(value: string): string {
  if (value === "grid") return "5 km grid";
  if (value === "admin_area") return "Admin area";
  if (value === "national_week") return "National week";
  if (value === "national_month") return "National month";
  return value || "Unknown grain";
}

function claimLabel(value: string): string {
  if (value === "local_grid_early_warning") return "Local early warning";
  if (value === "admin_area_early_warning") return "Admin early warning";
  if (value === "national_trend_alignment") return "Trend-level evidence";
  return value || "Evaluation";
}

function bestF1Label(row: ModelPerformanceBestF1Row): string {
  return SPATIAL_LABEL_NAMES[row.label] ?? row.label.replace(/^label_/, "").replaceAll("_", " ");
}

function BestF1Panel({ rows }: { rows: ModelPerformanceBestF1Row[] }) {
  const topRows = rows
    .filter((row) => row.f1 !== null)
    .slice()
    .sort((left, right) => (right.f1 ?? -1) - (left.f1 ?? -1))
    .slice(0, 5);
  if (topRows.length === 0) return null;

  return (
    <div
      className="rounded-2xl p-4"
      style={{
        background: S.panelAlt,
        border: `1px solid ${S.borderSoft}`,
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold" style={{ color: "rgba(255,255,255,0.9)" }}>
            Best F1 scorecard
          </div>
          <div className="text-xs" style={{ color: "rgba(255,255,255,0.48)" }}>
            Ranked weak-label operating points. Trend-level evidence is not a local early-warning claim.
          </div>
        </div>
        <span className="rounded-full px-2 py-1 text-[11px]" style={{ background: "rgba(96,165,250,0.12)", color: "rgba(147,197,253,0.95)" }}>
          Label-optimized thresholds
        </span>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-5">
        {topRows.map((row, index) => (
          <div
            key={`${row.grain}-${row.signal}-${row.label}-${row.horizonWeeks}-${row.decisionRule}-${index}`}
            className="rounded-xl p-3"
            style={{
              background: S.panelDeep,
              border: `1px solid ${S.borderSoft}`,
            }}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px]" style={{ color: "rgba(255,255,255,0.52)" }}>
                {grainLabel(row.grain)}
              </span>
              <span
                className="rounded-full px-1.5 py-0.5 text-[10px]"
                style={{
                  background: row.claimType === "national_trend_alignment" ? "rgba(96,165,250,0.12)" : "rgba(74,222,128,0.12)",
                  color: row.claimType === "national_trend_alignment" ? "rgba(147,197,253,0.95)" : "rgba(134,239,172,0.95)",
                }}
              >
                {claimLabel(row.claimType)}
              </span>
            </div>
            <div className="mt-2 text-2xl font-semibold tabular-nums" style={{ color: "rgba(255,255,255,0.92)" }}>
              {fmtPct(row.f1)}
            </div>
            <div className="mt-1 truncate text-xs" title={bestF1Label(row)} style={{ color: "rgba(255,255,255,0.68)" }}>
              {bestF1Label(row)}
            </div>
            <div className="mt-2 space-y-1 text-[11px]" style={{ color: "rgba(255,255,255,0.48)" }}>
              <div>{variantFor(row.signal)} · {formatHorizon(row.horizonWeeks)}</div>
              <div>Recall {fmtPctAdaptive(row.recall)} · Precision {fmtPctAdaptive(row.precision)}</div>
              <div>Alert rate {fmtPctAdaptive(row.alertRate)} · positives {fmtCount(row.positiveRows)}</div>
              <div>Coverage {fmtCount(row.commonRows)} rows</div>
              {row.upperBound ? <div>Uncapped upper bound</div> : null}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function findRow(
  grainReport: SpatialValidationReport | null,
  key: string,
  filters: { anomalyType: string; alertLevel: string; horizonWeeks: number },
): SpatialEvaluationTableRow | null {
  if (!grainReport) return null;
  return (
    filterSpatialEvaluationTableRows(grainReport, filters).find(
      (row) => row.groundTruthKey === key,
    ) ?? null
  );
}

type ResolvedRow = {
  plan: (typeof SOURCE_PLAN)[number];
  row: SpatialEvaluationTableRow | null;
  isNational: boolean;
};

export default function AlertsSpatialEvaluation({
  report,
  status,
  bestF1Rows = [],
  activeAnomalyType = "embedding",
  activeAlertLevel = "amber",
  activeHorizon,
}: {
  report: SpatialValidationReport | null;
  status?: "idle" | "loading" | "ready" | "error";
  bestF1Rows?: ModelPerformanceBestF1Row[];
  activeAnomalyType?: string;
  activeAlertLevel?: string;
  activeHorizon?: number;
}) {
  const resolvedHorizon = useMemo(() => {
    if (!report) return activeHorizon ?? 0;
    if (report.horizonsWeeks.includes(activeHorizon ?? Number.NaN)) {
      return activeHorizon ?? 0;
    }
    if (report.horizonsWeeks.includes(3)) return 3;
    return report.horizonsWeeks[0] ?? 0;
  }, [activeHorizon, report]);

  const resolvedRows = useMemo<ResolvedRow[]>(() => {
    if (!report) return [];
    const grains = report.grainReports.length ? report.grainReports : [report];
    const gridReport = grains.find((r) => r.spatialGrain === "grid") ?? null;
    const adminReport = grains.find((r) => r.spatialGrain === "admin_area") ?? null;
    const filters = {
      anomalyType: activeAnomalyType,
      alertLevel: activeAlertLevel,
      horizonWeeks: resolvedHorizon,
    };
    return SOURCE_PLAN.map((plan) => {
      if (plan.grain === "national") {
        return { plan, row: null, isNational: true };
      }
      const grainReport = plan.grain === "grid" ? gridReport : adminReport;
      return { plan, row: findRow(grainReport, plan.key, filters), isNational: false };
    });
  }, [activeAlertLevel, activeAnomalyType, report, resolvedHorizon]);

  if (!report) {
    return (
      <div
        className="flex min-h-[160px] items-center justify-center rounded-xl text-sm"
        style={{
          background: S.panelDeep,
          border: `1px solid ${S.borderSoft}`,
          color: "rgba(255,255,255,0.46)",
        }}
      >
        {status === "loading"
          ? "Loading evaluation…"
          : status === "error"
            ? "Evaluation could not load."
            : "No spatial early-warning evaluation is available yet. Regenerate the validation report once scores exist."}
      </div>
    );
  }

  if (report.evaluationTable.length === 0 && report.grainReports.length === 0) {
    return (
      <div
        className="rounded-xl px-4 py-5 text-sm"
        style={{
          background: S.panelDeep,
          border: `1px solid ${S.borderSoft}`,
          color: "rgba(255,255,255,0.52)",
        }}
      >
        This validation report predates the common-coverage evaluation table. Regenerate the modeling validation report to populate this tab.
      </div>
    );
  }

  const sovonContextRows = report.nationalContext?.sovonContextRows ?? null;

  return (
    <div className="space-y-3">
      <BestF1Panel rows={bestF1Rows} />
      <RunStrip report={report} />

      <div
        className="rounded-2xl p-4"
        style={{ background: S.panelAlt, border: `1px solid ${S.border}` }}
      >
        <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <h4
            className="inline-flex items-center text-sm font-semibold"
            style={{ color: "rgba(255,255,255,0.90)" }}
          >
            Early-warning skill by ground truth
            <InfoDot
              text={`Each ground truth is scored at its native resolution against the ${variantFor(
                activeAnomalyType,
              )} score, ${alertLevelFor(
                activeAlertLevel,
              ).toLowerCase()} decision, over a ${formatHorizon(
                resolvedHorizon,
              )} forward window. Metrics use only jointly-covered alert and ground-truth units; unobserved units are excluded, never counted as negatives.`}
            />
          </h4>
          <span
            className="font-mono text-[10px] uppercase tracking-[0.18em]"
            style={{ color: "rgba(255,255,255,0.4)" }}
          >
            {variantFor(activeAnomalyType)} · {alertLevelFor(activeAlertLevel)} ·{" "}
            {formatHorizon(resolvedHorizon)}
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] border-collapse text-sm">
            <thead>
              <tr
                style={{
                  color: "rgba(255,255,255,0.52)",
                  borderBottom: `1px solid ${S.borderSoft}`,
                }}
              >
                <HeaderCell label="Ground truth" align="left" />
                <HeaderCell label="Spatial unit" hint={UNIT_HINT} align="left" />
                <HeaderCell label="Status" align="left" />
                <HeaderCell label="Positives" hint={METRIC_HINTS.positives} />
                <HeaderCell label="TP / alerts" hint={METRIC_HINTS.hits} />
                <HeaderCell label="Recall" hint={METRIC_HINTS.recall} />
                <HeaderCell label="Precision" hint={METRIC_HINTS.precision} />
                <HeaderCell label="F1" hint={METRIC_HINTS.f1} />
                <HeaderCell label="Bal. acc." hint={METRIC_HINTS.balancedAccuracy} />
                <HeaderCell label="Alert rate" hint={METRIC_HINTS.alertRate} />
                <HeaderCell label="Threshold" hint={METRIC_HINTS.threshold} />
              </tr>
            </thead>
            <tbody>
              {resolvedRows.map(({ plan, row, isNational }) => {
                const ok = !isNational && row?.status === "ok";
                const description =
                  isNational && sovonContextRows !== null
                    ? `${plan.description} ${fmtCount(sovonContextRows)} scored rows carry this context.`
                    : plan.description;
                return (
                  <tr key={plan.key} style={{ borderTop: `1px solid ${S.borderSoft}` }}>
                    <td className="px-2 py-2 text-left" style={{ color: "rgba(255,255,255,0.90)" }}>
                      <span className="inline-flex items-center">
                        {plan.label}
                        <InfoDot text={description} />
                      </span>
                    </td>
                    <td className="px-2 py-2 text-left" style={{ color: "rgba(255,255,255,0.72)" }}>
                      {plan.unit}
                    </td>
                    <td className="px-2 py-2 text-left">
                      <StatusChip row={row} isNational={isNational} />
                    </td>
                    <td
                      className="px-2 py-2 text-right font-semibold tabular-nums"
                      style={{
                        color: isNational
                          ? "rgba(255,255,255,0.34)"
                          : row && row.positiveRows > 0
                            ? "rgba(255,255,255,0.86)"
                            : "rgba(252,165,165,0.76)",
                      }}
                    >
                      {isNational ? "—" : fmtCount(row?.positiveRows ?? null)}
                    </td>
                    <MetricCell ok={!isNational && Boolean(row)}>
                      {fmtHitCount(row ?? null)}
                    </MetricCell>
                    <MetricCell ok={Boolean(ok)}>{fmtPctAdaptive(row?.recall ?? null)}</MetricCell>
                    <MetricCell ok={Boolean(ok)}>{fmtPctAdaptive(row?.precision ?? null)}</MetricCell>
                    <MetricCell ok={Boolean(ok)}>{fmtPctAdaptive(row?.f1 ?? null)}</MetricCell>
                    <MetricCell ok={Boolean(ok)}>{fmtPct(row?.balancedAccuracy ?? null)}</MetricCell>
                    <MetricCell ok={Boolean(ok)}>{fmtPctAdaptive(row?.alertRate ?? null)}</MetricCell>
                    <MetricCell ok={!isNational && Boolean(row)}>
                      {fmtThreshold(row?.threshold ?? null)}
                    </MetricCell>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
