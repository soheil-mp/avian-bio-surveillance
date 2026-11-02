'use client';

import Image from 'next/image';
import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import { useState, useRef, useEffect, useCallback } from 'react';
import { Play, Square } from 'lucide-react';
import type WaveSurfer from 'wavesurfer.js';
import SpeciesAvatar from './SpeciesAvatar';
import { useDetectionSeekKeyboard } from './useDetectionSeekKeyboard';
import {
  type AudioGraph,
  type AudioInsight,
  buildAudioInsight,
  cleanupAudioGraph,
  prepareHearingAssist,
} from '@/lib/audioHearingAssist';

// ─── Types ───────────────────────────────────────────────────────────────────
type AudioUrls = { raw: string | null; processed: string | null; clip: string | null; direct: string | null };
type VisualUrls = { waveform: string | null; spectrogram: string | null };
type BgTag = { label: string; score: number };
type AudioSource = { kind: 'processed' | 'clip' | 'raw' | 'direct'; url: string };
type VisualMode = 'spectrogram' | 'waveform';
type DetectionData = {
  id: string; speciesId: string | null; time: string; species: string; scientificName: string | null;
  confidence: number; station: string; location: string | null;
  imageUrl: string | null;
  thumbnailUrl: string | null; speciesColor: string | null;
  startTime: number | null; endTime: number | null; clipDuration: number | null;
  anomalyScore: number | null;
  anomalySource: 'detection' | 'weekly' | null;
  bgTags: BgTag[];
  audioUrls: AudioUrls;
  visualUrls: VisualUrls;
};

// ─── Constants ───────────────────────────────────────────────────────────────
const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

const prefetchedResources = new Set<string>();

function preloadImageResource(url: string) {
  if (typeof document === 'undefined') return;
  const key = `image:${url}`;
  if (prefetchedResources.has(key)) return;
  prefetchedResources.add(key);

  const link = document.createElement('link');
  link.rel = 'preload';
  link.as = 'image';
  link.href = url;
  document.head.appendChild(link);
}

function resolveAudioSources(u: AudioUrls): AudioSource[] {
  const candidates: AudioSource[] = [
    { kind: 'processed', url: u.processed ?? '' },
    { kind: 'clip', url: u.clip ?? '' },
    { kind: 'raw', url: u.raw ?? '' },
    { kind: 'direct', url: u.direct ?? '' },
  ];

  const seen = new Set<string>();
  return candidates.filter((candidate) => {
    const url = candidate.url.trim();
    if (!url || seen.has(url)) return false;
    seen.add(url);
    candidate.url = url;
    return true;
  });
}

