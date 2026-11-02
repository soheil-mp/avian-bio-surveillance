'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Space_Grotesk } from 'next/font/google';
import { SlidersHorizontal, ChevronDown, X, ArrowLeft, RotateCcw } from 'lucide-react';
import type {
    FeedFilters,
    FeedFilterOptions,
    FeedFilterOption,
    FeedFilterSort,
} from '@/lib/home-feed-filters';

const headingFont = Space_Grotesk({ subsets: ['latin'], weight: ['500', '600'], display: 'swap' });

// Solid (no map bleed-through) when the panel is flipped into view.
const PANEL_BG = '#141414';
const FIELD_BG = 'rgba(255,255,255,0.035)';
const FIELD_BG_OPEN = 'rgba(255,255,255,0.07)';
const MENU_BG = '#0c0f0e';
const CHROME = 'rgba(255,255,255,0.12)';
const BORDER = 'rgba(255,255,255,0.14)';
const ROW_DIVIDER = 'rgba(255,255,255,0.06)';
const TEXT = '#e8edf2';
const TEXT_DIM = 'rgba(255,255,255,0.45)';
const ACCENT = '#34d399';
const ACCENT_BG = 'rgba(52,211,153,0.15)';

// Dropdown surface rendered through a portal to <body>, positioned at the
// trigger's on-screen rect. The panel lives on a rotateY(180deg) flip face with
// overflow:hidden; rendering the menu inside that subtree clips it and re-bases
// its coordinates. Portaling to the document root escapes both the transform and
// the clip so options are always fully visible and on top.
function MenuPortal({ anchorRef, open, onClose, children }: {
    anchorRef: React.RefObject<HTMLButtonElement | null>;
    open: boolean;
    onClose: () => void;
    children: React.ReactNode;
}) {
    const [rect, setRect] = useState<DOMRect | null>(null);
    const menuRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        if (!open) { setRect(null); return; }
        const update = () => {
            const el = anchorRef.current;
            if (el) setRect(el.getBoundingClientRect());
        };
        update();
        window.addEventListener('resize', update);
        window.addEventListener('scroll', update, true);
        return () => {
            window.removeEventListener('resize', update);
            window.removeEventListener('scroll', update, true);
        };
    }, [open, anchorRef]);

    useEffect(() => {
        if (!open) return;
        const onDoc = (e: MouseEvent) => {
            if (menuRef.current && menuRef.current.contains(e.target as Node)) return;
            if (anchorRef.current && anchorRef.current.contains(e.target as Node)) return;
            onClose();
        };
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        document.addEventListener('mousedown', onDoc);
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('mousedown', onDoc);
            document.removeEventListener('keydown', onKey);
        };
    }, [open, onClose, anchorRef]);

    if (!open || !rect || typeof document === 'undefined') return null;

    const MENU_MAX_H = 256;
    const spaceBelow = window.innerHeight - rect.bottom;
    const openUp = spaceBelow < MENU_MAX_H && rect.top > spaceBelow;
    const style: React.CSSProperties = {
        position: 'fixed',
        left: rect.left,
        width: rect.width,
        zIndex: 2147483000,
        ...(openUp
            ? { bottom: window.innerHeight - rect.top + 6 }
            : { top: rect.bottom + 6 }),
    };

    return createPortal(
        <div ref={menuRef} style={style}>
            <div style={{ background: MENU_BG, border: `1px solid ${BORDER}`, borderRadius: 12, padding: 5, boxShadow: '0 24px 70px rgba(0,0,0,0.65)' }}>
                {children}
            </div>
        </div>,
        document.body,
    );
}

// One option row. Generous height + a hairline divider between rows so the
// values read as distinct, not smashed together.
function OptionRow({ label, active, first, onClick }: {
    label: string;
    active: boolean;
    first: boolean;
    onClick: () => void;
}) {
    return (
        <button type="button" onClick={onClick}
            className="flex w-full items-center truncate px-2.5 text-left transition-colors"
            style={{
                fontSize: 13,
                lineHeight: '1.25',
                paddingTop: 9,
                paddingBottom: 9,
                color: active ? ACCENT : TEXT,
                background: active ? ACCENT_BG : 'transparent',
                borderTop: first ? 'none' : `1px solid ${ROW_DIVIDER}`,
                fontFamily: headingFont.style.fontFamily,
            }}
            onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = 'rgba(255,255,255,0.06)'; }}
            onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = 'transparent'; }}>
            {label}
        </button>
    );
}

