"use client";

import { useEffect, useRef, useState } from "react";
import { Space_Grotesk } from "next/font/google";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import AssistedAudioPlayer from "@/components/audio/AssistedAudioPlayer";
import type {
    SpeciesTrackerActivity,
    SpeciesTrackerAnomalySeriesPoint,
    SpeciesTrackerGrain,
    SpeciesTrackerHotspot,
    SpeciesTrackerMortality,
    SpeciesTrackerMortalityPoint,
    SpeciesTrackerPopulationPoint,
    SpeciesTrackerResponse,
    SpeciesTrackerSeasonalPresenceYear,
    SpeciesTrackerSpeciesOption,
} from "@/app/api/species-tracker/route";
import type {
    SpeciesTrackerDrilldownResponse,
    SpeciesTrackerHourDrilldownResponse,
    SpeciesTrackerStationDrilldownResponse,
} from "@/app/api/species-tracker/drilldown/route";
import type {
    SpeciesTrackerHabitatResponse,
} from "@/app/api/species-tracker/habitat/route";

const trackerTitleFont = Space_Grotesk({
    subsets: ["latin"],
    weight: ["500", "600"],
    display: "swap",
});
const ACCENT = "#34d399";
const PANEL_BG = "rgba(20,20,20,0.94)";
const PANEL_SURFACE = "rgba(24,24,24,0.88)";
const PANEL_SURFACE_ALT = "rgba(30,30,30,0.88)";
const PANEL_SURFACE_DEEP = "rgba(14,14,14,0.97)";
const PANEL_CHROME = "rgba(255,255,255,0.12)";
const PANEL_SHADOW = "0 18px 48px rgba(0,0,0,0.74), inset 0 1px 0 rgba(255,255,255,0.03)";
const BORDER = "rgba(255, 255, 255, 0.08)";
const TEXT = "rgba(255,255,255,0.92)";
const TEXT_DIM = "rgba(255,255,255,0.64)";
const TEXT_SOFT = "rgba(255,255,255,0.38)";
const MONO = "'IBM Plex Mono', monospace";

// Shared styles for chart card surfaces — Pulse/Rhythm/Mortality tabs all use
// the same card chrome so we hoist them out of the component bodies.
const CHART_CARD_STYLE: React.CSSProperties = {
    borderRadius: 12,
    border: "1px solid rgba(255,255,255,0.07)",
    background: "rgba(24,24,24,0.88)",
    padding: "10px 10px 9px",
    display: "flex",
    flexDirection: "column",
    gap: 8,
    flexShrink: 0,
};
const CARD_TITLE_STYLE: React.CSSProperties = {
    fontSize: 11,
    color: "rgba(255,255,255,0.92)",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    fontWeight: 600,
};

type TrackerTab = "activity" | "climate" | "risk";
type TooltipPayloadEntry = { value?: number | string | null; dataKey?: string | number | null; payload?: Record<string, unknown> };
type SpeciesTrackerGrainCache = Partial<Record<SpeciesTrackerGrain, SpeciesTrackerResponse>>;
type SpeciesTrackerResponseCache = Record<string, SpeciesTrackerGrainCache>;

const GRAIN_OPTIONS: { key: SpeciesTrackerGrain; label: string }[] = [
    { key: "day",   label: "d" },
    { key: "week",  label: "w" },
    { key: "month", label: "m" },
];

function speciesCacheKey(speciesId: string): string {
    return speciesId || "__default__";
}

function firstCachedResponse(cache: SpeciesTrackerGrainCache | undefined): SpeciesTrackerResponse | null {
    if (!cache) return null;

    for (const option of GRAIN_OPTIONS) {
        const response = cache[option.key];
        if (response) return response;
    }

    return null;
}

function parseUtcDate(value: string): Date | null {
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed : null;
}

function formatCompactCount(value: number): string {
    if (!Number.isFinite(value)) return "0";
    if (Math.abs(value) >= 1000) {
        return `${(value / 1000).toFixed(Math.abs(value) >= 10000 ? 0 : 1)}k`;
    }
    return `${Math.round(value)}`;
}

