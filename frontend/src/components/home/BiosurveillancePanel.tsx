"use client";

import { useEffect, useMemo, useState } from "react";

/* ─── Types ────────────────────────────────── */
interface SpeciesDay {
  species: string;
  day: string;
  detections: number;
  thumbnail: string;
}
interface MortalityRow {
  species: string;
  month: number;
  count: number;
  baseline: number;
  thumbnail: string;
}
interface StationHealth {
  id: string | number;
  name: string;
  type: string;
  detections: number;
  species: number;
  quality: number;
  availability90dPct?: number;
  lifetimeAvailabilityPct?: number;
  last_detection: string | null;
  status: "active" | "stale" | "dormant" | "unknown";
}

type Tab = "radar" | "mortality" | "net";

/* ─── Constants ────────────────────────────── */
const TC = "#34d399";
const TC_RGB = "52,211,153";
const typeColor = (t: string) =>
  t === "puc" ? "#34d399" : t === "birdnetpi" ? "#fbbf24" : "#38bdf8";
const statusColor = (s: string) =>
  s === "active" ? "#34d399" : s === "stale" ? "#fbbf24" : "#dc2626";

function formatNum(n: number) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(n);
}

const SPECIES_PALETTE = [
  "52,211,153", // emerald
  "56,189,248", // sky
  "167,139,250", // violet
  "251,146,60", // orange
  "244,114,182", // pink
  "251,191,36", // amber
  "34,211,238", // cyan
  "192,132,252", // purple
  "74,222,128", // green
  "248,113,113", // red
  "129,140,248", // indigo
];

/* ─── Demo data ────────────────────────────── */
const DEMO_SPECIES_NAMES = [
  "Eurasian Blackbird",
  "Common Wood-Pigeon",
  "Eurasian Blue Tit",
  "European Robin",
  "Song Thrush",
  "Common Chaffinch",
  "Dunnock",
  "Eurasian Wren",
];

const DEMO_SPECIES: SpeciesDay[] = (() => {
  const data: SpeciesDay[] = [];
  const bases = [180, 130, 100, 85, 65, 55, 40, 30];
  for (let si = 0; si < DEMO_SPECIES_NAMES.length; si++) {
    const base = bases[si];
    const phase = si * 0.8;
    for (let d = 0; d < 30; d++) {
      data.push({
        species: DEMO_SPECIES_NAMES[si],
        day: `2026-02-${String(d + 1).padStart(2, "0")}`,
        detections: Math.max(
          5,
          Math.round(
            base +
              Math.sin(d * 0.3 + phase) * base * 0.35 +
              Math.cos(d * 0.5 + phase * 2) * base * 0.15,
          ),
        ),
        thumbnail: "",
      });
    }
  }
  return data;
})();

const DEMO_MORTALITY: MortalityRow[] = (() => {
  const species = [
    "Eurasian Blackbird",
    "Song Thrush",
    "Common Wood-Pigeon",
    "European Robin",
    "Eurasian Blue Tit",
    "Dunnock",
  ];
  const data: MortalityRow[] = [];
  for (let si = 0; si < species.length; si++) {
    const base = si * 3 + 8;
    const phase = si * 0.7;
    for (let m = 1; m <= 12; m++) {
      data.push({
        species: species[si],
        month: m,
        count: Math.max(
          0,
          Math.round(base + Math.sin((m - 3) * 0.5 + phase) * base * 0.4),
        ),
        baseline: Math.max(
          0,
          Math.round(base * 0.8 + Math.sin((m - 3) * 0.5 + phase) * base * 0.3),
        ),
        thumbnail: "",
      });
    }
  }
  return data;
})();

