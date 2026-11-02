'use client';

import { useState, useTransition, useEffect, useCallback, useRef } from 'react';
import {
    Play, Loader2, CheckCircle2, AlertTriangle,
    Clock, Calendar, Database, Save, Power,
    ChevronDown, RotateCcw, X, ExternalLink, Timer,
} from 'lucide-react';
import { triggerPipeline, saveSchedule } from './actions';

/* ── types ────────────────────────────────────────────────── */

export type RunInfo = {
    id: string;
    state: string;
    startTime: string | null;
    endTime: string | null;
    duration: number | null;
};

export type PipelineKind = 'daily' | 'catchup' | 'repair' | 'post-processing';

export type PipelineDef = {
    id: string;
    name: string;
    source: string;
    kind: PipelineKind;
    frequency: string;
    time: string;
    enabled: boolean;
    table: string;
    orderCol: string;
    coverageCol: string;
    coverageSpan: 'daily' | 'monthly';
    ingestionCol: string;
    rows: number;
    earliest: string | null;
    latest: string | null;
    lastIngestion: string | null;
    coverage: { date: string; count: number }[];
    runHistory: RunInfo[];
};

type ActionResult = { ok: boolean; message: string; url?: string } | null;

const FREQUENCIES = ['Hourly', 'Daily', 'Weekly', 'Monthly'] as const;
const HOURS = Array.from({ length: 48 }, (_, i) => {
    const h = Math.floor(i / 2);
    const m = (i % 2) * 30;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
});
const BANNER_TTL = 8_000;

/* ── helpers ──────────────────────────────────────────────── */

const EXPECTED: Record<string, number> = {
    /* Daily ingestion — expect fresh data every 24h */
    'birdweather-daily': 24 * 60,
    'knmi-daily': 24 * 60,
    'ebird-daily': 24 * 60,
    'movebank-daily': 24 * 60,
    'sovon-daily': 30 * 24 * 60,
    'seismic-daily': 7 * 24 * 60,
    'earth-engine-grid-daily': 3 * 24 * 60,
    /* Historical repair / catchup — tolerance is generous */
    'birdweather-repair': 24 * 60,
    'knmi-repair': 24 * 60,
    'ebird-repair': 24 * 60,
    'movebank-repair': 24 * 60,
    'earth-engine-grid-repair': 24 * 60,
    'historical-catchup': 24 * 60,
    'cds-drought-catchup': 35 * 24 * 60,
    /* Post-processing */
    'station-summary-refresh': 24 * 60,
    'quality-audit': 24 * 60,
};

function freshnessColor(d: string | null, expectedMins: number): string {
    if (!d) return 'rgba(255,255,255,0.2)';
    const mins = (Date.now() - new Date(d).getTime()) / 60_000;
    if (mins <= expectedMins * 1.5) return '#34d399';
    if (mins <= expectedMins * 3) return '#f59e0b';
    return '#f87171';
}

function freshnessLabel(d: string | null, expectedMins: number): string {
    if (!d) return 'No data';
    const mins = (Date.now() - new Date(d).getTime()) / 60_000;
    if (mins <= expectedMins * 1.5) return 'On time';
    if (mins <= expectedMins * 3) return 'Late';
    return 'Overdue';
}

function fmtDate(d: string | null): string {
    if (!d) return '—';
    return new Date(d).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function fmtFull(d: string | null): string {
    if (!d) return '';
    return new Date(d).toLocaleString('en-GB', {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
    });
}

function fmtNum(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toLocaleString();
}

function timeAgo(d: string | null): string {
    if (!d) return '—';
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
/* ── next cron fire time ────────────────────────────────── */

function nextCronFire(frequency: string, time: string): Date {
    const now = new Date();
    const [hStr, mStr] = time.split(':');
    const h = parseInt(hStr, 10) || 0;
    const m = parseInt(mStr, 10) || 0;
    switch (frequency) {
        case 'Hourly': {
            const next = new Date(now);
            next.setUTCMinutes(m, 0, 0);
            if (next <= now) next.setUTCHours(next.getUTCHours() + 1);
            return next;
        }
        case 'Weekly': {
            const next = new Date(now);
            next.setUTCHours(h, m, 0, 0);
            let daysAhead = (7 - next.getUTCDay()) % 7;
            if (daysAhead === 0 && next <= now) daysAhead = 7;
            next.setUTCDate(next.getUTCDate() + daysAhead);
            return next;
        }
        case 'Monthly': {
            let next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1, h, m));
            if (next <= now) next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1, h, m));
            return next;
        }
        default: {
            const next = new Date(now);
            next.setUTCHours(h, m, 0, 0);
            if (next <= now) next.setUTCDate(next.getUTCDate() + 1);
            return next;
        }
    }
}