// ─── Card ────────────────────────────────────────────────────────────────────
function DetectionRow({ row, isPlaying, onPlayToggle, showRegion, visualMode }: {
  row: DetectionData; isPlaying: boolean;
  onPlayToggle: (id: string, play: boolean) => void;
  showRegion: boolean;
  visualMode: VisualMode;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const assistAbortRef = useRef<AbortController | null>(null);
  const assistPromiseRef = useRef<Promise<void> | null>(null);
  const audioGraphRef = useRef<AudioGraph | null>(null);
  const [isReady, setIsReady] = useState(false);
  const [audioError, setAudioError] = useState(false);
  const [audioInsight, setAudioInsight] = useState<AudioInsight | null>(null);
  const [waveformError, setWaveformError] = useState(false);
  const [spectrogramError, setSpectrogramError] = useState(false);
  const [isNearViewport, setIsNearViewport] = useState(false);
  const [loadRequested, setLoadRequested] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [activeAudioSourceKind, setActiveAudioSourceKind] = useState<AudioSource['kind'] | null>(null);

  const audioSources = resolveAudioSources(row.audioUrls);
  const waveformUrl = row.visualUrls.waveform;
  const spectrogramUrl = row.visualUrls.spectrogram;
  const hasAudio = audioSources.length > 0;
  const hasWaveform = !!waveformUrl && !waveformError;
  const hasSpectrogram = !!spectrogramUrl && !spectrogramError;
  const hasSavedVisual = hasWaveform || hasSpectrogram;
  const hasWaveformVisual = hasWaveform;
  const hasSpectrogramVisual = hasSpectrogram;
  const hasAnyVisual = hasSavedVisual;
  const isAvailable = hasAudio && !audioError;
  const pct = Math.round(row.confidence * 100);
  const confBright = pct >= 85 ? 0.85 : pct >= 70 ? 0.6 : pct >= 55 ? 0.4 : 0.25;
  const anomalyScoreLabel = row.anomalyScore != null ? row.anomalyScore.toFixed(2) : null;
  const anomalyTone = row.anomalyScore == null
    ? {
        background: 'rgba(255,255,255,0.03)',
        border: 'rgba(255,255,255,0.07)',
        color: 'rgba(255,255,255,0.4)',
      }
    : row.anomalyScore >= 0.65
      ? {
          background: 'rgba(239,68,68,0.12)',
          border: 'rgba(239,68,68,0.2)',
          color: 'rgba(254,202,202,0.92)',
        }
      : row.anomalyScore >= 0.35
        ? {
            background: 'rgba(245,158,11,0.1)',
            border: 'rgba(245,158,11,0.18)',
            color: 'rgba(253,230,138,0.9)',
          }
        : {
            background: 'rgba(255,255,255,0.05)',
            border: 'rgba(255,255,255,0.1)',
            color: 'rgba(255,255,255,0.72)',
          };

  useEffect(() => {
    setWaveformError(false);
    setSpectrogramError(false);
  }, [waveformUrl, spectrogramUrl]);

  useEffect(() => {
    const node = rowRef.current;
    if (!node || typeof IntersectionObserver === 'undefined') {
      setIsNearViewport(true);
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setIsNearViewport(true);
          observer.disconnect();
        }
      },
      { rootMargin: '240px 0px' },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!isNearViewport || !hasSavedVisual) return;

    const timer = window.setTimeout(() => {
      if (hasWaveform && waveformUrl) preloadImageResource(waveformUrl);
      if (hasSpectrogram && spectrogramUrl) preloadImageResource(spectrogramUrl);
    }, 150);

    return () => window.clearTimeout(timer);
  }, [hasSavedVisual, hasSpectrogram, hasWaveform, isNearViewport, spectrogramUrl, waveformUrl]);

  // WaveSurfer init — deferred until first play to avoid loading 20 audio files at once
  const loadRequestedRef = useRef(false);
  const initWaveSurfer = useCallback(() => {
    if (loadRequestedRef.current || !containerRef.current || audioSources.length === 0) {
      return;
    }
    loadRequestedRef.current = true;
    setLoadRequested(true);

    const failWaveSurferLoad = () => {
      loadRequestedRef.current = false;
      setLoadRequested(false);
      setActiveAudioSourceKind(null);
      setAudioInsight(null);
      assistAbortRef.current?.abort();
      assistPromiseRef.current = null;
      setAudioError(true);
      setIsReady(false);
      onPlayToggle(row.id, false);
    };

    void import('wavesurfer.js').then(({ default: WaveSurferLib }) => {
      const startWaveSurfer = (sourceIndex: number) => {
        const source = audioSources[sourceIndex];
        if (!containerRef.current || !source) {
          failWaveSurferLoad();
          return;
        }

        setAudioError(false); setIsReady(false); setCurrentTime(0); setDuration(0);
        setActiveAudioSourceKind(source.kind);
        setAudioInsight(null);

        abortRef.current?.abort();
        abortRef.current = new AbortController();
        assistAbortRef.current?.abort();
        try { wsRef.current?.destroy(); } catch { }

        const ws = WaveSurferLib.create({
          container: containerRef.current,
          waveColor: 'transparent',
          progressColor: 'transparent',
          cursorColor: 'transparent',
          cursorWidth: 0,
          height: 1,
          normalize: true,
          // MediaElement keeps presigned MinIO clips playable even when the bucket does not expose CORS headers.
          backend: 'MediaElement',
          fetchParams: { signal: abortRef.current.signal },
          plugins: []
        });

        const mediaElement = ws.getMediaElement();
        const assistAbort = new AbortController();
        assistAbortRef.current = assistAbort;
        assistPromiseRef.current = (async () => {
          try {
            const analysis = await prepareHearingAssist(mediaElement, source.url, audioGraphRef, assistAbort.signal);
            if (assistAbort.signal.aborted) return;
            setAudioInsight(buildAudioInsight(analysis));
          } catch (error) {
            if ((error as { name?: string })?.name === 'AbortError') {
              return;
            }
            setAudioInsight(null);
          }
        })();

        const syncCurrentTime = () => {
          const current = ws.getCurrentTime();
          setCurrentTime(current);
          if ((source.kind === 'raw' || source.kind === 'direct') && row.endTime != null && current >= row.endTime) {
            ws.pause();
            onPlayToggle(row.id, false);
          }
        };

        let handledFailure = false;
        const handleLoadFailure = () => {
          if (handledFailure) return;
          handledFailure = true;

          if (sourceIndex + 1 < audioSources.length) {
            startWaveSurfer(sourceIndex + 1);
            return;
          }

          failWaveSurferLoad();
        };

        ws.on('ready', () => {
          const loadedDuration = ws.getDuration();
          setIsReady(true);
          setDuration(loadedDuration);

          if ((source.kind === 'raw' || source.kind === 'direct') && row.startTime != null && loadedDuration > 0) {
            const seekStart = Math.max(0, Math.min(row.startTime, loadedDuration));
            if (seekStart > 0) {
              ws.seekTo(seekStart / loadedDuration);
              setCurrentTime(seekStart);
            }
          }
        });
        ws.on('timeupdate', syncCurrentTime);
        ws.on('audioprocess', syncCurrentTime);
        ws.on('finish', () => onPlayToggle(row.id, false));
        ws.on('error', (err) => {
          if (err?.name === 'AbortError') return;
          handleLoadFailure();
        });

        wsRef.current = ws;
        void ws.load(source.url).catch((err) => {
          if (err?.name === 'AbortError') return;
          handleLoadFailure();
        });
      };

      startWaveSurfer(0);
    }).catch(() => {
      failWaveSurferLoad();
    });
  }, [audioSources, hasSavedVisual, onPlayToggle, row.endTime, row.id, row.startTime]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      assistAbortRef.current?.abort();
      assistPromiseRef.current = null;
      setActiveAudioSourceKind(null);
      cleanupAudioGraph(audioGraphRef);
      try { wsRef.current?.destroy(); } catch { }
    };
  }, []);

  // Auto-init when play is requested externally
  useEffect(() => {
    if (isPlaying && !loadRequestedRef.current) {
      initWaveSurfer();
    }
  }, [isPlaying, initWaveSurfer]);

  useEffect(() => {
    const ws = wsRef.current;
    if (!ws || !isReady) return;
    let cancelled = false;

    if (isPlaying) {
      void (async () => {
        try {
          await (assistPromiseRef.current ?? Promise.resolve());
        } catch {
          // Ignore assist preparation failures and keep playback functional.
        }

        if (cancelled || wsRef.current !== ws || !isPlaying) {
          return;
        }
        ws.play().catch(() => { });
      })();
    } else {
      ws.pause();
    }

    return () => {
      cancelled = true;
    };
  }, [isPlaying, isReady]);

  const seekByDelta = useCallback((delta: number) => {
    const ws = wsRef.current;
    if (!ws || !isReady) return;

    const loadedDuration = ws.getDuration();
    if (loadedDuration <= 0) return;

    const nextTime = Math.max(
      0,
      Math.min(loadedDuration, ws.getCurrentTime() + delta),
    );
    ws.seekTo(nextTime / loadedDuration);
    setCurrentTime(nextTime);
  }, [isReady]);

  useDetectionSeekKeyboard({
    detectionId: row.id,
    enabled: isReady,
    onSeek: seekByDelta,
  });

  const handleSeek = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!hasAudio) return;
    // Lazy-init WaveSurfer on first click
    if (!loadRequestedRef.current) {
      initWaveSurfer();
      onPlayToggle(row.id, true);
      return;
    }
    if (!isReady || !wsRef.current || duration === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    wsRef.current.seekTo(pct);
    setCurrentTime(wsRef.current.getCurrentTime());
    if (!isPlaying) onPlayToggle(row.id, true);
  };

  const startPct = isReady && duration > 0 && row.startTime != null ? (row.startTime / duration) * 100 : null;
  const endPct = isReady && duration > 0 && row.endTime != null ? (row.endTime / duration) * 100 : null;
  const progressPct = isReady && duration > 0 ? Math.max(0, Math.min(100, (currentTime / duration) * 100)) : 0;
  const speciesLabel = row.species?.trim() || 'Unknown species';
  const showRegionOverlay =
    showRegion &&
    startPct != null &&
    endPct != null &&
    endPct > startPct;
  const activeVisualMode = visualMode === 'waveform' && !hasWaveform
    ? hasWaveformVisual
      ? 'waveform'
      : hasSpectrogramVisual
        ? 'spectrogram'
        : 'waveform'
    : visualMode === 'spectrogram' && !hasSpectrogram
      ? hasSpectrogramVisual
        ? 'spectrogram'
        : hasWaveformVisual
          ? 'waveform'
          : 'spectrogram'
      : visualMode;
  const showWaveform = activeVisualMode === 'waveform';
  const showSpectrogram = activeVisualMode === 'spectrogram';

  const renderSavedVisual = ({
    label,
    url,
    hasVisual,
    emptyMessage,
    onError,
    hidden = false,
  }: {
    label: string;
    url: string | null;
    hasVisual: boolean;
    emptyMessage?: string;
    onError: () => void;
    hidden?: boolean;
  }) => {
    if (hidden) {
      return null;
    }

    return (
    <div
      className="relative min-h-[66px] overflow-hidden rounded-lg border"
      style={{
        height: '100%',
        borderColor: 'rgba(255,255,255,0.05)',
        background: 'rgba(0,0,0,0.22)',
      }}
    >
      {hasVisual && url ? (
        <Image
          src={url}
          alt={`${label} for ${speciesLabel}`}
          fill
          unoptimized
          sizes="(min-width: 1024px) 45vw, 100vw"
          className="object-fill"
          draggable={false}
          onError={onError}
        />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center">
          <span
            className="font-mono tracking-[0.16em] uppercase"
            style={{ fontSize: 8.5, color: 'rgba(255,255,255,0.16)' }}
          >
            {emptyMessage ?? `no saved ${label.toLowerCase()}`}
          </span>
        </div>
      )}

      {hasVisual && showRegionOverlay && (
        <>
          <div
            className="absolute inset-y-0 left-0 z-20 pointer-events-none"
            style={{
              width: `${startPct}%`,
              background: 'rgba(0,0,0,0.35)',
            }}
          />
          <div
            className="absolute inset-y-0 z-20 pointer-events-none"
            style={{
              left: `${startPct}%`,
              width: `${endPct - startPct}%`,
              background:
                'linear-gradient(90deg, rgba(255,255,255,0.08) 0%, rgba(255,255,255,0.14) 50%, rgba(255,255,255,0.08) 100%)',
              borderLeft: '1px solid rgba(255,255,255,0.16)',
              borderRight: '1px solid rgba(255,255,255,0.16)',
            }}
          />
          <div
            className="absolute inset-y-0 right-0 z-20 pointer-events-none"
            style={{
              width: `${100 - endPct}%`,
              background: 'rgba(0,0,0,0.35)',
            }}
          />
        </>
      )}

      {isReady && progressPct > 0 && progressPct < 100 && hasVisual && (
        <div
          className="absolute top-0 bottom-0 z-30 pointer-events-none"
          style={{
            left: `${progressPct}%`,
            width: 1,
            background: 'rgba(255,255,255,0.7)',
            boxShadow: '0 0 10px rgba(255,255,255,0.35)',
          }}
        />
      )}
    </div>
    );
  };

  return (
    <div ref={rowRef} className="group relative shrink-0 flex flex-row items-stretch overflow-hidden rounded-2xl transition-all duration-300"
      style={{
        boxShadow: isPlaying
          ? '0 18px 36px rgba(0,0,0,0.34), inset 0 1px 0 rgba(255,255,255,0.05)'
          : '0 10px 24px rgba(0,0,0,0.18), inset 0 1px 0 rgba(255,255,255,0.02)',
        transform: isPlaying ? 'translateY(-1px)' : undefined,
        background: isPlaying ? 'rgba(34,34,34,0.96)' : 'rgba(24,24,24,0.96)',
        border: '1px solid',
        borderColor: isPlaying ? 'rgba(255,255,255,0.10)' : 'rgba(255,255,255,0.06)'
      }}>

      {/* Photo (moved to have more breathing room) */}
      <div className="w-[84px] shrink-0 flex items-center justify-center border-r transition-colors duration-300"
        style={{
          background: 'rgba(255,255,255,0.02)',
          borderColor: 'rgba(255,255,255,0.05)'
        }}>
        <div className="w-12 h-12 rounded-2xl overflow-hidden shrink-0 flex items-center justify-center transition-all duration-300"
          style={{
            boxShadow: isPlaying ? '0 12px 24px rgba(0,0,0,0.18)' : '0 8px 18px rgba(0,0,0,0.14)',
            background: 'rgba(255,255,255,0.03)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}>
          <div className="relative h-full w-full">
            <SpeciesAvatar
              thumbnailUrl={row.thumbnailUrl}
              imageUrl={row.imageUrl}
              speciesLabel={speciesLabel}
            />
          </div>
        </div>
      </div>

      {/* Meta */}
      <div className="w-60 shrink-0 px-4 py-3.5 flex flex-col justify-center border-r transition-colors duration-300 relative"
        style={{
          borderColor: 'rgba(255,255,255,0.05)',
          background: 'transparent',
        }}>

        {/* Name + confidence */}
        <div className="flex items-baseline justify-between gap-2 mb-0.5">
          <h3 className="text-[14px] font-semibold tracking-wide text-white truncate transition-colors duration-300"
            style={{ color: '#fff' }}>
            {speciesLabel}
          </h3>
          <span className="text-[11px] font-mono shrink-0 tabular-nums font-medium"
            style={{ color: isPlaying ? '#fff' : `rgba(255,255,255,${confBright})` }}>
            {pct}%
          </span>
        </div>

        {row.scientificName && (
          <p className="text-[10px] italic mb-2 truncate" style={{ color: 'rgba(255,255,255,0.4)' }}>
            {row.scientificName}
          </p>
        )}

        <div className="flex flex-col gap-1 mt-auto" style={{ fontSize: 10, color: 'rgba(255,255,255,0.35)', fontFamily: 'inherit' }}>
          <span className="font-mono tracking-wider font-medium" style={{ color: 'rgba(255,255,255,0.48)' }} suppressHydrationWarning>{row.time}</span>
          <span className="truncate flex items-center gap-1.5"><span className="w-1 h-1 rounded-full bg-white/20" />{row.station}</span>
          {row.location && <span className="truncate flex items-center gap-1.5"><span className="w-1 h-1 rounded-full bg-white/20" />{row.location}</span>}
        </div>

        <div className="mt-1 flex flex-wrap gap-1.5">
          {audioInsight && (
            <span
              className="inline-flex w-fit items-center rounded-full px-1.5 py-[2px] font-mono uppercase tracking-[0.14em]"
              style={{
                fontSize: 8,
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.08)',
                color: 'rgba(255,255,255,0.68)',
              }}>
              {audioInsight.label}
            </span>
          )}
          <span
            className="inline-flex w-fit items-center rounded-full px-1.5 py-[2px] font-mono uppercase tracking-[0.14em]"
            style={{
              fontSize: 8,
              background: anomalyTone.background,
              border: `1px solid ${anomalyTone.border}`,
              color: anomalyTone.color,
            }}>
            {anomalyScoreLabel
              ? `${row.anomalySource === 'weekly' ? 'weekly ' : ''}anomaly ${anomalyScoreLabel}`
              : 'anomaly n/a'}
          </span>
        </div>
      </div>

      {/* Visualization Area */}
      <div
        onClick={handleSeek}
        className="flex-1 relative min-w-0 cursor-pointer group px-2 py-2"
        style={{ background: 'rgba(14,14,14,1)', minHeight: 152 }}>

        {!isReady && isAvailable && hasAnyVisual && loadRequested && (
          <div
            className="absolute inset-2 rounded-lg"
            style={{
              background: 'linear-gradient(90deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.10) 50%, rgba(255,255,255,0.03) 100%)',
            }}
          />
        )}

        {/* WaveSurfer container — completely hidden */}
        <div ref={containerRef} style={{ display: 'none' }} />

        <div className="relative z-10 flex h-full flex-col gap-2">
          {renderSavedVisual({
            label: 'Waveform',
            url: waveformUrl,
            hasVisual: hasWaveform,
            emptyMessage: waveformError ? 'waveform unavailable' : 'no saved waveform',
            onError: () => setWaveformError(true),
            hidden: !showWaveform,
          })}
          {renderSavedVisual({
            label: 'Spectrogram',
            url: spectrogramUrl,
            hasVisual: hasSpectrogram,
            emptyMessage: spectrogramError ? 'spectrogram unavailable' : 'no saved spectrogram',
            onError: () => setSpectrogramError(true),
            hidden: !showSpectrogram,
          })}
        </div>

        {!hasAnyVisual && isAvailable && (
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="font-mono tracking-[0.18em] uppercase"
              style={{ fontSize: 9, color: 'rgba(255,255,255,0.14)' }}>no saved visuals</span>
          </div>
        )}

        {!isAvailable && !hasAnyVisual && (
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="font-mono tracking-[0.2em] uppercase"
              style={{ fontSize: 9, color: 'rgba(255,255,255,0.14)' }}>no signal</span>
          </div>
        )}
        {isAvailable && hasAnyVisual && !isReady && loadRequested && (
          <div className="absolute inset-0 flex items-center justify-center gap-[3px]">
            {[8, 14, 20, 14, 22, 16, 12, 18, 14, 10].map((h, i) => (
              <div key={i} className="w-[2px] rounded-full animate-pulse"
                style={{ height: h, background: 'rgba(255,255,255,0.22)', animationDelay: `${i * 0.07}s` }} />
            ))}
          </div>
        )}
        {isReady && (
          <div className="absolute bottom-1 right-2 z-40 font-mono tabular-nums"
            style={{ fontSize: 9, color: 'rgba(255,255,255,0.2)' }}>
            {fmt(currentTime)} / {row.clipDuration ? fmt(row.clipDuration) : fmt(duration)}
          </div>
        )}
        {isReady && !hasAnyVisual && (activeAudioSourceKind === 'direct' || activeAudioSourceKind === 'raw') && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <span className="font-mono tracking-[0.16em] uppercase"
              style={{ fontSize: 8.5, color: 'rgba(255,255,255,0.16)' }}>live audio fallback</span>
          </div>
        )}
      </div>

      {/* Play / Pause */}
      <div className="w-16 shrink-0 flex items-center justify-center border-l transition-colors duration-300"
        style={{
          background: 'rgba(255,255,255,0.02)',
          borderColor: 'rgba(255,255,255,0.05)'
        }}>
        <button
          onClick={() => {
            if (!isAvailable && hasAudio && !loadRequestedRef.current) {
              initWaveSurfer();
              onPlayToggle(row.id, true);
              return;
            }
            if (isAvailable) onPlayToggle(row.id, !isPlaying);
          }}
          disabled={!hasAudio}
          className={`w-9 h-9 rounded-full flex items-center justify-center transition-all duration-200 ${!hasAudio ? '' : 'hover:scale-110 active:scale-95'}`}
          style={{
            background: isPlaying ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.08)',
            color: isPlaying ? '#fff' : 'rgba(255,255,255,0.4)',
            cursor: hasAudio ? 'pointer' : 'default',
            opacity: hasAudio ? 1 : 0.3
          }}>
          {isPlaying
            ? <Square size={12} fill="currentColor" />
            : <Play size={14} fill="currentColor" strokeWidth={1} className="ml-0.5" />
          }
        </button>
      </div>
    </div>
  );
}