const DEMO_STATIONS: StationHealth[] = [
  {
    id: 1,
    name: "BRS-PUC-001",
    type: "puc",
    detections: 12450,
    species: 42,
    quality: 0.92,
    availability90dPct: 92,
    lifetimeAvailabilityPct: 87,
    last_detection: "2026-03-12",
    status: "active",
  },
  {
    id: 2,
    name: "BRS-PUC-002",
    type: "puc",
    detections: 8300,
    species: 38,
    quality: 0.88,
    availability90dPct: 88,
    lifetimeAvailabilityPct: 82,
    last_detection: "2026-03-11",
    status: "active",
  },
  {
    id: 3,
    name: "BRS-BPI-001",
    type: "birdnetpi",
    detections: 5100,
    species: 31,
    quality: 0.79,
    availability90dPct: 79,
    lifetimeAvailabilityPct: 68,
    last_detection: "2026-03-10",
    status: "active",
  },
  {
    id: 4,
    name: "BRS-BPI-002",
    type: "birdnetpi",
    detections: 3200,
    species: 27,
    quality: 0.74,
    availability90dPct: 74,
    lifetimeAvailabilityPct: 71,
    last_detection: "2026-03-01",
    status: "stale",
  },
  {
    id: 5,
    name: "BRS-APP-001",
    type: "app",
    detections: 920,
    species: 14,
    quality: 0.61,
    availability90dPct: 61,
    lifetimeAvailabilityPct: 49,
    last_detection: "2026-02-15",
    status: "stale",
  },
  {
    id: 6,
    name: "BRS-BPI-003",
    type: "birdnetpi",
    detections: 410,
    species: 8,
    quality: 0.45,
    availability90dPct: 45,
    lifetimeAvailabilityPct: 37,
    last_detection: "2026-01-02",
    status: "dormant",
  },
];

/* ─── SVG helpers ──────────────────────────── */
function polarXY(cx: number, cy: number, angle: number, radius: number) {
  const rad = ((angle - 90) * Math.PI) / 180;
  return { x: cx + radius * Math.cos(rad), y: cy + radius * Math.sin(rad) };
}

/** Smooth closed path via Catmull-Rom → cubic Bezier */
function smoothClosedPath(pts: { x: number; y: number }[]): string {
  const n = pts.length;
  if (n < 3) return "";
  const d: string[] = [`M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`];
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % n];
    const p3 = pts[(i + 2) % n];
    const cp1x = p1.x + (p2.x - p0.x) / 5;
    const cp1y = p1.y + (p2.y - p0.y) / 5;
    const cp2x = p2.x - (p3.x - p1.x) / 5;
    const cp2y = p2.y - (p3.y - p1.y) / 5;
    d.push(
      `C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`,
    );
  }
  d.push("Z");
  return d.join(" ");
}

/* ═══════════════════════════════════════════════
   SPECIES RADAR — radial area chart (population over time)
   ═══════════════════════════════════════════════ */
