'use client';

import { useEffect, useState } from 'react';
import {
    CheckSquare, Clock, Target, GitCompareArrows,
    Fingerprint, ShieldCheck, AlertTriangle,
    ChevronDown, CircleAlert, CircleCheck, Layers, Activity,
} from 'lucide-react';

/* ══════════════════════════════════════════════════════════════
   Public types (imported by page.tsx)
   ══════════════════════════════════════════════════════════════ */

export type QualityDashboardData = {
    compositeScore: number;
    dimensions: {
        key: string;
        name: string;
        score: number;
        description: string;
    }[];
    heatmap: {
        table: string;
        shortName: string;
        dimension: string;
        score: number;
        metricCount: number;
    }[];
    issues: {
        table: string;
        shortName: string;
        dimension: string;
        dimName: string;
        metric: string;
        status: string;
        value: number;
        detail: string | null;
        score: number;
    }[];
    stationStats: {
        source: string;
        count: number;
        avgCompleteness: number;
        avgConfidence: number | null;
        avgFreshnessDays: number;
        avgSpecies: number;
    }[];
    sourceCards: {
        table: string;
        shortName: string;
        avgScore: number;
        totalRows: number | null;
        freshnessHours: number | null;
        issueCount: number;
        dimensions: { key: string; score: number }[];
    }[];
    metricBreakdown: { pass: number; warn: number; fail: number; info: number };
};

/* ── Colour system — neutral grays + subtle accents ───────── */

function scoreColor(s: number): string {
    if (s >= 90) return '#6ee7b7';   // emerald-300
    if (s >= 75) return '#a3a3a3';   // neutral-400
    if (s >= 60) return '#fbbf24';   // amber-400
    if (s >= 40) return '#fb923c';   // orange-400
    return '#f87171';                // red-400
}

function scoreLabel(s: number): string {
    if (s >= 90) return 'Excellent';
    if (s >= 75) return 'Good';
    if (s >= 60) return 'Fair';
    if (s >= 40) return 'Attention';
    return 'Critical';
}

function scoreBg(s: number, opacity = 0.08): string {
    return `${scoreColor(s)}${Math.round(opacity * 255).toString(16).padStart(2, '0')}`;
}

/* ══════════════════════════════════════════════════════════════
   Panel — minimal card wrapper
   ══════════════════════════════════════════════════════════════ */

function Panel({ label, children, className = '' }: {
    label?: string;
    children: React.ReactNode;
    className?: string;
}) {
    return (
        <section className={`rounded-lg bg-[#1a1a1a] border border-neutral-800 ${className}`}>
            {label && (
                <div className="px-5 pt-4 pb-2">
                    <span className="text-[10px] font-medium text-neutral-500 uppercase tracking-widest">
                        {label}
                    </span>
                </div>
            )}
            <div className="px-5 pb-5">{children}</div>
        </section>
    );
}

/* ══════════════════════════════════════════════════════════════
   SCORE GAUGE — clean ring
   ══════════════════════════════════════════════════════════════ */

function ScoreGauge({ score }: { score: number }) {
    const [mounted, setMounted] = useState(false);
    useEffect(() => {
        const t = setTimeout(() => setMounted(true), 100);
        return () => clearTimeout(t);
    }, []);

    const size = 180;
    const cx = size / 2;
    const cy = size / 2;
    const r = 68;
    const C = 2 * Math.PI * r;
    const offset = C * (1 - (mounted ? Math.min(score, 100) : 0) / 100);
    const color = scoreColor(score);
    const label = scoreLabel(score);

    return (
        <div className="flex flex-col items-center">
            <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
                <circle cx={cx} cy={cy} r={r} fill="none"
                        stroke="rgba(255,255,255,0.04)" strokeWidth="6" />
                <circle cx={cx} cy={cy} r={r} fill="none"
                        stroke={color} strokeWidth="6" strokeLinecap="round"
                        strokeDasharray={C} strokeDashoffset={offset}
                        transform={`rotate(-90 ${cx} ${cy})`}
                        style={{
                            transition: 'stroke-dashoffset 1.4s cubic-bezier(0.4,0,0.2,1)',
                            opacity: 0.75,
                        }} />
                <text x={cx} y={cy - 6} textAnchor="middle" dominantBaseline="middle"
                      fill="#d4d4d4" fontSize="36" fontWeight="700"
                      fontFamily="system-ui, -apple-system, sans-serif"
                      style={{ opacity: mounted ? 1 : 0, transition: 'opacity 0.5s 0.3s' }}>
                    {score}
                </text>
                <text x={cx} y={cy + 20} textAnchor="middle" dominantBaseline="middle"
                      fill="#525252" fontSize="11" fontFamily="system-ui" letterSpacing="1">
                    / 100
                </text>
            </svg>
            <span className="text-[10px] font-medium mt-0.5" style={{ color }}>
                {label}
            </span>
        </div>
    );
}