function formatPercent(value: number | null): string {
    if (value == null || !Number.isFinite(value)) return "—";
    const rounded = Math.round(value * 10) / 10;
    return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}%`;
}

function formatConfidence(value: number | null): string {
    if (value == null || !Number.isFinite(value)) return "—";
    return `${Math.round(value * 100)}%`;
}

function formatUtcDateTime(value: string | null): string {
    if (!value) return "—";
    const parsed = parseUtcDate(value);
    if (!parsed) return value;
    return parsed.toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
    });
}

type SeasonalPresenceChartRow = {
    label: string;
    month: number;
    currentYear: number | null;
    historicalAverage: number | null;
};

type SeasonalPresenceChartData = {
    rows: SeasonalPresenceChartRow[];
    currentYear: number | null;
    baselineYearCount: number;
};

function buildSeasonalPresenceChartData(
    years: SpeciesTrackerSeasonalPresenceYear[],
): SeasonalPresenceChartData {
    // No history at all → nothing to draw.
    if (years.length === 0) {
        return { rows: [], currentYear: null, baselineYearCount: 0 };
    }

    // Helper returns ascending years already, but sort defensively so we
    // pick the latest as "current" regardless of input order.
    const sortedYears = [...years].sort((left, right) => left.year - right.year);
    const currentYearEntry = sortedYears[sortedYears.length - 1];
    const priorYears = sortedYears.slice(0, -1);

    const rows: SeasonalPresenceChartRow[] = Array.from({ length: 12 }, (_, index) => {
        const month = index + 1;
        const label = new Date(Date.UTC(2025, index, 1)).toLocaleDateString("en-GB", {
            month: "short",
            timeZone: "UTC",
        });

        // Use relative-abundance share, not temporal occupancy — the latter
        // saturates at 100% at network grain because a single station hearing
        // the species anywhere in NL flips the day to "active".
        const currentValue = currentYearEntry.series[index]?.relativeAbundancePct ?? null;

        // Historical average across prior years for this same month. Missing
        // (year, month) entries are treated as 0% because species_tracker_tbl
        // only writes rows where detections exist for that species in that
        // period — absence means the species was 0% of detections, not
        // unknown.
        let historicalAverage: number | null = null;
        if (priorYears.length > 0) {
            const sum = priorYears.reduce((acc, year) => {
                return acc + (year.series[index]?.relativeAbundancePct ?? 0);
            }, 0);
            historicalAverage = Math.round((sum / priorYears.length) * 100) / 100;
        }

        return {
            label,
            month,
            currentYear: currentValue,
            historicalAverage,
        };
    });

    return {
        rows,
        currentYear: currentYearEntry.year,
        baselineYearCount: priorYears.length,
    };
}

// Mirrors min_station_availability_ratio in
// src/avian_biosurveillance/modeling/count_baseline.py — the population anomaly
// model's A1 station-availability gate. KEEP IN SYNC with that constant. A
// bucket whose community station footprint falls below this fraction of its
// historical norm is flagged low-coverage: a shrunken sensor footprint depresses
// detections and would otherwise read as a population dip rather than a coverage
// artifact. The model holds such weeks out of its baseline; here we surface them
// visually so a dip can be read in context instead of silently dropped.
const STATION_AVAILABILITY_MIN_RATIO = 0.5;

// Effort-aware population-activity series for the "Activity index" chart. Unlike
// Relative abundance (this species' share of all detections), the activity index
// is the same composite the population anomaly model scores against — abundance,
// temporal occupancy, station presence, and detections-per-active-station-day —
// so it is far less distorted by how many stations were online. Plotted straight
// from population.series: the per-bucket activityIndex (current, solid) against
// historicalAverageActivityIndex (baseline, dashed), with the community station
// footprint overlaid and low-coverage buckets flagged. No expected/anomaly band
// is drawn — the seasonal baseline isn't deep enough yet for that to be honest.
type ActivityIndexChartRow = {
    label: string;
    activityIndex: number | null;
    historicalAverageActivityIndex: number | null;
    communityActiveStations: number | null;
    lowCoverage: boolean;
    // activityIndex echoed only where coverage is low, so the chart can drop an
    // amber marker on exactly those points without a custom dot renderer.
    lowCoverageMarker: number | null;
};

type ActivityIndexChartData = {
    rows: ActivityIndexChartRow[];
    hasData: boolean;
    hasBaseline: boolean;
    hasStations: boolean;
    hasLowCoverage: boolean;
};

function buildActivityIndexChartData(
    series: SpeciesTrackerPopulationPoint[],
): ActivityIndexChartData {
    const rows: ActivityIndexChartRow[] = series.map((point) => {
        const activityIndex = Number.isFinite(point.activityIndex) ? point.activityIndex : null;
        const communityActiveStations = Number.isFinite(point.communityActiveStations)
            ? point.communityActiveStations
            : null;
        const expectedStations =
            point.historicalAverageCommunityActiveStations != null &&
            Number.isFinite(point.historicalAverageCommunityActiveStations) &&
            point.historicalAverageCommunityActiveStations > 0
                ? point.historicalAverageCommunityActiveStations
                : null;
        // A1 gate: observed community footprint vs its historical norm. (The
        // model uses a seasonal-window median over prior years; the backend here
        // supplies an average over the same-period network years — same intent
        // and the same threshold, robust enough for a visual coverage cue.)
        const ratio =
            communityActiveStations != null && expectedStations != null
                ? communityActiveStations / expectedStations
                : null;
        const lowCoverage = ratio != null && ratio < STATION_AVAILABILITY_MIN_RATIO;
        return {
            label: point.label,
            activityIndex,
            historicalAverageActivityIndex:
                point.historicalAverageActivityIndex != null &&
                Number.isFinite(point.historicalAverageActivityIndex)
                    ? point.historicalAverageActivityIndex
                    : null,
            communityActiveStations,
            lowCoverage,
            lowCoverageMarker: lowCoverage ? activityIndex : null,
        };
    });
    return {
        rows,
        hasData: rows.some((row) => row.activityIndex != null),
        hasBaseline: rows.some((row) => row.historicalAverageActivityIndex != null),
        hasStations: rows.some((row) => row.communityActiveStations != null),
        hasLowCoverage: rows.some((row) => row.lowCoverage),
    };
}

function tooltipValue(payload: TooltipPayloadEntry[] | undefined, key: string): number | null {
    if (!payload?.length) return null;
    const match = payload.find((entry) => String(entry.dataKey ?? "") === key);
    if (match?.value == null) return null;
    const parsed = Number(match.value);
    return Number.isFinite(parsed) ? parsed : null;
}

// === Status-chip tone classification ===========================================
// Maps a magnitude-of-anomaly to NN/g status colors. Thresholds are the same on
// the positive and negative side because "way below baseline" is just as much a
// surveillance signal as "way above" — quiet birds can mean ongoing mortality.
type ChipTone = "neutral" | "watch" | "elevated" | "critical";

// === Mortality elevation =======================================================
// "Elevated" = recent month observations vs historical average for that month.
// Returns null when there is no signal worth surfacing — empty state should
// remain quiet rather than show a green "all clear" chip that crowds the header.
type MortalityElevation = { ratio: number; recent: number; tone: ChipTone } | null;

function classifyMortalitySignal(mortality: SpeciesTrackerMortality | null): MortalityElevation {
    if (!mortality) return null;
    const series = mortality.series;
    if (!series || series.length === 0) return null;

    // Walk backwards for the most recent non-zero bucket so an empty December
    // doesn't drown a real signal in November.
    let recentPoint: SpeciesTrackerMortalityPoint | null = null;
    for (let index = series.length - 1; index >= 0; index -= 1) {
        const candidate = series[index];
        if (candidate && candidate.count > 0) {
            recentPoint = candidate;
            break;
        }
    }
    if (!recentPoint) return null;

    const baseline = recentPoint.historicalAverageCount;
    if (baseline == null || baseline <= 0) {
        // No baseline yet — only surface when the raw count is clearly nontrivial.
        return recentPoint.count >= 5
            ? { ratio: Number.POSITIVE_INFINITY, recent: recentPoint.count, tone: "watch" }
            : null;
    }

    const ratio = recentPoint.count / baseline;
    if (ratio < 1.3) return null; // within normal seasonal noise

    const tone: ChipTone = ratio >= 3 ? "critical" : ratio >= 2 ? "elevated" : "watch";
    return { ratio, recent: recentPoint.count, tone };
}

// Pick the most-alarming mortality signal across the available mortality
// series so the header chip fires on the strongest signal. Sources without a
// signal return null and are skipped.
function pickMostElevatedMortality(
    sources: ReadonlyArray<SpeciesTrackerMortality | null | undefined>,
): { signal: NonNullable<MortalityElevation>; source: SpeciesTrackerMortality } | null {
    let best: { signal: NonNullable<MortalityElevation>; source: SpeciesTrackerMortality } | null = null;
    for (const candidate of sources) {
        if (!candidate) continue;
        const signal = classifyMortalitySignal(candidate);
        if (!signal) continue;
        if (!best || signal.ratio > best.signal.ratio) {
            best = { signal, source: candidate };
        }
    }
    return best;
}

// === Rhythm helpers ============================================================
// derivePeakHourBand finds the contiguous 3-hour window with the highest total
// detections (sliding wrap-around so a 22-1am peak still scores correctly). The
// "peak" within the band is the single highest-count hour inside the window so
// captions can name it.
type HourlyPoint = { hour: number; detections: number };
type PeakBand = { start: number; end: number; peak: number; share: number };

function derivePeakHourBand(hourly: readonly HourlyPoint[] | undefined): PeakBand | null {
    if (!hourly || hourly.length === 0) return null;
    const buckets = new Array<number>(24).fill(0);
    let total = 0;
    for (const point of hourly) {
        if (point.hour >= 0 && point.hour < 24) {
            const value = Math.max(0, Number(point.detections) || 0);
            buckets[point.hour] = value;
            total += value;
        }
    }
    if (total <= 0) return null;

    let bestStart = 0;
    let bestSum = -1;
    for (let start = 0; start < 24; start += 1) {
        const sum = buckets[start] + buckets[(start + 1) % 24] + buckets[(start + 2) % 24];
        if (sum > bestSum) {
            bestSum = sum;
            bestStart = start;
        }
    }

    let peakHour = bestStart;
    let peakValue = buckets[bestStart];
    for (let offset = 1; offset < 3; offset += 1) {
        const hour = (bestStart + offset) % 24;
        if (buckets[hour] > peakValue) {
            peakValue = buckets[hour];
            peakHour = hour;
        }
    }

    return {
        start: bestStart,
        end: (bestStart + 2) % 24,
        peak: peakHour,
        share: bestSum / total,
    };
}

// Caption follows ornithology convention rather than strict clock bands:
//   dawn caller : peak hour 4–8
//   diurnal     : peak hour 9–16
//   dusk caller : peak hour 17–21
//   overnight caller : peak hour 22–3 (wraps)
// Buckets overlap at the edges deliberately — a 4am peak reads "dawn caller"
// even though the previous hour is still dark.
function deriveRhythmCaption(peak: PeakBand | null): string {
    if (!peak) return "no rhythm data";
    const hour = peak.peak;
    if (hour >= 4 && hour <= 8) return "dawn caller";
    if (hour >= 9 && hour <= 16) return "diurnal";
    if (hour >= 17 && hour <= 21) return "dusk caller";
    return "overnight caller";
}

function formatHour24(hour: number): string {
    const wrapped = ((hour % 24) + 24) % 24;
    return `${String(wrapped).padStart(2, "0")}:00`;
}

function formatPeakBand(band: PeakBand): string {
    // End is the last hour IN the band; show the inclusive-exclusive window so
    // "05:00–08:00" reads as "from 5am through to 8am".
    const endExclusive = (band.end + 1) % 24;
    return `${formatHour24(band.start)}–${formatHour24(endExclusive)}`;
}

type AnomalyMetricKey = "avg" | "amber" | "red";
const ANOMALY_METRIC_OPTIONS: ReadonlyArray<{ key: AnomalyMetricKey; label: string }> = [
    { key: "avg", label: "avg score" },
    { key: "amber", label: "amber" },
    { key: "red", label: "red" },
];

type AnomalyScoreKey = "bioacoustic" | "fusion";
const ANOMALY_SCORE_OPTIONS: ReadonlyArray<{ key: AnomalyScoreKey; label: string }> = [
    { key: "bioacoustic", label: "bioacoustic" },
    { key: "fusion", label: "fusion" },
];

// Small popover select over a fixed set of string-keyed options, styled to match
// YearSelect. Used for the anomaly score and metric toggles (avg score / amber /
// red).
function ChipSelect<T extends string>({
    value,
    options,
    onChange,
    loading,
    disabled,
    minWidth = 104,
}: {
    value: T;
    options: ReadonlyArray<{ key: T; label: string }>;
    onChange: (next: T) => void;
    loading?: boolean;
    disabled?: boolean;
    minWidth?: number;
}) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!open) return;
        const onDocMouseDown = (event: MouseEvent) => {
            if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
        };
        document.addEventListener("mousedown", onDocMouseDown);
        return () => document.removeEventListener("mousedown", onDocMouseDown);
    }, [open]);

    const current = options.find((option) => option.key === value) ?? options[0];

    return (
        <div ref={ref} style={{ position: "relative", flexShrink: 0 }}>
            <button
                type="button"
                disabled={disabled}
                onClick={() => { if (!disabled) setOpen((prev) => !prev); }}
                style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 5,
                    border: `1px solid ${BORDER}`,
                    background: "rgba(255,255,255,0.05)",
                    color: TEXT_DIM,
                    borderRadius: 8,
                    padding: "4px 8px",
                    fontSize: 9,
                    fontFamily: MONO,
                    letterSpacing: "0.02em",
                    cursor: disabled ? "default" : "pointer",
                    opacity: disabled ? 0.4 : loading ? 0.6 : 1,
                    transition: "opacity 140ms ease",
                }}
            >
                <span>{current?.label ?? ""}</span>
                <span aria-hidden="true" style={{ fontSize: 8, color: TEXT_SOFT }}>&#9662;</span>
            </button>
            {open && !disabled ? (
                <div
                    style={{
                        position: "absolute",
                        right: 0,
                        top: "calc(100% + 4px)",
                        zIndex: 30,
                        minWidth,
                        background: "#15171a",
                        border: `1px solid ${BORDER}`,
                        borderRadius: 10,
                        padding: 4,
                        boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 2,
                    }}
                >
                    {options.map((option) => {
                        const active = option.key === value;
                        return (
                            <button
                                key={option.key}
                                type="button"
                                onClick={() => {
                                    onChange(option.key);
                                    setOpen(false);
                                }}
                                style={{
                                    textAlign: "left",
                                    border: "none",
                                    background: active ? "rgba(52,211,153,0.14)" : "transparent",
                                    color: active ? ACCENT : TEXT_DIM,
                                    borderRadius: 6,
                                    padding: "5px 8px",
                                    fontSize: 9,
                                    fontFamily: MONO,
                                    cursor: "pointer",
                                }}
                            >
                                {option.label}
                            </button>
                        );
                    })}
                </div>
            ) : null}
        </div>
    );
}

function YearSelect({
    value,
    years,
    onChange,
    loading,
}: {
    value: number | null;
    years: number[];
    onChange: (next: number) => void;
    loading?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!open) return;
        const onDocMouseDown = (event: MouseEvent) => {
            if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
        };
        document.addEventListener("mousedown", onDocMouseDown);
        return () => document.removeEventListener("mousedown", onDocMouseDown);
    }, [open]);

    const sortedYears = [...years].sort((a, b) => b - a);
    const sortedAsc = [...years].sort((a, b) => a - b);
    const current = value ?? (sortedAsc.length > 0 ? sortedAsc[0] : null);
    // With a single available year there is nothing to pick — render it as a
    // static chip rather than an interactive dropdown.
    const single = sortedYears.length <= 1;

    return (
        <div ref={ref} style={{ position: "relative", flexShrink: 0 }}>
            <button
                type="button"
                onClick={() => {
                    if (!single) setOpen((prev) => !prev);
                }}
                disabled={single}
                style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 5,
                    border: `1px solid ${BORDER}`,
                    background: "rgba(255,255,255,0.05)",
                    color: TEXT_DIM,
                    borderRadius: 8,
                    padding: "4px 8px",
                    fontSize: 9,
                    fontFamily: MONO,
                    letterSpacing: "0.02em",
                    cursor: single ? "default" : "pointer",
                    opacity: loading ? 0.6 : 1,
                    transition: "opacity 140ms ease",
                }}
            >
                <span>{current ?? "\u2014"}</span>
                {!single ? (
                    <span aria-hidden="true" style={{ fontSize: 8, color: TEXT_SOFT }}>&#9662;</span>
                ) : null}
            </button>
            {open && !single ? (
                <div
                    style={{
                        position: "absolute",
                        right: 0,
                        top: "calc(100% + 4px)",
                        zIndex: 30,
                        minWidth: 84,
                        background: "#15171a",
                        border: `1px solid ${BORDER}`,
                        borderRadius: 10,
                        padding: 4,
                        boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 2,
                        maxHeight: 184,
                        overflowY: "auto",
                    }}
                >
                    {sortedYears.map((year) => {
                        const active = year === current;
                        return (
                            <button
                                key={year}
                                type="button"
                                onClick={() => {
                                    onChange(year);
                                    setOpen(false);
                                }}
                                style={{
                                    textAlign: "left",
                                    border: "none",
                                    background: active ? "rgba(52,211,153,0.14)" : "transparent",
                                    color: active ? ACCENT : TEXT_DIM,
                                    borderRadius: 6,
                                    padding: "5px 8px",
                                    fontSize: 9,
                                    fontFamily: MONO,
                                    cursor: "pointer",
                                }}
                            >
                                {year}
                            </button>
                        );
                    })}
                </div>
            ) : null}
        </div>
    );
}

function SkeletonBlock({
    width,
    height,
    radius = 8,
    style,
}: {
    width: number | string;
    height: number | string;
    radius?: number;
    style?: React.CSSProperties;
}) {
    return (
        <div
            aria-hidden="true"
            style={{
                width,
                height,
                borderRadius: radius,
                background: "linear-gradient(90deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.11) 50%, rgba(255,255,255,0.04) 100%)",
                backgroundSize: "200% 100%",
                animation: "st-skeleton 1.55s ease-in-out infinite",
                ...style,
            }}
        />
    );
}

function SpeciesThumbFallback({ size }: { size: number }) {
    const ringSize = Math.max(12, Math.round(size * 0.42));

    return (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: Math.max(3, Math.round(size * 0.08)) }}>
            <div
                style={{
                    width: ringSize,
                    height: ringSize,
                    borderRadius: "50%",
                    border: "1px solid rgba(255,255,255,0.16)",
                    background: "rgba(255,255,255,0.03)",
                }}
            />
            <div
                style={{
                    width: Math.max(10, Math.round(size * 0.34)),
                    height: 2,
                    borderRadius: 999,
                    background: "rgba(255,255,255,0.14)",
                }}
            />
        </div>
    );
}

function TrackerLoadingState() {
    const cardStyle: React.CSSProperties = {
        borderRadius: 12,
        border: "1px solid rgba(255,255,255,0.07)",
        background: PANEL_SURFACE,
        padding: "10px 10px 9px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        flexShrink: 0,
    };

    return (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px 14px" }}>
            <div style={cardStyle}>
                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ minWidth: 0, flex: "1 1 220px", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        <SkeletonBlock width={92} height={11} radius={999} />
                    </div>
                    <div style={{ display: "flex", gap: 3 }}>
                        <SkeletonBlock width={24} height={22} radius={999} />
                        <SkeletonBlock width={24} height={22} radius={999} />
                        <SkeletonBlock width={24} height={22} radius={999} />
                    </div>
                </div>

                <div
                    style={{
                        height: 152,
                        borderRadius: 10,
                        border: "1px solid rgba(255,255,255,0.05)",
                        background: "rgba(255,255,255,0.02)",
                        padding: "14px 10px 10px",
                        display: "flex",
                        alignItems: "flex-end",
                        gap: 7,
                    }}
                >
                    {[44, 62, 78, 54, 96, 70, 86, 58, 74, 49].map((height, index) => (
                        <SkeletonBlock
                            key={`population-loading-bar-${index}`}
                            width="100%"
                            height={height}
                            radius={999}
                            style={{
                                flex: 1,
                                minWidth: 0,
                                opacity: 0.65 + ((index % 3) * 0.08),
                            }}
                        />
                    ))}
                </div>
            </div>

            <div style={cardStyle}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                    <SkeletonBlock width={78} height={11} radius={999} />
                    <div style={{ display: "flex", gap: 3 }}>
                        <SkeletonBlock width={46} height={22} radius={999} />
                        <SkeletonBlock width={72} height={22} radius={999} />
                    </div>
                </div>
                <div style={{ height: 152, display: "flex", flexDirection: "column", justifyContent: "space-between", padding: "8px 0 2px" }}>
                    {[0, 1, 2, 3].map((rowIndex) => (
                        <div key={`mortality-loading-row-${rowIndex}`} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <SkeletonBlock width={30} height={8} radius={999} style={{ opacity: 0.5 }} />
                            <SkeletonBlock width="100%" height={6} radius={999} style={{ flex: 1 }} />
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}

function TrackerEmptyState({ speciesName }: { speciesName: string | null }) {
    return (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px 14px" }}>
            <div
                style={{
                    borderRadius: 12,
                    border: "1px solid rgba(255,255,255,0.07)",
                    background: PANEL_SURFACE,
                    padding: "16px 16px 18px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                }}
            >
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <div
                        style={{
                            fontSize: 11,
                            fontFamily: trackerTitleFont.style.fontFamily,
                            color: TEXT,
                            letterSpacing: "0.08em",
                            textTransform: "uppercase",
                            fontWeight: 600,
                        }}
                    >
                        Population
                    </div>
                    <span
                        style={{
                            padding: "2px 6px",
                            borderRadius: 999,
                            border: `1px solid ${BORDER}`,
                            color: TEXT_SOFT,
                            fontSize: 9,
                            fontFamily: MONO,
                            letterSpacing: "0.06em",
                        }}
                    >
                        awaiting detail
                    </span>
                </div>
                <div
                    className={trackerTitleFont.className}
                    style={{ fontSize: 18, color: TEXT, lineHeight: 1.15, fontWeight: 500 }}
                >
                    No tracker detail available yet
                </div>
                <div style={{ fontSize: 11, color: TEXT_DIM, lineHeight: 1.55, maxWidth: 340 }}>
                    {speciesName
                        ? `${speciesName} is available in the selector, but this panel does not have population detail yet.`
                        : "This panel does not have population detail yet for the current selection."}
                </div>
            </div>
        </div>
    );
}

function SpeciesThumb({
    url,
    name,
    size = 32,
    loading = false,
}: {
    url: string | null | undefined;
    name: string | undefined;
    size?: number;
    loading?: boolean;
}) {
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        setFailed(false);
    }, [url]);

    const radius = Math.round(size * 0.25);
    const initials = name?.trim().slice(0, 2).toUpperCase() ?? "";

    return (
        <div
            style={{
                width: size,
                height: size,
                borderRadius: radius,
                overflow: "hidden",
                background: PANEL_SURFACE_ALT,
                flexShrink: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
            }}
        >
            {loading ? (
                <SkeletonBlock width={Math.round(size * 0.62)} height={Math.round(size * 0.62)} radius={Math.round(size * 0.22)} />
            ) : url && !failed ? (
                // PhyloPic silhouette from a remote CDN, rendered with an
                // invert filter at a dynamic size; next/image's fixed-dimension
                // model doesn't fit. Matches the SpeciesPortrait images below.
                // eslint-disable-next-line @next/next/no-img-element
                <img
                    src={url}
                    alt=""
                    style={{ width: "85%", height: "85%", objectFit: "contain", filter: "invert(1)" }}
                    onError={() => setFailed(true)}
                />
            ) : (
                initials ? (
                    <span
                        style={{
                            fontSize: Math.round(size * 0.33),
                            color: TEXT_SOFT,
                            fontFamily: trackerTitleFont.style.fontFamily,
                            fontWeight: 600,
                            letterSpacing: "0.04em",
                        }}
                    >
                        {initials}
                    </span>
                ) : (
                    <SpeciesThumbFallback size={size} />
                )
            )}
        </div>
    );
}

function InfoHint({
    label,
    children,
}: {
    label: string;
    children: React.ReactNode;
}) {
    const [open, setOpen] = useState(false);

    return (
        <span
            style={{ position: "relative", display: "inline-flex", alignItems: "center" }}
            onMouseEnter={() => setOpen(true)}
            onMouseLeave={() => setOpen(false)}
        >
            <button
                type="button"
                aria-label={`About ${label}`}
                onFocus={() => setOpen(true)}
                onBlur={() => setOpen(false)}
                style={{
                    width: 16,
                    height: 16,
                    borderRadius: 999,
                    border: `1px solid ${BORDER}`,
                    background: "rgba(255,255,255,0.04)",
                    color: TEXT_SOFT,
                    fontSize: 10,
                    fontFamily: MONO,
                    lineHeight: 1,
                    cursor: "help",
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    padding: 0,
                    outline: "none",
                }}
            >
                ?
            </button>
            {open && (
                <span
                    role="tooltip"
                    style={{
                        position: "absolute",
                        top: "calc(100% + 8px)",
                        left: 0,
                        minWidth: 220,
                        maxWidth: 260,
                        padding: "8px 10px",
                        borderRadius: 10,
                        border: `1px solid ${BORDER}`,
                        background: "rgba(10,10,10,0.96)",
                        color: TEXT_DIM,
                        fontSize: 10,
                        lineHeight: 1.5,
                        boxShadow: PANEL_SHADOW,
                        zIndex: 30,
                        pointerEvents: "none",
                    }}
                >
                    {children}
                </span>
            )}
        </span>
    );
}

function SpeciesDropdown({
    options,
    selectedId,
    onSelect,
    loading = false,
}: {
    options: SpeciesTrackerSpeciesOption[];
    selectedId: string;
    onSelect: (id: string) => void;
    loading?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const selected = options.find((o) => o.speciesId === selectedId) ?? options[0] ?? null;
    const showLoadingState = loading && options.length === 0;

    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, []);

    useEffect(() => {
        if (showLoadingState) {
            setOpen(false);
        }
    }, [showLoadingState]);

    return (
        <div ref={ref} style={{ position: "relative", zIndex: open ? 80 : 1 }}>
            <button
                disabled={showLoadingState}
                aria-busy={showLoadingState}
                onClick={() => {
                    if (!showLoadingState && options.length > 0) {
                        setOpen((v) => !v);
                    }
                }}
                style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "9px 11px",
                    borderRadius: 12,
                    border: `1px solid ${open ? "rgba(255,255,255,0.14)" : BORDER}`,
                    background: open ? PANEL_SURFACE_ALT : PANEL_SURFACE,
                    color: TEXT,
                    cursor: showLoadingState ? "default" : "pointer",
                    outline: "none",
                    boxShadow: open
                        ? PANEL_SHADOW
                        : "inset 0 1px 0 rgba(255,255,255,0.03)",
                    transition: "border-color 0.15s, box-shadow 0.15s",
                    textAlign: "left",
                }}
            >
                {showLoadingState ? (
                    <>
                        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
                            <SkeletonBlock width="58%" height={13} radius={999} />
                        </div>
                        <SkeletonBlock width={10} height={10} radius={999} style={{ flexShrink: 0, opacity: 0.72 }} />
                    </>
                ) : (
                    <>
                        <div style={{ flex: 1, minWidth: 0 }}>
                            <div
                                className={trackerTitleFont.className}
                                style={{
                                    fontSize: 13.5,
                                    color: TEXT,
                                    lineHeight: 1.3,
                                    fontWeight: 500,
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    whiteSpace: "nowrap",
                                }}
                            >
                                {selected?.commonName ?? "—"}
                            </div>
                        </div>
                        <svg
                            width="10"
                            height="10"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="rgba(255,255,255,0.5)"
                            strokeWidth="2.5"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            style={{
                                flexShrink: 0,
                                transition: "transform 0.15s",
                                transform: open ? "rotate(180deg)" : "rotate(0deg)",
                            }}
                        >
                            <path d="M6 9l6 6 6-6" />
                        </svg>
                    </>
                )}
            </button>

            {open && options.length > 0 && (
                <div
                    className="species-dropdown-list"
                    style={{
                        position: "absolute",
                        top: "calc(100% + 5px)",
                        left: 0,
                        right: 0,
                        zIndex: 90,
                        background: PANEL_SURFACE_DEEP,
                        border: `1px solid ${BORDER}`,
                        borderRadius: 12,
                        boxShadow: PANEL_SHADOW,
                        backdropFilter: "blur(24px)",
                        maxHeight: 220,
                        overflowY: "auto",
                        padding: 4,
                        isolation: "isolate",
                    }}
                >
                    {options.map((option) => {
                        const isActive = option.speciesId === selectedId;
                        return (
                            <button
                                key={option.speciesId}
                                onClick={() => {
                                    onSelect(option.speciesId);
                                    setOpen(false);
                                }}
                                style={{
                                    width: "100%",
                                    display: "flex",
                                    alignItems: "center",
                                    gap: 9,
                                    padding: "7px 8px",
                                    borderRadius: 8,
                                    border: "none",
                                    background: isActive ? "rgba(255,255,255,0.06)" : "transparent",
                                    cursor: "pointer",
                                    textAlign: "left",
                                    transition: "background 0.1s",
                                }}
                                onMouseEnter={(e) => {
                                    if (!isActive) {
                                        e.currentTarget.style.background = "rgba(255,255,255,0.04)";
                                    }
                                }}
                                onMouseLeave={(e) => {
                                    if (!isActive) e.currentTarget.style.background = "transparent";
                                }}
                            >
                                <SpeciesThumb
                                    url={option.silhouetteUrl ?? option.thumbnailUrl ?? option.imageUrl}
                                    name={option.commonName}
                                    size={28}
                                />
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div
                                        className={trackerTitleFont.className}
                                        style={{
                                            fontSize: 12.5,
                                            color: TEXT,
                                            lineHeight: 1.25,
                                            fontWeight: isActive ? 500 : 400,
                                            overflow: "hidden",
                                            textOverflow: "ellipsis",
                                            whiteSpace: "nowrap",
                                        }}
                                    >
                                        {option.commonName}
                                        {option.isTarget && (
                                            <span style={{ marginLeft: 5, fontSize: 8, color: ACCENT }}>
                                                ●
                                            </span>
                                        )}
                                    </div>
                                    {option.scientificName && (
                                        <div
                                            style={{
                                                marginTop: 2,
                                                fontSize: 9.5,
                                                color: TEXT_SOFT,
                                                fontFamily: MONO,
                                                fontStyle: "italic",
                                                overflow: "hidden",
                                                textOverflow: "ellipsis",
                                                whiteSpace: "nowrap",
                                            }}
                                        >
                                            {option.scientificName}
                                        </div>
                                    )}
                                </div>
                                {isActive && (
                                    <svg
                                        width="9"
                                        height="9"
                                        viewBox="0 0 24 24"
                                        fill="none"
                                        stroke={ACCENT}
                                        strokeWidth="2.5"
                                        strokeLinecap="round"
                                        strokeLinejoin="round"
                                        style={{ flexShrink: 0 }}
                                    >
                                        <polyline points="20 6 9 17 4 12" />
                                    </svg>
                                )}
                            </button>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

// === MortalityTooltip ==========================================================
// Recharts tooltip body for the SOVON mortality chart: the month's count plus
// the historical average.
function MortalityTooltip({
    active,
    label,
    payload,
    sourceLabel,
}: {
    active?: boolean;
    label?: string;
    payload?: TooltipPayloadEntry[];
    sourceLabel?: string;
}) {
    if (!active || !payload?.length) return null;
    const rows: Array<{ label: string; color: string; value: number | null }> = [
        { label: sourceLabel ?? "This year", color: ACCENT, value: tooltipValue(payload, "count") },
        { label: "Historical avg", color: "rgba(255,255,255,0.42)", value: tooltipValue(payload, "historicalAverageCount") },
    ];

    return (
        <div
            style={{
                background: PANEL_SURFACE_DEEP,
                border: `1px solid ${BORDER}`,
                borderRadius: 8,
                padding: "6px 8px",
                fontSize: 10,
                fontFamily: MONO,
                color: TEXT,
                boxShadow: PANEL_SHADOW,
            }}
        >
            <div style={{ color: TEXT_SOFT, marginBottom: 4 }}>{label ?? "—"}</div>
            {rows
                .filter((row) => row.value != null)
                .map((row) => (
                    <div
                        key={row.label}
                        style={{ display: "flex", alignItems: "center", gap: 10, lineHeight: 1.5 }}
                    >
                        <span
                            aria-hidden="true"
                            style={{
                                width: 6,
                                height: 6,
                                borderRadius: "50%",
                                background: row.color,
                                flexShrink: 0,
                            }}
                        />
                        <span style={{ color: TEXT_DIM }}>{row.label}</span>
                        <span style={{ marginLeft: "auto" }}>{formatCompactCount(row.value ?? 0)}</span>
                    </div>
                ))}
        </div>
    );
}

function SeasonalPresenceTooltip({
    active,
    label,
    payload,
    currentYearLabel,
}: {
    active?: boolean;
    label?: string;
    payload?: TooltipPayloadEntry[];
    currentYearLabel?: string;
}) {
    if (!active || !payload?.length) return null;

    // Translate semantic dataKeys ("currentYear" / "historicalAverage") into
    // human-readable labels. The actual year of the current series is passed
    // in from the consumer so the tooltip can show e.g. "2025" instead of
    // the literal key.
    const labelFor = (dataKey: string): string => {
        if (dataKey === "currentYear") return currentYearLabel ?? "This year";
        if (dataKey === "historicalAverage") return "Historical avg";
        return dataKey;
    };

    return (
        <div
            style={{
                background: PANEL_SURFACE_DEEP,
                border: `1px solid ${BORDER}`,
                borderRadius: 8,
                padding: "6px 8px",
                fontSize: 10,
                fontFamily: MONO,
                color: TEXT,
                boxShadow: PANEL_SHADOW,
            }}
        >
            <div style={{ color: TEXT_SOFT, marginBottom: 4 }}>{label ?? "—"}</div>
            {payload
                .filter((entry) => typeof entry.dataKey === "string" && Number.isFinite(Number(entry.value)))
                .map((entry) => (
                    <div
                        key={String(entry.dataKey)}
                        style={{ display: "flex", alignItems: "center", gap: 6, lineHeight: 1.5 }}
                    >
                        <span style={{ color: TEXT_DIM }}>{labelFor(String(entry.dataKey))}</span>
                        <span style={{ marginLeft: "auto" }}>{formatPercent(Number(entry.value))}</span>
                    </div>
                ))}
        </div>
    );
}

function ActivityIndexTooltip({
    active,
    label,
    payload,
}: {
    active?: boolean;
    label?: string;
    payload?: TooltipPayloadEntry[];
}) {
    if (!active || !payload?.length) return null;

    const labelFor = (dataKey: string): string | null => {
        if (dataKey === "activityIndex") return "This period";
        if (dataKey === "historicalAverageActivityIndex") return "Historical avg";
        if (dataKey === "communityActiveStations") return "Active stations";
        // lowCoverageMarker duplicates activityIndex; surfaced as a note below.
        return null;
    };
    const formatValue = (dataKey: string, value: number): string =>
        dataKey === "communityActiveStations" ? String(Math.round(value)) : value.toFixed(1);
    const isLowCoverage = payload.some(
        (entry) => entry.dataKey === "lowCoverageMarker" && Number.isFinite(Number(entry.value)),
    );

    return (
        <div
            style={{
                background: PANEL_SURFACE_DEEP,
                border: `1px solid ${BORDER}`,
                borderRadius: 8,
                padding: "6px 8px",
                fontSize: 10,
                fontFamily: MONO,
                color: TEXT,
                boxShadow: PANEL_SHADOW,
            }}
        >
            <div style={{ color: TEXT_SOFT, marginBottom: 4 }}>{label ?? "—"}</div>
            {payload
                .filter(
                    (entry) =>
                        typeof entry.dataKey === "string" &&
                        Number.isFinite(Number(entry.value)) &&
                        labelFor(String(entry.dataKey)) !== null,
                )
                .map((entry) => (
                    <div
                        key={String(entry.dataKey)}
                        style={{ display: "flex", alignItems: "center", gap: 6, lineHeight: 1.5 }}
                    >
                        <span style={{ color: TEXT_DIM }}>{labelFor(String(entry.dataKey))}</span>
                        <span style={{ marginLeft: "auto" }}>
                            {formatValue(String(entry.dataKey), Number(entry.value))}
                        </span>
                    </div>
                ))}
            {isLowCoverage ? (
                <div style={{ color: "#f59e42", marginTop: 4, lineHeight: 1.4 }}>low station coverage</div>
            ) : null}
        </div>
    );
}

const FLIP_CARD_SHELL_STYLE: React.CSSProperties = {
    position: "relative",
    minHeight: 216,
    overflow: "hidden",
    isolation: "isolate",
    transformStyle: "preserve-3d",
    WebkitTransformStyle: "preserve-3d",
    willChange: "contents",
};

const FLIP_CARD_FACE_STYLE: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    backfaceVisibility: "hidden",
    WebkitBackfaceVisibility: "hidden",
    transformStyle: "preserve-3d",
    WebkitTransformStyle: "preserve-3d",
    willChange: "transform, opacity",
    transition: "transform 360ms cubic-bezier(0.22, 1, 0.36, 1), opacity 0s linear 180ms",
};

type WindowKey = "24h" | "week" | "month" | "season" | "year" | "total";

const WINDOW_OPTIONS: ReadonlyArray<{ key: WindowKey; label: string }> = [
    { key: "24h", label: "Last 24h" },
    { key: "week", label: "Last week" },
    { key: "month", label: "Last month" },
    { key: "season", label: "Last season" },
    { key: "year", label: "Last year" },
    { key: "total", label: "All time" },
];

interface WindowResponse {
    window: WindowKey;
    hourly: HourlyPoint[];
    hotspots: SpeciesTrackerHotspot[];
}

// Fetches windowed hourly / top-station data from /api/species-tracker/window.
// Returns null until the first response so callers fall back to their props
// (the latest-complete-year payload) for the initial paint and on error.
function useWindowData(
    speciesId: string,
    windowKey: WindowKey,
    parts: "hourly" | "stations",
) {
    const [hourly, setHourly] = useState<HourlyPoint[] | null>(null);
    const [hotspots, setHotspots] = useState<SpeciesTrackerHotspot[] | null>(null);
    const [loading, setLoading] = useState(false);
    const requestRef = useRef<AbortController | null>(null);

    useEffect(() => {
        requestRef.current?.abort();
        const controller = new AbortController();
        requestRef.current = controller;
        setLoading(true);
        const params = new URLSearchParams({ species_id: speciesId, window: windowKey, parts });
        fetch(`/api/species-tracker/window?${params.toString()}`, {
            signal: controller.signal,
            cache: "no-store",
        })
            .then((response) => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json() as Promise<WindowResponse>;
            })
            .then((json) => {
                if (controller.signal.aborted) return;
                if (parts === "hourly") setHourly(Array.isArray(json.hourly) ? json.hourly : []);
                else setHotspots(Array.isArray(json.hotspots) ? json.hotspots : []);
            })
            .catch((error) => {
                // Swallow aborts; keep last good data on real failures (prop fallback covers it).
                if ((error as { name?: string })?.name === "AbortError") return;
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [speciesId, windowKey, parts]);

    return { hourly, hotspots, loading };
}

// Gray duration selector shown top-right of the Daily rhythm and Top stations cards.
function WindowSelect({
    value,
    onChange,
    loading,
}: {
    value: WindowKey;
    onChange: (next: WindowKey) => void;
    loading?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!open) return;
        const onDocMouseDown = (event: MouseEvent) => {
            if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
        };
        document.addEventListener("mousedown", onDocMouseDown);
        return () => document.removeEventListener("mousedown", onDocMouseDown);
    }, [open]);

    const current = WINDOW_OPTIONS.find((option) => option.key === value)
        ?? WINDOW_OPTIONS[WINDOW_OPTIONS.length - 1];

    return (
        <div ref={ref} style={{ position: "relative", flexShrink: 0 }}>
            <button
                type="button"
                onClick={() => setOpen((prev) => !prev)}
                style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 5,
                    border: `1px solid ${BORDER}`,
                    background: "rgba(255,255,255,0.05)",
                    color: TEXT_DIM,
                    borderRadius: 8,
                    padding: "4px 8px",
                    fontSize: 9,
                    fontFamily: MONO,
                    letterSpacing: "0.02em",
                    cursor: "pointer",
                    opacity: loading ? 0.6 : 1,
                    transition: "opacity 140ms ease",
                }}
            >
                <span>{current.label}</span>
                <span aria-hidden="true" style={{ fontSize: 8, color: TEXT_SOFT }}>&#9662;</span>
            </button>
            {open ? (
                <div
                    style={{
                        position: "absolute",
                        right: 0,
                        top: "calc(100% + 4px)",
                        zIndex: 30,
                        minWidth: 112,
                        background: "#15171a",
                        border: `1px solid ${BORDER}`,
                        borderRadius: 10,
                        padding: 4,
                        boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 2,
                    }}
                >
                    {WINDOW_OPTIONS.map((option) => {
                        const active = option.key === value;
                        return (
                            <button
                                key={option.key}
                                type="button"
                                onClick={() => {
                                    onChange(option.key);
                                    setOpen(false);
                                }}
                                style={{
                                    textAlign: "left",
                                    border: "none",
                                    background: active ? "rgba(52,211,153,0.14)" : "transparent",
                                    color: active ? ACCENT : TEXT_DIM,
                                    borderRadius: 6,
                                    padding: "5px 8px",
                                    fontSize: 9,
                                    fontFamily: MONO,
                                    cursor: "pointer",
                                }}
                            >
                                {option.label}
                            </button>
                        );
                    })}
                </div>
            ) : null}
        </div>
    );
}

function HourlyDrilldownCard({
    hourly,
    speciesId,
}: {
    hourly: SpeciesTrackerActivity["hourly"];
    speciesId: string;
}) {
    const [hoveredHour, setHoveredHour] = useState<number | null>(null);
    const [selectedHour, setSelectedHour] = useState<number | null>(null);
    const [detail, setDetail] = useState<SpeciesTrackerHourDrilldownResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const requestRef = useRef<AbortController | null>(null);

    useEffect(() => {
        setHoveredHour(null);
        setSelectedHour(null);
        setDetail(null);
        setError(null);
        setLoading(false);
        requestRef.current?.abort();
    }, [speciesId, hourly]);

    useEffect(() => {
        return () => {
            requestRef.current?.abort();
        };
    }, []);

    const [windowKey, setWindowKey] = useState<WindowKey>("total");
    const { hourly: windowHourly, loading: windowLoading } = useWindowData(speciesId, windowKey, "hourly");
    const effectiveHourly = windowHourly ?? hourly;

    const buckets = new Array<number>(24).fill(0);
    let maxValue = 0;
    for (const point of effectiveHourly) {
        if (point.hour >= 0 && point.hour < 24) {
            const value = Math.max(0, Number(point.detections) || 0);
            buckets[point.hour] = value;
            if (value > maxValue) maxValue = value;
        }
    }

    const peak = derivePeakHourBand(effectiveHourly);
    const peakHours = peak
        ? new Set<number>([peak.start, (peak.start + 1) % 24, (peak.start + 2) % 24])
        : new Set<number>();

    const loadHour = async (hour: number) => {
        requestRef.current?.abort();
        const controller = new AbortController();
        requestRef.current = controller;
        setSelectedHour(hour);
        setLoading(true);
        setError(null);
        setDetail(null);

        try {
            const params = new URLSearchParams({
                kind: "hour",
                species_id: speciesId,
                hour: String(hour),
            });
            const response = await fetch(`/api/species-tracker/drilldown?${params.toString()}`, {
                signal: controller.signal,
                cache: "no-store",
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const json = await response.json() as SpeciesTrackerDrilldownResponse;
            if (!controller.signal.aborted && json.kind === "hour") {
                setDetail(json);
            }
        } catch (fetchError) {
            if ((fetchError as { name?: string })?.name !== "AbortError") {
                setError("Failed to load hour detail");
            }
        } finally {
            if (!controller.signal.aborted) {
                setLoading(false);
            }
        }
    };

    const isFlipped = selectedHour !== null;

    return (
        <div style={{ ...CHART_CARD_STYLE, perspective: 1200, minHeight: 194 }}>
            <div style={{ ...FLIP_CARD_SHELL_STYLE, minHeight: 174 }}>
                <div
                    style={{
                        ...FLIP_CARD_FACE_STYLE,
                        opacity: isFlipped ? 0 : 1,
                        pointerEvents: isFlipped ? "none" : "auto",
                        zIndex: isFlipped ? 0 : 1,
                        transform: isFlipped ? "rotateY(-180deg)" : "rotateY(0deg)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 10,
                    }}
                >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                            <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                                Daily rhythm
                            </div>
                            <InfoHint label="Daily rhythm">
                                Detections per UTC hour for the selected duration. Hovering an hour emphasizes it; clicking opens a representative clip from that hour band.
                            </InfoHint>
                        </div>
                        <WindowSelect value={windowKey} onChange={setWindowKey} loading={windowLoading} />
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(24, minmax(0, 1fr))", gap: 3, alignItems: "end", height: 104 }}>
                        {buckets.map((value, hour) => {
                            const ratio = maxValue > 0 ? value / maxValue : 0;
                            const barHeight = value > 0 ? Math.max(4, Math.round(ratio * 88)) : 2;
                            const active = hoveredHour === hour;
                            const inPeak = peakHours.has(hour);
                            return (
                                <button
                                    key={hour}
                                    type="button"
                                    onMouseEnter={() => setHoveredHour(hour)}
                                    onMouseLeave={() => setHoveredHour((current) => current === hour ? null : current)}
                                    onClick={() => void loadHour(hour)}
                                    title={`${formatHour24(hour)} · ${value} detections`}
                                    style={{
                                        border: "none",
                                        background: "transparent",
                                        padding: 0,
                                        margin: 0,
                                        cursor: "pointer",
                                        display: "flex",
                                        alignItems: "flex-end",
                                        justifyContent: "center",
                                        minHeight: 100,
                                    }}
                                >
                                    <span
                                        aria-hidden="true"
                                        style={{
                                            width: "100%",
                                            height: barHeight,
                                            borderRadius: 999,
                                            background: active || inPeak ? ACCENT : "rgba(255,255,255,0.34)",
                                            opacity: value > 0 ? 1 : 0.22,
                                            boxShadow: active ? `0 0 6px ${ACCENT}` : "none",
                                            transform: active ? "scaleY(1.06)" : "scaleY(1)",
                                            transformOrigin: "center bottom",
                                            transition: "background 140ms ease, box-shadow 140ms ease, transform 140ms ease",
                                        }}
                                    />
                                </button>
                            );
                        })}
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 6 }}>
                        {[0, 6, 12, 18].map((hour) => (
                            <span key={hour} style={{ fontSize: 9, fontFamily: MONO, color: TEXT_SOFT }}>
                                {String(hour).padStart(2, "0")}
                            </span>
                        ))}
                    </div>
                </div>

                <div
                    style={{
                        ...FLIP_CARD_FACE_STYLE,
                        opacity: isFlipped ? 1 : 0,
                        pointerEvents: isFlipped ? "auto" : "none",
                        zIndex: isFlipped ? 1 : 0,
                        transform: isFlipped ? "rotateY(0deg)" : "rotateY(180deg)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 10,
                    }}
                >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                            Hour detail
                        </div>
                        <button
                            type="button"
                            onClick={() => {
                                requestRef.current?.abort();
                                setSelectedHour(null);
                                setDetail(null);
                                setError(null);
                                setLoading(false);
                            }}
                            style={{
                                border: `1px solid ${BORDER}`,
                                background: "rgba(255,255,255,0.04)",
                                color: TEXT_DIM,
                                borderRadius: 999,
                                padding: "4px 8px",
                                fontSize: 9,
                                fontFamily: MONO,
                                cursor: "pointer",
                            }}
                        >
                            Back
                        </button>
                    </div>
                    <div
                        className={trackerTitleFont.className}
                        style={{ fontSize: 18, color: TEXT, lineHeight: 1.15, fontWeight: 600 }}
                    >
                        {detail?.label ?? (selectedHour != null ? formatHour24(selectedHour) : "Hour")}
                    </div>
                    {loading ? (
                        <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>loading detail…</div>
                    ) : error ? (
                        <div style={{ color: "#fca5a5", fontSize: 10, fontFamily: MONO }}>{error}</div>
                    ) : detail?.representativeClip?.audioUrls.processed ? (
                        <div>
                            <div style={{ fontSize: 10, color: TEXT_DIM, fontFamily: MONO, marginBottom: 6 }}>
                                {detail.representativeClip.stationName ?? detail.representativeClip.stationId ?? "Unknown station"} · {formatConfidence(detail.representativeClip.confidence)} · {formatUtcDateTime(detail.representativeClip.detectedAt)}
                            </div>
                            <AssistedAudioPlayer src={detail.representativeClip.audioUrls.processed} className="w-full" style={{ width: "100%", height: 34 }} />
                        </div>
                    ) : (
                        <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>
                            no clip available for this hour
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function StationDrilldownCard({
    hotspots,
    speciesId,
    onFocusStation,
}: {
    hotspots: SpeciesTrackerHotspot[];
    speciesId: string;
    onFocusStation?: (stationId: string | null) => void;
}) {
    const [selectedStation, setSelectedStation] = useState<SpeciesTrackerHotspot | null>(null);
    const [detail, setDetail] = useState<SpeciesTrackerStationDrilldownResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const requestRef = useRef<AbortController | null>(null);

    useEffect(() => {
        setSelectedStation(null);
        setDetail(null);
        setError(null);
        setLoading(false);
        requestRef.current?.abort();
    }, [speciesId, hotspots]);

    useEffect(() => {
        return () => {
            requestRef.current?.abort();
        };
    }, []);

    const [windowKey, setWindowKey] = useState<WindowKey>("total");
    const { hotspots: windowHotspots, loading: windowLoading } = useWindowData(speciesId, windowKey, "stations");
    const effectiveHotspots = windowHotspots ?? hotspots;

    const topHotspots = effectiveHotspots.slice(0, 4);
    const maxHotspotDetections = topHotspots.reduce((max, hotspot) => Math.max(max, hotspot.detections), 0);

    const loadStation = async (hotspot: SpeciesTrackerHotspot) => {
        requestRef.current?.abort();
        const controller = new AbortController();
        requestRef.current = controller;
        setSelectedStation(hotspot);
        setLoading(true);
        setError(null);
        setDetail(null);
        onFocusStation?.(hotspot.stationId);

        try {
            const params = new URLSearchParams({
                kind: "station",
                species_id: speciesId,
                station_id: hotspot.stationId,
            });
            const response = await fetch(`/api/species-tracker/drilldown?${params.toString()}`, {
                signal: controller.signal,
                cache: "no-store",
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const json = await response.json() as SpeciesTrackerDrilldownResponse;
            if (!controller.signal.aborted && json.kind === "station") {
                setDetail(json);
            }
        } catch (fetchError) {
            if ((fetchError as { name?: string })?.name !== "AbortError") {
                setError("Failed to load station detail");
            }
        } finally {
            if (!controller.signal.aborted) {
                setLoading(false);
            }
        }
    };

    const isFlipped = selectedStation !== null;

    return (
        <div style={{ ...CHART_CARD_STYLE, perspective: 1200, minHeight: 236 }}>
            <div style={FLIP_CARD_SHELL_STYLE}>
                <div
                    style={{
                        ...FLIP_CARD_FACE_STYLE,
                        opacity: isFlipped ? 0 : 1,
                        pointerEvents: isFlipped ? "none" : "auto",
                        zIndex: isFlipped ? 0 : 1,
                        transform: isFlipped ? "rotateY(-180deg)" : "rotateY(0deg)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 8,
                    }}
                >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                            <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                                Top stations
                            </div>
                            <InfoHint label="Top stations">
                                Stations with the most detections for this species over the selected duration. Clicking a row opens station detail and highlights the station on the map.
                            </InfoHint>
                        </div>
                        <WindowSelect value={windowKey} onChange={setWindowKey} loading={windowLoading} />
                    </div>
                    {topHotspots.length === 0 ? (
                        <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO, padding: "8px 2px" }}>
                            no station hotspots
                        </div>
                    ) : (
                        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                            {topHotspots.map((hotspot) => {
                                const ratio = maxHotspotDetections > 0 ? hotspot.detections / maxHotspotDetections : 0;
                                return (
                                    <button
                                        key={hotspot.stationId}
                                        type="button"
                                        onClick={() => void loadStation(hotspot)}
                                        style={{
                                            position: "relative",
                                            padding: "8px 9px",
                                            borderRadius: 10,
                                            background: "rgba(255,255,255,0.025)",
                                            overflow: "hidden",
                                            border: `1px solid ${BORDER}`,
                                            cursor: "pointer",
                                            textAlign: "left",
                                        }}
                                    >
                                        <div
                                            aria-hidden="true"
                                            style={{
                                                position: "absolute",
                                                inset: 0,
                                                width: `${ratio * 100}%`,
                                                background: "rgba(52,211,153,0.10)",
                                            }}
                                        />
                                        <div style={{ position: "relative", display: "flex", alignItems: "baseline", gap: 8 }}>
                                            <span
                                                style={{
                                                    fontSize: 11,
                                                    color: TEXT,
                                                    overflow: "hidden",
                                                    textOverflow: "ellipsis",
                                                    whiteSpace: "nowrap",
                                                    flex: "1 1 auto",
                                                    minWidth: 0,
                                                }}
                                            >
                                                {hotspot.stationName}
                                            </span>
                                            <span style={{ fontSize: 9, fontFamily: MONO, color: TEXT_SOFT, flexShrink: 0 }}>
                                                {hotspot.detections.toLocaleString("en-GB")} · {hotspot.activeDays}d
                                            </span>
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    )}
                </div>

                <div
                    style={{
                        ...FLIP_CARD_FACE_STYLE,
                        opacity: isFlipped ? 1 : 0,
                        pointerEvents: isFlipped ? "auto" : "none",
                        zIndex: isFlipped ? 1 : 0,
                        transform: isFlipped ? "rotateY(0deg)" : "rotateY(180deg)",
                        display: "flex",
                        flexDirection: "column",
                        gap: 10,
                    }}
                >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                            Station detail
                        </div>
                        <button
                            type="button"
                            onClick={() => {
                                requestRef.current?.abort();
                                setSelectedStation(null);
                                setDetail(null);
                                setError(null);
                                setLoading(false);
                                onFocusStation?.(null);
                            }}
                            style={{
                                border: `1px solid ${BORDER}`,
                                background: "rgba(255,255,255,0.04)",
                                color: TEXT_DIM,
                                borderRadius: 999,
                                padding: "4px 8px",
                                fontSize: 9,
                                fontFamily: MONO,
                                cursor: "pointer",
                            }}
                        >
                            Back
                        </button>
                    </div>
                    <div>
                        <div className={trackerTitleFont.className} style={{ fontSize: 17, color: TEXT, lineHeight: 1.15, fontWeight: 600 }}>
                            {detail?.stationName ?? selectedStation?.stationName ?? "Station"}
                        </div>
                        <div style={{ marginTop: 3, fontSize: 10, color: TEXT_DIM, fontFamily: MONO }}>
                            Focused on the map while this detail card is open.
                        </div>
                    </div>
                    {loading ? (
                        <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>loading detail…</div>
                    ) : error ? (
                        <div style={{ color: "#fca5a5", fontSize: 10, fontFamily: MONO }}>{error}</div>
                    ) : (
                        <>
                            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
                                {[
                                    { label: "Detections", value: detail?.detections?.toLocaleString("en-GB") ?? "0" },
                                    { label: "Active days", value: detail?.activeDays?.toLocaleString("en-GB") ?? "0" },
                                    { label: "Avg confidence", value: formatConfidence(detail?.avgConfidence ?? null) },
                                    { label: "Last heard", value: formatUtcDateTime(detail?.lastDetectedAt ?? null) },
                                ].map((entry) => (
                                    <div key={entry.label} style={{ padding: "8px 9px", borderRadius: 10, background: "rgba(255,255,255,0.03)" }}>
                                        <div style={{ fontSize: 9, color: TEXT_SOFT, fontFamily: MONO }}>{entry.label}</div>
                                        <div style={{ marginTop: 4, fontSize: 13, color: TEXT, fontWeight: 600 }}>{entry.value}</div>
                                    </div>
                                ))}
                            </div>
                            {detail?.representativeClip?.audioUrls.processed ? (
                                <div>
                                    <div style={{ fontSize: 10, color: TEXT_DIM, fontFamily: MONO, marginBottom: 6 }}>
                                        {formatUtcDateTime(detail.representativeClip.detectedAt)} · {formatConfidence(detail.representativeClip.confidence)}
                                    </div>
                                    <AssistedAudioPlayer src={detail.representativeClip.audioUrls.processed} className="w-full" style={{ width: "100%", height: 34 }} />
                                </div>
                            ) : (
                                <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>
                                    no representative clip available for this station
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}

function ActivityTab({
    activity,
    populationSeries,
    hotspots,
    loading,
    empty,
    speciesId,
    speciesName,
    onFocusStation,
}: {
    activity: SpeciesTrackerActivity | null;
    populationSeries: SpeciesTrackerPopulationPoint[];
    hotspots: SpeciesTrackerHotspot[];
    loading: boolean;
    empty: boolean;
    speciesId: string;
    speciesName: string | null;
    onFocusStation?: (stationId: string | null) => void;
}) {
    if (loading) return <TrackerLoadingState />;
    if (empty) return <TrackerEmptyState speciesName={speciesName} />;

    const hourly = activity?.hourly ?? [];
    const seasonalPresence = activity?.seasonalPresence ?? [];
    const seasonalChartData = buildSeasonalPresenceChartData(seasonalPresence);
    const activityIndexChartData = buildActivityIndexChartData(populationSeries);

    return (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px 14px" }}>
            <div style={CHART_CARD_STYLE}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                            Relative abundance
                        </div>
                        <InfoHint label="Relative abundance">
                            This species&apos; share of all network detections each month, for the latest year with data. The dashed gray line is the average across previous years for the same months — peaks above mean the species was relatively louder than usual that month, dips below mean quieter.
                        </InfoHint>
                    </div>
                </div>
                <div style={{ height: 156 }}>
                    {seasonalChartData.currentYear === null ? (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>
                            no seasonal history
                        </div>
                    ) : (
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={seasonalChartData.rows} margin={{ top: 12, right: 10, bottom: 2, left: 4 }}>
                                <XAxis
                                    dataKey="label"
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={{ stroke: "rgba(255,255,255,0.06)" }}
                                />
                                <YAxis
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={false}
                                    width={34}
                                    tickCount={4}
                                    // Auto-scale upper bound: relative-abundance values
                                    // typically live in the 0–15% band, so a fixed
                                    // [0, 100] domain would squash every line against
                                    // the floor. Add 10% headroom so peaks don't kiss
                                    // the top edge.
                                    domain={[0, (dataMax: number) => Math.max(1, Math.ceil(dataMax * 1.1))]}
                                    allowDataOverflow={false}
                                    tickFormatter={(value: number) => `${value}%`}
                                />
                                <Tooltip
                                    content={
                                        <SeasonalPresenceTooltip
                                            currentYearLabel={
                                                seasonalChartData.currentYear !== null
                                                    ? String(seasonalChartData.currentYear)
                                                    : undefined
                                            }
                                        />
                                    }
                                    cursor={{ stroke: "rgba(255,255,255,0.07)", strokeWidth: 1 }}
                                />
                                {/* Historical average — dashed gray reference line.
                                    Only rendered when at least one prior year of
                                    data exists; otherwise there is nothing to
                                    average. */}
                                {seasonalChartData.baselineYearCount > 0 ? (
                                    <Line
                                        type="monotone"
                                        dataKey="historicalAverage"
                                        stroke="rgba(255,255,255,0.42)"
                                        strokeWidth={1.3}
                                        strokeDasharray="4 4"
                                        dot={false}
                                        activeDot={false}
                                        connectNulls
                                        isAnimationActive={false}
                                    />
                                ) : null}
                                {/* Current year — solid accent line, drawn on top
                                    so the active dot reads against the baseline. */}
                                <Line
                                    type="monotone"
                                    dataKey="currentYear"
                                    stroke={ACCENT}
                                    strokeWidth={2}
                                    dot={false}
                                    connectNulls
                                    activeDot={{ r: 3, fill: ACCENT, stroke: "none" }}
                                />
                            </LineChart>
                        </ResponsiveContainer>
                    )}
                </div>
            </div>

            {/* === Activity index chart — hidden for now, may be restored later ===
            <div style={CHART_CARD_STYLE}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                            Activity index
                        </div>
                        <InfoHint label="Activity index">
                            An effort-aware activity proxy (0&ndash;100) &mdash; the same composite the population anomaly model scores. It blends this species&apos; detection share, temporal occupancy, station presence, and detections per active station-day, so it is far less distorted by how many stations were online than raw counts or abundance share. The dashed gray line is the historical average for the same period. The faint blue line tracks how many stations were active, and amber rings flag buckets where that footprint fell below half its historical norm &mdash; the same coverage gate the model uses, so a dip there likely reflects lost sensors rather than a real decline. This is a relative activity index, not an absolute count.
                        </InfoHint>
                    </div>
                </div>
                <div style={{ height: 156 }}>
                    {!activityIndexChartData.hasData ? (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>
                            no activity data
                        </div>
                    ) : (
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={activityIndexChartData.rows} margin={{ top: 12, right: activityIndexChartData.hasStations ? 6 : 10, bottom: 2, left: 4 }}>
                                <XAxis
                                    dataKey="label"
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={{ stroke: "rgba(255,255,255,0.06)" }}
                                />
                                <YAxis
                                    yAxisId="index"
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={false}
                                    width={34}
                                    tickCount={4}
                                    domain={[0, (dataMax: number) => Math.max(1, Math.ceil(dataMax * 1.1))]}
                                    allowDataOverflow={false}
                                />
                                {activityIndexChartData.hasStations ? (
                                    <YAxis
                                        yAxisId="stations"
                                        orientation="right"
                                        tick={{ fill: "rgba(96,165,250,0.45)", fontSize: 8, fontFamily: MONO }}
                                        tickLine={false}
                                        axisLine={false}
                                        width={24}
                                        tickCount={3}
                                        domain={[0, (dataMax: number) => Math.max(1, Math.ceil(dataMax * 1.15))]}
                                        allowDataOverflow={false}
                                    />
                                ) : null}
                                <Tooltip
                                    content={<ActivityIndexTooltip />}
                                    cursor={{ stroke: "rgba(255,255,255,0.07)", strokeWidth: 1 }}
                                />
                                {activityIndexChartData.hasStations ? (
                                    <Line
                                        yAxisId="stations"
                                        type="monotone"
                                        dataKey="communityActiveStations"
                                        stroke="rgba(96,165,250,0.34)"
                                        strokeWidth={1}
                                        dot={false}
                                        activeDot={false}
                                        connectNulls
                                        isAnimationActive={false}
                                    />
                                ) : null}
                                {activityIndexChartData.hasBaseline ? (
                                    <Line
                                        yAxisId="index"
                                        type="monotone"
                                        dataKey="historicalAverageActivityIndex"
                                        stroke="rgba(255,255,255,0.42)"
                                        strokeWidth={1.3}
                                        strokeDasharray="4 4"
                                        dot={false}
                                        activeDot={false}
                                        connectNulls
                                        isAnimationActive={false}
                                    />
                                ) : null}
                                <Line
                                    yAxisId="index"
                                    type="monotone"
                                    dataKey="activityIndex"
                                    stroke={ACCENT}
                                    strokeWidth={2}
                                    dot={false}
                                    connectNulls
                                    activeDot={{ r: 3, fill: ACCENT, stroke: "none" }}
                                />
                                {activityIndexChartData.hasLowCoverage ? (
                                    <Line
                                        yAxisId="index"
                                        type="monotone"
                                        dataKey="lowCoverageMarker"
                                        stroke="none"
                                        strokeWidth={0}
                                        legendType="none"
                                        dot={{ r: 3.4, fill: "none", stroke: "#f59e42", strokeWidth: 1.4 }}
                                        activeDot={false}
                                        connectNulls={false}
                                        isAnimationActive={false}
                                    />
                                ) : null}
                            </LineChart>
                        </ResponsiveContainer>
                    )}
                </div>
                {activityIndexChartData.hasStations || activityIndexChartData.hasLowCoverage ? (
                    <div style={{ display: "flex", gap: 12, marginTop: 2, fontSize: 8, color: TEXT_SOFT, fontFamily: MONO, flexWrap: "wrap" }}>
                        {activityIndexChartData.hasStations ? (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                                <span style={{ display: "inline-block", width: 10, borderTop: "1px solid rgba(96,165,250,0.6)" }} /> active stations
                            </span>
                        ) : null}
                        {activityIndexChartData.hasLowCoverage ? (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                                <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", border: "1.4px solid #f59e42" }} /> low station coverage
                            </span>
                        ) : null}
                    </div>
                ) : null}
            </div>
            === end Activity index chart === */}

            <HourlyDrilldownCard hourly={hourly} speciesId={speciesId} />
            <StationDrilldownCard hotspots={hotspots} speciesId={speciesId} onFocusStation={onFocusStation} />
        </div>
    );
}

// === Risk charts ==============================================================
const ANOMALY_AMBER_COLOR = "#f59e42";
const ANOMALY_RED_COLOR = "#f87171";

const RISK_MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Roll the weekly anomaly series up to the latest year's twelve
// months (Jan-start) plus a per-month historical average over the prior five
// years — the same "current vs historical" shape as the mortality/activity charts.
type AnomalyMonthlyPoint = {
    month: number;
    label: string;
    amber: number;
    red: number;
    total: number;
    scoredCells: number;
    score: number | null;
};

function anomalyScoreForPoint(
    point: SpeciesTrackerAnomalySeriesPoint,
    scoreType: AnomalyScoreKey,
): number | null {
    if (scoreType === "bioacoustic") return point.avgBioacousticScore ?? null;
    return point.avgScore ?? null;
}

// Lays the per-month average anomaly score out across the selected scored year
// so the line always spans a full Jan->Dec axis. Months with no scored
// cell-weeks are left null (a gap), not zero. Year-bucketing keeps this correct
// when a run spans more than one year.
function buildAnomalyMonthlyChartData(
    series: SpeciesTrackerAnomalySeriesPoint[] | undefined,
    selectedYear: number | null = null,
    scoreType: AnomalyScoreKey = "bioacoustic",
): { data: AnomalyMonthlyPoint[]; hasSignal: boolean; year: number | null; total: number; availableYears: number[] } {
    if (!series || series.length === 0) {
        return { data: [], hasSignal: false, year: null, total: 0, availableYears: [] };
    }

    type Bucket = { amber: number; red: number; scoredCells: number; scoreSum: number; scoreWeight: number };
    const emptyBucket = (): Bucket => ({ amber: 0, red: 0, scoredCells: 0, scoreSum: 0, scoreWeight: 0 });
    const byYear = new Map<number, Map<number, Bucket>>();
    for (const point of series) {
        const iso = point.periodStart;
        if (!iso || iso.length < 7) continue;
        const year = Number(iso.slice(0, 4));
        const month = Number(iso.slice(5, 7));
        if (!Number.isFinite(year) || !Number.isFinite(month) || month < 1 || month > 12) continue;
        if (!byYear.has(year)) byYear.set(year, new Map());
        const monthMap = byYear.get(year)!;
        const bucket = monthMap.get(month) ?? emptyBucket();
        bucket.amber += point.amber;
        bucket.red += point.red;
        bucket.scoredCells += point.scoredCells;
        // Weight each contributing row's mean by how many cell-weeks it covered
        // so re-bucketing stays a true average if a month ever spans >1 row.
        const pointScore = anomalyScoreForPoint(point, scoreType);
        if (pointScore != null && point.scoredCells > 0) {
            bucket.scoreSum += pointScore * point.scoredCells;
            bucket.scoreWeight += point.scoredCells;
        }
        monthMap.set(month, bucket);
    }
    if (byYear.size === 0) {
        return { data: [], hasSignal: false, year: null, total: 0, availableYears: [] };
    }

    const years = [...byYear.keys()].sort((a, b) => a - b);
    const year =
        selectedYear !== null && byYear.has(selectedYear)
            ? selectedYear
            : years[0];
    const current = byYear.get(year) ?? new Map<number, Bucket>();
    let hasSignal = false;
    let total = 0;
    const data = RISK_MONTH_LABELS.map((label, index) => {
        const month = index + 1;
        const cur = current.get(month) ?? emptyBucket();
        const score = cur.scoreWeight > 0 ? cur.scoreSum / cur.scoreWeight : null;
        if (score != null) hasSignal = true;
        total += cur.amber + cur.red;
        return {
            month,
            label,
            amber: cur.amber,
            red: cur.red,
            total: cur.amber + cur.red,
            scoredCells: cur.scoredCells,
            score,
        };
    });

    return { data, hasSignal, year, total, availableYears: years };
}

function AnomalyTooltip({
    active,
    label,
    payload,
    scoreLabel,
}: {
    active?: boolean;
    label?: string | number;
    payload?: TooltipPayloadEntry[];
    scoreLabel: string;
}) {
    if (!active || !payload?.length) return null;
    const row = (payload[0]?.payload ?? {}) as Partial<AnomalyMonthlyPoint>;
    const score = row.score ?? null;
    const amber = row.amber ?? 0;
    const red = row.red ?? 0;
    const scoredCells = row.scoredCells ?? 0;
    const countRows: Array<{ label: string; color: string; value: number }> = [
        { label: "red", color: ANOMALY_RED_COLOR, value: red },
        { label: "amber", color: ANOMALY_AMBER_COLOR, value: amber },
    ];

    return (
        <div
            style={{
                background: PANEL_SURFACE_DEEP,
                border: `1px solid ${BORDER}`,
                borderRadius: 8,
                padding: "6px 8px",
                fontSize: 10,
                fontFamily: MONO,
                color: TEXT,
                boxShadow: PANEL_SHADOW,
            }}
        >
            <div style={{ color: TEXT_SOFT, marginBottom: 4 }}>{label ?? "\u2014"}</div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, lineHeight: 1.5 }}>
                <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: 2, background: ACCENT, flexShrink: 0 }} />
                <span style={{ color: TEXT_DIM }}>{scoreLabel}</span>
                <span style={{ marginLeft: "auto" }}>{score != null ? score.toFixed(3) : "\u2014"}</span>
            </div>
            {countRows.map((countRow) => (
                <div key={countRow.label} style={{ display: "flex", alignItems: "center", gap: 10, lineHeight: 1.5 }}>
                    <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: 2, background: countRow.color, flexShrink: 0 }} />
                    <span style={{ color: TEXT_DIM }}>{countRow.label}</span>
                    <span style={{ marginLeft: "auto" }}>{countRow.value.toLocaleString("en-GB")}</span>
                </div>
            ))}
            <div style={{ marginTop: 4, color: TEXT_SOFT }}>
                {scoredCells > 0
                    ? `${scoredCells.toLocaleString("en-GB")} cell-week${scoredCells === 1 ? "" : "s"} scored`
                    : "no cells scored"}
            </div>
        </div>
    );
}

interface ClimateStat {
    key: string;
    label: string;
    unit: string;
    p5: number | null;
    q1: number | null;
    median: number | null;
    q3: number | null;
    p95: number | null;
}

interface ClimateEnvelopeResponse {
    window: WindowKey;
    trailingDays: number;
    sampleCount: number;
    variables: ClimateStat[];
}

function useClimateData(speciesId: string, windowKey: WindowKey) {
    const [data, setData] = useState<ClimateEnvelopeResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const requestRef = useRef<AbortController | null>(null);

    useEffect(() => {
        requestRef.current?.abort();
        const controller = new AbortController();
        requestRef.current = controller;
        setLoading(true);
        const params = new URLSearchParams({ species_id: speciesId, window: windowKey });
        fetch(`/api/species-tracker/climate?${params.toString()}`, {
            signal: controller.signal,
            cache: "no-store",
        })
            .then((response) => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json() as Promise<ClimateEnvelopeResponse>;
            })
            .then((json) => {
                if (!controller.signal.aborted) setData(json);
            })
            .catch((error) => {
                if ((error as { name?: string })?.name === "AbortError") return;
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [speciesId, windowKey]);

    return { data, loading };
}

// Sensible absolute display range per variable so the box reads on a real scale;
// open-ended ones (wind/solar) stretch to fit when data exceeds the floor.
function climateDomain(key: string, p95: number | null): [number, number] {
    const top = p95 ?? 0;
    switch (key) {
        case "temperature": return [-5, 35];
        case "humidity": return [0, 100];
        case "cloud": return [0, 100];
        case "pressure": return [980, 1040];
        case "wind": return [0, Math.max(12, Math.ceil(top * 1.15))];
        default: return [0, Math.max(1, Math.ceil(top))];
    }
}

// Pressure reads cleanest without decimals (≈1013 hPa); everything else keeps
// one decimal place.
function formatClimateValue(value: number | null, key: string): string {
    if (value == null) return "—";
    return key === "pressure" ? value.toFixed(0) : value.toFixed(1);
}

// One full-width horizontal box-and-whisker row per weather variable: a thin
// 5–95% whisker, the interquartile box, and a bright median tick, positioned on
// a faint full-domain axis track. Stacked one per row inside a single card so
// there is no nested-box chrome. Mirrors the Activity tab's card language.
function ClimateVariableRow({ stat, first }: { stat: ClimateStat; first: boolean }) {
    const [hovered, setHovered] = useState(false);
    const ready = stat.median != null && stat.q1 != null && stat.q3 != null;
    const [domainMin, domainMax] = climateDomain(stat.key, stat.p95);
    const span = domainMax - domainMin || 1;
    const pos = (value: number | null): number => {
        if (value == null) return 0;
        return Math.max(0, Math.min(100, ((value - domainMin) / span) * 100));
    };

    const p5pos = pos(stat.p5);
    const p95pos = pos(stat.p95);
    const q1pos = pos(stat.q1);
    const q3pos = pos(stat.q3);
    const medianPos = pos(stat.median);

    const fmt = (value: number | null) => formatClimateValue(value, stat.key);

    // Anchor the hover card over the median but clamp it off the card edges so
    // it never clips. The five-point summary reads top-down like a vertical axis.
    const tooltipAnchor = Math.max(20, Math.min(80, medianPos));
    const summaryRows: Array<{ label: string; value: number | null; accent?: boolean }> = [
        { label: "95th", value: stat.p95 },
        { label: "75th", value: stat.q3 },
        { label: "Median", value: stat.median, accent: true },
        { label: "25th", value: stat.q1 },
        { label: "5th", value: stat.p5 },
    ];

    return (
        <div
            onMouseEnter={() => setHovered(true)}
            onMouseLeave={() => setHovered(false)}
            style={{
                display: "flex",
                flexDirection: "column",
                gap: 7,
                paddingTop: first ? 0 : 12,
                marginTop: first ? 0 : 12,
                borderTop: first ? "none" : "1px solid rgba(255,255,255,0.05)",
            }}
        >
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
                <span style={{ fontSize: 12, color: TEXT, fontFamily: trackerTitleFont.style.fontFamily, fontWeight: 500, letterSpacing: "0.01em" }}>
                    {stat.label}
                </span>
                {ready ? (
                    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 3 }}>
                        <span style={{ fontSize: 14, fontWeight: 600, color: TEXT, fontVariantNumeric: "tabular-nums" }}>
                            {fmt(stat.median)}
                        </span>
                        <span style={{ fontSize: 10, color: TEXT_DIM }}>{stat.unit}</span>
                    </span>
                ) : (
                    <span style={{ fontSize: 10, color: TEXT_SOFT, fontFamily: MONO }}>no data</span>
                )}
            </div>

            {ready ? (
                <div style={{ position: "relative", height: 26 }}>
                    {/* hover summary — styled to match the Activity tab tooltips */}
                    {hovered ? (
                        <div
                            role="tooltip"
                            style={{
                                position: "absolute",
                                bottom: "calc(100% + 8px)",
                                left: `${tooltipAnchor}%`,
                                transform: "translateX(-50%)",
                                zIndex: 30,
                                pointerEvents: "none",
                                whiteSpace: "nowrap",
                                minWidth: 124,
                                background: PANEL_SURFACE_DEEP,
                                border: `1px solid ${BORDER}`,
                                borderRadius: 8,
                                padding: "6px 9px",
                                fontSize: 10,
                                fontFamily: MONO,
                                color: TEXT,
                                boxShadow: PANEL_SHADOW,
                            }}
                        >
                            <div style={{ color: TEXT_SOFT, marginBottom: 4 }}>
                                {stat.label} · {stat.unit}
                            </div>
                            {summaryRows.map((row) => (
                                <div key={row.label} style={{ display: "flex", alignItems: "center", gap: 16, lineHeight: 1.55 }}>
                                    <span style={{ color: row.accent ? TEXT : TEXT_DIM }}>{row.label}</span>
                                    <span style={{ marginLeft: "auto", color: row.accent ? ACCENT : TEXT, fontWeight: row.accent ? 600 : 400, fontVariantNumeric: "tabular-nums" }}>
                                        {fmt(row.value)}
                                    </span>
                                </div>
                            ))}
                        </div>
                    ) : null}
                    {/* faint full-domain axis track */}
                    <div style={{ position: "absolute", top: 12.5, left: 0, right: 0, height: 1, background: "rgba(255,255,255,0.05)", borderRadius: 1 }} />
                    {/* 5–95% whisker */}
                    <div style={{ position: "absolute", top: 12, left: `${p5pos}%`, width: `${Math.max(0, p95pos - p5pos)}%`, height: 2, background: hovered ? "rgba(255,255,255,0.38)" : "rgba(255,255,255,0.26)", borderRadius: 1, transition: "background 120ms" }} />
                    {/* whisker end caps */}
                    <div style={{ position: "absolute", top: 7, left: `${p5pos}%`, width: 1.5, height: 12, background: "rgba(255,255,255,0.40)", transform: "translateX(-0.75px)", borderRadius: 1 }} />
                    <div style={{ position: "absolute", top: 7, left: `${p95pos}%`, width: 1.5, height: 12, background: "rgba(255,255,255,0.40)", transform: "translateX(-0.75px)", borderRadius: 1 }} />
                    {/* interquartile box */}
                    <div style={{ position: "absolute", top: 5, left: `${q1pos}%`, width: `${Math.max(1.2, q3pos - q1pos)}%`, height: 16, background: hovered ? "rgba(52,211,153,0.26)" : "rgba(52,211,153,0.16)", border: "1px solid rgba(52,211,153,0.55)", borderRadius: 4, transition: "background 120ms" }} />
                    {/* median */}
                    <div style={{ position: "absolute", top: 2, left: `${medianPos}%`, width: 2.5, height: 22, background: ACCENT, transform: "translateX(-1.25px)", borderRadius: 1, boxShadow: "0 0 6px rgba(52,211,153,0.55)" }} />
                </div>
            ) : (
                <div style={{ display: "flex", alignItems: "center", height: 26, fontSize: 9, color: TEXT_SOFT, fontFamily: MONO }}>
                    no data in this window
                </div>
            )}
        </div>
    );
}

// === Habitat tab ===============================================================
// Detection-weighted Google Earth Engine habitat metrics across the grid cells
// where the species is detected: a land-cover composition bar (Dynamic World), a
// seasonal-rhythm panel of detection-weighted small-multiple sparklines (greenness,
// productivity, surface temp day/night, soil moisture, precipitation, ET, snow,
// air NO2), and an inline context strip of static scalars. GEE composites are roughly monthly, so short
// windows resolve to the latest available composite; month/season/year/total
// are the meaningful spans. Mirrors the Climate tab's fetch/card language.

// Dynamic World band → swatch color. Greens for vegetation, gold for crops,
// red for built, blue for water, neutral for bare/snow.
// Muted, low-chroma earth-tone palette for the land-cover mix — same hue
// families as Dynamic World but desaturated so the bar reads calmly.
const HABITAT_BAND_COLORS: Record<string, string> = {
    trees: "#6b8f71",
    grass: "#9caa6f",
    crops: "#c2a86a",
    shrub: "#869a6b",
    built: "#b08f86",
    water: "#6f93a8",
    flooded: "#6f9e96",
    bare: "#bfae93",
    snow: "#d2d5d9",
};

function useHabitatData(speciesId: string, windowKey: WindowKey) {
    const [data, setData] = useState<SpeciesTrackerHabitatResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const requestRef = useRef<AbortController | null>(null);

    useEffect(() => {
        requestRef.current?.abort();
        const controller = new AbortController();
        requestRef.current = controller;
        setLoading(true);
        const params = new URLSearchParams({ species_id: speciesId, window: windowKey });
        fetch(`/api/species-tracker/habitat?${params.toString()}`, {
            signal: controller.signal,
            cache: "no-store",
        })
            .then((response) => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json() as Promise<SpeciesTrackerHabitatResponse>;
            })
            .then((json) => {
                if (!controller.signal.aborted) setData(json);
            })
            .catch((error) => {
                if ((error as { name?: string })?.name === "AbortError") return;
            })
            .finally(() => {
                if (!controller.signal.aborted) setLoading(false);
            });
        return () => controller.abort();
    }, [speciesId, windowKey]);

    return { data, loading };
}


// Skeleton body for the Climate card while the envelope fetch is in flight —
// one shimmer row per weather variable, mirroring the box-and-whisker layout so
// the panel doesn't reflow when data lands. The card header stays mounted above.
function ClimateTabSkeleton() {
    return (
        <div style={{ display: "flex", flexDirection: "column" }}>
            {[0, 1, 2, 3, 4].map((index) => (
                <div
                    key={index}
                    style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 7,
                        paddingTop: index === 0 ? 0 : 12,
                        marginTop: index === 0 ? 0 : 12,
                        borderTop: index === 0 ? "none" : "1px solid rgba(255,255,255,0.05)",
                    }}
                >
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                        <SkeletonBlock width={74} height={11} radius={999} />
                        <SkeletonBlock width={44} height={13} radius={999} />
                    </div>
                    <SkeletonBlock width="100%" height={16} radius={6} />
                </div>
            ))}
        </div>
    );
}

function ClimateTab({
    speciesId,
    speciesName,
}: {
    speciesId: string;
    speciesName?: string | null;
}) {
    const [windowKey, setWindowKey] = useState<WindowKey>("total");
    const { data, loading } = useClimateData(speciesId, windowKey);
    const { data: habitatData, loading: habitatLoading } = useHabitatData(speciesId, windowKey);

    const variables = data?.variables ?? [];
    const sampleCount = data?.sampleCount ?? 0;
    const composition = (habitatData?.composition ?? []).filter((band) => band.pct > 0);
    const legendBands = composition.filter((band) => band.pct >= 1);

    return (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px 14px" }}>
            {/* Land cover mix (Google Earth Engine, detection-weighted) */}
            <div style={CHART_CARD_STYLE}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                    <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                        Land cover
                    </div>
                    <InfoHint label="Land cover">
                        Detection-weighted satellite land-cover mix (Google Earth Engine, Dynamic World) across the grid cells where{speciesName ? ` ${speciesName}` : " this species"} is detected, leaning toward its most active cells.
                    </InfoHint>
                </div>
                {habitatLoading || !habitatData ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 7, marginTop: 8 }}>
                        <SkeletonBlock width="100%" height={12} radius={6} />
                        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                            {[48, 56, 40, 62, 44].map((width, index) => (
                                <SkeletonBlock key={index} width={width} height={9} radius={999} />
                            ))}
                        </div>
                    </div>
                ) : composition.length === 0 ? (
                    <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO, padding: "10px 2px" }}>
                        no land-cover data for this duration
                    </div>
                ) : (
                    <div style={{ display: "flex", flexDirection: "column", gap: 7, marginTop: 8 }}>
                        <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", border: `1px solid ${BORDER}` }}>
                            {composition.map((band) => (
                                <div
                                    key={band.key}
                                    title={`${band.label} ${band.pct}%`}
                                    style={{ width: `${band.pct}%`, background: HABITAT_BAND_COLORS[band.key] ?? "rgba(255,255,255,0.2)" }}
                                />
                            ))}
                        </div>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 12px" }}>
                            {legendBands.map((band) => (
                                <span
                                    key={band.key}
                                    style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 9, fontFamily: MONO, color: TEXT_DIM }}
                                >
                                    <span style={{ width: 8, height: 8, borderRadius: 2, background: HABITAT_BAND_COLORS[band.key] ?? "rgba(255,255,255,0.2)" }} />
                                    {band.label} {band.pct}%
                                </span>
                            ))}
                        </div>
                    </div>
                )}
            </div>

            {/* Weather while calling (unchanged) */}
            <div style={CHART_CARD_STYLE}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                    <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                        Weather while calling
                    </div>
                    <InfoHint label="Weather while calling">
                        For each variable, the spread of station-reported weather (BirdWeather) when{speciesName ? ` ${speciesName}` : " this species"} is detected calling, over the selected duration. Box = interquartile range (25–75%), centre line = median, whiskers = 5–95%. Taken from the weather attached to each detection; solar and drought will join once KNMI/CDS are loaded.
                    </InfoHint>
                </div>
                <WindowSelect value={windowKey} onChange={setWindowKey} loading={loading} />
            </div>

            {loading || !data ? (
                <ClimateTabSkeleton />
            ) : sampleCount === 0 ? (
                <div style={{ color: TEXT_SOFT, fontSize: 10, fontFamily: MONO, padding: "10px 2px" }}>
                    no climate data for this duration
                </div>
            ) : (
                <div style={{ display: "flex", flexDirection: "column" }}>
                    {variables.map((stat, index) => (
                        <ClimateVariableRow key={stat.key} stat={stat} first={index === 0} />
                    ))}
                </div>
            )}
            </div>
        </div>
    );
}

function RiskTab({
    sovon,
    anomalySeries,
    loading,
    empty,
    speciesName,
}: {
    sovon: SpeciesTrackerMortality | null;
    anomalySeries: SpeciesTrackerAnomalySeriesPoint[];
    loading: boolean;
    empty: boolean;
    speciesName: string | null;
}) {
    // Each Risk chart carries its own year selector (null => that chart's earliest
    // available year). They default independently rather than being pinned to a
    // single shared common year, so e.g. a 2024 anomaly run can be read against
    // 2025 mortality once the backfill widens the available years.
    const [mortalityYear, setMortalityYear] = useState<number | null>(null);
    const [anomalyYear, setAnomalyYear] = useState<number | null>(null);
    const [anomalyMetric, setAnomalyMetric] = useState<AnomalyMetricKey>("amber");
    const [anomalyScoreType, setAnomalyScoreType] = useState<AnomalyScoreKey>("bioacoustic");

    if (loading) return <TrackerLoadingState />;
    if (empty) return <TrackerEmptyState speciesName={speciesName} />;

    const activeMortality = sovon;
    const mortalityYears = activeMortality?.availableYears ?? [];
    const anomaly = buildAnomalyMonthlyChartData(anomalySeries, anomalyYear, anomalyScoreType);
    const anomalyIsScore = anomalyMetric === "avg";
    const anomalyScoreLabel =
        ANOMALY_SCORE_OPTIONS.find((option) => option.key === anomalyScoreType)?.label ?? "score";
    // Amber and avg both render in the shared dashboard green (ACCENT) so the
    // anomaly-score line matches the other charts; red keeps its severity color.
    const anomalyLineColor =
        anomalyMetric === "red" ? ANOMALY_RED_COLOR : ACCENT;
    const anomalyDataKey = anomalyMetric === "avg" ? "score" : anomalyMetric;
    const resolvedMortalityYear =
        mortalityYear != null && mortalityYears.includes(mortalityYear)
            ? mortalityYear
            : mortalityYears.length > 0
              ? Math.min(...mortalityYears)
              : null;
    const mortalitySeries =
        (resolvedMortalityYear != null
            ? activeMortality?.byYear?.find((entry) => entry.year === resolvedMortalityYear)?.series
            : activeMortality?.series) ??
        activeMortality?.series ??
        [];
    const mortalityHasBaseline = mortalitySeries.some((point) => point.historicalAverageCount != null);
    const sourceLabel = "SOVON";

    return (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "12px 14px 14px" }}>
            <div style={CHART_CARD_STYLE}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                        <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                            Mortality
                        </div>
                        <InfoHint label="Mortality chart">
                            Monthly dead-bird counts from SOVON citizen-science reports. The solid line is the selected year (January onward); the dashed gray line is the per-month historical average over the prior years.
                        </InfoHint>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        {mortalityYears.length > 0 ? (
                            <YearSelect value={resolvedMortalityYear} years={mortalityYears} onChange={setMortalityYear} loading={loading} />
                        ) : null}
                    </div>
                </div>
                <div style={{ height: 156 }}>
                    {mortalitySeries.length === 0 ? (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>
                            no {sourceLabel} mortality records
                        </div>
                    ) : (
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={mortalitySeries} margin={{ top: 12, right: 10, bottom: 2, left: 4 }}>
                                <XAxis
                                    dataKey="label"
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={{ stroke: "rgba(255,255,255,0.06)" }}
                                    interval="preserveStartEnd"
                                    minTickGap={14}
                                />
                                <YAxis
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={false}
                                    width={34}
                                    tickCount={4}
                                    allowDecimals={false}
                                    domain={[0, (dataMax: number) => Math.max(1, Math.ceil(dataMax * 1.15))]}
                                    allowDataOverflow={false}
                                    tickFormatter={formatCompactCount}
                                />
                                <Tooltip
                                    content={<MortalityTooltip sourceLabel={sourceLabel} />}
                                    cursor={{ stroke: "rgba(255,255,255,0.07)", strokeWidth: 1 }}
                                />
                                {mortalityHasBaseline ? (
                                    <Line type="monotone" dataKey="historicalAverageCount" stroke="rgba(255,255,255,0.42)" strokeWidth={1.3} strokeDasharray="4 4" dot={false} activeDot={false} connectNulls isAnimationActive={false} />
                                ) : null}
                                <Line type="monotone" dataKey="count" stroke={ACCENT} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: ACCENT, stroke: "none" }} connectNulls />
                            </LineChart>
                        </ResponsiveContainer>
                    )}
                </div>
            </div>

            <div style={CHART_CARD_STYLE}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                        <div style={{ ...CARD_TITLE_STYLE, fontFamily: trackerTitleFont.style.fontFamily }}>
                            Anomaly score
                        </div>
                        <InfoHint label="Anomaly score">
                            Monthly average of the selected anomaly score (0&ndash;1) for this species across the selected year&apos;s scored species-week-cell rows. Bioacoustic uses the embedding-drift score directly; fusion uses the model score from the active modeling run. Amber/red are the run&apos;s model alert counts. An empty month means no cells were scored, not a zero score.
                        </InfoHint>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <ChipSelect value={anomalyScoreType} options={ANOMALY_SCORE_OPTIONS} onChange={setAnomalyScoreType} loading={loading} disabled={anomalyMetric !== "avg"} minWidth={154} />
                        <ChipSelect value={anomalyMetric} options={ANOMALY_METRIC_OPTIONS} onChange={setAnomalyMetric} loading={loading} />
                        {anomaly.availableYears.length > 0 ? (
                            <YearSelect value={anomaly.year} years={anomaly.availableYears} onChange={setAnomalyYear} loading={loading} />
                        ) : null}
                    </div>
                </div>
                <div style={{ height: 156 }}>
                    {anomaly.data.length === 0 ? (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: TEXT_SOFT, fontSize: 10, fontFamily: MONO }}>
                            no anomaly scores available
                        </div>
                    ) : (
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={anomaly.data} margin={{ top: 12, right: 10, bottom: 2, left: 4 }}>
                                <XAxis
                                    dataKey="label"
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={{ stroke: "rgba(255,255,255,0.06)" }}
                                    interval="preserveStartEnd"
                                    minTickGap={14}
                                />
                                <YAxis
                                    tick={{ fill: TEXT_SOFT, fontSize: 9, fontFamily: MONO }}
                                    tickLine={false}
                                    axisLine={false}
                                    width={34}
                                    tickCount={4}
                                    allowDecimals={anomalyIsScore}
                                    domain={[
                                        0,
                                        anomalyIsScore
                                            ? (dataMax: number) => Math.max(0.05, Math.round(dataMax * 1.15 * 100) / 100)
                                            : (dataMax: number) => Math.max(1, Math.ceil(dataMax * 1.15)),
                                    ]}
                                    allowDataOverflow={false}
                                    tickFormatter={anomalyIsScore ? (value: number) => value.toFixed(2) : formatCompactCount}
                                />
                                <Tooltip
                                    content={<AnomalyTooltip scoreLabel={anomalyScoreLabel} />}
                                    cursor={{ stroke: "rgba(255,255,255,0.07)", strokeWidth: 1 }}
                                />
                                <Line type="monotone" dataKey={anomalyDataKey} name={anomalyIsScore ? anomalyScoreLabel : anomalyMetric} stroke={anomalyLineColor} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: anomalyLineColor, stroke: "none" }} connectNulls isAnimationActive={false} />
                            </LineChart>
                        </ResponsiveContainer>
                    )}
                </div>
                {anomaly.data.length > 0 && !anomaly.hasSignal ? (
                    <div style={{ marginTop: 4, fontSize: 9, color: TEXT_DIM, fontFamily: MONO }}>
                        no {anomalyScoreLabel} values for this species in the selected year
                    </div>
                ) : null}
            </div>
        </div>
    );
}

// === SpeciesPortrait ===========================================================
// Photo-first species image distinct from the existing SpeciesThumb (which
// renders an inverted PhyloPic silhouette on a flat background). The header
// wants a real species photo cover-fit; we fall back to the silhouette when no
// photo is available and to initials when nothing loads.
function SpeciesPortrait({
    species,
    size,
    loading = false,
}: {
    species: SpeciesTrackerSpeciesOption | null;
    size: number;
    loading?: boolean;
}) {
    const [failedPhoto, setFailedPhoto] = useState(false);
    const [failedSilhouette, setFailedSilhouette] = useState(false);

    const photoUrl = species?.thumbnailUrl || species?.imageUrl || null;
    const silhouetteUrl = species?.silhouetteUrl || null;
    const initials = species?.commonName?.trim().slice(0, 2).toUpperCase() ?? "";

    useEffect(() => {
        setFailedPhoto(false);
        setFailedSilhouette(false);
    }, [photoUrl, silhouetteUrl]);

    const radius = Math.round(size * 0.18);
    const showPhoto = !loading && photoUrl && !failedPhoto;
    const showSilhouette = !loading && !showPhoto && silhouetteUrl && !failedSilhouette;

    return (
        <div
            style={{
                width: size,
                height: size,
                borderRadius: radius,
                overflow: "hidden",
                background: PANEL_SURFACE_ALT,
                flexShrink: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                border: `1px solid ${BORDER}`,
                position: "relative",
            }}
        >
            {loading ? (
                <SkeletonBlock width="100%" height="100%" radius={radius} />
            ) : showPhoto ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                    src={photoUrl ?? ""}
                    alt=""
                    style={{ width: "100%", height: "100%", objectFit: "cover" }}
                    onError={() => setFailedPhoto(true)}
                />
            ) : showSilhouette ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                    src={silhouetteUrl ?? ""}
                    alt=""
                    style={{
                        width: "78%",
                        height: "78%",
                        objectFit: "contain",
                        filter: "invert(1)",
                        opacity: 0.7,
                    }}
                    onError={() => setFailedSilhouette(true)}
                />
            ) : initials ? (
                <span
                    style={{
                        fontSize: Math.round(size * 0.30),
                        color: TEXT_SOFT,
                        fontFamily: trackerTitleFont.style.fontFamily,
                        fontWeight: 600,
                        letterSpacing: "0.04em",
                    }}
                >
                    {initials}
                </span>
            ) : (
                <SpeciesThumbFallback size={size} />
            )}
        </div>
    );
}

// === TrackerHeader =============================================================
// Persistent header above the tab switcher. The 3-second answer to "what
// species are we looking at and is anything weird?". Replaces the old
// title-bar + dropdown-only header — keeps the SpeciesDropdown as the selector
// while adding photo, name pair, status chips, and the migration arc.
function TrackerHeader({
    selectedSpecies,
    speciesOptions,
    activeSpeciesId,
    dropdownLoading,
    // mortalitySignal is plumbed from the parent (headerMortalitySignal) for an
    // in-header mortality indicator that isn't rendered yet. Kept on the
    // interface so the wiring doesn't have to be rebuilt when the indicator
    // lands; remove both this prop and the parent's pass-through if that feature
    // is dropped.
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    mortalitySignal,
    onSelectSpecies,
}: {
    selectedSpecies: SpeciesTrackerSpeciesOption | null;
    speciesOptions: SpeciesTrackerSpeciesOption[];
    activeSpeciesId: string;
    dropdownLoading: boolean;
    mortalitySignal: MortalityElevation;
    onSelectSpecies: (speciesId: string) => void;
}) {
    return (
        <div
            className="px-5 pt-4 pb-3 shrink-0"
            style={{ position: "relative", zIndex: 40, display: "flex", flexDirection: "column", gap: 12 }}
        >
            <div className="flex items-center">
                <div className="flex items-center gap-2.5">
                    <div className="relative flex items-center justify-center w-2 h-2">
                        <div
                            className="absolute inset-0 rounded-full animate-ping opacity-75"
                            style={{ background: "rgba(52,211,153,0.8)" }}
                        />
                        <div
                            className="relative w-1.5 h-1.5 rounded-full"
                            style={{ background: ACCENT }}
                        />
                    </div>
                    <h2
                        className="text-[11px] font-semibold tracking-[0.10em] uppercase text-white/70"
                        style={{ fontFamily: trackerTitleFont.style.fontFamily }}
                    >
                        Species Tracker
                    </h2>
                </div>
            </div>

            <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
                <SpeciesPortrait species={selectedSpecies} size={64} loading={dropdownLoading} />
                <div style={{ minWidth: 0, flex: "1 1 auto", display: "flex", flexDirection: "column", gap: 6 }}>
                    <SpeciesDropdown
                        options={speciesOptions}
                        selectedId={activeSpeciesId}
                        loading={dropdownLoading}
                        onSelect={onSelectSpecies}
                    />
                    {selectedSpecies?.scientificName ? (
                        <div
                            style={{
                                fontSize: 11,
                                fontStyle: "italic",
                                color: TEXT_SOFT,
                                fontFamily: MONO,
                                paddingLeft: 2,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                            }}
                            title={selectedSpecies.scientificName}
                        >
                            {selectedSpecies.scientificName}
                        </div>
                    ) : null}
                </div>
            </div>

        </div>
    );
}

export function SpeciesTracker({ onFocusStation }: { onFocusStation?: (stationId: string | null) => void }) {
    const [tab, setTab] = useState<TrackerTab>("activity");
    const [selectedSpeciesId, setSelectedSpeciesId] = useState<string>("");
    // Grain state is kept (the cache-warming useEffect below still reads it to
    // pick which slice to fetch and warm), but the d/w/m UI switch is gone so
    // the setter is unused. Reserved for re-enabling per-grain navigation later.
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const [grain, _setGrain] = useState<SpeciesTrackerGrain>("week");
    const [responseCache, setResponseCache] = useState<SpeciesTrackerResponseCache>({});
    const [loadingKeys, setLoadingKeys] = useState<Record<string, boolean>>({});
    const inFlightRequests = useRef<Record<string, boolean>>({});

    const fetchGrainData = (
        cacheKey: string,
        grainKey: SpeciesTrackerGrain,
        signal: AbortSignal,
    ) => {
        const requestKey = `${cacheKey}:${grainKey}`;
        if (inFlightRequests.current[requestKey]) return;

        const params = new URLSearchParams({ grain: grainKey });
        if (selectedSpeciesId) params.set("species_id", selectedSpeciesId);

        inFlightRequests.current[requestKey] = true;
        setLoadingKeys((current) => current[requestKey] ? current : { ...current, [requestKey]: true });
        fetch(`/api/species-tracker?${params}`, { signal, cache: "no-store" })
            .then((r) => (r.ok ? (r.json() as Promise<SpeciesTrackerResponse>) : null))
            .then((json) => {
                if (!json) return;
                setResponseCache((current) => ({
                    ...current,
                    [cacheKey]: {
                        ...(current[cacheKey] ?? {}),
                        [grainKey]: json,
                    },
                }));
            })
            .catch(() => {})
            .finally(() => {
                delete inFlightRequests.current[requestKey];
                setLoadingKeys((current) => {
                    if (!current[requestKey]) return current;
                    const next = { ...current };
                    delete next[requestKey];
                    return next;
                });
            });
    };

    useEffect(() => {
        const controller = new AbortController();
        const cacheKey = speciesCacheKey(selectedSpeciesId);
        if (!responseCache[cacheKey]?.[grain]) {
            fetchGrainData(cacheKey, grain, controller.signal);
        }

        return () => {
            controller.abort();
        };
        // fetchGrainData is recreated each render, so adding it to the deps
        // would re-fire this effect every render. The fetch is already guarded
        // by the responseCache check above, and the effect re-runs on the
        // inputs that actually matter (species, grain, cache).
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedSpeciesId, grain, responseCache]);

    useEffect(() => {
        const controller = new AbortController();
        const cacheKey = speciesCacheKey(selectedSpeciesId);
        const cachedGrains = responseCache[cacheKey] ?? {};
        const activeGrainResponse = cachedGrains[grain];
        const grainsToWarm = GRAIN_OPTIONS
            .map((option) => option.key)
            .filter((grainKey) => grainKey !== grain && !cachedGrains[grainKey]);

        if (!activeGrainResponse || !grainsToWarm.length) {
            return () => {
                controller.abort();
            };
        }

        let timeoutId: ReturnType<typeof globalThis.setTimeout> | null = null;
        let idleId: number | null = null;
        const requestIdle =
            typeof window !== "undefined"
                ? window.requestIdleCallback?.bind(window)
                : null;
        const cancelIdle =
            typeof window !== "undefined"
                ? window.cancelIdleCallback?.bind(window)
                : null;

        const warmRemainingGrains = () => {
            grainsToWarm.forEach((grainKey) => {
                fetchGrainData(cacheKey, grainKey, controller.signal);
            });
        };

        if (requestIdle) {
            idleId = requestIdle(() => {
                warmRemainingGrains();
            }, { timeout: 1500 });
        } else {
            timeoutId = globalThis.setTimeout(() => {
                warmRemainingGrains();
            }, 250);
        }

        return () => {
            controller.abort();
            if (timeoutId !== null) {
                globalThis.clearTimeout(timeoutId);
            }
            if (idleId !== null && cancelIdle) {
                cancelIdle(idleId);
            }
        };
        // Same as the effect above: fetchGrainData is recreated each render and
        // is intentionally omitted to avoid re-firing the background warm-up on
        // every render. Warm-up is keyed off species, grain, and cache state.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedSpeciesId, grain, responseCache]);

    const cacheKey = speciesCacheKey(selectedSpeciesId);
    const speciesCache = responseCache[cacheKey];
    const activeData = speciesCache?.[grain] ?? null;
    const fallbackData = firstCachedResponse(speciesCache) ?? firstCachedResponse(responseCache[speciesCacheKey("")]);
    const speciesOptions = fallbackData?.speciesList ?? [];
    const activeSpeciesId = selectedSpeciesId || activeData?.speciesId || fallbackData?.speciesId || "";
    const selectedSpecies = speciesOptions.find((option) => option.speciesId === activeSpeciesId) ?? speciesOptions[0] ?? null;
    const activeRequestKey = `${cacheKey}:${grain}`;
    const isDropdownLoading = !fallbackData;
    const isPopulationLoading = !activeData || !!loadingKeys[activeRequestKey];
    const isPopulationEmpty = !!activeData && (activeData.empty || !activeData.detail);

    const hotspots = activeData?.detail?.population?.hotspots ?? [];
    const mortalitySources = activeData?.detail?.mortalitySources;

    // Persistent-header derivations: the strongest mortality signal feeds the
    // chip.
    const headerMortalitySignal = pickMostElevatedMortality([
        mortalitySources?.sovon,
        activeData?.detail?.mortality,
    ]);

    return (
        <div
            style={{
                position: "relative",
                width: "100%",
                height: "100%",
                background: PANEL_BG,
                color: TEXT,
                display: "flex",
                flexDirection: "column",
                overflow: "hidden",
            }}
        >
            <div
                className="pointer-events-none absolute inset-0 z-10 rounded-2xl"
                style={{
                    backgroundImage:
                        "repeating-linear-gradient(0deg, transparent, transparent 3px, rgba(0,0,0,0.03) 3px, rgba(0,0,0,0.03) 4px)",
                }}
            />

            <div className="pointer-events-none absolute inset-0 z-10">
                <div className="absolute top-2.5 left-2.5 w-3.5 h-3.5" style={{ borderTop: `1.5px solid ${PANEL_CHROME}`, borderLeft: `1.5px solid ${PANEL_CHROME}` }} />
                <div className="absolute top-2.5 right-2.5 w-3.5 h-3.5" style={{ borderTop: `1.5px solid ${PANEL_CHROME}`, borderRight: `1.5px solid ${PANEL_CHROME}` }} />
                <div className="absolute bottom-2.5 left-2.5 w-3.5 h-3.5" style={{ borderBottom: `1.5px solid ${PANEL_CHROME}`, borderLeft: `1.5px solid ${PANEL_CHROME}` }} />
                <div className="absolute bottom-2.5 right-2.5 w-3.5 h-3.5" style={{ borderBottom: `1.5px solid ${PANEL_CHROME}`, borderRight: `1.5px solid ${PANEL_CHROME}` }} />
            </div>

            <TrackerHeader
                selectedSpecies={selectedSpecies}
                speciesOptions={speciesOptions}
                activeSpeciesId={activeSpeciesId}
                dropdownLoading={isDropdownLoading}
                mortalitySignal={headerMortalitySignal?.signal ?? null}
                onSelectSpecies={(id) => {
                    setSelectedSpeciesId(id);
                    setTab("activity");
                    onFocusStation?.(null);
                }}
            />

            <div className="px-5 pb-3 shrink-0" style={{ position: "relative", zIndex: 40 }}>
                <div className="flex gap-2">
                    {(
                        [
                            { key: "activity", label: "Activity" },
                            { key: "climate", label: "Habitat & climate" },
                            { key: "risk", label: "Risk" },
                        ] as Array<{ key: TrackerTab; label: string }>
                    ).map((entry) => (
                        <button
                            key={entry.key}
                            onClick={() => setTab(entry.key)}
                            className="text-[10px] font-medium tracking-[0.08em] px-3 py-1.5 rounded-full transition-all duration-150"
                            style={{
                                border: `1px solid ${tab === entry.key ? "rgba(52,211,153,0.42)" : BORDER}`,
                                background: tab === entry.key ? "rgba(52,211,153,0.14)" : "rgba(255,255,255,0.03)",
                                color: tab === entry.key ? TEXT : TEXT_DIM,
                                cursor: "pointer",
                                outline: "none",
                                fontFamily: trackerTitleFont.style.fontFamily,
                            }}
                        >
                            {entry.label}
                        </button>
                    ))}
                </div>
            </div>

            <div className="shrink-0 h-[1px] overflow-hidden" style={{ position: "relative", zIndex: 30 }}>
                <div className="h-full w-full st-scan-line" />
            </div>

            <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden", position: "relative", zIndex: 10 }}>
                {tab === "activity" && (
                    <ActivityTab
                        activity={activeData?.detail?.activity ?? null}
                        populationSeries={activeData?.detail?.population?.series ?? []}
                        hotspots={hotspots}
                        speciesId={activeSpeciesId}
                        loading={isPopulationLoading}
                        empty={isPopulationEmpty}
                        speciesName={selectedSpecies?.commonName ?? null}
                        onFocusStation={onFocusStation}
                    />
                )}
                {tab === "climate" && (
                    <ClimateTab
                        speciesId={activeSpeciesId}
                        speciesName={selectedSpecies?.commonName ?? null}
                    />
                )}
                {tab === "risk" && (
                    <RiskTab
                        sovon={mortalitySources?.sovon ?? null}
                        anomalySeries={activeData?.detail?.context?.anomalySeries ?? []}
                        loading={isPopulationLoading}
                        empty={isPopulationEmpty}
                        speciesName={selectedSpecies?.commonName ?? null}
                    />
                )}
            </div>

            <style dangerouslySetInnerHTML={{
                __html: `
                    @keyframes st-scan {
                        0%   { transform: translateX(-100%); }
                        100% { transform: translateX(100%); }
                    }
                    @keyframes st-skeleton {
                        0% { background-position: 100% 50%; }
                        100% { background-position: -100% 50%; }
                    }
                    .st-scan-line {
                        background: linear-gradient(90deg, transparent, rgba(255,255,255,0.16), transparent);
                        animation: st-scan 3s ease-in-out infinite;
                    }
                    .species-dropdown-list::-webkit-scrollbar { width: 3px; }
                    .species-dropdown-list::-webkit-scrollbar-track { background: transparent; }
                    .species-dropdown-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 2px; }
                `
            }} />
        </div>
    );
}
