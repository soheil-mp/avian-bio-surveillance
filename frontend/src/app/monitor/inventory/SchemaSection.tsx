'use client';

import { useState, useTransition } from 'react';
import { fetchColumnProfile, type ColumnProfile } from './actions';
import { Loader2 } from 'lucide-react';

type Props = {
    tableKey: string;
    accentRgb: string;
    columnCount: number;
};

function NullBar({ pct }: { pct: number }) {
    const color = pct > 50 ? 'rgba(248,113,113,0.5)' : pct > 10 ? 'rgba(251,191,36,0.4)' : 'rgba(52,211,153,0.35)';
    return (
        <div className="w-[60px] h-[6px] rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.04)' }}>
            <div className="h-full rounded-full" style={{ width: `${Math.max(pct, 1)}%`, background: color }} />
        </div>
    );
}

export default function SchemaSection({ tableKey, accentRgb, columnCount }: Props) {
    const [profile, setProfile] = useState<ColumnProfile[] | null>(null);
    const [isPending, startTransition] = useTransition();
    const [loaded, setLoaded] = useState(false);

    function handleToggle(e: React.SyntheticEvent<HTMLDetailsElement>) {
        const open = (e.target as HTMLDetailsElement).open;
        if (open && !loaded) {
            setLoaded(true);
            startTransition(async () => {
                try {
                    const data = await fetchColumnProfile(tableKey);
                    setProfile(data);
                } catch { /* non-critical */ }
            });
        }
    }

    return (
        <details onToggle={handleToggle} className="pb-2">
            <summary
                    className="cursor-pointer select-none text-[12px] font-mono font-semibold uppercase tracking-wider mb-3 list-none flex items-center gap-1.5 group"
                    style={{ color: 'rgba(255,255,255,0.3)' }}
                >
                    <span className="transition-transform group-open:rotate-90 text-[10px]" style={{ color: 'rgba(255,255,255,0.2)' }}>▶</span>
                    Schema
                    <span className="text-[10px] font-normal" style={{ color: 'rgba(255,255,255,0.15)' }}>({columnCount} columns)</span>
                    {isPending && <Loader2 size={12} className="animate-spin ml-1" style={{ color: `rgba(${accentRgb},0.5)` }} />}
                </summary>

                {profile ? (
                    <div className="overflow-x-auto rounded-lg" style={{ border: '1px solid rgba(255,255,255,0.06)' }}>
                        <table className="w-full text-[11px] font-mono border-collapse">
                            <thead>
                                <tr style={{ background: 'rgba(255,255,255,0.03)' }}>
                                    {['Column', 'Type', 'Nullable', 'Null %', '', 'Distinct', 'Min', 'Max'].map((h, i) => (
                                        <th
                                            key={`${h}-${i}`}
                                            className={`px-3 py-2 text-left font-semibold whitespace-nowrap ${h === '' ? 'w-[80px]' : ''}`}
                                            style={{ color: 'rgba(255,255,255,0.4)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}
                                        >
                                            {h}
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {profile.map((col) => (
                                    <tr
                                        key={col.name}
                                        className="transition-colors hover:bg-white/[0.02]"
                                        style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
                                    >
                                        <td className="px-3 py-1.5 font-semibold" style={{ color: `rgba(${accentRgb},0.75)` }}>{col.name}</td>
                                        <td className="px-3 py-1.5" style={{ color: 'rgba(255,255,255,0.4)' }}>{col.dataType}</td>
                                        <td className="px-3 py-1.5" style={{ color: col.nullable ? 'rgba(251,191,36,0.6)' : 'rgba(255,255,255,0.25)' }}>
                                            {col.nullable ? 'yes' : 'no'}
                                        </td>
                                        <td className="px-3 py-1.5 whitespace-nowrap" style={{ color: col.nullPct > 50 ? 'rgba(248,113,113,0.7)' : col.nullPct > 10 ? 'rgba(251,191,36,0.6)' : 'rgba(255,255,255,0.4)' }}>
                                            {col.nullPct}%
                                        </td>
                                        <td className="px-3 py-1.5">
                                            <NullBar pct={col.nullPct} />
                                        </td>
                                        <td className="px-3 py-1.5" style={{ color: 'rgba(255,255,255,0.5)' }}>{col.distinctCount.toLocaleString()}</td>
                                        <td className="px-3 py-1.5 max-w-[160px] truncate" style={{ color: 'rgba(255,255,255,0.35)' }} title={col.minVal ?? ''}>{col.minVal ?? '—'}</td>
                                        <td className="px-3 py-1.5 max-w-[160px] truncate" style={{ color: 'rgba(255,255,255,0.35)' }} title={col.maxVal ?? ''}>{col.maxVal ?? '—'}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : isPending ? (
                    <div className="py-6 text-center text-[11px] font-mono" style={{ color: 'rgba(255,255,255,0.2)' }}>
                        Loading schema…
                    </div>
                ) : null}
            </details>
    );
}