/* ══════════════════════════════════════════════════════════════
   SYSTEM HEALTH — score gauge + metric breakdown
   ══════════════════════════════════════════════════════════════ */

function SystemHealth({ data }: { data: QualityDashboardData }) {
    const { pass, warn, fail } = data.metricBreakdown;
    const total = pass + warn + fail;

    return (
        <div className="flex flex-col items-center pt-2 pb-1">
            <ScoreGauge score={data.compositeScore} />
            <div className="flex items-center gap-4 mt-4">
                {fail > 0 && (
                    <span className="inline-flex items-center gap-1.5 text-[10px] font-medium text-red-400">
                        <CircleAlert size={11} /> {fail} Fail
                    </span>
                )}
                {warn > 0 && (
                    <span className="inline-flex items-center gap-1.5 text-[10px] font-medium text-amber-400">
                        <AlertTriangle size={11} /> {warn} Warn
                    </span>
                )}
                <span className="inline-flex items-center gap-1.5 text-[10px] font-medium text-neutral-500">
                    <CircleCheck size={11} /> {pass} Pass
                </span>
            </div>
            {total > 0 && (
                <div className="flex h-1 w-36 mt-3 overflow-hidden rounded-full bg-neutral-800">
                    {pass > 0 && <div className="h-full" style={{ width: `${(pass / total) * 100}%`, background: '#6ee7b7' }} />}
                    {warn > 0 && <div className="h-full" style={{ width: `${(warn / total) * 100}%`, background: '#fbbf24' }} />}
                    {fail > 0 && <div className="h-full" style={{ width: `${(fail / total) * 100}%`, background: '#f87171' }} />}
                </div>
            )}
        </div>
    );
}

/* ══════════════════════════════════════════════════════════════
   SPIDER CHART — hexagonal radar for dimension analysis
   ══════════════════════════════════════════════════════════════ */

function SpiderChart({ dimensions }: { dimensions: QualityDashboardData['dimensions'] }) {
    const w = 320;
    const h = 280;
    const cx = w / 2;
    const cy = 135;
    const maxR = 90;
    const n = dimensions.length;

    const angleFor = (i: number) => (Math.PI * 2 * i) / n - Math.PI / 2;
    const pointAt = (i: number, value: number) => {
        const angle = angleFor(i);
        const r = (value / 100) * maxR;
        return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
    };

    const gridLevels = [25, 50, 75, 100];
    const polygonPts = (level: number) =>
        Array.from({ length: n }, (_, i) => pointAt(i, level))
            .map(p => `${p.x},${p.y}`)
            .join(' ');

    const dataPoints = dimensions.map((d, i) => pointAt(i, d.score));
    const dataPoly = dataPoints.map(p => `${p.x},${p.y}`).join(' ');

    return (
        <div className="flex justify-center py-2">
            <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
                {gridLevels.map(level => (
                    <polygon key={level} points={polygonPts(level)} fill="none"
                             stroke={level === 100 ? 'rgba(163,163,163,0.12)' : 'rgba(163,163,163,0.05)'}
                             strokeWidth="1" />
                ))}

                {dimensions.map((_, i) => {
                    const outer = pointAt(i, 100);
                    return (
                        <line key={i} x1={cx} y1={cy} x2={outer.x} y2={outer.y}
                              stroke="rgba(163,163,163,0.06)" strokeWidth="1" />
                    );
                })}

                <polygon points={dataPoly}
                         fill="rgba(163,163,163,0.06)"
                         stroke="rgba(163,163,163,0.35)"
                         strokeWidth="1.5"
                         strokeLinejoin="round" />

                {dataPoints.map((p, i) => (
                    <circle key={i} cx={p.x} cy={p.y} r="3.5"
                            fill={scoreColor(dimensions[i].score)}
                            stroke="#1a1a1a" strokeWidth="1.5" />
                ))}

                {dimensions.map((dim, i) => {
                    const labelR = maxR + 28;
                    const angle = angleFor(i);
                    const lx = cx + labelR * Math.cos(angle);
                    const ly = cy + labelR * Math.sin(angle);
                    const cosA = Math.cos(angle);
                    const anchor = Math.abs(cosA) < 0.3 ? 'middle' : cosA > 0 ? 'start' : 'end';

                    return (
                        <g key={dim.key}>
                            <text x={lx} y={ly - 5}
                                  textAnchor={anchor} dominantBaseline="middle"
                                  fill="#737373" fontSize="8.5" fontFamily="system-ui"
                                  fontWeight="500" letterSpacing="0.3">
                                {dim.name}
                            </text>
                            <text x={lx} y={ly + 8}
                                  textAnchor={anchor} dominantBaseline="middle"
                                  fill={scoreColor(dim.score)} fontSize="12"
                                  fontFamily="system-ui" fontWeight="700">
                                {dim.score}
                            </text>
                        </g>
                    );
                })}
            </svg>
        </div>
    );
}

