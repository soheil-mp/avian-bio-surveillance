"use client";

import { useEffect, useState } from "react";

import type { GridCellIdentity, GridCellOverviewResponse } from "@/app/api/grid-cell/overview/route";

// Muted, low-chroma earth-tone palette for the land-cover mix bar.
const BAND_COLORS: Record<string, string> = {
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

// Live-feed surface so the grid popup matches the floating feed panel.
const PANEL_BG = "rgba(20,20,20,0.94)";

// === slippy-map tile math (Web Mercator) ===
function lonToTileX(lon: number, z: number): number {
    return ((lon + 180) / 360) * 2 ** z;
}
function latToTileY(lat: number, z: number): number {
    const r = (lat * Math.PI) / 180;
    return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z;
}
function tileXToLon(x: number, z: number): number {
    return (x / 2 ** z) * 360 - 180;
}
function tileYToLat(y: number, z: number): number {
    const n = Math.PI - (2 * Math.PI * y) / 2 ** z;
    return (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
}
function zoomForSize(km: number): number {
    if (km <= 2.5) return 14;
    if (km <= 5) return 13;
    if (km <= 10) return 12;
    return 11;
}
const ESRI_TILE = (z: number, x: number, y: number) =>
    `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`;

// === formatting helpers ===
function fmtCompact(n: number): string {
    if (!Number.isFinite(n)) return "—";
    if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(Math.abs(n) >= 10_000 ? 0 : 1)}k`;
    return `${Math.round(n)}`;
}
function dash(v: number | null | undefined): string {
    return v == null || !Number.isFinite(v) ? "—" : `${v}`;
}
function fmtMonthYear(iso: string | null): string | null {
    if (!iso) return null;
    const parsed = new Date(iso);
    if (!Number.isFinite(parsed.getTime())) return null;
    return parsed.toLocaleDateString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
}

export function Sparkline({ points, color, height = 26 }: { points: number[]; color: string; height?: number }) {
    if (points.length < 2) return null;
    const max = Math.max(...points);
    const min = Math.min(...points);
    const span = max - min || 1;
    const width = 100;
    const path = points
        .map((value, index) => {
            const x = (index / (points.length - 1)) * width;
            const y = height - ((value - min) / span) * (height - 3) - 1.5;
            return `${x.toFixed(2)},${y.toFixed(2)}`;
        })
        .join(" ");
    return (
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="w-full" style={{ height }}>
            <polyline points={path} fill="none" stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        </svg>
    );
}

// === satellite chip: 2×2 Esri tile mosaic centered on the cell, with outline ===
function GridSatelliteThumb({ identity, size = 76 }: { identity: GridCellIdentity; size?: number }) {
    const { lat, lon, bbox, gridSizeKm } = identity;
    if (lat == null || lon == null) return null;
    const z = zoomForSize(gridSizeKm ?? 5);
    const xf = lonToTileX(lon, z);
    const yf = latToTileY(lat, z);
    const tx0 = Math.floor(xf - 0.5);
    const ty0 = Math.floor(yf - 0.5);
    const tiles: Array<[number, number]> = [
        [tx0, ty0],
        [tx0 + 1, ty0],
        [tx0, ty0 + 1],
        [tx0 + 1, ty0 + 1],
    ];
    const west = tileXToLon(tx0, z);
    const east = tileXToLon(tx0 + 2, z);
    const north = tileYToLat(ty0, z);
    const south = tileYToLat(ty0 + 2, z);
    let outline: { left: number; top: number; width: number; height: number } | null = null;
    if (bbox) {
        const left = ((bbox.minLon - west) / (east - west)) * 100;
        const right = ((bbox.maxLon - west) / (east - west)) * 100;
        const top = ((north - bbox.maxLat) / (north - south)) * 100;
        const bottom = ((north - bbox.minLat) / (north - south)) * 100;
        outline = { left, top, width: right - left, height: bottom - top };
    }
    return (
        <div className="relative shrink-0 overflow-hidden rounded-md border border-white/15" style={{ width: size, height: size }}>
            <div className="absolute inset-0 grid grid-cols-2 grid-rows-2">
                {tiles.map(([x, y]) => (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img key={`${x}-${y}`} src={ESRI_TILE(z, x, y)} alt="" className="h-full w-full object-cover" loading="lazy" />
                ))}
            </div>
            {outline && (
                <div
                    className="absolute rounded-[2px] border-2 border-brand-300/90"
                    style={{
                        left: `${outline.left}%`,
                        top: `${outline.top}%`,
                        width: `${outline.width}%`,
                        height: `${outline.height}%`,
                        boxShadow: "0 0 0 9999px rgba(2,10,7,0.34)",
                    }}
                />
            )}
        </div>
    );
}

// Point-centred Esri satellite chip for a station/node (2×2 mosaic + marker).
export function PointSatelliteThumb({
    lat,
    lon,
    size = 76,
    zoom = 13,
}: {
    lat: number;
    lon: number;
    size?: number;
    zoom?: number;
}) {
    const z = zoom;
    const xf = lonToTileX(lon, z);
    const yf = latToTileY(lat, z);
    const tx0 = Math.floor(xf - 0.5);
    const ty0 = Math.floor(yf - 0.5);
    const tiles: Array<[number, number]> = [
        [tx0, ty0],
        [tx0 + 1, ty0],
        [tx0, ty0 + 1],
        [tx0 + 1, ty0 + 1],
    ];
    const markerLeft = ((xf - tx0) / 2) * 100;
    const markerTop = ((yf - ty0) / 2) * 100;
    return (
        <div className="relative overflow-hidden rounded-md border border-brand-500/15" style={{ width: size, height: size }}>
            <div className="absolute inset-0 grid grid-cols-2 grid-rows-2">
                {tiles.map(([x, y]) => (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img key={`${x}-${y}`} src={ESRI_TILE(z, x, y)} alt="" className="h-full w-full object-cover" loading="lazy" />
                ))}
            </div>
            <div
                className="absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white shadow-[0_0_0_2px_rgba(0,0,0,0.4)]"
                style={{ left: `${markerLeft}%`, top: `${markerTop}%`, background: "#34d399" }}
            />
        </div>
    );
}

// === reverse geocode (PDOK locatieserver) — best-effort place name ===
function usePlaceName(lat: number | null, lon: number | null): string | null {
    const [name, setName] = useState<string | null>(null);
    useEffect(() => {
        setName(null);
        if (lat == null || lon == null) return;
        const controller = new AbortController();
        const url =
            `https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse` +
            `?lat=${lat}&lon=${lon}&rows=1&type=woonplaats&fl=weergavenaam,woonplaatsnaam,gemeentenaam`;
        fetch(url, { signal: controller.signal })
            .then((response) => (response.ok ? response.json() : null))
            .then((json) => {
                if (controller.signal.aborted || !json) return;
                const doc = json?.response?.docs?.[0];
                const resolved: string | undefined =
                    doc?.woonplaatsnaam ?? doc?.gemeentenaam ?? doc?.weergavenaam;
                if (resolved) setName(String(resolved).split(",")[0]);
            })
            .catch(() => {});
        return () => controller.abort();
    }, [lat, lon]);
    return name;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
    return (
        <div className="text-[8px] font-mono uppercase tracking-[0.18em] text-text-muted/60">{children}</div>
    );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
    return (
        <div className="rounded-md border border-white/8 bg-white/[0.03] px-2.5 py-2 text-[10px] leading-snug text-text-secondary">
            {children}
        </div>
    );
}

function PopupSkeleton() {
    return (
        <div className="flex animate-pulse flex-col gap-3">
            <div className="flex items-stretch gap-3">
                <div className="h-[76px] w-[76px] shrink-0 rounded-md bg-white/[0.06]" />
                <div className="flex flex-1 flex-col justify-center gap-2">
                    <div className="h-3 w-2/3 rounded bg-white/[0.06]" />
                    <div className="h-2 w-1/3 rounded bg-white/[0.05]" />
                </div>
            </div>
            <div className="h-3 w-full rounded bg-white/[0.05]" />
            <div className="h-14 w-full rounded-md bg-white/[0.04]" />
        </div>
    );
}

export function GridCellPopup({
    overview,
    loading,
    fallbackLabel,
}: {
    overview: GridCellOverviewResponse | null;
    loading: boolean;
    fallbackLabel: string;
}) {
    const identity = overview?.identity ?? null;
    const placeName = usePlaceName(identity?.lat ?? null, identity?.lon ?? null);

    const referenceLabel = identity?.referenceLabel ?? fallbackLabel;
    const title = placeName ?? `Grid ${referenceLabel}`;

    const firstMY = fmtMonthYear(overview?.species.firstDetection ?? null);
    const lastMY = fmtMonthYear(overview?.species.lastDetection ?? null);
    const coverageSpan = firstMY && lastMY ? `${firstMY} – ${lastMY}` : firstMY ?? lastMY ?? null;

    return (
        <div
            className="w-[340px] rounded-md border border-white/10 p-3.5 text-text-primary shadow-[0_8px_32px_rgba(0,0,0,0.8)] backdrop-blur-xl"
            style={{ background: PANEL_BG }}
        >
            {loading && !overview ? (
                <PopupSkeleton />
            ) : !overview ? (
                <EmptyNote>Grid data is unavailable right now.</EmptyNote>
            ) : (
                (() => {
                    const { habitat, alerts, species } = overview;
                    const composition = (habitat?.composition ?? []).filter((band) => band.pct > 0);
                    const legend = composition.filter((band) => band.pct >= 3);
                    // Coverage gate: anomaly needs a BirdWeather recording station
                    // in this cell.
                    const hasBioacousticCoverage = species.stationCount > 0;
                    const metrics: Array<{ label: string; value: string; title?: string }> = [
                        { label: "Detections", value: fmtCompact(species.totalDetections) },
                        { label: "Stations", value: dash(species.stationCount) },
                        {
                            label: "Avg anomaly",
                            value: !hasBioacousticCoverage
                                ? "No coverage"
                                : alerts.avgScore == null
                                  ? "—"
                                  : `${Math.round(alerts.avgScore * 100)}%`,
                            title: "Mean anomaly score across this cell",
                        },
                    ];

                    return (
                        <>
                            {/* Header — satellite chip + title + detection date range */}
                            <div className="flex items-stretch gap-3 pr-5">
                                {identity && <GridSatelliteThumb identity={identity} size={76} />}
                                <div className="flex min-w-0 flex-1 flex-col justify-center">
                                    <h3 className="truncate font-mono text-[13px] font-semibold tracking-[0.06em] text-brand-100">
                                        {title}
                                    </h3>
                                    {coverageSpan && (
                                        <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-text-muted/65">
                                            {coverageSpan}
                                        </div>
                                    )}
                                </div>
                            </div>

                            {/* Land cover mix */}
                            {composition.length > 0 && (
                                <div className="mt-3 flex flex-col gap-1.5">
                                    <SectionLabel>Land cover mix</SectionLabel>
                                    <div className="flex h-3 overflow-hidden rounded border border-white/10">
                                        {composition.map((band) => (
                                            <div
                                                key={band.key}
                                                title={`${band.label} ${band.pct}%`}
                                                style={{ width: `${band.pct}%`, background: BAND_COLORS[band.key] ?? "rgba(255,255,255,0.2)" }}
                                            />
                                        ))}
                                    </div>
                                    <div className="flex flex-wrap gap-x-2.5 gap-y-1">
                                        {legend.map((band) => (
                                            <span key={band.key} className="inline-flex items-center gap-1 text-[9px] font-mono text-text-secondary">
                                                <span className="h-2 w-2 rounded-[2px]" style={{ background: BAND_COLORS[band.key] ?? "rgba(255,255,255,0.2)" }} />
                                                {band.label} {band.pct}%
                                            </span>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {/* Metric boxes */}
                            <div className="mt-3 grid grid-cols-3 overflow-hidden rounded-md border border-white/8 bg-white/[0.02]">
                                {metrics.map((metric, index) => (
                                    <div
                                        key={metric.label}
                                        title={metric.title}
                                        className={`px-2 py-2 ${index % 3 !== 0 ? "border-l border-white/[0.06]" : ""} ${index >= 3 ? "border-t border-white/[0.06]" : ""}`}
                                    >
                                        <div className="text-[8px] font-mono uppercase tracking-[0.1em] text-text-muted/50">{metric.label}</div>
                                        <div className="mt-0.5 font-mono text-[13px] tabular-nums text-brand-100">{metric.value}</div>
                                    </div>
                                ))}
                            </div>

                            {/* Top species — photos only (tight, overlapping), hover for the value */}
                            {species.top.length > 0 && (
                                <div className="mt-3 flex items-center gap-2">
                                    <SectionLabel>Top species</SectionLabel>
                                    <div className="flex -space-x-1.5">
                                        {species.top.slice(0, 6).map((sp) =>
                                            sp.thumbnailUrl ? (
                                                // eslint-disable-next-line @next/next/no-img-element
                                                <img
                                                    key={sp.speciesId}
                                                    src={sp.thumbnailUrl}
                                                    alt=""
                                                    title={`${sp.commonName ?? sp.speciesId} · ${fmtCompact(sp.detections)} detections`}
                                                    className="h-6 w-6 rounded-full border border-surface-elevated object-cover ring-1 ring-brand-500/20 transition-transform hover:z-10 hover:scale-125"
                                                />
                                            ) : (
                                                <div
                                                    key={sp.speciesId}
                                                    title={`${sp.commonName ?? sp.speciesId} · ${fmtCompact(sp.detections)} detections`}
                                                    className="h-6 w-6 rounded-full border border-surface-elevated bg-brand-500/10 ring-1 ring-brand-500/20"
                                                />
                                            ),
                                        )}
                                    </div>
                                </div>
                            )}
                        </>
                    );
                })()
            )}
        </div>
    );
}

export default GridCellPopup;