function SpeciesRadar({ data }: { data: SpeciesDay[] }) {
  const cx = 130,
    cy = 130,
    outerR = 115,
    innerR = 42;
  const [hovered, setHovered] = useState<string | null>(null);

  const speciesMap = useMemo(() => {
    const map = new Map<
      string,
      { days: SpeciesDay[]; total: number; thumbnail: string }
    >();
    for (const row of data) {
      if (!map.has(row.species))
        map.set(row.species, {
          days: [],
          total: 0,
          thumbnail: row.thumbnail || "",
        });
      const entry = map.get(row.species)!;
      entry.days.push(row);
      entry.total += row.detections;
      if (row.thumbnail && !entry.thumbnail) entry.thumbnail = row.thumbnail;
    }
    return map;
  }, [data]);

  const speciesList = useMemo(
    () =>
      [...speciesMap.entries()]
        .sort((a, b) => b[1].total - a[1].total)
        .map(([name, val], i) => ({
          name,
          ...val,
          color: SPECIES_PALETTE[i % SPECIES_PALETTE.length],
        })),
    [speciesMap],
  );

  const maxDet = useMemo(
    () => Math.max(...data.map((d) => d.detections), 1),
    [data],
  );
  const numDays = useMemo(() => {
    const days = new Set(data.map((d) => d.day));
    return Math.max(days.size, 1);
  }, [data]);

  const totalDet = useMemo(
    () => speciesList.reduce((s, sp) => s + sp.total, 0),
    [speciesList],
  );

  /* Build SVG paths: most-detected rendered first (behind), least last (front) */
  const paths = useMemo(() => {
    return [...speciesList].reverse().map((sp) => {
      const angleStep = 360 / numDays;
      const pts = sp.days.map((day, di) => {
        const angle = di * angleStep;
        const ratio = day.detections / maxDet;
        const r = innerR + (outerR - innerR) * ratio;
        return polarXY(cx, cy, angle, r);
      });
      return {
        name: sp.name,
        color: sp.color,
        path: smoothClosedPath(pts),
        total: sp.total,
      };
    });
  }, [speciesList, numDays, maxDet]);

  /* Day labels around the edge, every 5 days */
  const dayLabels = useMemo(() => {
    const allDays = [...new Set(data.map((d) => d.day))].sort();
    const angleStep = 360 / Math.max(allDays.length, 1);
    return allDays
      .filter((_, i) => i % 5 === 0)
      .map((day, idx) => {
        const angle = idx * 5 * angleStep;
        const pos = polarXY(cx, cy, angle, outerR + 10);
        return { label: day.slice(5), ...pos };
      });
  }, [data]);

  return (
    <div className="flex flex-col items-center gap-2 w-full">
      <svg viewBox="0 0 260 260" className="w-full max-w-[250px]">
        {/* Guide rings */}
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <circle
            key={f}
            cx={cx}
            cy={cy}
            r={innerR + (outerR - innerR) * f}
            fill="none"
            stroke={`rgba(${TC_RGB},0.06)`}
            strokeWidth="0.5"
          />
        ))}
        {/* Inner circle (dark core) */}
        <circle
          cx={cx}
          cy={cy}
          r={innerR}
          fill="#0a0f0d"
          stroke={`rgba(${TC_RGB},0.1)`}
          strokeWidth="0.5"
        />

        {/* Species area blobs — layered with transparency */}
        {paths.map((p) => (
          <path
            key={p.name}
            d={p.path}
            fill={`rgba(${p.color},${hovered === null || hovered === p.name ? 0.25 : 0.08})`}
            stroke={`rgba(${p.color},${hovered === null || hovered === p.name ? 0.6 : 0.15})`}
            strokeWidth={hovered === p.name ? 1.5 : 0.7}
            className="transition-all duration-300"
            style={{
              filter:
                hovered === p.name
                  ? `drop-shadow(0 0 6px rgba(${p.color},0.5))`
                  : undefined,
            }}
          />
        ))}

        {/* Day labels */}
        {dayLabels.map((l, i) => (
          <text
            key={i}
            x={l.x}
            y={l.y}
            textAnchor="middle"
            dominantBaseline="middle"
            fill={`rgba(${TC_RGB},0.2)`}
            fontSize="5"
            fontFamily="monospace"
          >
            {l.label}
          </text>
        ))}

        {/* Scanning sweep */}
        <line
          x1={cx}
          y1={cy}
          x2={cx}
          y2={cy - outerR - 4}
          stroke={`rgba(${TC_RGB},0.12)`}
          strokeWidth="0.8"
          style={{
            transformOrigin: `${cx}px ${cy}px`,
            animation: "radar-scan 8s linear infinite",
          }}
        />

        {/* Center text */}
        <text
          x={cx}
          y={cy - 4}
          textAnchor="middle"
          fill={TC}
          fontSize="14"
          fontWeight="700"
          fontFamily="monospace"
        >
          {formatNum(totalDet)}
        </text>
        <text
          x={cx}
          y={cy + 9}
          textAnchor="middle"
          fill={`rgba(${TC_RGB},0.35)`}
          fontSize="5"
          fontFamily="monospace"
          letterSpacing="0.1em"
        >
          30-DAY TOTAL
        </text>
      </svg>

      {/* Legend */}
      <div className="w-full max-h-[100px] overflow-y-auto custom-scrollbar-panel px-1">
        {speciesList.map((sp) => (
          <div
            key={sp.name}
            className="flex items-center gap-2 py-[3px] px-1.5 rounded transition-colors duration-200 cursor-default"
            style={{
              background:
                hovered === sp.name ? `rgba(${sp.color},0.06)` : undefined,
            }}
            onMouseEnter={() => setHovered(sp.name)}
            onMouseLeave={() => setHovered(null)}
          >
            {sp.thumbnail ? (
              <img
                src={sp.thumbnail}
                alt=""
                className="w-3.5 h-3.5 rounded-full object-cover shrink-0"
              />
            ) : (
              <div
                className="w-3.5 h-3.5 rounded-full shrink-0"
                style={{ background: `rgba(${sp.color},0.4)` }}
              />
            )}
            <span
              className="text-[9px] truncate flex-1"
              style={{
                color: `rgba(255,255,255,${hovered === sp.name ? 0.9 : 0.5})`,
              }}
            >
              {sp.name}
            </span>
            <span
              className="text-[9px] font-mono tabular-nums shrink-0"
              style={{ color: `rgba(${sp.color},0.6)` }}
            >
              {formatNum(sp.total)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════
   MORTALITY HEAT — thermal heatmap, all species
   ═══════════════════════════════════════════════ */
function MortalityHeat({
  data,
  thumbnailMap,
}: {
  data: MortalityRow[];
  thumbnailMap: Map<string, string>;
}) {
  const [hovered, setHovered] = useState<{
    species: string;
    month: number;
  } | null>(null);

  const speciesData = useMemo(() => {
    const map = new Map<
      string,
      { months: Map<number, MortalityRow>; total: number; maxCount: number }
    >();
    for (const row of data) {
      if (!map.has(row.species))
        map.set(row.species, { months: new Map(), total: 0, maxCount: 0 });
      const entry = map.get(row.species)!;
      entry.months.set(row.month, row);
      entry.total += row.count;
      entry.maxCount = Math.max(entry.maxCount, row.count);
    }
    return [...map.entries()]
      .sort((a, b) => b[1].total - a[1].total)
      .map(([name, val]) => ({ name, ...val }));
  }, [data]);

  const globalMax = useMemo(
    () => Math.max(...data.map((d) => d.count), 1),
    [data],
  );
  const months = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];
  const currentMonth = new Date().getMonth() + 1;

  function heatColor(count: number, baseline: number): string {
    const ratio = count / globalMax;
    const spike = baseline > 0 && count > baseline * 1.3;
    if (spike) return `rgba(220,38,38,${(0.3 + ratio * 0.6).toFixed(2)})`;
    if (ratio < 0.1) return `rgba(${TC_RGB},0.04)`;
    if (ratio < 0.35) return `rgba(${TC_RGB},${(ratio * 0.6).toFixed(2)})`;
    if (ratio < 0.65)
      return `rgba(251,191,36,${(0.15 + ratio * 0.35).toFixed(2)})`;
    return `rgba(220,38,38,${(0.15 + ratio * 0.5).toFixed(2)})`;
  }

  return (
    <div className="flex flex-col gap-2 w-full">
      {/* Month header */}
      <div className="flex items-center gap-0 ml-[30px] mr-[28px]">
        {months.map((m, i) => (
          <div key={i} className="flex-1 text-center">
            <span
              className="text-[6px] font-mono"
              style={{
                color: i + 1 === currentMonth ? TC : `rgba(${TC_RGB},0.2)`,
              }}
            >
              {m}
            </span>
          </div>
        ))}
      </div>

      {/* Species rows */}
      <div className="flex flex-col gap-[3px]">
        {speciesData.map((sp) => {
          const thumb =
            thumbnailMap.get(sp.name) ||
            data.find((d) => d.species === sp.name)?.thumbnail ||
            "";
          return (
            <div key={sp.name} className="flex items-center gap-1.5">
              {/* Thumbnail */}
              <div
                className="w-[22px] h-[22px] shrink-0 rounded-full overflow-hidden flex items-center justify-center"
                style={{
                  background: "rgba(0,0,0,0.4)",
                  border: `1px solid rgba(${TC_RGB},0.08)`,
                }}
              >
                {thumb ? (
                  <img
                    src={thumb}
                    alt=""
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <span
                    className="text-[6px] font-mono font-bold"
                    style={{ color: `rgba(${TC_RGB},0.3)` }}
                  >
                    {sp.name.slice(0, 2).toUpperCase()}
                  </span>
                )}
              </div>

              {/* Heatmap cells */}
              <div className="flex-1 flex gap-[2px]">
                {Array.from({ length: 12 }, (_, mi) => {
                  const row = sp.months.get(mi + 1);
                  const count = row?.count ?? 0;
                  const baseline = row?.baseline ?? 0;
                  const isHov =
                    hovered?.species === sp.name && hovered?.month === mi + 1;
                  const spike = baseline > 0 && count > baseline * 1.3;

                  return (
                    <div
                      key={mi}
                      className="flex-1 aspect-[1.2] rounded-[2px] relative cursor-default transition-all duration-200"
                      style={{
                        background: heatColor(count, baseline),
                        boxShadow: isHov
                          ? `0 0 6px ${spike ? "rgba(220,38,38,0.4)" : `rgba(${TC_RGB},0.3)`}`
                          : undefined,
                        outline:
                          mi + 1 === currentMonth
                            ? `1px solid rgba(${TC_RGB},0.15)`
                            : undefined,
                      }}
                      onMouseEnter={() =>
                        setHovered({ species: sp.name, month: mi + 1 })
                      }
                      onMouseLeave={() => setHovered(null)}
                    >
                      {count > 0 && (
                        <span
                          className="absolute inset-0 flex items-center justify-center text-[5.5px] font-mono font-bold"
                          style={{
                            color: spike
                              ? "rgba(255,255,255,0.85)"
                              : `rgba(255,255,255,${Math.min(count / globalMax + 0.2, 0.7).toFixed(2)})`,
                          }}
                        >
                          {count}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Row total */}
              <span
                className="text-[8px] font-mono tabular-nums w-[24px] text-right shrink-0"
                style={{ color: `rgba(${TC_RGB},0.4)` }}
              >
                {sp.total}
              </span>
            </div>
          );
        })}
      </div>

      {/* Hover tooltip */}
      {hovered &&
        (() => {
          const row = data.find(
            (d) => d.species === hovered.species && d.month === hovered.month,
          );
          if (!row) return null;
          const spike = row.baseline > 0 && row.count > row.baseline * 1.3;
          return (
            <div
              className="px-2 py-1.5 rounded mx-1 mt-1"
              style={{
                background: `rgba(${TC_RGB},0.04)`,
                border: `1px solid rgba(${TC_RGB},0.08)`,
              }}
            >
              <div
                className="text-[9px] font-mono"
                style={{ color: "rgba(255,255,255,0.65)" }}
              >
                {hovered.species} &middot; {months[hovered.month - 1]}
              </div>
              <div className="flex items-center gap-3 mt-0.5">
                <span
                  className="text-[8px] font-mono"
                  style={{ color: spike ? "#dc2626" : `rgba(${TC_RGB},0.5)` }}
                >
                  DEAD: {row.count}
                </span>
                <span
                  className="text-[8px] font-mono"
                  style={{ color: `rgba(${TC_RGB},0.25)` }}
                >
                  AVG: {row.baseline}
                </span>
                {spike && (
                  <span
                    className="text-[7px] font-mono px-1 py-0.5 rounded"
                    style={{
                      color: "#dc2626",
                      background: "rgba(220,38,38,0.1)",
                      border: "1px solid rgba(220,38,38,0.15)",
                    }}
                  >
                    ABOVE BASELINE
                  </span>
                )}
              </div>
            </div>
          );
        })()}

      {/* Legend strip */}
      <div className="flex items-center gap-3 px-1 mt-1">
        <div className="flex items-center gap-1">
          <div
            className="w-3 h-2 rounded-[1px]"
            style={{ background: `rgba(${TC_RGB},0.15)` }}
          />
          <span
            className="text-[7px] font-mono"
            style={{ color: `rgba(${TC_RGB},0.25)` }}
          >
            LOW
          </span>
        </div>
        <div className="flex items-center gap-1">
          <div
            className="w-3 h-2 rounded-[1px]"
            style={{ background: "rgba(251,191,36,0.35)" }}
          />
          <span
            className="text-[7px] font-mono"
            style={{ color: "rgba(251,191,36,0.3)" }}
          >
            MED
          </span>
        </div>
        <div className="flex items-center gap-1">
          <div
            className="w-3 h-2 rounded-[1px]"
            style={{ background: "rgba(220,38,38,0.5)" }}
          />
          <span
            className="text-[7px] font-mono"
            style={{ color: "rgba(220,38,38,0.4)" }}
          >
            HIGH
          </span>
        </div>
        <div className="flex items-center gap-1 ml-auto">
          <div
            className="w-3 h-2 rounded-[1px]"
            style={{ background: "rgba(220,38,38,0.7)" }}
          />
          <span
            className="text-[7px] font-mono"
            style={{ color: "rgba(220,38,38,0.4)" }}
          >
            SPIKE
          </span>
        </div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════
   NETWORK SIGNAL — Station health dashboard
   ═══════════════════════════════════════════════ */
function NetworkSignal({ data }: { data: StationHealth[] }) {
  const counts = useMemo(
    () => ({
      active: data.filter((d) => d.status === "active").length,
      stale: data.filter((d) => d.status === "stale").length,
      dormant: data.filter((d) => d.status === "dormant").length,
    }),
    [data],
  );

  const groups = useMemo(() => {
    const typeOrder = ["puc", "birdnetpi", "app"];
    const grouped = new Map<string, StationHealth[]>();
    for (const type of typeOrder) {
      const stations = data.filter((s) => s.type === type);
      if (stations.length) grouped.set(type, stations);
    }
    const known = new Set(typeOrder);
    const other = data.filter((s) => !known.has(s.type));
    if (other.length) grouped.set("other", other);
    return grouped;
  }, [data]);

  const typeLabels: Record<string, string> = {
    puc: "PUC",
    birdnetpi: "BIRDNET-PI",
    app: "APP",
    other: "OTHER",
  };

  function signalBars(quality: number, status: string) {
    const filled = Math.ceil(quality * 5);
    const sc = statusColor(status);
    return (
      <div className="flex items-end gap-[1.5px] h-[13px]">
        {[3, 5, 7, 9, 11].map((h, i) => (
          <div
            key={i}
            className="w-[3px] rounded-t-[1px] transition-all duration-300"
            style={{
              height: `${h}px`,
              background: i < filled ? sc : "rgba(255,255,255,0.06)",
              opacity: i < filled ? 0.8 : 1,
              boxShadow:
                i < filled && i === filled - 1 ? `0 0 4px ${sc}40` : undefined,
            }}
          />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 w-full">
      {/* Summary bar */}
      <div
        className="flex items-center justify-between px-2 py-1.5 rounded"
        style={{
          background: "rgba(255,255,255,0.02)",
          border: "1px solid rgba(255,255,255,0.04)",
        }}
      >
        {(["active", "stale", "dormant"] as const).map((s) => (
          <div key={s} className="flex items-center gap-1.5">
            <div
              className="relative w-2 h-2 rounded-full"
              style={{ background: statusColor(s) }}
            >
              {s === "active" && (
                <div
                  className="absolute inset-0 rounded-full animate-ping opacity-40"
                  style={{ background: statusColor(s) }}
                />
              )}
            </div>
            <span
              className="text-[10px] font-mono font-bold tabular-nums"
              style={{ color: statusColor(s) }}
            >
              {counts[s]}
            </span>
            <span
              className="text-[7px] font-mono tracking-wider uppercase"
              style={{ color: "rgba(255,255,255,0.2)" }}
            >
              {s}
            </span>
          </div>
        ))}
      </div>

      {/* Station list grouped by type */}
      <div className="flex flex-col gap-2">
        {[...groups.entries()].map(([type, stations]) => (
          <div key={type}>
            {/* Type header */}
            <div className="flex items-center gap-1.5 mb-1.5 px-1">
              <div
                className="w-1.5 h-1.5 rounded-full"
                style={{ background: typeColor(type) }}
              />
              <span
                className="text-[7px] font-mono tracking-[0.15em] uppercase"
                style={{ color: typeColor(type), opacity: 0.6 }}
              >
                {typeLabels[type] || type.toUpperCase()}
              </span>
              <div
                className="flex-1 h-[1px]"
                style={{ background: `${typeColor(type)}15` }}
              />
            </div>

            {/* Stations */}
            <div className="flex flex-col gap-[2px]">
              {stations.map((st) => {
                const sc = statusColor(st.status);
                return (
                  <div
                    key={st.id}
                    className="flex items-center gap-2 py-1.5 px-2 rounded transition-all duration-200 hover:bg-white/[0.02]"
                  >
                    {/* Status dot */}
                    <div
                      className="w-1.5 h-1.5 rounded-full shrink-0"
                      style={{
                        background: sc,
                        boxShadow:
                          st.status === "active"
                            ? `0 0 4px ${sc}60`
                            : undefined,
                      }}
                    />

                    {/* Name + availability */}
                    <div className="flex min-w-0 flex-1 flex-col">
                      <span
                        className="text-[9px] font-mono truncate"
                        style={{
                          color:
                            st.status === "dormant"
                              ? "rgba(255,255,255,0.25)"
                              : "rgba(255,255,255,0.55)",
                        }}
                      >
                        {st.name}
                      </span>
                      <span
                        className="text-[7px] font-mono uppercase tracking-[0.14em]"
                        style={{ color: "rgba(255,255,255,0.28)" }}
                      >
                        90d {Math.round(st.availability90dPct ?? st.quality * 100)}% · life {Math.round(st.lifetimeAvailabilityPct ?? st.quality * 100)}%
                      </span>
                    </div>

                    {/* Detection count */}
                    <span
                      className="text-[7px] font-mono tabular-nums shrink-0"
                      style={{ color: `rgba(${TC_RGB},0.25)` }}
                    >
                      {formatNum(st.detections)}
                    </span>

                    {/* Signal bars */}
                    {signalBars(st.quality, st.status)}

                    {/* Quality % */}
                    <span
                      className="text-[8px] font-mono tabular-nums w-[26px] text-right shrink-0"
                      style={{ color: `${sc}90` }}
                    >
                      {Math.round(st.quality * 100)}%
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Network coverage bar */}
      <div className="px-1 mt-1">
        <div
          className="text-[7px] font-mono tracking-wider uppercase mb-1"
          style={{ color: `rgba(${TC_RGB},0.2)` }}
        >
          NETWORK COVERAGE
        </div>
        <div
          className="h-[6px] rounded-full overflow-hidden flex"
          style={{ background: "rgba(255,255,255,0.03)" }}
        >
          {counts.active > 0 && (
            <div
              className="h-full"
              style={{
                width: `${(counts.active / data.length) * 100}%`,
                background: `rgba(${TC_RGB},0.5)`,
              }}
            />
          )}
          {counts.stale > 0 && (
            <div
              className="h-full"
              style={{
                width: `${(counts.stale / data.length) * 100}%`,
                background: "rgba(251,191,36,0.4)",
              }}
            />
          )}
          {counts.dormant > 0 && (
            <div
              className="h-full"
              style={{
                width: `${(counts.dormant / data.length) * 100}%`,
                background: "rgba(220,38,38,0.35)",
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════
   MAIN PANEL
   ═══════════════════════════════════════════════ */
export function BiosurveillancePanel() {
  const [tab, setTab] = useState<Tab>("radar");
  const [speciesData, setSpeciesData] = useState<SpeciesDay[]>(DEMO_SPECIES);
  const [mortalityData, setMortalityData] =
    useState<MortalityRow[]>(DEMO_MORTALITY);
  const [networkData, setNetworkData] =
    useState<StationHealth[]>(DEMO_STATIONS);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const [sp, mo, ne] = await Promise.all([
        fetch("/api/species-activity")
          .then((r) => (r.ok ? r.json() : []))
          .catch(() => []),
        fetch("/api/mortality")
          .then((r) => (r.ok ? r.json() : []))
          .catch(() => []),
        fetch("/api/network-health")
          .then((r) => (r.ok ? r.json() : []))
          .catch(() => []),
      ]);
      if (cancelled) return;
      if (sp.length) setSpeciesData(sp);
      if (mo.length) setMortalityData(mo);
      if (ne.length) setNetworkData(ne);
      setLoaded(true);
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  /* Shared thumbnail map built from species data — used by mortality tab too */
  const thumbnailMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of speciesData) {
      if (row.thumbnail && !map.has(row.species))
        map.set(row.species, row.thumbnail);
    }
    return map;
  }, [speciesData]);

  const uniqueSpecies = useMemo(
    () => new Set(speciesData.map((d) => d.species)).size,
    [speciesData],
  );
  const totalDetections = useMemo(
    () => speciesData.reduce((s, d) => s + d.detections, 0),
    [speciesData],
  );
  const activeStations = useMemo(
    () => networkData.filter((d) => d.status === "active").length,
    [networkData],
  );
  const totalStations = networkData.length;

  const tabs: { key: Tab; label: string }[] = [
    { key: "radar", label: "RADAR" },
    { key: "mortality", label: "MORTALITY" },
    { key: "net", label: "NET" },
  ];

  return (
    <div
      className="relative flex flex-col h-full w-full"
      style={{ background: "rgba(17,17,17,0.85)" }}
    >
      {/* Scanline overlay */}
      <div
        className="pointer-events-none absolute inset-0 z-10 rounded-2xl"
        style={{
          backgroundImage:
            "repeating-linear-gradient(0deg, transparent, transparent 3px, rgba(0,0,0,0.03) 3px, rgba(0,0,0,0.03) 4px)",
        }}
      />

      {/* HUD corner brackets */}
      <div className="pointer-events-none absolute inset-0 z-10">
        <div
          className="absolute top-2.5 left-2.5 w-3.5 h-3.5"
          style={{
            borderTop: `1.5px solid rgba(${TC_RGB},0.18)`,
            borderLeft: `1.5px solid rgba(${TC_RGB},0.18)`,
          }}
        />
        <div
          className="absolute top-2.5 right-2.5 w-3.5 h-3.5"
          style={{
            borderTop: `1.5px solid rgba(${TC_RGB},0.18)`,
            borderRight: `1.5px solid rgba(${TC_RGB},0.18)`,
          }}
        />
        <div
          className="absolute bottom-2.5 left-2.5 w-3.5 h-3.5"
          style={{
            borderBottom: `1.5px solid rgba(${TC_RGB},0.18)`,
            borderLeft: `1.5px solid rgba(${TC_RGB},0.18)`,
          }}
        />
        <div
          className="absolute bottom-2.5 right-2.5 w-3.5 h-3.5"
          style={{
            borderBottom: `1.5px solid rgba(${TC_RGB},0.18)`,
            borderRight: `1.5px solid rgba(${TC_RGB},0.18)`,
          }}
        />
      </div>

      {/* Header */}
      <div className="px-5 pt-4 pb-3 shrink-0">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="relative flex items-center justify-center w-2 h-2">
              <div
                className="absolute inset-0 rounded-full animate-ping opacity-75"
                style={{ background: `rgba(${TC_RGB},0.8)` }}
              />
              <div
                className="relative w-1.5 h-1.5 rounded-full"
                style={{ background: TC }}
              />
            </div>
            <h2 className="text-[11px] font-semibold tracking-[0.2em] uppercase text-brand-300">
              Biosurveillance
            </h2>
          </div>
          {!loaded && (
            <div
              className="text-[8px] font-mono tracking-wider"
              style={{ color: `rgba(${TC_RGB},0.3)` }}
            >
              SYNC&hellip;
            </div>
          )}
        </div>
      </div>

      {/* KPI strip */}
      <div className="shrink-0 flex items-center gap-1 px-4 pb-3">
        {[
          { label: "SPECIES", value: uniqueSpecies, color: TC },
          { label: "DET-30D", value: formatNum(totalDetections), color: TC },
          {
            label: "NODES",
            value: `${activeStations}/${totalStations}`,
            color: activeStations === totalStations ? TC : "#fbbf24",
          },
        ].map((kpi) => (
          <div
            key={kpi.label}
            className="flex-1 flex flex-col items-center py-1.5 rounded"
            style={{
              background: "rgba(255,255,255,0.02)",
              border: "1px solid rgba(255,255,255,0.04)",
            }}
          >
            <span
              className="text-[13px] font-mono font-bold tabular-nums"
              style={{ color: kpi.color }}
            >
              {kpi.value}
            </span>
            <span
              className="text-[6px] font-mono tracking-[0.18em] uppercase mt-0.5"
              style={{ color: `rgba(${TC_RGB},0.3)` }}
            >
              {kpi.label}
            </span>
          </div>
        ))}
      </div>

      {/* Tab bar */}
      <div className="shrink-0 flex items-center px-4 gap-0.5">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className="flex-1 text-center py-1.5 text-[8px] font-mono tracking-[0.18em] uppercase rounded-t transition-all duration-200"
            style={{
              color: tab === t.key ? TC : `rgba(${TC_RGB},0.3)`,
              background:
                tab === t.key ? `rgba(${TC_RGB},0.06)` : "transparent",
              borderBottom: `1.5px solid ${tab === t.key ? `rgba(${TC_RGB},0.3)` : "transparent"}`,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Scan line */}
      <div className="shrink-0 h-[1px] overflow-hidden">
        <div className="h-full w-full bio-scan-line" />
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto custom-scrollbar-panel p-4">
        {tab === "radar" && <SpeciesRadar data={speciesData} />}
        {tab === "mortality" && (
          <MortalityHeat data={mortalityData} thumbnailMap={thumbnailMap} />
        )}
        {tab === "net" && <NetworkSignal data={networkData} />}
      </div>

      {/* Footer */}
      <div
        className="shrink-0 h-[1px]"
        style={{ background: "rgba(255,255,255,0.04)" }}
      />
      <div className="shrink-0 flex items-center justify-center px-4 py-3">
        <span
          className="text-[8px] font-mono tracking-[0.15em] uppercase"
          style={{ color: `rgba(${TC_RGB},0.25)` }}
        >
          TARGET &middot; {uniqueSpecies} TRACKED SPECIES
        </span>
      </div>

      <style
        dangerouslySetInnerHTML={{
          __html: `
                .custom-scrollbar-panel::-webkit-scrollbar { width: 3px; }
                .custom-scrollbar-panel::-webkit-scrollbar-track { background: transparent; }
                .custom-scrollbar-panel::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.05); border-radius: 2px; }
                .custom-scrollbar-panel:hover::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.09); }

                @keyframes bio-scan {
                    0% { transform: translateX(-100%); }
                    100% { transform: translateX(100%); }
                }
                .bio-scan-line {
                    background: linear-gradient(90deg, transparent, rgba(${TC_RGB},0.35), transparent);
                    animation: bio-scan 3s ease-in-out infinite;
                }
            `,
        }}
      />
    </div>
  );
}