function formatCountdown(target: Date): string {
    const ms = target.getTime() - Date.now();
    if (ms <= 0) return 'Now';
    const totalMin = Math.floor(ms / 60_000);
    if (totalMin < 60) return `in ${totalMin}m`;
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    if (h < 24) return `in ${h}h ${m}m`;
    const d = Math.floor(h / 24);
    return `in ${d}d ${h % 24}h`;
}

/* ── health score ─────────────────────────────────────── */

function computeHealthScore(p: PipelineDef): number {
    const expected = EXPECTED[p.id] ?? 1440;

    /* freshness (40 %) */
    let freshness = 0;
    if (p.lastIngestion) {
        const mins = (Date.now() - new Date(p.lastIngestion).getTime()) / 60_000;
        freshness = mins <= expected ? 100
            : mins <= expected * 2 ? 100 - ((mins - expected) / expected) * 50
            : mins <= expected * 4 ? 50 - ((mins - expected * 2) / (expected * 2)) * 50
            : 0;
    }

    /* coverage (35 %) */
    const coveredBuckets = p.coverage.filter((d) => d.count > 0).length;
    const totalBuckets = p.coverageSpan === 'monthly' ? 12 : 30;
    const coverageScore = (coveredBuckets / totalBuckets) * 100;

    /* consistency (25 %) — coefficient of variation of non-zero daily counts */
    const nonZero = p.coverage.map((d) => d.count).filter((c) => c > 0);
    let consistency = 0;
    if (nonZero.length > 1) {
        const mean = nonZero.reduce((a, b) => a + b, 0) / nonZero.length;
        const std = Math.sqrt(nonZero.reduce((a, c) => a + (c - mean) ** 2, 0) / nonZero.length);
        consistency = Math.max(0, (1 - std / mean) * 100);
    } else if (nonZero.length === 1) {
        consistency = 50;
    }

    return Math.round(Math.max(0, Math.min(100, freshness * 0.4 + coverageScore * 0.35 + consistency * 0.25)));
}

function scoreColor(s: number): string {
    return s >= 80 ? '#34d399' : s >= 60 ? '#f59e0b' : '#f87171';
}

/* ── sparkline data builder ─────────────────────────────── */

function buildSparklineData(coverage: { date: string; count: number }[], span: 'daily' | 'monthly'): number[] {
    if (span === 'monthly') {
        const now = new Date();
        const map = new Map<string, number>();
        for (const c of coverage) {
            const d = new Date(c.date);
            const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
            map.set(key, (map.get(key) ?? 0) + c.count);
        }
        return Array.from({ length: 12 }, (_, i) => {
            const d = new Date(now.getFullYear(), now.getMonth() - 11 + i, 1);
            const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
            return map.get(key) ?? 0;
        });
    }
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const map = new Map<string, number>();
    for (const c of coverage) {
        const key = new Date(c.date).toISOString().slice(0, 10);
        map.set(key, (map.get(key) ?? 0) + c.count);
    }
    return Array.from({ length: 30 }, (_, i) => {
        const d = new Date(today);
        d.setDate(d.getDate() - 29 + i);
        return map.get(d.toISOString().slice(0, 10)) ?? 0;
    });
}
/* ── list ─────────────────────────────────────────────────── */

const KIND_LABELS: Record<PipelineKind, string> = {
    daily: 'Daily Ingestion',
    catchup: 'Catchup',
    repair: 'Repair',
    'post-processing': 'Post-Processing',
};

const KIND_ORDER: PipelineKind[] = ['daily', 'catchup', 'repair', 'post-processing'];

