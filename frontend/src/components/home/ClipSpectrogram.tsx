'use client';

import { useEffect, useRef, useState } from 'react';

/**
 * Renders a spectrogram (or waveform) for a detection clip directly in the
 * browser from the processed audio, via the Web Audio API + a small FFT.
 *
 * Why client-side: the pipeline only generates spectrogram/waveform PNGs for a
 * tiny sampled subset of detections (most rows carry
 * feature_artifacts.artifact_upload_status = "skipped"), so the modal's old
 * <img> fallback URL 404s for almost every live-feed detection. The processed
 * audio clip, however, exists for any row with has_processed_clip — so we draw
 * the visual from the audio itself. The clip must be fetched same-origin
 * (audio-clip ?raw=1 proxy) because MinIO serves no CORS headers.
 */

type Mode = 'spectrogram' | 'waveform';

// Decode once per URL, shared across tab switches / re-opens.
const bufferCache = new Map<string, Promise<AudioBuffer>>();
let sharedCtx: AudioContext | null = null;

function getAudioContext(): AudioContext {
  if (!sharedCtx) {
    const Ctor =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    sharedCtx = new Ctor();
  }
  return sharedCtx;
}

async function loadBuffer(url: string): Promise<AudioBuffer> {
  let pending = bufferCache.get(url);
  if (!pending) {
    pending = (async () => {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`audio fetch failed: ${res.status}`);
      const bytes = await res.arrayBuffer();
      return await getAudioContext().decodeAudioData(bytes);
    })();
    bufferCache.set(url, pending);
    // Don't cache failures — allow a retry on next open.
    pending.catch(() => bufferCache.delete(url));
  }
  return pending;
}

function toMono(buffer: AudioBuffer): Float32Array {
  const channels = buffer.numberOfChannels;
  const length = buffer.length;
  const mono = new Float32Array(length);
  for (let c = 0; c < channels; c++) {
    const data = buffer.getChannelData(c);
    for (let i = 0; i < length; i++) mono[i] += data[i] / channels;
  }
  return mono;
}

// In-place iterative radix-2 Cooley-Tukey FFT. `re`/`im` length must be a power of 2.
function fft(re: Float32Array, im: Float32Array): void {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      const tr = re[i];
      re[i] = re[j];
      re[j] = tr;
      const ti = im[i];
      im[i] = im[j];
      im[j] = ti;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wr = Math.cos(ang);
    const wi = Math.sin(ang);
    const half = len >> 1;
    for (let i = 0; i < n; i += len) {
      let cr = 1;
      let ci = 0;
      for (let k = 0; k < half; k++) {
        const ar = re[i + k];
        const ai = im[i + k];
        const br = re[i + k + half] * cr - im[i + k + half] * ci;
        const bi = re[i + k + half] * ci + im[i + k + half] * cr;
        re[i + k] = ar + br;
        im[i + k] = ai + bi;
        re[i + k + half] = ar - br;
        im[i + k + half] = ai - bi;
        const ncr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = ncr;
      }
    }
  }
}

// Perceptual "inferno"-style ramp: dark -> purple -> red -> orange -> pale yellow.
const RAMP: Array<[number, number, number, number]> = [
  [0.0, 6, 4, 18],
  [0.22, 52, 12, 78],
  [0.45, 132, 28, 92],
  [0.68, 214, 70, 52],
  [0.86, 246, 150, 38],
  [1.0, 252, 255, 168],
];

function rampColor(t: number): [number, number, number] {
  const clamped = t <= 0 ? 0 : t >= 1 ? 1 : t;
  for (let i = 1; i < RAMP.length; i++) {
    if (clamped <= RAMP[i][0]) {
      const [t0, r0, g0, b0] = RAMP[i - 1];
      const [t1, r1, g1, b1] = RAMP[i];
      const f = (clamped - t0) / (t1 - t0 || 1);
      return [r0 + (r1 - r0) * f, g0 + (g1 - g0) * f, b0 + (b1 - b0) * f];
    }
  }
  return [252, 255, 168];
}

