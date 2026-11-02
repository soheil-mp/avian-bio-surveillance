'use client';

import { Fragment, useState, useEffect, useRef, useCallback } from 'react';
import { ChevronDown } from 'lucide-react';
import { TABLE_DEFS, type TableDef } from './tables';
import { fetchTableDetail, type TableDetail } from './actions';
import DataTable from './DataTable';
import SchemaSection from './SchemaSection';
import type { TableInventory, DimensionScore, QualityMetricRow } from './page';

/* ── helpers ──────────────────────────────────────────────── */

function fmtNum(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toLocaleString();
}

function fmtBytes(b: number): string {
    if (b >= 1_073_741_824) return `${(b / 1_073_741_824).toFixed(1)} GB`;
    if (b >= 1_048_576) return `${(b / 1_048_576).toFixed(1)} MB`;
    if (b >= 1_024) return `${(b / 1_024).toFixed(1)} KB`;
    return `${b} B`;
}

function fmtDate(d: string | Date | null): string {
    if (!d) return '—';
    return new Date(d).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function timeAgo(d: string | Date | null): string {
    if (!d) return 'Never';
    const ms = Date.now() - new Date(d).getTime();
    const m = Math.floor(ms / 60_000);
    if (m < 1) return 'Just now';
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const days = Math.floor(h / 24);
    if (days < 30) return `${days}d ago`;
    return `${Math.floor(days / 30)}mo ago`;
}

function freshnessColor(d: string | null): string {
    if (!d) return 'rgba(255,255,255,0.15)';
    const h = (Date.now() - new Date(d).getTime()) / 3_600_000;
    if (h < 2) return '#34d399';
    if (h < 48) return '#fbbf24';
    return '#f87171';
}

function scoreColor(score: number): string {
    return score >= 80 ? '#34d399' : score >= 50 ? '#fbbf24' : '#f87171';
}

function fmtVal(v: number): string {
    if (v % 1 === 0) return v.toLocaleString();
    return v < 1 ? v.toFixed(4) : v.toFixed(2);
}

/* ── registry ─────────────────────────────────────────────── */

type Props = { tables: TableInventory[]; pageSize: number };

export default function InventoryRegistry({ tables, pageSize }: Props) {
    const [expandedKey, setExpandedKey] = useState<string | null>(null);
    const detailCache = useRef<Record<string, TableDetail>>({});
    const [detail, setDetail] = useState<TableDetail | null>(null);
    const [loadingDetail, setLoadingDetail] = useState(false);

    const handleToggle = useCallback((key: string) => {
        if (expandedKey === key) { setExpandedKey(null); return; }
        setExpandedKey(key);
        const cached = detailCache.current[key];
        if (cached) { setDetail(cached); setLoadingDetail(false); }
        else { setDetail(null); setLoadingDetail(true); }
    }, [expandedKey]);

    useEffect(() => {
        if (!expandedKey || detailCache.current[expandedKey]) return;
        let cancelled = false;
        fetchTableDetail(expandedKey, pageSize).then((d) => {
            if (cancelled) return;
            detailCache.current[expandedKey] = d;
            setDetail(d);
            setLoadingDetail(false);
        }).catch(() => { if (!cancelled) setLoadingDetail(false); });
        return () => { cancelled = true; };
    }, [expandedKey, pageSize]);

    const colTpl = 'minmax(160px,1.2fr) 80px 90px 90px 110px 100px 32px';

    return (
        <div className="rounded-lg overflow-hidden bg-[#1a1a1a] border border-neutral-800">
            <div className="overflow-x-auto">
                <div style={{ minWidth: '720px' }}>
                    {/* ── header ──────────────────── */}
                    <div
                        className="grid items-center gap-3 px-4 py-2.5 text-[9px] font-medium uppercase tracking-widest"
                        style={{
                            gridTemplateColumns: colTpl,
                            borderBottom: '1px solid rgba(255,255,255,0.06)',
                            color: 'rgba(255,255,255,0.3)',
                        }}
                    >
                        <span>Source</span>
                        <span>Type</span>
                        <span className="text-right">Records</span>
                        <span className="text-right">Size</span>
                        <span>Freshness</span>
                        <span className="text-right">Quality</span>
                        <span />
                    </div>

                    {/* ── rows ──────────────────────────── */}
                    {tables.map((t) => {
                        const def = TABLE_DEFS.find((d) => d.key === t.key)!;
                        const isExpanded = expandedKey === t.key;
                        const Icon = def.icon;

                        return (
                            <Fragment key={t.key}>
                                {/* row */}
                                <div
                                    role="button"
                                    tabIndex={0}
                                    onClick={() => handleToggle(t.key)}
                                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') handleToggle(t.key); }}
                                    className="grid items-center gap-3 px-4 py-3 cursor-pointer transition-colors hover:bg-white/[0.02]"
                                    style={{
                                        gridTemplateColumns: colTpl,
                                        borderBottom: isExpanded ? 'none' : '1px solid rgba(255,255,255,0.04)',
                                        background: isExpanded ? 'rgba(255,255,255,0.015)' : undefined,
                                    }}
                                >
                                    {/* source */}
                                    <div className="flex items-center gap-2.5 min-w-0">
                                        <div
                                            className="w-7 h-7 rounded-md flex items-center justify-center shrink-0 bg-neutral-800/60 border border-neutral-700/40"
                                        >
                                            <Icon size={13} style={{ color: '#737373' }} />
                                        </div>
                                        <div className="flex flex-col min-w-0">
                                            <span className="text-[12px] font-semibold text-neutral-200 truncate">{def.name}</span>
                                            <span className="text-[9px] text-neutral-700 truncate">{t.key}</span>
                                        </div>
                                    </div>

                                    {/* type */}
                                    <span
                                        className="text-[9px] px-1.5 py-0.5 rounded w-fit"
                                        style={{
                                            background: def.type === 'Hypertable' ? 'rgba(52,211,153,0.08)' : 'rgba(255,255,255,0.04)',
                                            color: def.type === 'Hypertable' ? 'rgba(52,211,153,0.7)' : 'rgba(255,255,255,0.35)',
                                        }}
                                    >
                                        {def.type}
                                    </span>

                                    {/* records */}
                                    <span className="text-[12px] tabular-nums text-right text-neutral-400">
                                        {fmtNum(t.rows)}
                                    </span>

                                    {/* size */}
                                    <span className="text-[11px] tabular-nums text-right text-neutral-500">
                                        {fmtBytes(t.sizeBytes)}
                                    </span>

                                    {/* freshness */}
                                    <div className="flex items-center gap-2">
                                        <div
                                            className="w-[6px] h-[6px] rounded-full shrink-0"
                                            style={{ background: freshnessColor(t.freshness) }}
                                        />
                                        <span className="text-[11px] text-neutral-500">
                                            {timeAgo(t.freshness)}
                                        </span>
                                    </div>

                                    {/* quality */}
                                    <div className="flex items-center gap-2 justify-end">
                                        {t.quality ? (
                                            <>
                                                <QualityMiniBar score={t.quality.compositeScore} />
                                                <span className="text-[12px] font-bold tabular-nums" style={{ color: scoreColor(t.quality.compositeScore) }}>
                                                    {t.quality.compositeScore.toFixed(0)}
                                                </span>
                                            </>
                                        ) : (
                                            <span className="text-[11px] text-neutral-700">—</span>
                                        )}
                                    </div>

                                    {/* chevron */}
                                    <div className="flex justify-center">
                                        <ChevronDown
                                            size={12}
                                            className={`transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`}
                                            style={{ color: 'rgba(255,255,255,0.2)' }}
                                        />
                                    </div>
                                </div>

                                {/* ── expanded detail ──────── */}
                                {isExpanded && (
                                    <div
                                        style={{
                                            background: 'rgba(255,255,255,0.01)',
                                            borderBottom: '1px solid rgba(255,255,255,0.06)',
                                            borderLeft: '2px solid rgba(163,163,163,0.15)',
                                        }}
                                    >
                                        {loadingDetail && !detail ? (
                                            <div className="px-6 py-8 flex items-center justify-center">
                                                <div className="flex items-center gap-3 text-neutral-500">
                                                    <div className="w-4 h-4 border-2 border-neutral-600 border-t-neutral-400 rounded-full animate-spin" />
                                                    <span className="text-[11px]">Loading table details…</span>
                                                </div>
                                            </div>
                                        ) : detail ? (
                                            <ExpandedDetail table={t} detail={detail} def={def} pageSize={pageSize} />
                                        ) : null}
                                    </div>
                                )}
                            </Fragment>
                        );
                    })}
                </div>
            </div>
        </div>
    );
}

/* ── expanded detail panel ─────────────────────────────────── */

function ExpandedDetail({ table: t, detail, def, pageSize }: { table: TableInventory; detail: TableDetail; def: TableDef; pageSize: number }) {
    return (
        <div className="px-6 py-5 space-y-6">
            {/* ── Overview ──────────────────────────────────────── */}
            <section>
                <SectionHeader label="Overview" />
                <div className="flex items-center gap-8 flex-wrap mt-3">
                    <Stat label="Rows" value={fmtNum(t.rows)} />
                    <Stat label="Columns" value={String(t.columns.length)} />
                    <Stat label="Size" value={fmtBytes(t.sizeBytes)} />
                    <Stat label="Date Range" value={`${fmtDate(detail.earliest)} → ${fmtDate(detail.latest)}`} />
                    <Stat label="Last Ingestion" value={timeAgo(detail.lastIngestion)} dotColor={freshnessColor(detail.lastIngestion)} />
                </div>
            </section>

            {/* ── Quality dimensions ──────────────────── */}
            {t.quality && (
                <section>
                    <SectionHeader label="Quality Assessment">
                        <span className="text-[14px] font-bold tabular-nums" style={{ color: scoreColor(t.quality.compositeScore) }}>
                            {t.quality.compositeScore.toFixed(0)}
                            <span className="text-[9px] font-normal text-neutral-600">/100</span>
                        </span>
                    </SectionHeader>
                    <div className="flex flex-wrap gap-4 mt-4">
                        {t.quality.dimensions.map((d) => (
                            <DimensionGauge key={d.dimension} dim={d} />
                        ))}
                    </div>

                    {/* metric details */}
                    <details className="mt-4 group">
                        <summary className="cursor-pointer select-none text-[10px] uppercase tracking-wider flex items-center gap-1.5 list-none text-neutral-600 hover:text-neutral-400 transition-colors">
                            <span className="transition-transform text-[9px] group-open:rotate-90">▶</span>
                            Metric Details ({t.quality.metrics.filter((m) => m.status !== 'INFO').length} checks)
                        </summary>
                        <MetricTable metrics={t.quality.metrics} />
                    </details>
                </section>
            )}

            {/* ── Schema ─────────────────────────────── */}
            <SchemaSection tableKey={t.key} accentRgb={def.accentRgb} columnCount={t.columns.length} />

            {/* ── Data Sample ─────────────────────────── */}
            <section>
                <SectionHeader label="Data Sample" />
                <div className="mt-3">
                    <DataTable
                        tableKey={t.key}
                        accentRgb={def.accentRgb}
                        initialColumns={t.columns}
                        initialRows={detail.initialRows}
                        totalRows={t.rows}
                        pageSize={pageSize}
                    />
                </div>
            </section>
        </div>
    );
}

/* ── sub-components ────────────────────────────────────────── */

function SectionHeader({ label, children }: { label: string; children?: React.ReactNode }) {
    return (
        <div className="flex items-center gap-3">
            <span className="text-[10px] font-medium uppercase tracking-widest shrink-0 text-neutral-500">
                {label}
            </span>
            <div className="flex-1 h-px bg-neutral-800" />
            {children}
        </div>
    );
}

function Stat({ label, value, dotColor }: { label: string; value: string; dotColor?: string }) {
    return (
        <div className="flex flex-col gap-0.5">
            <span className="text-[8px] font-medium uppercase tracking-widest text-neutral-600">{label}</span>
            <div className="flex items-center gap-1.5">
                {dotColor && <div className="w-[6px] h-[6px] rounded-full shrink-0" style={{ background: dotColor }} />}
                <span className="text-[13px] font-semibold text-neutral-300 tabular-nums">{value}</span>
            </div>
        </div>
    );
}

function QualityMiniBar({ score }: { score: number }) {
    const color = scoreColor(score);
    return (
        <div className="w-[40px] h-[4px] rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.06)' }}>
            <div className="h-full rounded-full" style={{ width: `${Math.min(score, 100)}%`, background: color }} />
        </div>
    );
}

function DimensionGauge({ dim }: { dim: DimensionScore }) {
    const size = 64;
    const cx = size / 2;
    const cy = size / 2;
    const r = 24;
    const C = 2 * Math.PI * r;
    const offset = C * (1 - Math.min(dim.score, 100) / 100);
    const color = scoreColor(dim.score);

    return (
        <div className="flex flex-col items-center gap-1">
            <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
                <circle cx={cx} cy={cy} r={r} fill="none"
                        stroke="rgba(255,255,255,0.04)" strokeWidth="4" />
                <circle cx={cx} cy={cy} r={r} fill="none"
                        stroke={color} strokeWidth="4" strokeLinecap="round"
                        strokeDasharray={C} strokeDashoffset={offset}
                        transform={`rotate(-90 ${cx} ${cy})`}
                        style={{ opacity: 0.7 }} />
                <text x={cx} y={cy + 1} textAnchor="middle" dominantBaseline="middle"
                      fill="#d4d4d4" fontSize="13" fontWeight="700"
                      fontFamily="system-ui">
                    {dim.score.toFixed(0)}
                </text>
            </svg>
            <span className="text-[8px] font-medium uppercase tracking-wider text-neutral-500">
                {dim.dimension}
            </span>
        </div>
    );
}

function MetricTable({ metrics }: { metrics: QualityMetricRow[] }) {
    const scored = metrics.filter((m) => m.status !== 'INFO');
    return (
        <div className="overflow-x-auto rounded-md mt-2" style={{ border: '1px solid rgba(255,255,255,0.04)' }}>
            <table className="w-full text-[10px] font-mono border-collapse">
                <thead>
                    <tr style={{ background: 'rgba(255,255,255,0.02)' }}>
                        {['Dimension', 'Metric', 'Value', 'Threshold', 'Status', 'Detail'].map((h) => (
                            <th
                                key={h}
                                className="px-3 py-1.5 text-left font-semibold whitespace-nowrap"
                                style={{ color: 'rgba(255,255,255,0.3)', borderBottom: '1px solid rgba(255,255,255,0.04)' }}
                            >
                                {h}
                            </th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {scored.map((m) => (
                        <tr
                            key={`${m.dimension}-${m.metric}`}
                            className="hover:bg-white/[0.01]"
                            style={{ borderBottom: '1px solid rgba(255,255,255,0.02)' }}
                        >
                            <td className="px-3 py-1.5 whitespace-nowrap" style={{ color: 'rgba(255,255,255,0.3)' }}>{m.dimension}</td>
                            <td className="px-3 py-1.5 whitespace-nowrap" style={{ color: 'rgba(255,255,255,0.5)' }}>{m.metric}</td>
                            <td className="px-3 py-1.5 tabular-nums whitespace-nowrap" style={{ color: 'rgba(255,255,255,0.6)' }}>{fmtVal(m.value)}</td>
                            <td className="px-3 py-1.5 tabular-nums whitespace-nowrap" style={{ color: 'rgba(255,255,255,0.25)' }}>
                                {m.threshold != null ? `≥ ${m.threshold}` : '—'}
                            </td>
                            <td className="px-3 py-1.5"><StatusBadge status={m.status} /></td>
                            <td className="px-3 py-1.5 max-w-[280px] truncate" style={{ color: 'rgba(255,255,255,0.25)' }} title={m.detail ?? ''}>
                                {m.detail ?? ''}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function StatusBadge({ status }: { status: string }) {
    const cfg: Record<string, { bg: string; fg: string }> = {
        PASS: { bg: 'rgba(52,211,153,0.1)', fg: '#34d399' },
        WARN: { bg: 'rgba(251,191,36,0.1)', fg: '#fbbf24' },
        FAIL: { bg: 'rgba(248,113,113,0.12)', fg: '#f87171' },
        INFO: { bg: 'rgba(255,255,255,0.03)', fg: 'rgba(255,255,255,0.3)' },
    };
    const s = cfg[status] ?? cfg.INFO;
    return (
        <span className="text-[8px] font-bold px-1.5 py-0.5 rounded uppercase" style={{ background: s.bg, color: s.fg }}>
            {status}
        </span>
    );
}