export default function PipelineCards({ pipelines }: { pipelines: PipelineDef[] }) {
    const grouped = KIND_ORDER.map((kind) => ({
        kind,
        label: KIND_LABELS[kind],
        items: pipelines.filter((p) => p.kind === kind),
    })).filter((g) => g.items.length > 0);

    return (
        <div className="flex flex-col gap-6">
            {grouped.map((group) => (
                <section key={group.kind}>
                    <h2
                        className="text-[10px] font-mono uppercase tracking-widest mb-3 px-1"
                        style={{ color: 'rgba(255,255,255,0.25)' }}
                    >
                        {group.label}
                    </h2>
                    <div className="flex flex-col gap-4">
                        {group.items.map((p) => (
                            <PipelineCard key={p.id} pipeline={p} />
                        ))}
                    </div>
                </section>
            ))}
        </div>
    );
}

/* ── card ─────────────────────────────────────────────────── */

function PipelineCard({ pipeline: p }: { pipeline: PipelineDef }) {
    /* ── local editable state with tracked "saved" baseline ── */
    const [frequency, setFrequency] = useState(p.frequency);
    const [time, setTime] = useState(p.time);
    const [enabled, setEnabled] = useState(p.enabled);

    const [savedFreq, setSavedFreq] = useState(p.frequency);
    const [savedTime, setSavedTime] = useState(p.time);
    const [savedEnabled, setSavedEnabled] = useState(p.enabled);

    const isDirty = frequency !== savedFreq || time !== savedTime || enabled !== savedEnabled;

    /* ── dropdown open state ─────────────────────────────────── */
    const [showFreqMenu, setShowFreqMenu] = useState(false);
    const [showTimeMenu, setShowTimeMenu] = useState(false);

    /* close dropdowns on Escape */
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') { setShowFreqMenu(false); setShowTimeMenu(false); }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, []);

    /* ── async action state ──────────────────────────────────── */
    const [isSaving, startSave] = useTransition();
    const [saveResult, setSaveResult] = useState<ActionResult>(null);

    const [isTriggering, startTrigger] = useTransition();
    const [triggerResult, setTriggerResult] = useState<ActionResult>(null);

    /* auto-dismiss banners */
    useAutoDismiss(saveResult, setSaveResult);
    useAutoDismiss(triggerResult, setTriggerResult);

    const handleSave = () => {
        setSaveResult(null);
        startSave(async () => {
            const res = await saveSchedule(p.id, { frequency, time, enabled });
            setSaveResult(res);
            if (res.ok) {
                setSavedFreq(frequency);
                setSavedTime(time);
                setSavedEnabled(enabled);
            }
        });
    };

    const handleDiscard = () => {
        setFrequency(savedFreq);
        setTime(savedTime);
        setEnabled(savedEnabled);
    };

    const handleTrigger = () => {
        setTriggerResult(null);
        startTrigger(async () => {
            const res = await triggerPipeline(p.id);
            setTriggerResult(res);
        });
    };

    /* ── derived display values ──────────────────────────────── */
    const expected = EXPECTED[p.id] ?? 1440;
    const color = freshnessColor(p.lastIngestion, expected);
    const statusLabel = freshnessLabel(p.lastIngestion, expected);
    const healthScore = computeHealthScore(p);
    const sparklineData = buildSparklineData(p.coverage, p.coverageSpan);

    const minute = time.split(':')[1] ?? '00';
    const scheduleDesc = frequency === 'Hourly'
        ? `Every hour at :${minute}`
        : frequency === 'Weekly'
            ? `Weekly at ${time} UTC`
            : frequency === 'Monthly'
                ? `1st of month at ${time} UTC`
                : `Daily at ${time} UTC`;

    /* use *saved* enabled for opacity so the dim only applies to actually inactive pipes */
    const dimmed = !savedEnabled;

    return (
        <div
            className="rounded-xl overflow-hidden transition-opacity duration-300"
            style={{
                background: 'rgba(255,255,255,0.02)',
                border: `1px solid ${isDirty ? 'rgba(56,189,248,0.15)' : 'rgba(255,255,255,0.06)'}`,
                opacity: dimmed ? 0.5 : 1,
            }}
        >
            <div className="h-[2px] shrink-0" style={{ background: 'rgba(255,255,255,0.06)' }} />

            {/* ── row 1: name + source + toggle + run now ─────── */}
            <div className="px-5 pt-5 pb-4 flex items-start gap-3">
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                        <div
                            className="w-2 h-2 rounded-full shrink-0 transition-colors duration-300"
                            style={{ background: enabled ? color : 'rgba(255,255,255,0.15)' }}
                        />
                        <h3 className="text-[15px] font-semibold text-white">{p.name}</h3>
                        <HealthBadge score={healthScore} />
                    </div>
                    <p className="text-[11px] font-mono" style={{ color: 'rgba(255,255,255,0.25)' }}>
                        {p.source}
                    </p>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                    {/* sliding toggle */}
                    <ToggleSwitch
                        checked={enabled}
                        onChange={setEnabled}
                        label={enabled ? 'Enabled' : 'Disabled'}
                    />

                    {/* run now */}
                    <button
                        onClick={handleTrigger}
                        disabled={isTriggering || !enabled}
                        className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-mono font-medium transition-all
                            disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-125"
                        style={{
                            background: 'rgba(255,255,255,0.05)',
                            color: 'rgba(255,255,255,0.5)',
                            border: '1px solid rgba(255,255,255,0.08)',
                        }}
                    >
                        {isTriggering ? <Loader2 size={11} className="animate-spin" /> : <Play size={10} />}
                        {isTriggering ? 'Running…' : 'Run Now'}
                    </button>
                </div>
            </div>

            <div className="mx-5" style={{ height: 1, background: 'rgba(255,255,255,0.05)' }} />

            {/* ── row 2: schedule config + monitoring stats ──── */}
            <div className="px-5 py-4 flex flex-wrap items-start gap-x-8 gap-y-4">
                {/* schedule config */}
                <div className="flex items-start gap-6">
                    <Field label="Frequency">
                        <Dropdown
                            value={frequency}
                            options={[...FREQUENCIES]}
                            onChange={setFrequency}
                            open={showFreqMenu}
                            setOpen={setShowFreqMenu}
                        />
                    </Field>

                    <Field label="Time (UTC)">
                        {frequency === 'Hourly' ? (
                            <div
                                className="px-2.5 py-1.5 rounded-md text-[12px] font-mono"
                                style={{ background: 'rgba(255,255,255,0.03)', color: 'rgba(255,255,255,0.35)' }}
                            >
                                Every hour
                            </div>
                        ) : (
                            <Dropdown
                                value={time}
                                options={HOURS}
                                onChange={setTime}
                                open={showTimeMenu}
                                setOpen={setShowTimeMenu}
                            />
                        )}
                    </Field>

                    <Field label="Schedule">
                        <div className="flex items-center gap-1.5 pt-1.5">
                            <Calendar size={11} style={{ color: 'rgba(255,255,255,0.2)' }} />
                            <span className="text-[12px] font-mono" style={{ color: 'rgba(255,255,255,0.55)' }}>
                                {scheduleDesc}
                            </span>
                        </div>
                    </Field>
                </div>

                {/* monitoring stats — pushed right */}
                <div className="flex items-start gap-6 ml-auto">
                    <Field label="Records">
                        <div className="flex items-center gap-1.5 pt-1.5">
                            <Database size={11} style={{ color: 'rgba(255,255,255,0.2)' }} />
                            <span className="text-[12px] font-mono" style={{ color: 'rgba(255,255,255,0.55)' }}>
                                {fmtNum(p.rows)}
                            </span>
                        </div>
                    </Field>
                    <Field label="Last Ingestion">
                        <div className="flex items-center gap-1.5 pt-1.5" title={fmtFull(p.lastIngestion)}>
                            <Clock size={11} style={{ color: 'rgba(255,255,255,0.2)' }} />
                            <span className="text-[12px] font-mono" style={{ color }}>
                                {timeAgo(p.lastIngestion)}
                            </span>
                        </div>
                    </Field>
                    <Field label="Status">
                        <div className="flex items-center gap-1.5 pt-1.5">
                            <div className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
                            <span className="text-[12px] font-mono" style={{ color }}>{statusLabel}</span>
                        </div>
                    </Field>
                    <Field label="Next Run">
                        <div className="flex items-center gap-1.5 pt-1.5">
                            <Timer size={11} style={{ color: 'rgba(255,255,255,0.2)' }} />
                            <span className="text-[12px] font-mono" style={{ color: 'rgba(255,255,255,0.55)' }}>
                                {enabled ? formatCountdown(nextCronFire(frequency, time)) : '\u2014'}
                            </span>
                        </div>
                    </Field>
                </div>
            </div>

            <div className="mx-5" style={{ height: 1, background: 'rgba(255,255,255,0.05)' }} />

            {/* ── row 3: data range + save / discard ─────────── */}
            <div className="px-5 py-3 flex items-center justify-between gap-4">
                <div className="flex items-center gap-4">
                    <div className="flex items-center gap-2">
                        <span className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'rgba(255,255,255,0.2)' }}>
                            Data Range
                        </span>
                        <span
                            className="text-[11px] font-mono tabular-nums"
                            style={{ color: 'rgba(255,255,255,0.4)' }}
                            title={`${fmtFull(p.earliest)} → ${fmtFull(p.latest)}`}
                        >
                            {fmtDate(p.earliest)} → {fmtDate(p.latest)}
                        </span>
                    </div>
                    <Sparkline data={sparklineData} />
                </div>

                {isDirty && (
                    <div className="flex items-center gap-2">
                        <button
                            onClick={handleDiscard}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[11px] font-mono transition-all hover:brightness-125"
                            style={{
                                color: 'rgba(255,255,255,0.35)',
                                border: '1px solid rgba(255,255,255,0.06)',
                            }}
                        >
                            <RotateCcw size={10} />
                            Discard
                        </button>
                        <button
                            onClick={handleSave}
                            disabled={isSaving}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[11px] font-mono font-medium transition-all
                                disabled:opacity-50 hover:brightness-125"
                            style={{
                                background: 'rgba(56,189,248,0.1)',
                                color: 'rgba(56,189,248,0.85)',
                                border: '1px solid rgba(56,189,248,0.2)',
                            }}
                        >
                            {isSaving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />}
                            {isSaving ? 'Saving…' : 'Save Changes'}
                        </button>
                    </div>
                )}
            </div>

            {/* ── coverage strip ─────────────────────── */}
            <div className="mx-5" style={{ height: 1, background: 'rgba(255,255,255,0.05)' }} />
            <CoverageStrip coverage={p.coverage} span={p.coverageSpan} />

            {/* ── run history strip ───────────────────────────── */}
            {p.runHistory.length > 0 && (
                <>
                    <div className="mx-5" style={{ height: 1, background: 'rgba(255,255,255,0.05)' }} />
                    <RunHistoryStrip runs={p.runHistory} />
                </>
            )}

            {/* ── feedback banners ────────────────────────────── */}
            <FeedbackBanner result={saveResult} onDismiss={() => setSaveResult(null)} />
            <FeedbackBanner result={triggerResult} onDismiss={() => setTriggerResult(null)} />
        </div>
    );
}

