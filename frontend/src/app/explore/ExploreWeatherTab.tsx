'use client';

import dynamic from 'next/dynamic';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useState } from 'react';

import {
  normalizeWeatherDate,
  resolveWeatherSelectedDate,
  type ExploreWeatherPayload,
} from './weather';

const WEATHER_SURFACE = {
  panelDeep: '#101010',
  borderSoft: '#232323',
} as const;

const WEATHER_PAYLOAD_CACHE_VERSION = 'v4';

const ExploreWeatherMap = dynamic(() => import('./ExploreWeatherMap'), {
  ssr: false,
  loading: () => (
    <div
      className="flex min-h-0 flex-1 items-center justify-center rounded-xl text-sm"
      style={{
        background: WEATHER_SURFACE.panelDeep,
        border: `1px solid ${WEATHER_SURFACE.borderSoft}`,
        color: 'rgba(255,255,255,0.44)',
      }}
    >
      Loading weather map...
    </div>
  ),
});

const weatherPayloadCache = new Map<string, ExploreWeatherPayload>();

function weatherPayloadCacheKey(key: string) {
  return `${WEATHER_PAYLOAD_CACHE_VERSION}:${key}`;
}

function loadExploreWeatherData(weatherDate: string | null) {
  const cacheKey = weatherPayloadCacheKey(weatherDate ?? 'auto');
  const cached = weatherPayloadCache.get(cacheKey);
  if (cached) {
    return Promise.resolve(cached);
  }

  const query = weatherDate ? `?dateFrom=${weatherDate}&dateTo=${weatherDate}` : '';
  const route = `/api/explore/weather${query}`;
  return fetch(route, { cache: 'force-cache' })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`Weather request failed with ${response.status}`);
      }

      return response.json() as Promise<ExploreWeatherPayload>;
    })
    .then((payload) => {
      weatherPayloadCache.set(cacheKey, payload);
      weatherPayloadCache.set(weatherPayloadCacheKey(payload.dateTo), payload);
      weatherPayloadCache.set(weatherPayloadCacheKey(`${payload.dateFrom}:${payload.dateTo}`), payload);
      return payload;
    });
}

export default function ExploreWeatherTab() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const rawWeatherDate = searchParams.get('weatherDate');
  const rawDateFrom = searchParams.get('weatherDateFrom');
  const rawDateTo = searchParams.get('weatherDateTo');
  const hasLegacyDateRange = Boolean(rawDateFrom || rawDateTo);
  const normalizedWeatherDate = normalizeWeatherDate(rawWeatherDate);
  const selectedWeatherDate = normalizedWeatherDate
    ?? (hasLegacyDateRange ? resolveWeatherSelectedDate(null, rawDateFrom, rawDateTo) : null);
  const dataKey = weatherPayloadCacheKey(selectedWeatherDate ?? 'auto');
  const cachedPayload = weatherPayloadCache.get(dataKey)
    ?? (selectedWeatherDate ? weatherPayloadCache.get(weatherPayloadCacheKey(`${selectedWeatherDate}:${selectedWeatherDate}`)) : null)
    ?? null;
  const [data, setData] = useState<ExploreWeatherPayload | null>(cachedPayload);
  const [status, setStatus] = useState<'loading' | 'error' | 'ready'>(cachedPayload ? 'ready' : 'loading');

  useEffect(() => {
    let cancelled = false;

    const cached = weatherPayloadCache.get(dataKey);
    if (cached) {
      setData(cached);
      setStatus('ready');
      return () => {
        cancelled = true;
      };
    }

    setStatus('loading');
    loadExploreWeatherData(selectedWeatherDate)
      .then((payload) => {
        if (cancelled) {
          return;
        }

        setData(payload);
        setStatus('ready');

        if (rawWeatherDate !== payload.dateTo || hasLegacyDateRange) {
          const nextParams = new URLSearchParams(searchParams.toString());
          nextParams.set('tab', 'weather');
          nextParams.delete('weatherDateFrom');
          nextParams.delete('weatherDateTo');
          nextParams.set('weatherDate', payload.dateTo);
          const nextQuery = nextParams.toString();
          router.replace(nextQuery ? `${pathname}?${nextQuery}` : pathname);
        }
      })
      .catch((error) => {
        if (cancelled) {
          return;
        }

        console.error('[explore/weather] failed to load weather data', error);
        setStatus('error');
      });

    return () => {
      cancelled = true;
    };
  }, [dataKey, hasLegacyDateRange, pathname, rawWeatherDate, router, searchParams, selectedWeatherDate]);

  const municipalities = data?.municipalities ?? [];

  return (
    <div className="h-full min-h-0">
      {status === 'error' ? (
        <div className="flex h-full items-center justify-center px-6 text-sm" style={{ color: 'rgba(255,255,255,0.46)' }}>
          Weather data could not be loaded from the local stack.
        </div>
      ) : status === 'loading' && !data ? (
        <div className="flex h-full items-center justify-center text-sm" style={{ color: 'rgba(255,255,255,0.44)' }}>
          Loading municipality weather...
        </div>
      ) : municipalities.length === 0 ? (
        <div className="flex h-full items-center justify-center px-6 text-sm" style={{ color: 'rgba(255,255,255,0.46)' }}>
          No municipality weather features are available for the current window.
        </div>
      ) : (
        <ExploreWeatherMap municipalities={municipalities} windVectors={data?.windVectors ?? []} />
      )}
    </div>
  );
}