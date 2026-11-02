'use client';

import { useState, useTransition, useCallback, useRef } from 'react';
import { fetchPage, type Filter, type SortDir } from './actions';
import {
    ChevronLeft, ChevronRight, Loader2, ArrowUp, ArrowDown,
    X, Search, Download, Columns3,
} from 'lucide-react';

const PAGE_SIZES = [25, 50, 100] as const;

type Props = {
    tableKey: string;
    accentRgb: string;
    initialColumns: string[];
    initialRows: Record<string, unknown>[];
    totalRows: number;
    pageSize: number;
};

function fmt(v: unknown): string {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(v)) {
        return new Date(v).toLocaleString('en-GB', {
            day: '2-digit', month: 'short', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    }
    if (typeof v === 'number') return v % 1 === 0 ? v.toLocaleString() : v.toFixed(4);
    const s = String(v);
    return s.length > 80 ? s.slice(0, 77) + '…' : s;
}

/** Raw value for CSV — no formatting, no truncation */
function rawVal(v: unknown): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string' && v.includes(',')) return `"${v.replace(/"/g, '""')}"`;
    return String(v);
}

export default function DataTable({ tableKey, accentRgb, initialColumns, initialRows, totalRows, pageSize: initialPageSize }: Props) {
    const allColumns = initialColumns;
    const [visibleCols, setVisibleCols] = useState<Set<string>>(new Set(initialColumns));
    const [showColPicker, setShowColPicker] = useState(false);
    const [rows, setRows] = useState(initialRows);
    const [total, setTotal] = useState(totalRows);
    const [page, setPage] = useState(0);
    const [currentPageSize, setCurrentPageSize] = useState(initialPageSize);
    const [isPending, startTransition] = useTransition();

    /* ── filters ──────────────────────────────── */
    const [filters, setFilters] = useState<Record<string, string>>({});
    const [activeFilters, setActiveFilters] = useState<Filter[]>([]);
    const [showFilters, setShowFilters] = useState(false);
    const debounceRef = useRef<ReturnType<typeof setTimeout>>(null);

    /* ── sorting ──────────────────────────────── */
    const [sortCol, setSortCol] = useState<string | undefined>(undefined);
    const [sortDir, setSortDir] = useState<SortDir>('desc');

    const columns = allColumns.filter((c) => visibleCols.has(c));
    const totalPages = Math.max(1, Math.ceil(total / currentPageSize));
    const from = total === 0 ? 0 : page * currentPageSize + 1;
    const to = Math.min((page + 1) * currentPageSize, total);

    const doFetch = useCallback((p: number, ps: number, f: Filter[], sc?: string, sd: SortDir = 'desc') => {
        startTransition(async () => {
            const res = await fetchPage(tableKey, p, ps, f, sc, sd);
            setRows(res.rows);
            setTotal(res.total);
            setPage(p);
        });
    }, [tableKey, startTransition]);

    function handleSort(col: string) {
        const newDir: SortDir = sortCol === col && sortDir === 'desc' ? 'asc' : 'desc';
        setSortCol(col);
        setSortDir(newDir);
        doFetch(0, currentPageSize, activeFilters, col, newDir);
    }

    function handleFilterChange(col: string, value: string) {
        const next = { ...filters, [col]: value };
        setFilters(next);

        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
            const newFilters = Object.entries(next)
                .filter(([, v]) => v.trim() !== '')
                .map(([column, v]) => ({ column, value: v }));
            setActiveFilters(newFilters);
            doFetch(0, currentPageSize, newFilters, sortCol, sortDir);
        }, 400);
    }

    function clearFilters() {
        setFilters({});
        setActiveFilters([]);
        doFetch(0, currentPageSize, [], sortCol, sortDir);
    }

    function handlePageSizeChange(size: number) {
        setCurrentPageSize(size);
        doFetch(0, size, activeFilters, sortCol, sortDir);
    }

    function toggleColumn(col: string) {
        setVisibleCols((prev) => {
            const next = new Set(prev);
            if (next.has(col)) { if (next.size > 1) next.delete(col); } else { next.add(col); }
            return next;
        });
    }

    function exportCsv() {
        const header = columns.map((c) => rawVal(c)).join(',');
        const body = rows.map((row) => columns.map((c) => rawVal(row[c])).join(',')).join('\n');
        const blob = new Blob([header + '\n' + body], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${tableKey}_page${page + 1}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    const activeCount = activeFilters.length;
    const hiddenCount = allColumns.length - visibleCols.size;

    return (
        <div className="flex flex-col gap-3">
            {/* ── toolbar ─────────────────────────────── */}
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-3">
                    <span className="text-[11px] font-mono" style={{ color: 'rgba(255,255,255,0.3)' }}>
                        {total === 0 ? 'No rows' : `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()} rows`}
                    </span>
                    {activeCount > 0 && (
                        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded" style={{ background: `rgba(${accentRgb},0.1)`, color: `rgba(${accentRgb},0.7)` }}>
                            {activeCount} filter{activeCount > 1 ? 's' : ''} active
                        </span>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    {isPending && <Loader2 size={13} className="animate-spin" style={{ color: `rgba(${accentRgb},0.6)` }} />}

                    {/* page size selector */}
                    <select
                        value={currentPageSize}
                        onChange={(e) => handlePageSizeChange(Number(e.target.value))}
                        className="px-1.5 py-1 rounded-md text-[10px] font-mono outline-none cursor-pointer appearance-none"
                        style={{
                            background: 'rgba(255,255,255,0.04)',
                            color: 'rgba(255,255,255,0.4)',
                            border: '1px solid rgba(255,255,255,0.06)',
                        }}
                    >
                        {PAGE_SIZES.map((s) => (
                            <option key={s} value={s} style={{ background: '#1a1a1a' }}>{s} rows</option>
                        ))}
                    </select>

                    {/* column visibility toggle */}
                    <div className="relative">
                        <button
                            onClick={() => setShowColPicker(!showColPicker)}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[10.5px] font-mono transition-colors"
                            style={{
                                background: showColPicker ? `rgba(${accentRgb},0.1)` : 'rgba(255,255,255,0.04)',
                                color: showColPicker ? `rgba(${accentRgb},0.8)` : 'rgba(255,255,255,0.4)',
                            }}
                        >
                            <Columns3 size={11} />
                            Columns
                            {hiddenCount > 0 && (
                                <span className="text-[9px] px-1 rounded" style={{ background: `rgba(${accentRgb},0.15)`, color: `rgba(${accentRgb},0.8)` }}>
                                    -{hiddenCount}
                                </span>
                            )}
                        </button>
                        {showColPicker && (
                            <div
                                className="absolute right-0 top-full mt-1 z-50 rounded-lg p-2 max-h-[280px] overflow-y-auto min-w-[180px]"
                                style={{ background: '#1a1a1a', border: '1px solid rgba(255,255,255,0.08)', boxShadow: '0 8px 24px rgba(0,0,0,0.5)' }}
                            >
                                {allColumns.map((col) => (
                                    <label
                                        key={col}
                                        className="flex items-center gap-2 px-2 py-1 rounded cursor-pointer transition-colors hover:bg-white/[0.04]"
                                    >
                                        <input
                                            type="checkbox"
                                            checked={visibleCols.has(col)}
                                            onChange={() => toggleColumn(col)}
                                            className="accent-emerald-500"
                                        />
                                        <span className="text-[10px] font-mono truncate" style={{ color: 'rgba(255,255,255,0.55)' }}>{col}</span>
                                    </label>
                                ))}
                            </div>
                        )}
                    </div>

                    {/* filter toggle */}
                    <button
                        onClick={() => setShowFilters(!showFilters)}
                        className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[10.5px] font-mono transition-colors"
                        style={{
                            background: showFilters ? `rgba(${accentRgb},0.1)` : 'rgba(255,255,255,0.04)',
                            color: showFilters ? `rgba(${accentRgb},0.8)` : 'rgba(255,255,255,0.4)',
                        }}
                    >
                        <Search size={11} />
                        Filters
                    </button>

                    {activeCount > 0 && (
                        <button
                            onClick={clearFilters}
                            className="flex items-center gap-1 px-2 py-1.5 rounded-md text-[10px] font-mono transition-colors"
                            style={{ background: 'rgba(255,255,255,0.04)', color: 'rgba(255,255,255,0.35)' }}
                        >
                            <X size={10} /> Clear
                        </button>
                    )}

                    {/* CSV export */}
                    <button
                        onClick={exportCsv}
                        className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[10.5px] font-mono transition-colors"
                        style={{ background: 'rgba(255,255,255,0.04)', color: 'rgba(255,255,255,0.4)' }}
                        title="Export current page as CSV"
                    >
                        <Download size={11} />
                        CSV
                    </button>

                    {/* pagination */}
                    <div className="flex items-center gap-1 ml-1">
                        <button
                            onClick={() => doFetch(page - 1, currentPageSize, activeFilters, sortCol, sortDir)}
                            disabled={page === 0 || isPending}
                            className="p-1.5 rounded-md transition-colors disabled:opacity-25"
                            style={{ background: 'rgba(255,255,255,0.04)' }}
                        >
                            <ChevronLeft size={13} style={{ color: 'rgba(255,255,255,0.5)' }} />
                        </button>
                        <span className="text-[11px] font-mono px-1.5 tabular-nums" style={{ color: 'rgba(255,255,255,0.35)' }}>
                            {page + 1} / {totalPages}
                        </span>
                        <button
                            onClick={() => doFetch(page + 1, currentPageSize, activeFilters, sortCol, sortDir)}
                            disabled={page >= totalPages - 1 || isPending}
                            className="p-1.5 rounded-md transition-colors disabled:opacity-25"
                            style={{ background: 'rgba(255,255,255,0.04)' }}
                        >
                            <ChevronRight size={13} style={{ color: 'rgba(255,255,255,0.5)' }} />
                        </button>
                    </div>
                </div>
            </div>

            {/* ── table ───────────────────────────────── */}
            <div
                className="overflow-x-auto rounded-lg"
                style={{ border: '1px solid rgba(255,255,255,0.06)' }}
            >
                <table className="w-full text-[11px] font-mono border-collapse">
                    <thead>
                        {/* column headers */}
                        <tr style={{ background: 'rgba(255,255,255,0.03)' }}>
                            {columns.map((col) => {
                                const isActive = sortCol === col;
                                return (
                                    <th
                                        key={col}
                                        onClick={() => handleSort(col)}
                                        className="px-3 py-2.5 text-left font-semibold whitespace-nowrap cursor-pointer select-none group"
                                        style={{
                                            color: isActive ? `rgba(${accentRgb},0.8)` : 'rgba(255,255,255,0.45)',
                                            borderBottom: '1px solid rgba(255,255,255,0.06)',
                                        }}
                                    >
                                        <span className="inline-flex items-center gap-1">
                                            {col}
                                            {isActive ? (
                                                sortDir === 'asc'
                                                    ? <ArrowUp size={10} style={{ color: `rgba(${accentRgb},0.6)` }} />
                                                    : <ArrowDown size={10} style={{ color: `rgba(${accentRgb},0.6)` }} />
                                            ) : (
                                                <ArrowDown size={10} className="opacity-0 group-hover:opacity-30 transition-opacity" />
                                            )}
                                        </span>
                                    </th>
                                );
                            })}
                        </tr>

                        {/* filter row */}
                        {showFilters && (
                            <tr style={{ background: 'rgba(255,255,255,0.015)' }}>
                                {columns.map((col) => (
                                    <th key={col} className="px-2 py-1.5" style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
                                        <input
                                            type="text"
                                            value={filters[col] ?? ''}
                                            onChange={(e) => handleFilterChange(col, e.target.value)}
                                            placeholder="Filter…"
                                            className="w-full min-w-[60px] px-2 py-1 rounded text-[10px] font-mono outline-none transition-colors placeholder:text-white/15"
                                            style={{
                                                background: 'rgba(255,255,255,0.03)',
                                                color: 'rgba(255,255,255,0.6)',
                                                border: `1px solid ${filters[col] ? `rgba(${accentRgb},0.25)` : 'rgba(255,255,255,0.06)'}`,
                                            }}
                                        />
                                    </th>
                                ))}
                            </tr>
                        )}
                    </thead>
                    <tbody>
                        {rows.length === 0 ? (
                            <tr>
                                <td
                                    colSpan={columns.length}
                                    className="px-3 py-10 text-center"
                                    style={{ color: 'rgba(255,255,255,0.25)' }}
                                >
                                    {activeCount > 0 ? 'No rows match the current filters' : 'No data available'}
                                </td>
                            </tr>
                        ) : (
                            rows.map((row, i) => (
                                <tr
                                    key={i}
                                    className="transition-colors hover:bg-white/[0.02]"
                                    style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
                                >
                                    {columns.map((col) => (
                                        <td
                                            key={col}
                                            className="px-3 py-2 whitespace-nowrap max-w-[220px] truncate"
                                            style={{ color: 'rgba(255,255,255,0.55)' }}
                                            title={String(row[col] ?? '')}
                                        >
                                            {fmt(row[col])}
                                        </td>
                                    ))}
                                </tr>
                            ))
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