/* ── auto-dismiss hook ────────────────────────────────────── */

function useAutoDismiss(result: ActionResult, setter: (v: ActionResult) => void) {
    const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
    useEffect(() => {
        if (result) {
            clearTimeout(timerRef.current);
            timerRef.current = setTimeout(() => setter(null), BANNER_TTL);
        }
        return () => clearTimeout(timerRef.current);
    }, [result, setter]);
}

/* ── toggle switch ────────────────────────────────────────── */

function ToggleSwitch({
    checked, onChange, label,
}: {
    checked: boolean; onChange: (v: boolean) => void; label: string;
}) {
    return (
        <button
            role="switch"
            aria-checked={checked}
            onClick={() => onChange(!checked)}
            className="flex items-center gap-2 text-[10px] font-mono transition-colors"
            style={{ color: checked ? '#34d399' : 'rgba(255,255,255,0.3)' }}
        >
            <div
                className="relative w-[30px] h-[16px] rounded-full transition-colors duration-200"
                style={{
                    background: checked ? 'rgba(52,211,153,0.25)' : 'rgba(255,255,255,0.08)',
                    border: `1px solid ${checked ? 'rgba(52,211,153,0.3)' : 'rgba(255,255,255,0.1)'}`,
                }}
            >
                <div
                    className="absolute top-[2px] w-[10px] h-[10px] rounded-full transition-all duration-200"
                    style={{
                        background: checked ? '#34d399' : 'rgba(255,255,255,0.25)',
                        left: checked ? '16px' : '3px',
                    }}
                />
            </div>
            {label}
        </button>
    );
}

