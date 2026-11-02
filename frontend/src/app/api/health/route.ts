import { NextResponse } from 'next/server';

import { withMeasuredApiRoute } from '@/lib/performance/server';

/** Lightweight liveness probe — no DB queries. */
const handleGet = (_request: Request) => {
    return NextResponse.json({ status: 'ok' });
};

export const GET = withMeasuredApiRoute(handleGet);
