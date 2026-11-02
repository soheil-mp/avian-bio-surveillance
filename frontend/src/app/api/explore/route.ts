import { NextResponse } from 'next/server';

import { loadExplorePageData } from '@/app/explore/data';
import { withMeasuredApiRoute } from '@/lib/performance/server';

export const dynamic = 'force-dynamic';

async function handleGet(request: Request) {
  const url = new URL(request.url);
  const searchParams: Record<string, string | string[] | undefined> = {};

  for (const [key, value] of url.searchParams.entries()) {
    const existing = searchParams[key];
    if (existing == null) {
      searchParams[key] = value;
      continue;
    }

    searchParams[key] = Array.isArray(existing)
      ? [...existing, value]
      : [existing, value];
  }

  const data = await loadExplorePageData(searchParams);

  return NextResponse.json(
    {
      data: data.data,
      currentPage: data.currentPage,
      hasNextPage: data.hasNextPage,
      pageSize: data.pageSize,
      stationNotInDb: data.stationNotInDb,
      loadFailed: data.loadFailed,
    },
    {
      headers: {
        'Cache-Control': 'private, no-store, max-age=0, must-revalidate',
      },
    },
  );
}

export const GET = withMeasuredApiRoute(handleGet);