/* ── reusable dropdown ────────────────────────────────────── */

function Dropdown({
    value, options, onChange, open, setOpen,
}: {
    value: string;
    options: string[];
    onChange: (v: string) => void;
    open: boolean;
    setOpen: (v: boolean) => void;
}) {
    const listRef = useRef<HTMLDivElement>(null);

    /* scroll the selected item into view when opening */
    useEffect(() => {
        if (open && listRef.current) {
            const active = listRef.current.querySelector('[data-active="true"]');
            active?.scrollIntoView({ block: 'nearest' });
        }
    }, [open]);

    return (
        <div className="relative">
            <button
                onClick={() => setOpen(!open)}
                className="flex items-center gap-2 px-2.5 py-1.5 rounded-md text-[12px] font-mono transition-colors hover:brightness-125 min-w-[90px]"
                style={{
                    background: 'rgba(255,255,255,0.05)',
                    border: '1px solid rgba(255,255,255,0.08)',
                    color: 'rgba(255,255,255,0.65)',
                }}
            >
                <span className="flex-1 text-left">{value}</span>
                <ChevronDown
                    size={11}
                    className="transition-transform duration-150"
                    style={{
                        color: 'rgba(255,255,255,0.25)',
                        transform: open ? 'rotate(180deg)' : undefined,
                    }}
                />
            </button>

            {open && (
                <>
                    <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
                    <div
                        ref={listRef}
                        className="absolute left-0 top-full mt-1 z-20 rounded-lg py-1 min-w-[140px] max-h-[200px] overflow-y-auto"
                        style={{
                            background: '#1a1a1a',
                            border: '1px solid rgba(255,255,255,0.1)',
                            boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
                        }}
                    >
                        {options.map((opt) => (
                            <button
                                key={opt}
                                data-active={opt === value}
                                onClick={() => { onChange(opt); setOpen(false); }}
                                className="w-full text-left px-3 py-1.5 text-[11px] font-mono transition-colors"
                                style={{
                                    color: opt === value ? 'rgba(255,255,255,0.8)' : 'rgba(255,255,255,0.4)',
                                    background: opt === value ? 'rgba(255,255,255,0.06)' : 'transparent',
                                }}
                                onMouseEnter={(e) => {
                                    if (opt !== value) e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
                                }}
                                onMouseLeave={(e) => {
                                    if (opt !== value) e.currentTarget.style.background = 'transparent';
                                }}
                            >
                                {opt}
                            </button>
                        ))}
                    </div>
                </>
            )}
        </div>
    );
}

