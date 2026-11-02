'use client';

import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import {
  type AudioGraph,
  type AudioInsight,
  buildAudioInsight,
  cleanupAudioGraph,
  prepareHearingAssist,
} from '@/lib/audioHearingAssist';

export default function AssistedAudioPlayer({
  src,
  className,
  style,
}: {
  src: string;
  className?: string;
  style?: CSSProperties;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const graphRef = useRef<AudioGraph | null>(null);
  const analysisAbortRef = useRef<AbortController | null>(null);
  const preparePromiseRef = useRef<Promise<void> | null>(null);
  const preparedSrcRef = useRef<string | null>(null);
  const replayAfterPrepareRef = useRef(false);
  const [audioInsight, setAudioInsight] = useState<AudioInsight | null>(null);

  const prepare = useCallback(async () => {
    const audioEl = audioRef.current;
    if (!audioEl) {
      return;
    }
    if (preparedSrcRef.current === src) {
      return;
    }
    if (preparePromiseRef.current) {
      await preparePromiseRef.current;
      return;
    }

    const abort = new AbortController();
    analysisAbortRef.current?.abort();
    analysisAbortRef.current = abort;

    let currentPromise: Promise<void> | null = null;
    currentPromise = (async () => {
      try {
        const analysis = await prepareHearingAssist(audioEl, src, graphRef, abort.signal);
        if (abort.signal.aborted) {
          return;
        }
        preparedSrcRef.current = src;
        setAudioInsight(buildAudioInsight(analysis));
      } catch (error) {
        if ((error as { name?: string })?.name === 'AbortError') {
          return;
        }
        setAudioInsight(null);
      } finally {
        if (analysisAbortRef.current === abort) {
          analysisAbortRef.current = null;
        }
        if (preparePromiseRef.current === currentPromise) {
          preparePromiseRef.current = null;
        }
      }
    })();

    preparePromiseRef.current = currentPromise;
    await currentPromise;
  }, [src]);

  useEffect(() => {
    preparedSrcRef.current = null;
    replayAfterPrepareRef.current = false;
    preparePromiseRef.current = null;
    analysisAbortRef.current?.abort();
    setAudioInsight(null);
  }, [src]);

  useEffect(() => {
    return () => {
      analysisAbortRef.current?.abort();
      preparePromiseRef.current = null;
      cleanupAudioGraph(graphRef);
    };
  }, []);

  const handlePlay = useCallback(async () => {
    const audioEl = audioRef.current;
    if (!audioEl) {
      return;
    }
    if (replayAfterPrepareRef.current) {
      replayAfterPrepareRef.current = false;
      return;
    }
    if (preparedSrcRef.current === src) {
      return;
    }

    audioEl.pause();

    try {
      await prepare();
      replayAfterPrepareRef.current = true;
      await audioEl.play();
    } catch (error) {
      if ((error as { name?: string })?.name !== 'AbortError') {
        console.error('Audio playback failed', error);
      }
    }
  }, [prepare, src]);

  return (
    <div>
      <audio
        ref={audioRef}
        controls
        className={className}
        src={src}
        preload="metadata"
        style={style}
        onPointerDownCapture={() => {
          void prepare();
        }}
        onPlay={() => {
          void handlePlay();
        }}
      />
      {audioInsight ? (
        <div className="mt-2 flex">
          <span
            className="inline-flex items-center rounded-full px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em]"
            style={{
              background: audioInsight.tone === 'warning' ? 'rgba(245,158,11,0.12)' : 'rgba(16,185,129,0.10)',
              border: `1px solid ${audioInsight.tone === 'warning' ? 'rgba(245,158,11,0.26)' : 'rgba(16,185,129,0.22)'}`,
              color: audioInsight.tone === 'warning' ? 'rgba(251,191,36,0.88)' : 'rgba(16,185,129,0.82)',
            }}>
            {audioInsight.label}
          </span>
        </div>
      ) : null}
    </div>
  );
}