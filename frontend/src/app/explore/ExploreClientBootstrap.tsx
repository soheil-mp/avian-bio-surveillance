'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState } from 'react';

type ExploreClientDetection = {
  id: string;
  speciesId: string | null;
  time: string;
  species: string;
  scientificName: string | null;
  confidence: number;
  station: string;
  location: string | null;
  imageUrl: string | null;
  thumbnailUrl: string | null;
  speciesColor: string | null;
  startTime: number | null;
  endTime: number | null;
  clipDuration: number | null;
  anomalyScore: number | null;
  anomalySource: 'detection' | 'weekly' | null;
  bgTags: Array<{ label: string; score: number }>;
  audioUrls: {
    raw: string | null;
    processed: string | null;
    clip: string | null;
    direct: string | null;
  };
  visualUrls: {
    waveform: string | null;
    spectrogram: string | null;
  };
};

type ExplorePagePayload = {
  data: ExploreClientDetection[];
  currentPage: number;
  hasNextPage: boolean;
  pageSize: number;
  stationNotInDb: boolean;
  loadFailed: boolean;
};

const exploreBootstrapCache = new Map<string, ExplorePagePayload>();

const DataInventoryClient = dynamic(() => import('./DataInventoryClient'), {
  loading: () => (
    <div
      className="flex h-full items-center justify-center rounded-2xl border"
      style={{
        background: 'rgba(20,20,20,0.96)',
        borderColor: 'rgba(255,255,255,0.08)',
      }}
    >
      <span
        className="font-mono text-[11px] tracking-widest uppercase"
        style={{ color: 'rgba(255,255,255,0.2)' }}
      >
        Loading detections
      </span>
    </div>
  ),
});

function loadExploreData(queryString: string): Promise<ExplorePagePayload> {
  const cacheKey = queryString;
  const cached = exploreBootstrapCache.get(cacheKey);
  if (cached) {
    return Promise.resolve(cached);
  }

  const route = `/api/explore${queryString ? `?${queryString}` : ''}`;
  return fetch(route, { cache: 'no-store' })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`Explore bootstrap request failed with ${response.status}`);
      }

      return response.json() as Promise<ExplorePagePayload>;
    })
    .then((payload) => {
      exploreBootstrapCache.set(cacheKey, payload);
      return payload;
    });
}

export default function ExploreClientBootstrap({
  initialQueryString,
  initialPayload = null,
}: {
  initialQueryString: string;
  initialPayload?: ExplorePagePayload | null;
}) {
  const cachedPayload = exploreBootstrapCache.get(initialQueryString) ?? initialPayload;
  const [data, setData] = useState<ExplorePagePayload | null>(cachedPayload ?? null);
  const [status, setStatus] = useState<'loading' | 'error' | 'ready'>(
    cachedPayload ? 'ready' : 'loading',
  );

  useEffect(() => {
    let cancelled = false;

    if (initialPayload) {
      exploreBootstrapCache.set(initialQueryString, initialPayload);
      setData(initialPayload);
      setStatus('ready');
      return () => {
        cancelled = true;
      };
    }

    const cached = exploreBootstrapCache.get(initialQueryString);
    if (cached) {
      setData(cached);
      setStatus('ready');
      return () => {
        cancelled = true;
      };
    }

    setStatus('loading');
    setData(null);

    loadExploreData(initialQueryString)
      .then((payload) => {
        if (cancelled) {
          return;
        }

        setData(payload);
        setStatus('ready');
      })
      .catch((error) => {
        if (cancelled) {
          return;
        }

        console.error('[explore] failed to bootstrap detections', error);
        setStatus('error');
      });

    return () => {
      cancelled = true;
    };
  }, [initialPayload, initialQueryString]);

  if (data) {
    return (
      <DataInventoryClient
        initialData={data.data}
        currentPage={data.currentPage}
        hasNextPage={data.hasNextPage}
        pageSize={data.pageSize}
        stationNotInDb={data.stationNotInDb}
        loadFailed={data.loadFailed}
      />
    );
  }

  return (
    <div
      className="flex h-full items-center justify-center rounded-2xl border"
      style={{
        background: 'rgba(20,20,20,0.96)',
        borderColor: 'rgba(255,255,255,0.08)',
      }}
    >
      <span
        className="font-mono text-[11px] tracking-widest uppercase"
        style={{ color: status === 'error' ? 'rgba(255,255,255,0.46)' : 'rgba(255,255,255,0.2)' }}
      >
        {status === 'error' ? 'Explore data unavailable' : 'Loading detections'}
      </span>
    </div>
  );
}