/* ── tiny subcomponents ───────────────────────────────────── */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div className="flex flex-col gap-1">
            <span className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'rgba(255,255,255,0.22)' }}>
                {label}
            </span>
            {children}
        </div>
    );
}

function FeedbackBanner({ result, onDismiss }: { result: ActionResult; onDismiss: () => void }) {
    if (!result) return null;
    const ok = result.ok;
    return (
        <>
            <div className="mx-5" style={{ height: 1, background: 'rgba(255,255,255,0.05)' }} />
            <div
                className="px-5 py-2.5 flex items-center gap-2 text-[11px] font-mono"
                style={{
                    color: ok ? '#34d399' : '#f87171',
                    background: ok ? 'rgba(52,211,153,0.04)' : 'rgba(248,113,113,0.04)',
                }}
            >
                {ok ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
                <span className="flex-1">{result.message}</span>

                {result.url && (
                    <a
                        href={result.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1 underline underline-offset-2 transition-opacity hover:opacity-80"
                        style={{ color: ok ? '#34d399' : '#f87171' }}
                    >
                        View in Dagster <ExternalLink size={10} />
                    </a>
                )}

                <button
                    onClick={onDismiss}
                    className="ml-1 p-0.5 rounded transition-opacity hover:opacity-70"
                    aria-label="Dismiss"
                >
                    <X size={12} />
                </button>
            </div>
        </>
    );
}

/* ── coverage heatmap strip ───────────────────────────────── */

function CoverageStrip({ coverage, span }: { coverage: { date: string; count: number }[]; span: 'daily' | 'monthly' }) {
    if (span === 'monthly') return <MonthlyCoverageStrip coverage={coverage} />;
    return <DailyCoverageStrip coverage={coverage} />;
}

function MonthlyCoverageStrip({ coverage }: { coverage: { date: string; count: number }[] }) {
    const now = new Date();

    const countMap = new Map<string, number>();
    for (const { date, count } of coverage) {
        const d = new Date(date);
        const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
        countMap.set(key, (countMap.get(key) ?? 0) + count);
    }

    const months: { key: string; label: string; count: number }[] = [];
    for (let i = 11; i >= 0; i--) {
        const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
        const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
        const label = d.toLocaleDateString('en-GB', { month: 'short', year: '2-digit' });
        months.push({ key, label, count: countMap.get(key) ?? 0 });
    }

    const maxCount = Math.max(1, ...months.map((m) => m.count));
    const coveredMonths = months.filter((m) => m.count > 0).length;

    return (
        <div className="px-5 py-3">
            <div className="flex items-center justify-between mb-2">
                <span className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'rgba(255,255,255,0.2)' }}>
                    12-Month Coverage
                </span>
                <span
                    className="text-[10px] font-mono tabular-nums"
                    style={{ color: coveredMonths >= 11 ? '#34d399' : coveredMonths >= 8 ? '#f59e0b' : '#f87171' }}
                >
                    {coveredMonths}/12 months
                </span>
            </div>
            <div className="flex gap-[3px]">
                {months.map((m) => {
                    const intensity = m.count > 0 ? 0.25 + 0.75 * (m.count / maxCount) : 0;
                    return (
                        <div
                            key={m.key}
                            title={`${m.label}: ${m.count.toLocaleString()} records`}
                            className="flex-1 h-[14px] rounded-[2px] transition-colors cursor-default"
                            style={{
                                background: m.count > 0
                                    ? `rgba(52,211,153,${intensity})`
                                    : 'rgba(255,255,255,0.04)',
                                border: `1px solid ${m.count > 0 ? `rgba(52,211,153,${intensity * 0.4})` : 'rgba(255,255,255,0.04)'}`,
                                minWidth: 0,
                            }}
                        />
                    );
                })}
            </div>
        </div>
    );
}

