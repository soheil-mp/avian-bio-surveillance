'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { SlidersHorizontal } from 'lucide-react';
import { Space_Grotesk } from 'next/font/google';
import { AlertReviewModal } from '@/app/alerts/AlertReviewModal';
import type { AlertVisualPoint, AlertsModelingSummary } from '@/app/alerts/types';
import {
    STATION_TYPE_OPTIONS,
    matchesStationTypeFilter,
} from '@/config/stations';
import {
    type AudioGraph,
    cleanupAudioGraph,
    prepareHearingAssist,
} from '@/lib/audioHearingAssist';
import { FeedFilterPanel } from './FeedFilterPanel';
import {
    DEFAULT_FEED_FILTERS,
    buildFeedQuery,
    countActiveFilters,
    isFilteredFeed,
    type FeedFilters,
    type FeedFilterOptions,
} from '@/lib/home-feed-filters';

export type LiveDetection = {
    id: string;
    time: string;
    species: string;
    scientificName: string | null;
    confidence: number;
    station: string;
    location: string | null;
    stationType: string;
    stationQuality: number;
    imageUrl: string | null;
    thumbnailUrl: string | null;
    silhouetteUrl: string | null;
    observationDate?: string | null;
    detectedAt?: string | null;
    anomalyScore?: number | null;
    audioUrls?: { raw: string | null; processed: string | null; direct: string | null };
};

type PhyloPicLookup = { svgUrl: string | null; thumbUrl: string | null };

const livePanelHeadingFont = Space_Grotesk({
    subsets: ['latin'],
    weight: ['500', '600'],
    display: 'swap',
});
const PANEL_BG = 'rgba(20,20,20,0.94)';
const PANEL_SURFACE = 'rgba(18,18,18,0.9)';
const PANEL_CHROME = 'rgba(255,255,255,0.12)';
const PANEL_TEXT = 'rgba(255,255,255,0.9)';
const PANEL_TEXT_MUTED = 'rgba(255,255,255,0.52)';
const ACCENT = '#34d399';

const phylopicCache = new Map<string, PhyloPicLookup>();

function SpeciesVisual({ row, speciesLabel }: { row: LiveDetection; speciesLabel: string }) {
    const lookupName = (row.scientificName || row.species || '').trim().toLowerCase();
    const [silhouetteUrl, setSilhouetteUrl] = useState(row.silhouetteUrl);
    const [failedUrls, setFailedUrls] = useState<Set<string>>(() => new Set());
    const hasActualImage = Boolean(row.thumbnailUrl || row.imageUrl);

    useEffect(() => {
        setSilhouetteUrl(row.silhouetteUrl);
        setFailedUrls(new Set());
    }, [row.id, row.silhouetteUrl, row.thumbnailUrl, row.imageUrl]);

    useEffect(() => {
        if (silhouetteUrl || hasActualImage || !lookupName) return;

        const cached = phylopicCache.get(lookupName);
        if (cached) {
            setSilhouetteUrl(cached.svgUrl || cached.thumbUrl);
            return;
        }

        let cancelled = false;
        fetch(`/api/phylopic?name=${encodeURIComponent(lookupName)}`)
            .then((response) => response.json())
            .then((json) => {
                if (cancelled) return;
                const entry = {
                    svgUrl: typeof json.svgUrl === 'string' ? json.svgUrl : null,
                    thumbUrl: typeof json.thumbUrl === 'string' ? json.thumbUrl : null,
                };
                phylopicCache.set(lookupName, entry);
                setSilhouetteUrl(entry.svgUrl || entry.thumbUrl);
            })
            .catch(() => {
                phylopicCache.set(lookupName, { svgUrl: null, thumbUrl: null });
            });

        return () => {
            cancelled = true;
        };
    }, [hasActualImage, lookupName, silhouetteUrl]);

    const candidates = [row.thumbnailUrl, row.imageUrl, silhouetteUrl].filter(
        (url): url is string => typeof url === 'string' && url.length > 0 && !failedUrls.has(url),
    );
    const imageUrl = candidates[0] ?? null;
    const isSilhouette = !!imageUrl && imageUrl === silhouetteUrl;

    return (
        <div className="w-full h-full rounded overflow-hidden"
            style={{ background: PANEL_SURFACE }}>
            {imageUrl ? (
                <img
                    src={imageUrl}
                    alt=""
                    loading="lazy"
                    decoding="async"
                    className={`w-full h-full transition-transform duration-500 group-hover:scale-110 ${isSilhouette ? 'object-contain p-1.5' : 'object-cover'}`}
                    style={isSilhouette ? {
                        filter: 'invert(78%) sepia(24%) saturate(917%) hue-rotate(102deg) brightness(95%) contrast(88%)',
                        opacity: 0.86,
                    } : undefined}
                    onError={() => {
                        setFailedUrls((current) => new Set(current).add(imageUrl));
                    }}
                />
            ) : (
                <div className="w-full h-full flex items-center justify-center">
                    <span className="text-[9px] font-semibold tracking-[0.08em]" style={{ color: PANEL_TEXT_MUTED, fontFamily: livePanelHeadingFont.style.fontFamily }}>
                        {speciesLabel.slice(0, 2).toUpperCase()}
                    </span>
                </div>
            )}
        </div>
    );
}