// ─── Main ────────────────────────────────────────────────────────────────────
export default function DataInventoryClient({ initialData, currentPage, hasNextPage, pageSize, stationNotInDb = false, loadFailed = false }: {
  initialData: DetectionData[];
  currentPage: number;
  hasNextPage: boolean;
  pageSize: number;
  stationNotInDb?: boolean;
  loadFailed?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [focusedIdx, setFocusedIdx] = useState<number>(-1);
  const [visualMode, setVisualMode] = useState<VisualMode>('spectrogram');
  const listRef = useRef<HTMLDivElement>(null);

  // Read showRegion from URL param (default: on)
  const showRegion = searchParams.get('region') === '1';

  // Build a URL that preserves current filter params but changes a specific key
  const buildUrl = (key: string, value: string) => {
    const p = new URLSearchParams(searchParams.toString());
    p.set(key, value);
    if (key !== 'page') p.set('page', '1');
    return `${pathname}?${p.toString()}`;
  };

  useEffect(() => {
    const suppress = (e: PromiseRejectionEvent) => { if (e.reason?.name === 'AbortError') e.preventDefault(); };
    window.addEventListener('unhandledrejection', suppress);
    return () => window.removeEventListener('unhandledrejection', suppress);
  }, []);

  const goToPage = (n: number) => { setPlayingId(null); setFocusedIdx(-1); router.push(buildUrl('page', String(n))); };
  const toggle = useCallback((id: string, play: boolean) =>
    setPlayingId(play ? id : prev => prev === id ? null : prev), []);
  const visualModeOptions: { key: VisualMode; label: string }[] = [
    { key: 'spectrogram', label: 'Spectrogram' },
    { key: 'waveform', label: 'Waveform' },
  ];

  // ── Keyboard shortcuts ──
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Don't capture when user is typing in an input
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

      if (e.code === 'Space') {
        e.preventDefault();
        // Toggle play on focused or currently-playing detection
        const targetIdx = focusedIdx >= 0 ? focusedIdx : 0;
        const targetRow = initialData[targetIdx];
        if (!targetRow) return;
        if (playingId === targetRow.id) {
          setPlayingId(null);
        } else {
          setPlayingId(targetRow.id);
          setFocusedIdx(targetIdx);
        }
      } else if (e.code === 'ArrowDown') {
        e.preventDefault();
        setFocusedIdx(prev => {
          const next = Math.min(prev + 1, initialData.length - 1);
          scrollToRow(next);
          return next;
        });
      } else if (e.code === 'ArrowUp') {
        e.preventDefault();
        setFocusedIdx(prev => {
          const next = Math.max(prev - 1, 0);
          scrollToRow(next);
          return next;
        });
      } else if (e.code === 'ArrowRight' || e.code === 'ArrowLeft') {
        // Seek ±5s on the currently playing track
        if (!playingId) return;
        const rowIdx = initialData.findIndex(r => r.id === playingId);
        if (rowIdx < 0) return;
        // Dispatch a custom event that DetectionRow will listen to
        const delta = e.code === 'ArrowRight' ? 5 : -5;
        window.dispatchEvent(new CustomEvent('avian-seek', { detail: { id: playingId, delta } }));
      } else if (e.code === 'Enter' && focusedIdx >= 0) {
        e.preventDefault();
        const targetRow = initialData[focusedIdx];
        if (!targetRow) return;
        setPlayingId(prev => prev === targetRow.id ? null : targetRow.id);
      }
    };

    const scrollToRow = (idx: number) => {
      const list = listRef.current;
      if (!list) return;
      const rows = list.querySelectorAll('[data-detection-row]');
      rows[idx]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [focusedIdx, playingId, initialData]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border px-4 py-4 animate-in fade-in duration-500"
      style={{
        background: 'rgba(20,20,20,0.96)',
        borderColor: 'rgba(255,255,255,0.08)',
        boxShadow: '0 18px 40px rgba(0,0,0,0.28)',
      }}>

      {/* ── Header Area ── */}
      <div className="flex items-center justify-end pb-3 shrink-0 mb-3 border-b"
        style={{ borderColor: 'rgba(255,255,255,0.05)' }}>
        <div className="flex items-center gap-0.5 rounded-full p-[3px]"
          style={{
            background: 'rgba(16,16,16,0.96)',
            border: '1px solid rgba(255,255,255,0.08)',
            boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.03)',
          }}>
          {visualModeOptions.map(({ key, label }) => {
            const active = visualMode === key;
            return (
              <button
                key={key}
                type="button"
                onClick={() => setVisualMode(key)}
                className="rounded-full px-3 py-1 transition-all duration-150"
                style={{
                  background: active ? 'rgba(255,255,255,0.08)' : 'transparent',
                  border: `1px solid ${active ? 'rgba(255,255,255,0.1)' : 'transparent'}`,
                  color: active ? 'rgba(255,255,255,0.94)' : 'rgba(255,255,255,0.52)',
                  boxShadow: active ? 'inset 0 1px 0 rgba(255,255,255,0.04)' : 'none',
                }}>
                <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: '0.01em' }}>
                  {label}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Detection List ── */}
      <div ref={listRef} className="flex-1 min-h-0 overflow-y-auto custom-scrollbar flex flex-col gap-3 pb-4 pr-1">
        {initialData.length === 0
          ? <div className="flex-1 flex flex-col items-center justify-center gap-3">
            {loadFailed ? (
              <div className="flex flex-col items-center gap-3">
                <div className="w-10 h-10 rounded-full flex items-center justify-center" style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.08)' }}>
                  <span style={{ fontSize: 18, color: 'rgba(255,255,255,0.7)' }}>!</span>
                </div>
                <span className="font-mono tracking-wider uppercase" style={{ fontSize: 11, color: 'rgba(255,255,255,0.72)' }}>
                  Explore data unavailable
                </span>
                <span className="font-mono text-center max-w-xs" style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.28)', lineHeight: 1.5 }}>
                  The server could not reach the database in time. Retry once the data connection is healthy.
                </span>
                <button
                  onClick={() => router.refresh()}
                  className="mt-1 px-3 py-1.5 rounded-md font-mono uppercase tracking-wider transition-colors"
                  style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.74)', border: '1px solid rgba(255,255,255,0.12)', background: 'rgba(255,255,255,0.05)' }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'rgba(255,255,255,0.08)'; }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'rgba(255,255,255,0.05)'; }}
                >
                  Retry
                </button>
              </div>
            ) : stationNotInDb ? (
              <div className="flex flex-col items-center gap-3">
                <div className="w-10 h-10 rounded-full flex items-center justify-center" style={{ background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.2)' }}>
                  <span style={{ fontSize: 18 }}>⚠</span>
                </div>
                <span className="font-mono tracking-wider uppercase" style={{ fontSize: 11, color: 'rgba(251,191,36,0.7)' }}>
                  Station not in database
                </span>
                <span className="font-mono text-center max-w-xs" style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.25)', lineHeight: 1.5 }}>
                  “{searchParams.get('station')}” has not been imported into the database yet.
                  Detection data from this station is available in the raw files but hasn&apos;t been seeded.
                </span>
                <button
                  onClick={() => router.push(pathname)}
                  className="mt-1 px-3 py-1.5 rounded-md font-mono uppercase tracking-wider transition-colors"
                  style={{ fontSize: 9.5, color: 'rgba(52,211,153,0.7)', border: '1px solid rgba(52,211,153,0.2)', background: 'rgba(52,211,153,0.05)' }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'rgba(52,211,153,0.1)'; }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'rgba(52,211,153,0.05)'; }}
                >
                  Clear all filters
                </button>
              </div>
            ) : (
              <>
            <span className="font-mono tracking-widest uppercase"
              style={{ fontSize: 11, color: 'rgba(255,255,255,0.3)' }}>No detections found</span>
            {(() => {
              const activeFilters: string[] = [];
              const st = searchParams.get('station');
              const sp = searchParams.get('species');
              const loc = searchParams.get('location');
              if (st) activeFilters.push(`Station: "${st}"`);
              if (sp) activeFilters.push(`Species: "${sp}"`);
              if (loc) activeFilters.push(`Location: "${loc}"`);
              if (activeFilters.length === 0) return null;
              return (
                <div className="flex flex-col items-center gap-2">
                  <span className="font-mono" style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.18)' }}>
                    Active filters: {activeFilters.join(' · ')}
                  </span>
                  <span className="font-mono" style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.18)' }}>
                    This station may not have data in the database yet.
                  </span>
                  <button
                    onClick={() => router.push(pathname)}
                    className="mt-1 px-3 py-1.5 rounded-md font-mono uppercase tracking-wider transition-colors"
                    style={{ fontSize: 9.5, color: 'rgba(52,211,153,0.7)', border: '1px solid rgba(52,211,153,0.2)', background: 'rgba(52,211,153,0.05)' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'rgba(52,211,153,0.1)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'rgba(52,211,153,0.05)'; }}
                  >
                    Clear all filters
                  </button>
                </div>
              );
            })()}
              </>
            )}
          </div>
          : initialData.map((row, idx) => (
            <div key={row.id} data-detection-row
              onClick={() => setFocusedIdx(idx)}
              style={{
                outline: focusedIdx === idx ? '1px solid rgba(52,211,153,0.25)' : 'none',
                borderRadius: 12,
              }}>
              <DetectionRow row={row}
                isPlaying={playingId === row.id}
                onPlayToggle={toggle}
                showRegion={showRegion}
                visualMode={visualMode} />
            </div>
          ))
        }
      </div>

      <div className="flex items-center justify-between pt-2.5 border-t shrink-0"
        style={{ borderColor: 'rgba(255,255,255,0.05)' }}>
        <span className="font-mono" style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.2)' }}>
          PAGE {currentPage} {initialData.length > 0 && `· ${initialData.length}/${pageSize} shown`}
        </span>
        <div className="flex gap-1.5">
          {[
            { label: '← PREV', disabled: currentPage <= 1, page: currentPage - 1 },
            { label: 'NEXT →', disabled: !hasNextPage, page: currentPage + 1 },
          ].map(({ label, disabled, page }) => (
            <button key={label} onClick={() => goToPage(page)} disabled={disabled}
              className="px-2.5 py-1 rounded transition-all"
              style={{
                fontSize: 9.5, fontFamily: 'monospace', letterSpacing: '0.1em',
                color: disabled ? 'rgba(255,255,255,0.14)' : 'rgba(255,255,255,0.45)',
                border: '1px solid rgba(255,255,255,0.07)',
                background: 'rgba(255,255,255,0.025)',
                cursor: disabled ? 'default' : 'pointer',
              }}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <style dangerouslySetInnerHTML={{
        __html: `
        .custom-scrollbar::-webkit-scrollbar { width: 3px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.05); border-radius: 2px; }
        .custom-scrollbar:hover::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.09); }
        input[type="date"]::-webkit-calendar-picker-indicator { filter: invert(0.4); cursor: pointer; }
      ` }} />
    </div>
  );
}
