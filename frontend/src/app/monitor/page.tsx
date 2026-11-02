import Link from 'next/link';
import { Layers, Zap, ShieldCheck, ArrowRight } from 'lucide-react';

/* ── card definitions ─────────────────────────────────────── */

const cards = [
    {
        href: '/monitor/inventory',
        Icon: Layers,
        title: 'Data Inventory',
        desc: 'Browse every dataset in the database — row counts, date ranges, storage metrics, and key dimensions for each table.',
    },
    {
        href: '/monitor/pipelines',
        Icon: Zap,
        title: 'Pipelines & Schedules',
        desc: 'View ingestion schedules, check data freshness, inspect pipeline configuration, and trigger manual runs.',
    },
    {
        href: '/monitor/quality',
        Icon: ShieldCheck,
        title: 'Data Quality',
        desc: 'Assess quality across six dimensions — completeness, timeliness, accuracy, consistency, uniqueness, and validity.',
    },
] as const;

/* ── page ──────────────────────────────────────────────────── */

export default function MonitorPage() {
    return (
        <div className="h-full w-full overflow-y-auto flex flex-col">
            {/* header */}
            <div className="px-10 md:px-16 lg:px-24 xl:px-32 pt-16 pb-1">
                <h1 className="text-[17px] font-semibold tracking-tight text-white">Monitor</h1>
                <p className="text-[12.5px] mt-1" style={{ color: 'rgba(255,255,255,0.3)' }}>
                    Data inventory, ingestion pipelines, and quality metrics
                </p>
            </div>

            {/* cards — centered */}
            <div className="flex-1 flex items-start justify-center px-10 md:px-16 lg:px-24 xl:px-32 py-7">
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 w-full">
                    {cards.map((c) => (
                        <Link
                            key={c.href}
                            href={c.href}
                            className="group relative flex flex-col rounded-xl overflow-hidden transition-all duration-200 hover:-translate-y-0.5"
                            style={{
                                background: 'rgba(255,255,255,0.018)',
                                border: '1px solid rgba(255,255,255,0.06)',
                            }}
                        >
                            {/* top strip */}
                            <div className="h-[2px] shrink-0" style={{ background: 'rgba(255,255,255,0.06)' }} />

                            <div className="p-5 flex flex-col flex-1">
                                <div className="flex items-start justify-between mb-3">
                                    <div
                                        className="w-8 h-8 rounded-lg flex items-center justify-center"
                                        style={{
                                            background: 'rgba(255,255,255,0.04)',
                                            border: '1px solid rgba(255,255,255,0.07)',
                                        }}
                                    >
                                        <c.Icon size={15} style={{ color: 'rgba(255,255,255,0.45)' }} strokeWidth={1.6} />
                                    </div>
                                    <ArrowRight
                                        size={13}
                                        className="text-white/10 group-hover:text-white/40 transition-colors mt-0.5"
                                    />
                                </div>
                                <h2 className="text-[16px] font-semibold text-white tracking-tight mb-1.5">
                                    {c.title}
                                </h2>
                                <p className="text-[11.5px] leading-[1.6]" style={{ color: 'rgba(255,255,255,0.30)' }}>
                                    {c.desc}
                                </p>
                            </div>

                            {/* hover border */}
                            <div
                                className="absolute inset-0 rounded-xl pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity duration-200"
                                style={{ border: '1px solid rgba(255,255,255,0.12)' }}
                            />
                        </Link>
                    ))}
                </div>
            </div>
        </div>
    );
}