interface LiveDetectionsTableProps {
    initialData: LiveDetection[];
    filters?: { types: string[]; species: string[]; minQuality?: number; showStations?: boolean };
    /** When set (a station selected on the live map), the feed spotlights that station. */
    spotlightStationId?: string | null;
    /** When set (a grid cell selected on the live map), the feed spotlights that cell. */
    spotlightGridCellId?: string | null;
    /** Reports the open detail's coordinates so the map can fly to it (null on close). */
    onDetailFocusChange?: (focus: { lat: number; lon: number } | null) => void;
}

export type LiveFeedSort = 'latest' | 'anomaly';

export function LiveDetectionsTable({ initialData, filters, spotlightStationId, spotlightGridCellId, onDetailFocusChange }: LiveDetectionsTableProps) {
    const [data, setData] = useState<LiveDetection[]>(initialData);
    const [isLoading, setIsLoading] = useState(initialData.length === 0);

    // Card-flip: 'feed' shows the live feed, 'filter' shows the filter panel on
    // the reverse face. Applying a filter set re-queries the feed server-side
    // (always bounded); the draft set is what the panel edits before Apply.
    const [face, setFace] = useState<'feed' | 'filter'>('feed');
    const [draftFilters, setDraftFilters] = useState<FeedFilters>(DEFAULT_FEED_FILTERS);
    const [appliedFilters, setAppliedFilters] = useState<FeedFilters>(DEFAULT_FEED_FILTERS);
    const [options, setOptions] = useState<FeedFilterOptions | null>(null);
    const [optionsLoading, setOptionsLoading] = useState(false);
    // A station/grid selected on the live map spotlights that slice of the feed
    // (relaxed, recent), overriding manual filters until the popup closes.
    const effectiveFilters: FeedFilters = spotlightStationId
        ? { ...DEFAULT_FEED_FILTERS, stationId: spotlightStationId }
        : spotlightGridCellId
        ? { ...DEFAULT_FEED_FILTERS, gridCellId: spotlightGridCellId }
        : appliedFilters;
    const sort = effectiveFilters.sort;
    const appliedQuery = buildFeedQuery(effectiveFilters);
    const initialApplied = useRef(false);

    // The feed is loaded client-side so a cold DB query plan never blocks the
    // initial page paint. Ordering ("latest" vs highest "anomaly" score) is
    // resolved server-side so each mode returns the correct bounded set, and the
    // result per mode is cached (stale-while-revalidate) so toggling is instant
    // once both are warm. Rows already on screen are kept during a re-fetch so
    // the order flips immediately (see `sorted`) without a loading flash.
    useEffect(() => {
        // Use the server-rendered rows for the unfiltered default exactly once.
        if (!initialApplied.current) {
            initialApplied.current = true;
            if (initialData.length > 0 && appliedQuery === '') {
                setData(initialData);
                setIsLoading(false);
                return;
            }
        }
        let cancelled = false;
        if (appliedQuery) {
            setData([]);
            setIsLoading(true);
        } else {
            setIsLoading((prev) => (data.length === 0 ? true : prev));
        }
        fetch(`/api/home/live-detections${appliedQuery ? `?${appliedQuery}` : ''}`)
            .then((response) => (response.ok ? response.json() : { detections: [] }))
            .then((json) => {
                if (cancelled) return;
                const next: LiveDetection[] = Array.isArray(json?.detections) ? json.detections : [];
                // For the default feed, don't let a transient empty refresh wipe a
                // populated feed; for an explicit filter, an empty result is a real
                // "no matches" and must replace the rows.
                setData((prev) => (next.length === 0 && prev.length > 0 && appliedQuery === '' ? prev : next));
            })
            .catch(() => {
                // Keep whatever is already on screen on a transient failure.
            })
            .finally(() => {
                if (!cancelled) setIsLoading(false);
            });
        return () => {
            cancelled = true;
        };
        // Re-fetch whenever the applied filter set changes (serialised to a query
        // string); `initialData`/`data` are read via the guard above.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [appliedQuery]);

    const ensureOptions = useCallback(() => {
        if (options || optionsLoading) return;
        setOptionsLoading(true);
        fetch('/api/home/feed-filters')
            .then((response) => (response.ok ? response.json() : null))
            .then((json) => { if (json) setOptions(json as FeedFilterOptions); })
            .catch(() => {})
            .finally(() => setOptionsLoading(false));
    }, [options, optionsLoading]);

    const openFilter = useCallback(() => {
        setDraftFilters(appliedFilters);
        setFace('filter');
        ensureOptions();
    }, [appliedFilters, ensureOptions]);

    const applyFilters = useCallback(() => {
        setAppliedFilters(draftFilters);
        setFace('feed');
    }, [draftFilters]);

    const resetFilters = useCallback(() => {
        setDraftFilters(DEFAULT_FEED_FILTERS);
        setAppliedFilters(DEFAULT_FEED_FILTERS);
    }, []);

    // Detection-evidence popup: the exact /alerts review modal, opened on row
    // click. Full point detail is fetched lazily from the alerts detail API and
    // memoised so re-opening (and prev/next stepping) is instant.
    type DetailPayload = { point: AlertVisualPoint; modeling: AlertsModelingSummary };
    const [selectedDetail, setSelectedDetail] = useState<DetailPayload | null>(null);
    const [detailLoadingId, setDetailLoadingId] = useState<string | null>(null);
    const [copyState, setCopyState] = useState<'idle' | 'copied' | 'error'>('idle');
    const detailCacheRef = useRef<Map<string, DetailPayload>>(new Map());
    const detailAbortRef = useRef<AbortController | null>(null);

    const filtered = useMemo(() => {
        let result = data;
        if (filters?.types && filters.types.length > 0 && filters.types.length < STATION_TYPE_OPTIONS.length) {
            result = result.filter((d) => matchesStationTypeFilter(d.stationType, filters.types));
        }
        if (filters?.species && filters.species.length > 0) {
            const spSet = new Set(filters.species);
            result = result.filter((d) => spSet.has(d.species));
        }
        if (filters?.minQuality && filters.minQuality > 0) {
            result = result.filter((d) => (d.stationQuality ?? 0) >= filters.minQuality!);
        }
        return result;
    }, [data, filters]);

    // Visible order. The server already returns each mode's correct bounded set;
    // this re-applies the active ordering locally so the toggle flips instantly
    // (and client-side filtering can't disturb it) before any re-fetch lands.
    const sorted = useMemo(() => {
        const rows = [...filtered];
        const byLatest = (a: LiveDetection, b: LiveDetection) => {
            const ta = a.detectedAt ? Date.parse(a.detectedAt) : 0;
            const tb = b.detectedAt ? Date.parse(b.detectedAt) : 0;
            return tb - ta;
        };
        if (sort === 'anomaly') {
            rows.sort((a, b) => (b.anomalyScore ?? -1) - (a.anomalyScore ?? -1) || byLatest(a, b));
        } else if (sort === 'confidence') {
            rows.sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1) || byLatest(a, b));
        } else {
            rows.sort(byLatest);
        }
        return rows;
    }, [filtered, sort]);

    const [playingId, setPlayingId] = useState<string | null>(null);
    const audioRef = useRef<HTMLAudioElement | null>(null);
    const audioGraphRef = useRef<AudioGraph | null>(null);
    const analysisAbortRef = useRef<AbortController | null>(null);
    const activeAudioUrlRef = useRef<string | null>(null);
    const playRequestRef = useRef(0);

    // Cleanup audio on unmount
    useEffect(() => {
        return () => {
            analysisAbortRef.current?.abort();
            if (audioRef.current) {
                audioRef.current.pause();
                audioRef.current.onended = null;
            }
            cleanupAudioGraph(audioGraphRef);
        };
    }, []);

    const ensureAudioEl = () => {
        if (audioRef.current) {
            return audioRef.current;
        }

        const nextAudio = new Audio();
        nextAudio.preload = 'none';
        nextAudio.onended = () => {
            setPlayingId(null);
        };
        audioRef.current = nextAudio;
        return nextAudio;
    };

    const handlePlay = async (row: LiveDetection) => {
        const url = row.audioUrls?.processed;
        if (!url) return;

        const audioEl = ensureAudioEl();

        if (playingId === row.id) {
            playRequestRef.current += 1;
            analysisAbortRef.current?.abort();
            analysisAbortRef.current = null;
            audioEl.pause();
            audioEl.currentTime = 0;
            setPlayingId(null);
            return;
        }

        const requestId = ++playRequestRef.current;
        const analysisAbort = new AbortController();
        analysisAbortRef.current?.abort();
        analysisAbortRef.current = analysisAbort;

        // Stop existing audio
        audioEl.pause();
        audioEl.currentTime = 0;
        setPlayingId(row.id);

        try {
            await prepareHearingAssist(audioEl, url, audioGraphRef, analysisAbort.signal);
        } catch (error) {
            if ((error as { name?: string })?.name === 'AbortError') {
                return;
            }
        }
        if (playRequestRef.current !== requestId) {
            return;
        }

        if (activeAudioUrlRef.current !== url) {
            audioEl.src = url;
            activeAudioUrlRef.current = url;
        }

        try {
            await audioEl.play();
            if (playRequestRef.current === requestId) {
                setPlayingId(row.id);
            }
        } catch (error) {
            if ((error as { name?: string })?.name !== 'AbortError') {
                console.error('Audio playback failed', error);
            }
            if (playRequestRef.current === requestId) {
                setPlayingId(null);
            }
        } finally {
            if (analysisAbortRef.current === analysisAbort) {
                analysisAbortRef.current = null;
            }
        }
    };

    const openDetail = async (detectionId: string, observationDate?: string | null) => {
        const cached = detailCacheRef.current.get(detectionId);
        if (cached) {
            setSelectedDetail(cached);
            return;
        }

        detailAbortRef.current?.abort();
        const controller = new AbortController();
        detailAbortRef.current = controller;
        setDetailLoadingId(detectionId);

        try {
            const params = new URLSearchParams({ detectionId, backend: 'perch_v2' });
            if (observationDate) params.set('observationDate', observationDate);
            const response = await fetch(`/api/alerts/embedding-detail?${params.toString()}`, {
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`detail request failed: ${response.status}`);
            const json = await response.json();
            if (!json?.point) throw new Error('detail response missing point');

            const detail: DetailPayload = {
                point: json.point as AlertVisualPoint,
                modeling: json.modeling as AlertsModelingSummary,
            };
            detailCacheRef.current.set(detectionId, detail);
            if (!controller.signal.aborted) setSelectedDetail(detail);
        } catch (error) {
            if ((error as { name?: string })?.name !== 'AbortError') {
                console.error('Failed to load detection detail', error);
            }
        } finally {
            setDetailLoadingId((current) => (current === detectionId ? null : current));
        }
    };

    const selectedIndex = selectedDetail
        ? sorted.findIndex((row) => row.id === selectedDetail.point.id)
        : -1;

    const shiftSelection = (delta: number) => {
        if (selectedIndex < 0 || sorted.length === 0) return;
        const nextIndex = (selectedIndex + delta + sorted.length) % sorted.length;
        const nextRow = sorted[nextIndex];
        if (nextRow) void openDetail(nextRow.id, nextRow.observationDate);
    };

    const closeDetail = () => {
        detailAbortRef.current?.abort();
        setSelectedDetail(null);
    };

    // Fly the live map to the open detail's station; restore the view on close.
    useEffect(() => {
        if (!onDetailFocusChange) return;
        const p = selectedDetail?.point;
        if (p && typeof p.latitude === 'number' && typeof p.longitude === 'number') {
            onDetailFocusChange({ lat: p.latitude, lon: p.longitude });
        } else {
            onDetailFocusChange(null);
        }
    }, [selectedDetail, onDetailFocusChange]);

    const copyDetectionView = async () => {
        if (!selectedDetail) return;
        try {
            await navigator.clipboard.writeText(
                `${window.location.origin}/alerts?detectionId=${selectedDetail.point.id}`,
            );
            setCopyState('copied');
        } catch {
            setCopyState('error');
        }
        window.setTimeout(() => setCopyState('idle'), 1500);
    };

    return (
        <div className="feed-flip-scene relative h-full w-full">
            <div className="feed-flip" data-face={face}>
                <div className="feed-flip-face feed-flip-front">
        <div className="relative flex flex-col h-full w-full"
            style={{ background: PANEL_BG }}>
            {/* Scanline overlay */}
            <div className="pointer-events-none absolute inset-0 z-10 rounded-2xl"
                style={{
                    backgroundImage: 'repeating-linear-gradient(0deg, transparent, transparent 3px, rgba(0,0,0,0.03) 3px, rgba(0,0,0,0.03) 4px)',
                }} />

            {/* HUD corner brackets */}
            <div className="pointer-events-none absolute inset-0 z-10">
                <div className="absolute top-2.5 left-2.5 w-3.5 h-3.5" style={{ borderTop: `1.5px solid ${PANEL_CHROME}`, borderLeft: `1.5px solid ${PANEL_CHROME}` }} />
                <div className="absolute top-2.5 right-2.5 w-3.5 h-3.5" style={{ borderTop: `1.5px solid ${PANEL_CHROME}`, borderRight: `1.5px solid ${PANEL_CHROME}` }} />
                <div className="absolute bottom-2.5 left-2.5 w-3.5 h-3.5" style={{ borderBottom: `1.5px solid ${PANEL_CHROME}`, borderLeft: `1.5px solid ${PANEL_CHROME}` }} />
                <div className="absolute bottom-2.5 right-2.5 w-3.5 h-3.5" style={{ borderBottom: `1.5px solid ${PANEL_CHROME}`, borderRight: `1.5px solid ${PANEL_CHROME}` }} />
            </div>

            {/* Header */}
            <div className="px-5 pt-4 pb-3 shrink-0">
                <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2.5">
                        <div className="relative flex items-center justify-center w-2 h-2">
                            <div className="absolute inset-0 rounded-full animate-ping opacity-75" style={{ background: 'rgba(52,211,153,0.8)' }} />
                            <div className="relative w-1.5 h-1.5 rounded-full" style={{ background: '#34d399' }} />
                        </div>
                        <h2
                            className="text-[13px] font-semibold tracking-[0.08em] uppercase text-white/90"
                            style={{ fontFamily: livePanelHeadingFont.style.fontFamily }}>
                            Live Feed
                        </h2>
                    </div>

                    {/* Filter/spotlight state is intentionally not shown on top. */}
                    <span className="shrink-0" />
                </div>
            </div>

            {/* Animated scan line */}
            <div className="shrink-0 h-[1px] overflow-hidden">
                <div className="h-full w-full feed-scan-line" />
            </div>

            {/* Feed */}
            <div className="flex-1 overflow-y-auto custom-scrollbar flex flex-col py-0.5">
                {isLoading && sorted.length === 0
                    ? Array.from({ length: 8 }).map((_, i) => (
                          <div
                              key={`feed-skeleton-${i}`}
                              className="mx-4 my-1.5 h-12 rounded-lg animate-pulse"
                              style={{ background: 'rgba(255,255,255,0.04)' }}
                          />
                      ))
                    : null}
                {!isLoading && sorted.length === 0 ? (
                    <div
                        className="px-5 py-8 text-center text-[11px] font-mono tracking-wide"
                        style={{ color: PANEL_TEXT_MUTED }}>
                        {spotlightStationId ? 'No recent detections from this station' : spotlightGridCellId ? 'No recent detections in this grid cell' : isFilteredFeed(appliedFilters) ? 'No detections match these filters' : 'No recent detections'}
                    </div>
                ) : null}
                {sorted.map((row, i) => {
                    const isPlaying = playingId === row.id;
                    const hasAudio = !!row.audioUrls?.processed;
                    const speciesLabel = row.species?.trim() || 'Unknown species';

                    return (
                        <div key={row.id}
                            role="button"
                            tabIndex={0}
                            onClick={() => { void openDetail(row.id, row.observationDate); }}
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                    event.preventDefault();
                                    void openDetail(row.id, row.observationDate);
                                }
                            }}
                            aria-busy={detailLoadingId === row.id}
                            className="relative group cursor-pointer feed-row feed-card"
                            style={{ animationDelay: `${i * 30}ms` }}>

                            {/* Left edge glow when playing */}
                            <div className="absolute left-0 top-1 bottom-1 w-[2px] rounded-full transition-all duration-300"
                                style={{
                                    background: isPlaying ? '#34d399' : 'transparent',
                                    boxShadow: isPlaying ? '0 0 10px rgba(52,211,153,0.6)' : 'none',
                                }} />

                            <div className="px-4 py-3">
                                <div className="flex items-start gap-3">
                                    {/* Surveillance thumbnail — bigger with overlay */}
                                    <div className="relative w-11 h-11 shrink-0">
                                        <SpeciesVisual row={row} speciesLabel={speciesLabel} />
                                        {/* Surveillance overlay: corner brackets */}
                                        <div className="pointer-events-none absolute inset-0">
                                            <div className="absolute top-0 left-0 w-2 h-2" style={{ borderTop: '1px solid rgba(52,211,153,0.4)', borderLeft: '1px solid rgba(52,211,153,0.4)' }} />
                                            <div className="absolute top-0 right-0 w-2 h-2" style={{ borderTop: '1px solid rgba(52,211,153,0.4)', borderRight: '1px solid rgba(52,211,153,0.4)' }} />
                                            <div className="absolute bottom-0 left-0 w-2 h-2" style={{ borderBottom: '1px solid rgba(52,211,153,0.4)', borderLeft: '1px solid rgba(52,211,153,0.4)' }} />
                                            <div className="absolute bottom-0 right-0 w-2 h-2" style={{ borderBottom: '1px solid rgba(52,211,153,0.4)', borderRight: '1px solid rgba(52,211,153,0.4)' }} />
                                            {/* Crosshair */}
                                            <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-3 h-3">
                                                <div className="absolute top-0 left-1/2 w-[1px] h-full -translate-x-1/2" style={{ background: 'rgba(52,211,153,0.15)' }} />
                                                <div className="absolute top-1/2 left-0 w-full h-[1px] -translate-y-1/2" style={{ background: 'rgba(52,211,153,0.15)' }} />
                                            </div>
                                        </div>
                                    </div>

                                    {/* Content area */}
                                    <div className="flex-1 min-w-0">
                                        {/* Row 1: species name + audio insight + play button */}
                                        <div className="flex items-center gap-1.5">
                                            <span className="truncate text-[13.5px] font-medium leading-snug transition-colors"
                                                style={{ color: isPlaying ? '#fff' : PANEL_TEXT, fontFamily: livePanelHeadingFont.style.fontFamily, letterSpacing: '0.02em' }}>
                                                {speciesLabel}
                                            </span>

                                            <div className="ml-auto flex items-center gap-1.5 shrink-0">
                                                <div className={`flex items-end gap-[1.5px] h-3 opacity-0 transition-opacity duration-300 ${isPlaying ? 'opacity-100 viz-active' : ''}`}>
                                                    <div className="viz-bar w-[1.5px] rounded-sm" style={{ backgroundColor: 'rgba(52,211,153,0.8)' }} />
                                                    <div className="viz-bar w-[1.5px] rounded-sm" style={{ backgroundColor: 'rgba(52,211,153,0.8)' }} />
                                                    <div className="viz-bar w-[1.5px] rounded-sm" style={{ backgroundColor: 'rgba(52,211,153,0.8)' }} />
                                                    <div className="viz-bar w-[1.5px] rounded-sm" style={{ backgroundColor: 'rgba(52,211,153,0.8)' }} />
                                                </div>
                                                {hasAudio ? (
                                                    <button
                                                        onClick={(event) => {
                                                            event.stopPropagation();
                                                            void handlePlay(row);
                                                        }}
                                                        aria-label={isPlaying ? `Stop ${speciesLabel} audio` : `Play ${speciesLabel} audio with hearing assist`}
                                                        className="w-6 h-6 rounded-full flex items-center justify-center transition-all duration-300 hover:scale-110 feed-play-btn"
                                                        style={{
                                                            background: isPlaying ? 'rgba(52,211,153,0.12)' : 'rgba(255,255,255,0.02)',
                                                            border: `1px solid ${isPlaying ? 'rgba(52,211,153,0.35)' : 'rgba(255,255,255,0.06)'}`,
                                                            color: isPlaying ? '#34d399' : 'rgba(255,255,255,0.35)',
                                                        }}>
                                                        {isPlaying ? (
                                                            <div className="w-2 h-2 rounded-[2px] bg-current" />
                                                        ) : (
                                                            <svg viewBox="0 0 24 24" fill="currentColor" className="w-2.5 h-2.5 ml-0.5">
                                                                <path d="M8 5v14l11-7z" />
                                                            </svg>
                                                        )}
                                                    </button>
                                                ) : (
                                                    <span className="w-6" />
                                                )}
                                            </div>
                                        </div>

                                        {/* Row 2: location left, time right */}
                                        <div className="flex items-center justify-between mt-1.5">
                                            <div className="flex items-center min-w-0">
                                                <span className="w-1.5 h-1.5 rounded-full shrink-0 mr-1.5"
                                                    style={{ background: ACCENT, boxShadow: '0 0 4px rgba(52,211,153,0.25)' }} />
                                                <span className="text-[10px] font-mono tracking-wide truncate leading-none"
                                                    style={{ color: 'rgba(255,255,255,0.45)' }}>
                                                    {row.location ?? '—'}
                                                </span>
                                            </div>
                                            <span className="text-[10px] font-mono tracking-wider shrink-0 leading-none"
                                                style={{ color: isPlaying ? 'rgba(52,211,153,0.7)' : 'rgba(255,255,255,0.38)' }}>
                                                {row.time}
                                            </span>
                                        </div>

                                    </div>
                                </div>
                            </div>

                            {/* Subtle divider */}
                            <div className="h-[1px] mx-4" style={{ background: 'rgba(255,255,255,0.04)' }} />
                        </div>
                    );
                })}
            </div>

            {/* Footer — flips the panel to the filter face */}
            <div className="shrink-0 h-[1px]"
                style={{ background: 'rgba(255,255,255,0.04)' }} />
            <button type="button" onClick={openFilter}
                className="shrink-0 flex items-center justify-center gap-2 px-4 py-3.5 transition-all duration-200 group/footer">
                <SlidersHorizontal className="w-3 h-3 transition-colors duration-200 group-hover/footer:text-white"
                    style={{ color: 'rgba(255,255,255,0.4)' }} />
                <span
                    className="text-[10.5px] font-medium tracking-[0.12em] uppercase transition-colors duration-200 group-hover/footer:text-white"
                    style={{ color: PANEL_TEXT_MUTED, fontFamily: livePanelHeadingFont.style.fontFamily }}>
                    Filter{countActiveFilters(appliedFilters) > 0 ? ` · ${countActiveFilters(appliedFilters)}` : ''}
                </span>
            </button>

            {selectedDetail && typeof document !== 'undefined'
                ? createPortal(
                      <AlertReviewModal
                          point={selectedDetail.point}
                          modeling={selectedDetail.modeling}
                          placement="left"
                          showTabs={false}
                          showActions={false}
                          showNav={false}
                          baselinePoints={[]}
                          comparisonTargets={[]}
                          onTabChange={() => {}}
                          index={selectedIndex >= 0 ? selectedIndex : 0}
                          total={sorted.length}
                          onClose={closeDetail}
                          onNext={() => shiftSelection(1)}
                          onPrevious={() => shiftSelection(-1)}
                          onSelectRelated={(id) => { void openDetail(id); }}
                          onCopyView={() => { void copyDetectionView(); }}
                          copyState={copyState}
                      />,
                      document.body,
                  )
                : null}

            <style dangerouslySetInnerHTML={{
                __html: `
        .custom-scrollbar::-webkit-scrollbar { width: 3px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.05); border-radius: 2px; }
        .custom-scrollbar:hover::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.09); }

        @keyframes viz {
            0% { height: 2px; }
            50% { height: 10px; }
            100% { height: 2px; }
        }
        .viz-bar { transition: height 0.1s; height: 2px; }
        .viz-active .viz-bar { animation: viz 0.7s infinite ease-in-out; }
        .viz-active .viz-bar:nth-child(1) { animation-delay: 0.0s; }
        .viz-active .viz-bar:nth-child(2) { animation-delay: 0.2s; }
        .viz-active .viz-bar:nth-child(3) { animation-delay: 0.4s; }
        .viz-active .viz-bar:nth-child(4) { animation-delay: 0.6s; }

        @keyframes feed-scan {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }
        .feed-scan-line {
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.16), transparent);
            animation: feed-scan 3s ease-in-out infinite;
        }

        @keyframes scan-ring-rotate {
            from { transform: rotate(0deg); }
            to   { transform: rotate(360deg); }
        }
        .scan-ring { animation: scan-ring-rotate 8s linear infinite; }

        .feed-card { transition: background 0.3s, box-shadow 0.3s; }
        .feed-card:hover {
            background: rgba(255,255,255,0.03) !important;
            box-shadow: inset 2px 0 0 rgba(255,255,255,0.12);
        }

        .feed-play-btn:hover {
            border-color: rgba(255,255,255,0.16) !important;
            color: rgba(255,255,255,0.84) !important;
            box-shadow: 0 0 10px rgba(255,255,255,0.06);
        }

        @keyframes feed-row-in {
            from { opacity: 0; transform: translateX(-6px); }
            to   { opacity: 1; transform: translateX(0); }
        }
        .feed-row {
            animation: feed-row-in 0.4s ease-out both;
        }

        .feed-flip-scene { perspective: 1800px; }
        .feed-flip {
            position: relative;
            width: 100%;
            height: 100%;
            transform-style: preserve-3d;
            transition: transform 0.7s cubic-bezier(0.22, 1, 0.36, 1);
        }
        .feed-flip[data-face="filter"] { transform: rotateY(180deg); }
        .feed-flip-face {
            position: absolute;
            inset: 0;
            border-radius: 1rem;
            overflow: hidden;
            backface-visibility: hidden;
            -webkit-backface-visibility: hidden;
        }
        .feed-flip-back { transform: rotateY(180deg); }
        @media (prefers-reduced-motion: reduce) {
            .feed-flip { transition: none; }
        }
      `}} />
        </div>
                </div>
                <div className="feed-flip-face feed-flip-back">
                    <FeedFilterPanel
                        options={options}
                        optionsLoading={optionsLoading}
                        value={draftFilters}
                        onChange={setDraftFilters}
                        onApply={applyFilters}
                        onReset={resetFilters}
                        onBack={() => setFace('feed')}
                        activeCount={countActiveFilters(draftFilters)}
                        hasPending={buildFeedQuery(draftFilters) !== appliedQuery}
                    />
                </div>
            </div>
        </div>
    );
}