/* ══════════════════════════════════════════════════════════════
   SOURCE x DIMENSION HEATMAP
   ══════════════════════════════════════════════════════════════ */

function SourceMatrix({ heatmap, dimensions }: {
    heatmap: QualityDashboardData['heatmap'];
    dimensions: QualityDashboardData['dimensions'];
}) {
    const dimKeys = dimensions.map((d) => d.key);
    const tables = [...new Map(heatmap.map((h) => [h.table, h.shortName]))];

    const cell = (table: string, dim: string) =>
        heatmap.find((h) => h.table === table && h.dimension === dim);

    const rowAvg = (table: string) => {
        const cells = dimKeys.map((d) => cell(table, d)).filter(Boolean);
        return cells.length ? Math.round(cells.reduce((s, c) => s + c!.score, 0) / cells.length) : null;
    };

    const colAvg = (dim: string) => {
        const cells = tables.map(([t]) => cell(t, dim)).filter(Boolean);
        return cells.length ? Math.round(cells.reduce((s, c) => s + c!.score, 0) / cells.length) : null;
    };

    const sortedTables = [...tables].sort((a, b) => (rowAvg(a[0]) ?? 0) - (rowAvg(b[0]) ?? 0));

    return (
        <div className="overflow-x-auto">
            <table className="w-full" style={{ borderCollapse: 'separate', borderSpacing: '2px 2px' }}>
                <thead>
                    <tr>
                        <th className="text-left px-3 py-2.5 text-[9px] font-medium text-neutral-500
                                       uppercase tracking-widest">
                            Source
                        </th>
                        {dimensions.map((d) => (
                            <th key={d.key}
                                className="px-2 py-2.5 text-center text-[9px] font-medium
                                           text-neutral-500 uppercase tracking-widest whitespace-nowrap">
                                {d.name}
                            </th>
                        ))}
                        <th className="px-3 py-2.5 text-center text-[9px] font-medium
                                       text-neutral-500 uppercase tracking-widest">
                            AVG
                        </th>
                    </tr>
                </thead>
                <tbody>
                    {sortedTables.map(([table, shortName]) => {
                        const avg = rowAvg(table);
                        return (
                            <tr key={table} className="group">
                                <td className="px-3 py-3 text-[11px] font-medium text-neutral-400
                                               whitespace-nowrap rounded group-hover:bg-white/[0.02] transition-colors">
                                    {shortName}
                                </td>
                                {dimKeys.map((dim) => {
                                    const c = cell(table, dim);
                                    if (!c) return (
                                        <td key={dim}
                                            className="px-2 py-3 text-center text-[10px] text-neutral-700 rounded">
                                            —
                                        </td>
                                    );
                                    const sc = scoreColor(c.score);
                                    return (
                                        <td key={dim}
                                            className="px-2 py-3 text-center rounded text-[11px] font-semibold cursor-default"
                                            style={{
                                                background: scoreBg(c.score, 0.08),
                                                color: sc,
                                            }}
                                            title={`${c.metricCount} metrics`}>
                                            {c.score}
                                        </td>
                                    );
                                })}
                                <td className="px-3 py-3 text-center rounded text-[11px] font-bold"
                                    style={{
                                        background: avg != null ? 'rgba(255,255,255,0.02)' : undefined,
                                        color: avg != null ? scoreColor(avg) : 'rgba(255,255,255,0.08)',
                                    }}>
                                    {avg ?? '—'}
                                </td>
                            </tr>
                        );
                    })}

                    <tr>
                        <td className="px-3 pt-3 pb-2 text-[9px] font-medium text-neutral-600
                                       uppercase tracking-wider">
                            Baseline
                        </td>
                        {dimKeys.map((dim) => {
                            const avg = colAvg(dim);
                            return (
                                <td key={dim}
                                    className="px-2 pt-3 pb-2 text-center text-[10px]
                                               font-medium text-neutral-600">
                                    {avg ?? '—'}
                                </td>
                            );
                        })}
                        <td />
                    </tr>
                </tbody>
            </table>
        </div>
    );
}