function DailyCoverageStrip({ coverage }: { coverage: { date: string; count: number }[] }) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    // Build a map of date-string → count from coverage data
    const countMap = new Map<string, number>();
    for (const { date, count } of coverage) {
        // Normalize to YYYY-MM-DD
        const key = new Date(date).toISOString().slice(0, 10);
        countMap.set(key, (countMap.get(key) ?? 0) + count);
    }

    // Generate the last 30 days
    const days: { key: string; label: string; count: number }[] = [];
    for (let i = 29; i >= 0; i--) {
        const d = new Date(today);
        d.setDate(d.getDate() - i);
        const key = d.toISOString().slice(0, 10);
        const label = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
        days.push({ key, label, count: countMap.get(key) ?? 0 });
    }

    const maxCount = Math.max(1, ...days.map((d) => d.count));
    const coveredDays = days.filter((d) => d.count > 0).length;

    return (
        <div className="px-5 py-3">
            <div className="flex items-center justify-between mb-2">
                <span className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'rgba(255,255,255,0.2)' }}>
                    30-Day Coverage
                </span>
                <span
                    className="text-[10px] font-mono tabular-nums"
                    style={{ color: coveredDays >= 28 ? '#34d399' : coveredDays >= 20 ? '#f59e0b' : '#f87171' }}
                >
                    {coveredDays}/30 days
                </span>
            </div>
            <div className="flex gap-[3px]">
                {days.map((d) => {
                    const intensity = d.count > 0 ? 0.25 + 0.75 * (d.count / maxCount) : 0;
                    return (
                        <div
                            key={d.key}
                            title={`${d.label}: ${d.count.toLocaleString()} records`}
                            className="flex-1 h-[14px] rounded-[2px] transition-colors cursor-default"
                            style={{
                                background: d.count > 0
                                    ? `rgba(52,211,153,${intensity})`
                                    : 'rgba(255,255,255,0.04)',
                                border: `1px solid ${d.count > 0 ? `rgba(52,211,153,${intensity * 0.4})` : 'rgba(255,255,255,0.04)'}`,
                                minWidth: 0,
                            }}
                        />
                    );
                })}
            </div>
        </div>
    );
}

