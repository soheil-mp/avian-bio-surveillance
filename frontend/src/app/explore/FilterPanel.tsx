'use client';

import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import { useState, useCallback, useRef, useEffect } from 'react';
import { SlidersHorizontal, RotateCcw, ChevronDown, X, Volume2 } from 'lucide-react';

const FILTER_SURFACE = {
    panel: 'rgba(20,20,20,0.96)',
    panelAlt: 'rgba(16,16,16,0.96)',
    panelDeep: 'rgba(10,10,10,0.98)',
    border: 'rgba(255,255,255,0.08)',
    borderSoft: 'rgba(255,255,255,0.05)',
};

const CONTROL_TEXT = 'rgba(255,255,255,0.72)';
const CONTROL_TEXT_MUTED = 'rgba(255,255,255,0.4)';

// ─── Searchable combobox ─────────────────────────────────────────────────────
function SearchableSelect({
    options,
    value,
    onChange,
    placeholder,
}: {
    options: string[];
    value: string;
    onChange: (v: string) => void;
    placeholder: string;
}) {
    const [open, setOpen] = useState(false);
    const [search, setSearch] = useState('');
    const ref = useRef<HTMLDivElement>(null);

    const filtered = search
        ? options.filter(o => o.toLowerCase().includes(search.toLowerCase()))
        : options;

    useEffect(() => {
        if (!open) return;
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) {
                setOpen(false);
                setSearch('');
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    const select = (v: string) => { onChange(v); setOpen(false); setSearch(''); };
    const clear = (e: React.MouseEvent) => { e.stopPropagation(); onChange(''); setOpen(false); setSearch(''); };

    return (
        <div ref={ref} className="relative w-full">
            <button type="button"
                onClick={() => { setOpen(o => !o); setSearch(''); }}
                className="w-full flex items-center justify-between gap-3 rounded-xl px-2.5 py-1.5 text-left transition-all duration-200"
                style={{
                    fontSize: 13,
                    background: open ? FILTER_SURFACE.panel : FILTER_SURFACE.panelAlt,
                    border: `1px solid ${FILTER_SURFACE.border}`,
                    color: value ? CONTROL_TEXT : CONTROL_TEXT_MUTED,
                    boxShadow: open ? 'inset 0 1px 0 rgba(255,255,255,0.04)' : 'none',
                }}>
                <span className="truncate">{value || placeholder}</span>
                <span className="flex items-center gap-0.5 shrink-0 ml-1">
                    {value && (
                        <X size={10} onClick={clear} style={{ color: 'rgba(255,255,255,0.35)' }}
                            className="hover:text-white/70 transition-colors" />
                    )}
                    <ChevronDown size={10}
                        style={{ color: 'rgba(255,255,255,0.25)', transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }} />
                </span>
            </button>

            {open && (
                <div className="absolute z-50 top-full left-0 right-0 mt-1.5 rounded-xl p-1.5 shadow-2xl"
                    style={{
                        background: FILTER_SURFACE.panelDeep,
                        border: `1px solid ${FILTER_SURFACE.border}`,
                        boxShadow: '0 20px 70px rgba(0,0,0,0.46)',
                        maxHeight: 220,
                    }}>
                    <div className="mb-1.5 rounded-xl px-2.5 py-1.5" style={{ background: FILTER_SURFACE.panelAlt, border: `1px solid ${FILTER_SURFACE.border}` }}>
                        <input autoFocus value={search} onChange={e => setSearch(e.target.value)}
                            placeholder="Search…"
                            className="w-full bg-transparent focus:outline-none"
                            style={{ fontSize: 14, color: 'rgba(255,255,255,0.86)', caretColor: 'rgba(255,255,255,0.9)' }} />
                    </div>
                    <div className="overflow-y-auto flex flex-col gap-1 pr-1" style={{ maxHeight: 172 }}>
                        {filtered.length === 0
                            ? <p className="rounded-xl px-2.5 py-1.5 text-sm" style={{ background: FILTER_SURFACE.panelAlt, border: `1px solid ${FILTER_SURFACE.border}`, color: 'rgba(255,255,255,0.44)' }}>No matches</p>
                            : filtered.map(opt => (
                                <button key={opt} type="button" onClick={() => select(opt)}
                                    className="w-full rounded-xl px-2.5 py-1.5 text-left transition-colors truncate block"
                                    style={{
                                        fontSize: 14,
                                        color: 'rgba(255,255,255,0.82)',
                                        background: opt === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt,
                                        border: `1px solid ${FILTER_SURFACE.border}`,
                                    }}
                                    onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.06)')}
                                    onMouseLeave={e => (e.currentTarget.style.background = opt === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt)}>
                                    {opt}
                                </button>
                            ))}
                    </div>
                </div>
            )}
        </div>
    );
}

// ─── Sort dropdown ───────────────────────────────────────────────────────────
const SORT_OPTS = [
    { key: 'date-desc', label: 'Newest first' },
    { key: 'date-asc', label: 'Oldest first' },
    { key: 'confidence-desc', label: 'Highest confidence' },
    { key: 'confidence-asc', label: 'Lowest confidence' },
    { key: 'species-asc', label: 'Species A \u2192 Z' },
    { key: 'species-desc', label: 'Species Z \u2192 A' },
];

const CONFIDENCE_OPTS = [
    { key: '', label: 'Any confidence' },
    { key: '40', label: '40%+' },
    { key: '50', label: '50%+' },
    { key: '60', label: '60%+' },
    { key: '70', label: '70%+' },
    { key: '80', label: '80%+' },
    { key: '90', label: '90%+' },
];

function SortSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const currentLabel = SORT_OPTS.find(o => o.key === value)?.label || 'Newest first';

    useEffect(() => {
        if (!open) return;
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    return (
        <div ref={ref} className="relative w-full">
            <button type="button" onClick={() => setOpen(o => !o)}
                className="w-full flex items-center justify-between gap-3 rounded-xl px-2.5 py-1.5 text-left transition-all duration-200"
                style={{
                    fontSize: 13,
                    background: open ? FILTER_SURFACE.panel : FILTER_SURFACE.panelAlt,
                    border: `1px solid ${FILTER_SURFACE.border}`,
                    color: CONTROL_TEXT,
                    boxShadow: open ? 'inset 0 1px 0 rgba(255,255,255,0.04)' : 'none',
                }}>
                <span className="truncate">{currentLabel}</span>
                <ChevronDown size={10}
                    style={{ color: 'rgba(255,255,255,0.28)', transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }} />
            </button>
            {open && (
                <div className="absolute z-50 top-full left-0 right-0 mt-1.5 rounded-xl p-1.5 shadow-2xl"
                    style={{
                        background: FILTER_SURFACE.panelDeep,
                        border: `1px solid ${FILTER_SURFACE.border}`,
                        boxShadow: '0 20px 70px rgba(0,0,0,0.46)',
                    }}>
                    <div className="flex flex-col gap-1">
                    {SORT_OPTS.map(opt => (
                        <button key={opt.key} type="button"
                            onClick={() => { onChange(opt.key); setOpen(false); }}
                            className="w-full rounded-xl px-2.5 py-1.5 text-left transition-colors truncate block"
                            style={{
                                fontSize: 14,
                                color: 'rgba(255,255,255,0.82)',
                                background: opt.key === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt,
                                border: `1px solid ${FILTER_SURFACE.border}`,
                            }}
                            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.06)')}
                            onMouseLeave={e => (e.currentTarget.style.background = opt.key === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt)}>
                            {opt.label}
                        </button>
                    ))}
                    </div>
                </div>
            )}
        </div>
    );
}

// ─── Filter panel ────────────────────────────────────────────────────────────
type Filters = {
    species: string; station: string; location: string;
    minConf: string; dateFrom: string; dateTo: string;
    audio: 'processed' | 'all';
    sort: string;
    region: 'visible' | 'hidden';
};
const DEFAULT_SORT = 'date-desc';
const DEFAULT_REGION: Filters['region'] = 'hidden';
const EMPTY: Filters = {
    species: '',
    station: '',
    location: '',
    minConf: '',
    dateFrom: '',
    dateTo: '',
    audio: 'processed',
    sort: DEFAULT_SORT,
    region: DEFAULT_REGION,
};
const FILTER_KEYS: (keyof Omit<Filters, 'audio'>)[] = ['species', 'station', 'location', 'minConf', 'dateFrom', 'dateTo'];

/** Section heading label */
function SectionLabel({ children }: { children: React.ReactNode }) {
    return (
        <p className="mb-1 block font-mono tracking-[0.18em] uppercase"
            style={{ fontSize: 10, color: 'rgba(255,255,255,0.30)' }}>
            {children}
        </p>
    );
}

function ConfidenceSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const currentLabel = CONFIDENCE_OPTS.find(o => o.key === value)?.label || 'Any confidence';

    useEffect(() => {
        if (!open) return;
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    return (
        <div ref={ref} className="relative w-full">
            <button type="button" onClick={() => setOpen(o => !o)}
                className="w-full flex items-center justify-between gap-3 rounded-xl px-2.5 py-1.5 text-left transition-all duration-200"
                style={{
                    fontSize: 13,
                    background: open ? FILTER_SURFACE.panel : FILTER_SURFACE.panelAlt,
                    border: `1px solid ${FILTER_SURFACE.border}`,
                    color: CONTROL_TEXT,
                    boxShadow: open ? 'inset 0 1px 0 rgba(255,255,255,0.04)' : 'none',
                }}>
                <span className="truncate">{currentLabel}</span>
                <ChevronDown size={10}
                    style={{ color: 'rgba(255,255,255,0.28)', transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }} />
            </button>
            {open && (
                <div className="absolute z-50 top-full left-0 right-0 mt-1.5 rounded-xl p-1.5 shadow-2xl"
                    style={{
                        background: FILTER_SURFACE.panelDeep,
                        border: `1px solid ${FILTER_SURFACE.border}`,
                        boxShadow: '0 20px 70px rgba(0,0,0,0.46)',
                    }}>
                    <div className="flex flex-col gap-1">
                    {CONFIDENCE_OPTS.map(opt => (
                        <button key={opt.key || 'any'} type="button"
                            onClick={() => { onChange(opt.key); setOpen(false); }}
                            className="w-full rounded-xl px-2.5 py-1.5 text-left transition-colors truncate block"
                            style={{
                                fontSize: 13,
                                color: 'rgba(255,255,255,0.82)',
                                background: opt.key === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt,
                                border: `1px solid ${FILTER_SURFACE.border}`,
                            }}
                            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.06)')}
                            onMouseLeave={e => (e.currentTarget.style.background = opt.key === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt)}>
                            {opt.label}
                        </button>
                    ))}
                    </div>
                </div>
            )}
        </div>
    );
}

function AudioToggle({ value, onChange }: { value: 'processed' | 'all'; onChange: (value: 'processed' | 'all') => void }) {
    const checked = value === 'processed';

    return (
        <button type="button"
            role="switch"
            aria-checked={checked}
            onClick={() => onChange(checked ? 'all' : 'processed')}
            className="w-full flex items-center justify-between gap-3 rounded-xl px-2.5 py-1.5 transition-all duration-200"
            style={{
                background: FILTER_SURFACE.panelAlt,
                border: `1px solid ${FILTER_SURFACE.border}`,
                color: CONTROL_TEXT,
            }}>
            <span className="flex items-center gap-2 min-w-0">
                <Volume2 size={11} className="shrink-0" />
                <span className="truncate" style={{ fontSize: 13 }}>
                    Processed audio only
                </span>
            </span>
            <span className="relative shrink-0 rounded-full transition-all duration-200"
                style={{
                    width: 28,
                    height: 14,
                    background: checked ? 'rgba(255,255,255,0.18)' : 'rgba(255,255,255,0.08)',
                }}>
                <span className="absolute top-0.5 rounded-full transition-all duration-200"
                    style={{
                        left: checked ? 15 : 2,
                        width: 10,
                        height: 10,
                        background: checked ? 'rgba(255,255,255,0.92)' : 'rgba(255,255,255,0.35)',
                    }} />
            </span>
        </button>
    );
}

export default function FilterPanel({
    speciesOpts, stationOpts, locationOpts,
}: {
    speciesOpts: string[];
    stationOpts: string[];
    locationOpts: string[];
}) {
    const router = useRouter();
    const pathname = usePathname();
    const searchParams = useSearchParams();

    const [f, setF] = useState<Filters>({
        species: searchParams.get('species') || '',
        station: searchParams.get('station') || '',
        location: searchParams.get('location') || '',
        minConf: searchParams.get('minConf') || '',
        dateFrom: searchParams.get('dateFrom') || '',
        dateTo: searchParams.get('dateTo') || '',
        audio: searchParams.get('audio') === 'all' ? 'all' : 'processed',
        sort: searchParams.get('sort') || DEFAULT_SORT,
        region: searchParams.get('region') === '1' ? 'visible' : DEFAULT_REGION,
    });

    const buildParams = useCallback((filters: Filters) => {
        const p = new URLSearchParams(searchParams.toString());
        p.set('page', '1');

        for (const key of FILTER_KEYS) {
            const value = filters[key];
            if (value) p.set(key, value);
            else p.delete(key);
        }
        if (filters.audio === 'all') p.set('audio', 'all');
        else p.delete('audio');
        if (filters.sort && filters.sort !== DEFAULT_SORT) p.set('sort', filters.sort);
        else p.delete('sort');
        if (filters.region === 'visible') p.set('region', '1');
        else p.delete('region');

        return p;
    }, [searchParams]);

    useEffect(() => {
        setF({
            species: searchParams.get('species') || '',
            station: searchParams.get('station') || '',
            location: searchParams.get('location') || '',
            minConf: searchParams.get('minConf') || '',
            dateFrom: searchParams.get('dateFrom') || '',
            dateTo: searchParams.get('dateTo') || '',
            audio: searchParams.get('audio') === 'all' ? 'all' : 'processed',
            sort: searchParams.get('sort') || DEFAULT_SORT,
            region: searchParams.get('region') === '1' ? 'visible' : DEFAULT_REGION,
        });
    }, [searchParams]);

    const apply = useCallback(() => {
        const next = buildParams(f).toString();
        const current = (() => {
            const p = new URLSearchParams(searchParams.toString());
            p.set('page', '1');
            return p.toString();
        })();

        if (next !== current) {
            router.replace(`${pathname}?${next}`);
        }
    }, [buildParams, f, pathname, router, searchParams]);

    const reset = useCallback(() => {
        setF(EMPTY);
        const p = buildParams(EMPTY);
        const next = p.toString();
        const current = (() => {
            const currentParams = new URLSearchParams(searchParams.toString());
            currentParams.set('page', '1');
            return currentParams.toString();
        })();

        if (next !== current) {
            router.replace(`${pathname}?${next}`);
        }
    }, [buildParams, pathname, router, searchParams]);

    const set = (k: keyof Omit<Filters, 'audio'>) => (v: string) => setF(prev => ({ ...prev, [k]: v }));
    const setE = (k: keyof Omit<Filters, 'audio'>) => (e: React.ChangeEvent<HTMLInputElement>) =>
        setF(prev => ({ ...prev, [k]: e.target.value }));

    const activeCount = Object.entries(f).filter(([key, value]) => {
        if (key === 'audio') return value === 'processed';
        if (key === 'sort' || key === 'region') return false;
        return Boolean(value);
    }).length;
    const hasPendingChanges = (() => {
        const currentParams = new URLSearchParams(searchParams.toString());
        currentParams.set('page', '1');
        return buildParams(f).toString() !== currentParams.toString();
    })();

    const inputCls = [
        'w-full rounded-xl px-2.5 py-1.5 transition-all duration-200',
        'bg-transparent placeholder-white/20',
        'border border-white/[0.08] focus:outline-none focus:ring-0',
    ].join(' ');

    const dateInputStyle = {
        fontSize: 13,
        fontFamily: 'inherit',
        fontWeight: 400,
        background: FILTER_SURFACE.panelAlt,
        color: CONTROL_TEXT,
        WebkitTextFillColor: CONTROL_TEXT,
    } as const;

    return (
        <>
        <div className="w-[264px] h-full min-h-0 shrink-0 flex flex-col overflow-hidden rounded-2xl p-3 z-10"
            style={{
                background: FILTER_SURFACE.panel,
                border: `1px solid ${FILTER_SURFACE.border}`,
                boxShadow: '0 18px 40px rgba(0,0,0,0.28)',
            }}>

            {/* ── Header ── */}
            <div className="mb-3 flex items-start justify-between gap-3 shrink-0">
                <div>
                    <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.24em]"
                        style={{ color: 'rgba(255,255,255,0.42)' }}>
                        <SlidersHorizontal size={12} />
                        Filters
                    </div>
                    {activeCount > 0 ? (
                        <p className="mt-1 text-xs leading-relaxed" style={{ color: 'rgba(255,255,255,0.42)' }}>
                            {activeCount} active filter{activeCount === 1 ? '' : 's'}
                        </p>
                    ) : null}
                </div>
            </div>

            {/* ── Scrollable body ── */}
            <div className="flex-1 overflow-y-auto custom-scrollbar pr-1"
                style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>

                {/* ─ Filter fields ─ */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    <div>
                        <SectionLabel>Species</SectionLabel>
                        <SearchableSelect options={speciesOpts} value={f.species}
                            onChange={set('species')} placeholder="All species" />
                    </div>
                    <div>
                        <SectionLabel>Station</SectionLabel>
                        <SearchableSelect options={stationOpts} value={f.station}
                            onChange={set('station')} placeholder="All stations" />
                    </div>
                    <div>
                        <SectionLabel>Location</SectionLabel>
                        <SearchableSelect options={locationOpts} value={f.location}
                            onChange={set('location')} placeholder="All locations" />
                    </div>
                    <div>
                        <SectionLabel>Min. confidence</SectionLabel>
                        <ConfidenceSelect value={f.minConf}
                            onChange={(v) => setF(prev => ({ ...prev, minConf: v }))} />
                    </div>
                    <div>
                        <SectionLabel>From</SectionLabel>
                        <input type="date" className={`${inputCls} filterDateInput [color-scheme:dark]`}
                            value={f.dateFrom} onChange={setE('dateFrom')}
                            style={dateInputStyle} />
                    </div>
                    <div>
                        <SectionLabel>To</SectionLabel>
                        <input type="date" className={`${inputCls} filterDateInput [color-scheme:dark]`}
                            value={f.dateTo} onChange={setE('dateTo')}
                            style={dateInputStyle} />
                    </div>
                    <div>
                        <SectionLabel>Audio</SectionLabel>
                        <AudioToggle value={f.audio}
                            onChange={(value) => setF(prev => ({ ...prev, audio: value }))} />
                    </div>
                    <div>
                        <SectionLabel>Sort</SectionLabel>
                        <SortSelect value={f.sort}
                            onChange={(v) => setF(prev => ({ ...prev, sort: v }))} />
                    </div>
                    <div>
                        <SectionLabel>Region</SectionLabel>
                        <RegionSelect value={f.region}
                            onChange={(v) => setF(prev => ({ ...prev, region: v as Filters['region'] }))} />
                    </div>
                </div>
            </div>

            <div className="mt-3 grid shrink-0 grid-cols-2 gap-2 border-t pt-3"
                style={{ borderColor: FILTER_SURFACE.borderSoft }}>
                <button
                    type="button"
                    onClick={apply}
                    disabled={!hasPendingChanges}
                    className="inline-flex items-center justify-center rounded-xl px-3 py-2 text-sm font-medium transition-all duration-200"
                    style={{
                        background: hasPendingChanges ? 'rgba(255,255,255,0.10)' : FILTER_SURFACE.panelAlt,
                        border: `1px solid ${hasPendingChanges ? 'rgba(255,255,255,0.16)' : FILTER_SURFACE.border}`,
                        color: hasPendingChanges ? 'rgba(255,255,255,0.92)' : 'rgba(255,255,255,0.46)',
                        cursor: hasPendingChanges ? 'pointer' : 'default',
                    }}>
                    Apply
                </button>
                <button
                    type="button"
                    onClick={reset}
                    className="inline-flex items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition-all duration-200"
                    style={{
                        background: FILTER_SURFACE.panelAlt,
                        border: `1px solid ${FILTER_SURFACE.border}`,
                        color: 'rgba(255,255,255,0.68)',
                    }}>
                    <RotateCcw size={13} />
                    Reset
                </button>
            </div>

        </div>
        <style jsx>{`
            .filterDateInput::-webkit-datetime-edit,
            .filterDateInput::-webkit-datetime-edit-text,
            .filterDateInput::-webkit-datetime-edit-month-field,
            .filterDateInput::-webkit-datetime-edit-day-field,
            .filterDateInput::-webkit-datetime-edit-year-field {
                color: ${CONTROL_TEXT};
                font-family: inherit;
            }

            .filterDateInput::-webkit-calendar-picker-indicator {
                opacity: 0.45;
            }
        `}</style>
        </>
    );
}

// ─── Region visibility dropdown (matches SortSelect style) ───────────────────
const REGION_OPTS = [
    { key: 'visible', label: 'Visible' },
    { key: 'hidden', label: 'Hidden' },
];

function RegionSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const currentLabel = REGION_OPTS.find(o => o.key === value)?.label || 'Visible';

    useEffect(() => {
        if (!open) return;
        const handler = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    return (
        <div ref={ref} className="relative w-full">
            <button type="button" onClick={() => setOpen(o => !o)}
                className="w-full flex items-center justify-between gap-3 rounded-xl px-2.5 py-1.5 text-left transition-all duration-200"
                style={{
                    fontSize: 13,
                    background: open ? FILTER_SURFACE.panel : FILTER_SURFACE.panelAlt,
                    border: `1px solid ${FILTER_SURFACE.border}`,
                    color: CONTROL_TEXT,
                    boxShadow: open ? 'inset 0 1px 0 rgba(255,255,255,0.04)' : 'none',
                }}>
                <span className="truncate">{currentLabel}</span>
                <ChevronDown size={10}
                    style={{ color: 'rgba(255,255,255,0.28)', transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }} />
            </button>
            {open && (
                <div className="absolute z-50 top-full left-0 right-0 mt-1.5 rounded-xl p-1.5 shadow-2xl"
                    style={{
                        background: FILTER_SURFACE.panelDeep,
                        border: `1px solid ${FILTER_SURFACE.border}`,
                        boxShadow: '0 20px 70px rgba(0,0,0,0.46)',
                    }}>
                    <div className="flex flex-col gap-1">
                    {REGION_OPTS.map(opt => (
                        <button key={opt.key} type="button"
                            onClick={() => { onChange(opt.key); setOpen(false); }}
                            className="w-full rounded-xl px-2.5 py-1.5 text-left transition-colors truncate block"
                            style={{
                                fontSize: 14,
                                color: 'rgba(255,255,255,0.82)',
                                background: opt.key === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt,
                                border: `1px solid ${FILTER_SURFACE.border}`,
                            }}
                            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.06)')}
                            onMouseLeave={e => (e.currentTarget.style.background = opt.key === value ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt)}>
                            {opt.label}
                        </button>
                    ))}
                    </div>
                </div>
            )}
        </div>
    );
}