/* ══════════════════════════════════════════════════════════════
   ALERT LOG
   ══════════════════════════════════════════════════════════════ */

function AlertLog({ issues }: { issues: QualityDashboardData['issues'] }) {
    const [expanded, setExpanded] = useState(false);
    const fails = issues.filter((i) => i.status === 'FAIL');
    const warns = issues.filter((i) => i.status === 'WARN');
    const visible = expanded ? issues : issues.slice(0, 8);

    if (issues.length === 0) {
        return (
            <div className="flex items-center gap-3 py-3 px-4 rounded border border-neutral-800 bg-neutral-800/30">
                <CircleCheck size={14} className="text-emerald-400" />
                <span className="text-[11px] text-neutral-400">
                    All checks passed — no issues detected
                </span>
            </div>
        );
    }

    return (
        <div>
            <div className="flex items-center gap-3 mb-3">
                {fails.length > 0 && (
                    <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded
                                     text-[10px] font-semibold bg-red-500/10
                                     text-red-400 border border-red-500/10">
                        <span className="w-1.5 h-1.5 rounded-full bg-red-400" />
                        {fails.length} Critical
                    </span>
                )}
                {warns.length > 0 && (
                    <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded
                                     text-[10px] font-semibold bg-amber-500/10
                                     text-amber-400 border border-amber-500/10">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                        {warns.length} Warning
                    </span>
                )}
            </div>

            <div className="flex flex-col gap-1">
                {visible.map((issue, i) => {
                    const isFail = issue.status === 'FAIL';
                    const accent = isFail ? '#f87171' : '#fbbf24';
                    return (
                        <div key={i}
                             className="flex items-start gap-2 px-3 py-2 rounded
                                        transition-colors hover:bg-white/[0.015]"
                             style={{
                                 borderLeft: `2px solid ${accent}`,
                                 background: `${accent}05`,
                             }}>
                            <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className="text-[9px] font-semibold px-1 py-px rounded"
                                          style={{ color: accent, background: `${accent}15` }}>
                                        {issue.status}
                                    </span>
                                    <span className="text-[10px] text-neutral-300 font-medium">
                                        {issue.shortName}
                                    </span>
                                    <span className="text-[9px] text-neutral-600 uppercase">
                                        {issue.dimName}
                                    </span>
                                </div>
                                <p className="text-[10px] text-neutral-500 mt-0.5 truncate">
                                    <span className="text-neutral-400">{issue.metric}</span>
                                    {': '}
                                    <span style={{ color: accent }}>
                                        {Number(issue.value).toFixed(1)}
                                    </span>
                                    {issue.detail && (
                                        <span className="text-neutral-600 ml-1">
                                            — {issue.detail.length > 60
                                                ? issue.detail.slice(0, 60) + '…'
                                                : issue.detail}
                                        </span>
                                    )}
                                </p>
                            </div>
                        </div>
                    );
                })}
            </div>

            {issues.length > 8 && (
                <button onClick={() => setExpanded(!expanded)}
                        className="mt-2 flex items-center gap-1.5 text-[10px] font-medium
                                   text-neutral-500 hover:text-neutral-300 transition-colors">
                    <ChevronDown size={11}
                                 style={{
                                     transform: expanded ? 'rotate(180deg)' : undefined,
                                     transition: 'transform 0.2s',
                                 }} />
                    {expanded ? 'Show fewer' : `Show all ${issues.length}`}
                </button>
            )}
        </div>
    );
}

/* ══════════════════════════════════════════════════════════════
   MAIN DASHBOARD LAYOUT
   ——————————————————————————————————————————————————————————————
   Row 1: System Health | Spider Chart | Heatmap
   Row 2: Alert Log (full width)
   ══════════════════════════════════════════════════════════════ */

export default function QualityDashboard({ data }: { data: QualityDashboardData }) {
    return (
        <div className="flex flex-col gap-6">
            {/* Row 1 — three equal panels */}
            <div className="grid grid-cols-1 lg:grid-cols-[1fr_1.2fr_1.6fr] gap-5">
                <Panel label="System Health">
                    <SystemHealth data={data} />
                </Panel>

                <Panel label="Dimension Analysis">
                    <SpiderChart dimensions={data.dimensions} />
                </Panel>

                <Panel label="Source × Dimension">
                    <SourceMatrix heatmap={data.heatmap} dimensions={data.dimensions} />
                </Panel>
            </div>

            {/* Row 2 — Alert log */}
            <Panel label="Alert Log">
                <AlertLog issues={data.issues} />
            </Panel>
        </div>
    );
}