const TRIGGER_CLASS = 'flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left transition-colors duration-200';

function FieldLabel({ children }: { children: React.ReactNode }) {
    return (
        <p className="mb-1.5 font-mono uppercase tracking-[0.18em]"
            style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.34)' }}>
            {children}
        </p>
    );
}

// Searchable, clearable combobox for variable-length option lists.
function HudCombo({ options, value, onChange, placeholder, searchable = true }: {
    options: FeedFilterOption[];
    value: string | null;
    onChange: (v: string | null) => void;
    placeholder: string;
    searchable?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const [search, setSearch] = useState('');
    const btnRef = useRef<HTMLButtonElement | null>(null);
    const selected = options.find((o) => o.id === value) || null;
    const filtered = search
        ? options.filter((o) => o.label.toLowerCase().includes(search.toLowerCase()))
        : options;
    const close = () => { setOpen(false); setSearch(''); };

    return (
        <>
            <button ref={btnRef} type="button" onClick={() => setOpen((o) => !o)}
                className={TRIGGER_CLASS}
                style={{ fontSize: 13, background: open ? FIELD_BG_OPEN : FIELD_BG, border: `1px solid ${BORDER}` }}>
                <span className="truncate" style={{ color: selected ? TEXT : TEXT_DIM, fontFamily: headingFont.style.fontFamily, letterSpacing: '0.01em' }}>
                    {selected ? selected.label : placeholder}
                </span>
                <span className="flex shrink-0 items-center gap-0.5">
                    {selected ? (
                        <X size={12} onClick={(e) => { e.stopPropagation(); onChange(null); close(); }}
                            style={{ color: 'rgba(255,255,255,0.4)' }} className="transition-colors hover:text-white/80" />
                    ) : null}
                    <ChevronDown size={12}
                        style={{ color: 'rgba(255,255,255,0.4)', transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }} />
                </span>
            </button>
            <MenuPortal anchorRef={btnRef} open={open} onClose={close}>
                {searchable ? (
                    <div className="mb-1 rounded-md px-2.5 py-2" style={{ background: 'rgba(255,255,255,0.04)', border: `1px solid ${BORDER}` }}>
                        <input autoFocus value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search…"
                            className="w-full bg-transparent focus:outline-none"
                            style={{ fontSize: 13, color: TEXT, caretColor: ACCENT }} />
                    </div>
                ) : null}
                <div className="feed-filter-scroll overflow-y-auto" style={{ maxHeight: 208 }}>
                    {filtered.length === 0 ? (
                        <p className="px-2.5 py-2.5" style={{ fontSize: 12.5, color: TEXT_DIM }}>No matches</p>
                    ) : filtered.map((opt, i) => (
                        <OptionRow key={opt.id} label={opt.label} active={opt.id === value} first={i === 0}
                            onClick={() => { onChange(opt.id); close(); }} />
                    ))}
                </div>
            </MenuPortal>
        </>
    );
}

// Compact fixed-option dropdown (min confidence / min anomaly / sort) — same
// trigger + menu styling as the combobox, just without the search field.
function HudChoice({ options, value, onChange }: {
    options: FeedFilterOption[];
    value: string;
    onChange: (v: string) => void;
}) {
    const [open, setOpen] = useState(false);
    const btnRef = useRef<HTMLButtonElement | null>(null);
    const current = options.find((o) => o.id === value) || options[0];
    const close = () => setOpen(false);

    return (
        <>
            <button ref={btnRef} type="button" onClick={() => setOpen((o) => !o)}
                className={TRIGGER_CLASS}
                style={{ fontSize: 13, background: open ? FIELD_BG_OPEN : FIELD_BG, border: `1px solid ${BORDER}`, color: TEXT }}>
                <span className="truncate" style={{ color: TEXT, fontFamily: headingFont.style.fontFamily }}>{current?.label}</span>
                <ChevronDown size={12}
                    style={{ color: 'rgba(255,255,255,0.4)', transform: open ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }} />
            </button>
            <MenuPortal anchorRef={btnRef} open={open} onClose={close}>
                <div className="flex flex-col">
                    {options.map((opt, i) => (
                        <OptionRow key={opt.id || 'any'} label={opt.label} active={opt.id === value} first={i === 0}
                            onClick={() => { onChange(opt.id); close(); }} />
                    ))}
                </div>
            </MenuPortal>
        </>
    );
}

const SORT_OPTIONS: FeedFilterOption[] = [
    { id: 'latest', label: 'Latest first' },
    { id: 'confidence', label: 'Highest confidence' },
];
const CONFIDENCE_OPTIONS: FeedFilterOption[] = [
    { id: '', label: 'Any confidence' },
    { id: '0.4', label: '40%+' },
    { id: '0.5', label: '50%+' },
    { id: '0.6', label: '60%+' },
    { id: '0.7', label: '70%+' },
    { id: '0.8', label: '80%+' },
    { id: '0.9', label: '90%+' },
];
const toThresholdId = (v: number | null) => (v == null || v <= 0 ? '' : String(v));
const fromThresholdId = (id: string) => (id ? Number(id) : null);

interface FeedFilterPanelProps {
    options: FeedFilterOptions | null;
    optionsLoading: boolean;
    value: FeedFilters;
    onChange: (next: FeedFilters) => void;
    onApply: () => void;
    onReset: () => void;
    onBack: () => void;
    activeCount: number;
    hasPending: boolean;
}

export function FeedFilterPanel({
    options, optionsLoading, value, onChange, onApply, onReset, onBack, activeCount, hasPending,
}: FeedFilterPanelProps) {
    const weekOptions = useMemo<FeedFilterOption[]>(
        () => (options?.weeks ?? []).map((w) => ({ id: w.id, label: w.label })),
        [options],
    );

    function update<K extends keyof FeedFilters>(key: K, v: FeedFilters[K]) {
        onChange({ ...value, [key]: v } as FeedFilters);
    }

    return (
        <div className="relative flex h-full w-full flex-col" style={{ background: PANEL_BG }}>
            {/* HUD corner brackets — mirror the live feed */}
            <div className="pointer-events-none absolute inset-0 z-10">
                <div className="absolute left-2.5 top-2.5 h-3.5 w-3.5" style={{ borderTop: `1.5px solid ${CHROME}`, borderLeft: `1.5px solid ${CHROME}` }} />
                <div className="absolute right-2.5 top-2.5 h-3.5 w-3.5" style={{ borderTop: `1.5px solid ${CHROME}`, borderRight: `1.5px solid ${CHROME}` }} />
                <div className="absolute bottom-2.5 left-2.5 h-3.5 w-3.5" style={{ borderBottom: `1.5px solid ${CHROME}`, borderLeft: `1.5px solid ${CHROME}` }} />
                <div className="absolute bottom-2.5 right-2.5 h-3.5 w-3.5" style={{ borderBottom: `1.5px solid ${CHROME}`, borderRight: `1.5px solid ${CHROME}` }} />
            </div>

            {/* Header */}
            <div className="shrink-0 px-5 pb-3 pt-4">
                <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2.5">
                        <SlidersHorizontal size={13} style={{ color: ACCENT }} />
                        <h2 className="text-[13px] font-semibold uppercase tracking-[0.08em] text-white/90" style={{ fontFamily: headingFont.style.fontFamily }}>
                            Filter Feed
                        </h2>
                    </div>
                    {activeCount > 0 ? (
                        <span className="rounded-full px-2 py-[2px] text-[9px] font-mono uppercase tracking-[0.12em]"
                            style={{ background: ACCENT_BG, border: '1px solid rgba(52,211,153,0.28)', color: ACCENT }}>
                            {activeCount} active
                        </span>
                    ) : null}
                </div>
            </div>

            {/* Scan line (reuses the live feed keyframe) */}
            <div className="h-[1px] shrink-0 overflow-hidden"><div className="feed-scan-line h-full w-full" /></div>

            {/* Body — every field full-width so all dropdown menus share one width */}
            <div className="feed-filter-scroll flex-1 overflow-y-auto px-5 py-4">
                {optionsLoading && !options ? (
                    <div className="flex flex-col gap-3">
                        {Array.from({ length: 6 }).map((_, i) => (
                            <div key={i} className="h-12 animate-pulse rounded-lg" style={{ background: 'rgba(255,255,255,0.05)' }} />
                        ))}
                    </div>
                ) : (
                    <div className="flex flex-col gap-3.5">
                        <div>
                            <FieldLabel>Week</FieldLabel>
                            <HudCombo options={weekOptions} value={value.week} onChange={(v) => update('week', v)} placeholder="Latest available" />
                        </div>
                        <div>
                            <FieldLabel>Species</FieldLabel>
                            <HudCombo options={options?.species ?? []} value={value.speciesId} onChange={(v) => update('speciesId', v)} placeholder="All species" />
                        </div>
                        <div>
                            <FieldLabel>Station</FieldLabel>
                            <HudCombo options={options?.stations ?? []} value={value.stationId} onChange={(v) => update('stationId', v)} placeholder="All stations" />
                        </div>
                        <div>
                            <FieldLabel>Grid cell</FieldLabel>
                            <HudCombo options={options?.grids ?? []} value={value.gridCellId} onChange={(v) => update('gridCellId', v)} placeholder="All cells" />
                        </div>
                        <div>
                            <FieldLabel>Min confidence</FieldLabel>
                            <HudChoice options={CONFIDENCE_OPTIONS} value={toThresholdId(value.minConfidence)} onChange={(v) => update('minConfidence', fromThresholdId(v))} />
                        </div>
                        <div>
                            <FieldLabel>Sort</FieldLabel>
                            <HudChoice options={SORT_OPTIONS} value={value.sort} onChange={(v) => update('sort', v as FeedFilterSort)} />
                        </div>
                    </div>
                )}
            </div>

            {/* Action row */}
            <div className="grid shrink-0 grid-cols-2 gap-2 px-5 pt-3">
                <button type="button" onClick={onApply} disabled={!hasPending}
                    className="inline-flex items-center justify-center rounded-lg px-3 py-2 text-[12px] font-medium transition-colors"
                    style={{ background: hasPending ? 'rgba(52,211,153,0.18)' : 'rgba(255,255,255,0.04)', border: `1px solid ${hasPending ? 'rgba(52,211,153,0.34)' : BORDER}`, color: hasPending ? ACCENT : 'rgba(255,255,255,0.42)', cursor: hasPending ? 'pointer' : 'default', fontFamily: headingFont.style.fontFamily }}>
                    Apply filters
                </button>
                <button type="button" onClick={onReset}
                    className="inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-[12px] font-medium transition-colors"
                    style={{ background: 'rgba(255,255,255,0.03)', border: `1px solid ${BORDER}`, color: 'rgba(255,255,255,0.7)', fontFamily: headingFont.style.fontFamily }}>
                    <RotateCcw size={12} /> Reset
                </button>
            </div>

            {/* Footer — occupies the exact position of the feed's Filter button */}
            <div className="mt-3 h-[1px] shrink-0" style={{ background: 'rgba(255,255,255,0.05)' }} />
            <button type="button" onClick={onBack}
                className="group/back flex shrink-0 items-center justify-center gap-2 px-4 py-3.5 transition-colors">
                <ArrowLeft size={12} className="transition-transform group-hover/back:-translate-x-0.5" style={{ color: 'rgba(255,255,255,0.45)' }} />
                <span className="text-[10.5px] font-medium uppercase tracking-[0.12em] transition-colors group-hover/back:text-white"
                    style={{ color: TEXT_DIM, fontFamily: headingFont.style.fontFamily }}>
                    Back to live feed
                </span>
            </button>

            <style dangerouslySetInnerHTML={{ __html: `
        .feed-filter-scroll::-webkit-scrollbar { width: 3px; }
        .feed-filter-scroll::-webkit-scrollbar-track { background: transparent; }
        .feed-filter-scroll::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }
        .feed-filter-scroll:hover::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.14); }
      `}} />
        </div>
    );
}