/* ── health badge ─────────────────────────────────────────── */

function HealthBadge({ score }: { score: number }) {
    const c = scoreColor(score);
    const [r, g, b] = c === '#34d399' ? [52, 211, 153] : c === '#f59e0b' ? [245, 158, 11] : [248, 113, 113];
    return (
        <div
            className="flex items-center justify-center w-7 h-7 rounded-full text-[10px] font-mono font-bold shrink-0"
            style={{
                background: `rgba(${r},${g},${b},0.1)`,
                color: c,
                border: `1px solid rgba(${r},${g},${b},0.2)`,
            }}
            title={`Health: ${score}/100`}
        >
            {score}
        </div>
    );
}

/* ── sparkline (SVG) ──────────────────────────────────────── */

function Sparkline({ data, width = 110, height = 26 }: { data: number[]; width?: number; height?: number }) {
    if (data.length < 2 || data.every((v) => v === 0)) return null;
    const max = Math.max(1, ...data);
    const pts = data.map((v, i) => {
        const x = (i / (data.length - 1)) * width;
        const y = height - 1 - (v / max) * (height - 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const line = `M${pts.join(' L')}`;
    const area = `${line} L${width},${height} L0,${height} Z`;
    return (
        <svg width={width} height={height} className="shrink-0" aria-label="30-day volume trend">
            <path d={area} fill="rgba(52,211,153,0.08)" />
            <path d={line} fill="none" stroke="rgba(52,211,153,0.5)" strokeWidth="1.5" strokeLinejoin="round" />
        </svg>
    );
}

/* ── run history strip ────────────────────────────────────── */

function RunHistoryStrip({ runs }: { runs: RunInfo[] }) {
    const successes = runs.filter((r) => r.state === 'COMPLETED').length;
    const durations = runs.map((r) => r.duration).filter((d): d is number => d != null && d > 0);
    const avgDur = durations.length > 0 ? durations.reduce((a, b) => a + b, 0) / durations.length : null;

    const fmtDur = (s: number): string => {
        if (s < 60) return `${Math.round(s)}s`;
        const m = Math.floor(s / 60);
        const sec = Math.round(s % 60);
        return sec > 0 ? `${m}m ${sec}s` : `${m}m`;
    };

    const STATE_COLOR: Record<string, string> = {
        COMPLETED: '#34d399', FAILED: '#f87171', CRASHED: '#f87171',
        CANCELLED: '#f59e0b', CANCELLING: '#f59e0b',
        RUNNING: '#38bdf8', PENDING: 'rgba(255,255,255,0.25)', SCHEDULED: 'rgba(255,255,255,0.25)',
    };

    return (
        <div className="px-5 py-3">
            <div className="flex items-center justify-between mb-2">
                <span className="text-[9px] font-mono uppercase tracking-wider" style={{ color: 'rgba(255,255,255,0.2)' }}>
                    Recent Runs
                </span>
                <div className="flex items-center gap-3">
                    <span
                        className="text-[10px] font-mono tabular-nums"
                        style={{ color: successes === runs.length ? '#34d399' : successes >= runs.length * 0.7 ? '#f59e0b' : '#f87171' }}
                    >
                        {successes}/{runs.length} success
                    </span>
                    {avgDur != null && (
                        <span className="text-[10px] font-mono tabular-nums" style={{ color: 'rgba(255,255,255,0.3)' }}>
                            avg {fmtDur(avgDur)}
                        </span>
                    )}
                </div>
            </div>
            <div className="flex items-center gap-[6px]">
                {runs.map((r) => (
                    <div
                        key={r.id}
                        title={`${r.state}${r.startTime ? ` \u2014 ${new Date(r.startTime).toLocaleString('en-GB')}` : ''}${r.duration ? ` (${fmtDur(r.duration)})` : ''}`}
                        className="w-3 h-3 rounded-full cursor-default transition-transform hover:scale-125"
                        style={{ background: STATE_COLOR[r.state] ?? 'rgba(255,255,255,0.15)' }}
                    />
                ))}
            </div>
        </div>
    );
}