function drawSpectrogram(canvas: HTMLCanvasElement, buffer: AudioBuffer): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const mono = toMono(buffer);
  const samples = mono.length;
  const fftSize = 1024;
  const cols = 512;
  const hop = Math.max(1, Math.floor((samples - fftSize) / cols));
  const halfBins = fftSize >> 1;
  const nyquist = buffer.sampleRate / 2;
  const freqCap = Math.min(nyquist, 12000); // emphasise the bird-call band
  const rows = Math.max(1, Math.round(halfBins * (freqCap / nyquist)));

  const window = new Float32Array(fftSize);
  for (let i = 0; i < fftSize; i++) {
    window[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (fftSize - 1));
  }

  const re = new Float32Array(fftSize);
  const im = new Float32Array(fftSize);
  const db = new Float32Array(cols * rows);
  let maxDb = -Infinity;

  for (let x = 0; x < cols; x++) {
    const start = x * hop;
    for (let i = 0; i < fftSize; i++) {
      const s = start + i;
      re[i] = s < samples ? mono[s] * window[i] : 0;
      im[i] = 0;
    }
    fft(re, im);
    for (let y = 0; y < rows; y++) {
      const mag = Math.sqrt(re[y] * re[y] + im[y] * im[y]) / fftSize;
      const value = 20 * Math.log10(mag + 1e-7);
      db[x * rows + y] = value;
      if (value > maxDb) maxDb = value;
    }
  }

  const floorDb = maxDb - 80; // 80 dB dynamic range
  const span = maxDb - floorDb || 1;
  canvas.width = cols;
  canvas.height = rows;
  const image = ctx.createImageData(cols, rows);
  for (let x = 0; x < cols; x++) {
    for (let y = 0; y < rows; y++) {
      const t = (db[x * rows + y] - floorDb) / span;
      const [r, g, b] = rampColor(t);
      const py = rows - 1 - y; // low frequencies at the bottom
      const idx = (py * cols + x) * 4;
      image.data[idx] = r;
      image.data[idx + 1] = g;
      image.data[idx + 2] = b;
      image.data[idx + 3] = 255;
    }
  }
  ctx.putImageData(image, 0, 0);
}

function drawWaveform(canvas: HTMLCanvasElement, buffer: AudioBuffer): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const mono = toMono(buffer);
  const width = 900;
  const height = 240;
  canvas.width = width;
  canvas.height = height;
  ctx.clearRect(0, 0, width, height);
  const mid = height / 2;
  const step = Math.max(1, Math.floor(mono.length / width));

  ctx.fillStyle = 'rgba(52,211,153,0.22)';
  ctx.strokeStyle = 'rgba(52,211,153,0.95)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = 0; x < width; x++) {
    let min = 1;
    let max = -1;
    const start = x * step;
    for (let i = 0; i < step; i++) {
      const v = mono[start + i] ?? 0;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const yMax = mid - max * mid * 0.95;
    const yMin = mid - min * mid * 0.95;
    ctx.moveTo(x + 0.5, yMax);
    ctx.lineTo(x + 0.5, Math.max(yMin, yMax + 1));
  }
  ctx.stroke();
  ctx.strokeStyle = 'rgba(255,255,255,0.12)';
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(width, mid);
  ctx.stroke();
}

export function ClipSpectrogram({
  audioUrl,
  mode = 'spectrogram',
  height = 178,
}: {
  audioUrl: string;
  mode?: Mode;
  height?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');

  useEffect(() => {
    let cancelled = false;
    setStatus('loading');
    (async () => {
      try {
        const buffer = await loadBuffer(audioUrl);
        if (cancelled) return;
        const canvas = canvasRef.current;
        if (!canvas) return;
        if (mode === 'waveform') drawWaveform(canvas, buffer);
        else drawSpectrogram(canvas, buffer);
        if (!cancelled) setStatus('ready');
      } catch {
        if (!cancelled) setStatus('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [audioUrl, mode]);

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      <canvas
        ref={canvasRef}
        style={{
          width: '100%',
          height,
          display: status === 'ready' ? 'block' : 'none',
          borderRadius: 8,
          border: '1px solid rgba(255,255,255,0.08)',
          background: '#0c0c0c',
        }}
      />
      {status !== 'ready' ? (
        <div
          style={{
            height,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            borderRadius: 8,
            border: '1px solid rgba(255,255,255,0.08)',
            background: '#0c0c0c',
            color: 'rgba(255,255,255,0.4)',
            fontSize: 13,
          }}
        >
          {status === 'loading'
            ? mode === 'waveform'
              ? 'Rendering waveform…'
              : 'Rendering spectrogram…'
            : 'Audio unavailable for this clip'}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Picks the best available visual for a clip: shows the pre-generated PNG when
 * one exists (scored 2024 detections record it in feature_artifacts), and
 * otherwise renders live from the audio. If the PNG URL is present but fails to
 * load, it falls back to the live render too.
 */
export function ClipVisual({
  pngUrl,
  audioUrl,
  mode = 'spectrogram',
  height = 178,
  label,
}: {
  pngUrl: string | null;
  audioUrl: string | null;
  mode?: Mode;
  height?: number;
  label?: string;
}) {
  const [pngFailed, setPngFailed] = useState(false);
  useEffect(() => {
    setPngFailed(false);
  }, [pngUrl]);

  if (pngUrl && !pngFailed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={pngUrl}
        alt={label ?? mode}
        style={{
          width: '100%',
          maxHeight: height,
          borderRadius: 8,
          border: '1px solid rgba(255,255,255,0.08)',
          objectFit: 'cover',
        }}
        decoding="async"
        loading="eager"
        onError={() => setPngFailed(true)}
      />
    );
  }
  if (audioUrl) {
    return <ClipSpectrogram audioUrl={audioUrl} mode={mode} height={height} />;
  }
  return (
    <div
      style={{
        height,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        borderRadius: 8,
        border: '1px solid rgba(255,255,255,0.08)',
        background: '#0c0c0c',
        color: 'rgba(255,255,255,0.4)',
        fontSize: 13,
      }}
    >
      No spectrogram or waveform for this clip
    </div>
  );
}
