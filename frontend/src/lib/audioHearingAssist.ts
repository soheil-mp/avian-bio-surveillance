'use client';

export type AudioQuality = 'normal' | 'quiet' | 'very-quiet';

export type AudioAnalysis = {
  gainDb: number;
  quality: AudioQuality;
  rmsDbfs: number;
  dominantHz: number;
  playbackRate: number;
};

export type AudioGraph = {
  context: AudioContext;
  compressor: DynamicsCompressorNode;
  gain: GainNode;
  makeup: GainNode;
  highPass: BiquadFilterNode;
  presence: BiquadFilterNode;
  source: MediaElementAudioSourceNode;
};

export type AudioInsight = {
  label: string;
  tone: 'boost' | 'warning';
};

export const defaultAudioAnalysis: AudioAnalysis = {
  gainDb: 0,
  quality: 'normal',
  rmsDbfs: 0,
  dominantHz: 0,
  playbackRate: 1,
};

const TARGET_RMS_DBFS = -16;
const MAX_GAIN_DB = 42;
const QUIET_RMS_DBFS = -40;
const VERY_QUIET_RMS_DBFS = -65;

// When a clip's dominant frequency sits above this, playback is slowed (with
// pitch preservation off) until that peak lands here — the most audible band
// for small speakers and human hearing. Clips already at/below it are untouched.
const TARGET_DOMINANT_HZ = 3200;
// Floor on the slow-down so a very high call (e.g. a kinglet ~9 kHz) is brought
// well down without stretching the clip to an unusable length.
const MIN_PLAYBACK_RATE = 0.4;

const audioAnalysisCache = new Map<string, AudioAnalysis>();

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), maximum);
}

function dbfsFromLinear(value: number) {
  return 20 * Math.log10(Math.max(value, 1e-12));
}

function linearGainFromDb(value: number) {
  return 10 ** (value / 20);
}

function resolveAudioQuality(rmsDbfs: number): AudioQuality {
  if (rmsDbfs <= VERY_QUIET_RMS_DBFS) {
    return 'very-quiet';
  }
  if (rmsDbfs <= QUIET_RMS_DBFS) {
    return 'quiet';
  }
  return 'normal';
}

function getAudioContextConstructor() {
  if (typeof window === 'undefined') {
    return null;
  }
  const maybeWebkitWindow = window as Window & { webkitAudioContext?: typeof AudioContext };
  return window.AudioContext ?? maybeWebkitWindow.webkitAudioContext ?? null;
}

function extractMonoSamples(audioBuffer: AudioBuffer) {
  if (audioBuffer.numberOfChannels === 1) {
    return audioBuffer.getChannelData(0);
  }

  const mono = new Float32Array(audioBuffer.length);
  for (let channel = 0; channel < audioBuffer.numberOfChannels; channel += 1) {
    const channelData = audioBuffer.getChannelData(channel);
    for (let index = 0; index < channelData.length; index += 1) {
      mono[index] += channelData[index];
    }
  }

  for (let index = 0; index < mono.length; index += 1) {
    mono[index] /= audioBuffer.numberOfChannels;
  }

  return mono;
}

// In-place iterative radix-2 FFT (Cooley–Tukey). `re`/`im` length must be a
// power of two; on return they hold the complex transform.
function fftRadix2(re: Float32Array, im: Float32Array): void {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i += 1) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) {
      j ^= bit;
    }
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
    const angle = (-2 * Math.PI) / len;
    const wpr = Math.cos(angle);
    const wpi = Math.sin(angle);
    const halfLen = len >> 1;
    for (let i = 0; i < n; i += len) {
      let wr = 1;
      let wi = 0;
      for (let k = 0; k < halfLen; k += 1) {
        const a = i + k;
        const b = a + halfLen;
        const tr = re[b] * wr - im[b] * wi;
        const ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
        const nextWr = wr * wpr - wi * wpi;
        wi = wr * wpi + wi * wpr;
        wr = nextWr;
      }
    }
  }
}

// Dominant-frequency estimate: a Hann-windowed FFT of the loudest ~85ms of the
// clip (so silence and background don't skew it), taking the peak bin within the
// bird band. Uniform ~12Hz bins read a call's pitch directly regardless of
// amplitude — unlike a sparse probe bank. Runs once per clip on decode and is
// cached with the rest of the analysis. Returns 0 when it can't get a read.
function estimateDominantHz(samples: Float32Array, sampleRate: number): number {
  const length = samples.length;
  const fftSize = 4096;
  if (length < fftSize || !Number.isFinite(sampleRate) || sampleRate <= 0) {
    return 0;
  }

  // Locate the loudest quarter-second frame to centre the analysis window on.
  const frame = Math.max(1, Math.floor(sampleRate * 0.25));
  let bestStart = 0;
  let bestEnergy = -1;
  for (let start = 0; start < length; start += frame) {
    const end = Math.min(start + frame, length);
    let frameEnergy = 0;
    for (let index = start; index < end; index += 1) {
      frameEnergy += samples[index] * samples[index];
    }
    if (frameEnergy > bestEnergy) {
      bestEnergy = frameEnergy;
      bestStart = start;
    }
  }
  if (bestEnergy <= 0) {
    return 0;
  }

  let winStart = bestStart + Math.floor(frame / 2) - (fftSize >> 1);
  if (winStart < 0) {
    winStart = 0;
  }
  if (winStart + fftSize > length) {
    winStart = length - fftSize;
  }

  const re = new Float32Array(fftSize);
  const im = new Float32Array(fftSize);
  for (let index = 0; index < fftSize; index += 1) {
    const hann = 0.5 - 0.5 * Math.cos((2 * Math.PI * index) / (fftSize - 1));
    re[index] = samples[winStart + index] * hann;
  }
  fftRadix2(re, im);

  const nyquist = sampleRate / 2;
  const minHz = 700;
  const maxHz = Math.min(11000, nyquist - 200);
  if (maxHz <= minHz) {
    return 0;
  }
  const minBin = Math.max(1, Math.floor((minHz * fftSize) / sampleRate));
  const maxBin = Math.min(fftSize >> 1, Math.ceil((maxHz * fftSize) / sampleRate));

  let peakBin = minBin;
  let peakMagnitude = -1;
  for (let bin = minBin; bin <= maxBin; bin += 1) {
    const magnitude = re[bin] * re[bin] + im[bin] * im[bin];
    if (magnitude > peakMagnitude) {
      peakMagnitude = magnitude;
      peakBin = bin;
    }
  }

  return (peakBin * sampleRate) / fftSize;
}

// Map a dominant frequency to a playback rate. High calls are slowed (rate < 1)
// so that, with pitch preservation off, their pitch drops into the audible band;
// calls already comfortable are left at full speed.
function playbackRateForDominant(dominantHz: number): number {
  if (!Number.isFinite(dominantHz) || dominantHz <= TARGET_DOMINANT_HZ) {
    return 1;
  }
  return clamp(TARGET_DOMINANT_HZ / dominantHz, MIN_PLAYBACK_RATE, 1);
}

export async function ensureAudioGraph(
  mediaEl: HTMLMediaElement,
  graphRef: { current: AudioGraph | null },
) {
  const currentGraph = graphRef.current;
  if (currentGraph) {
    if (currentGraph.context.state === 'suspended') {
      await currentGraph.context.resume();
    }
    return currentGraph;
  }

  const AudioContextCtor = getAudioContextConstructor();
  if (!AudioContextCtor) {
    return null;
  }

  const context = new AudioContextCtor();
  const source = context.createMediaElementSource(mediaEl);
  const highPass = context.createBiquadFilter();
  highPass.type = 'highpass';
  highPass.frequency.value = 160;
  highPass.Q.value = 0.7;

  const presence = context.createBiquadFilter();
  presence.type = 'peaking';
  presence.frequency.value = 2600;
  presence.Q.value = 0.9;
  presence.gain.value = 0;

  const gain = context.createGain();
  gain.gain.value = 1;

  // Aggressive limiter: pins peaks low so the makeup stage below can lift the
  // whole clip hard without clipping the output.
  const compressor = context.createDynamicsCompressor();
  compressor.threshold.value = -16;
  compressor.knee.value = 6;
  compressor.ratio.value = 16;
  compressor.attack.value = 0.002;
  compressor.release.value = 0.18;

  // Post-limiter makeup gain — the main "make quiet clips loud" stage.
  const makeup = context.createGain();
  makeup.gain.value = linearGainFromDb(10);

  source.connect(highPass);
  highPass.connect(presence);
  presence.connect(gain);
  gain.connect(compressor);
  compressor.connect(makeup);
  makeup.connect(context.destination);

  const graph = { context, compressor, gain, makeup, highPass, presence, source } satisfies AudioGraph;
  graphRef.current = graph;

  if (context.state === 'suspended') {
    await context.resume();
  }

  return graph;
}

export function cleanupAudioGraph(graphRef: { current: AudioGraph | null }) {
  const currentGraph = graphRef.current;
  if (!currentGraph) {
    return;
  }
  void currentGraph.context.close().catch(() => undefined);
  graphRef.current = null;
}

export function applyAudioAnalysis(graph: AudioGraph, analysis: AudioAnalysis) {
  const now = graph.context.currentTime;
  graph.gain.gain.setValueAtTime(linearGainFromDb(analysis.gainDb), now);
  graph.highPass.frequency.setValueAtTime(
    analysis.quality === 'very-quiet' ? 220 : 160,
    now,
  );
  graph.presence.gain.setValueAtTime(
    analysis.quality === 'normal' ? 3 : analysis.quality === 'quiet' ? 6 : 8,
    now,
  );
  // Lower limiter ceiling for quieter sources so the makeup below can lift them
  // further while the limiter still catches the peaks.
  graph.compressor.threshold.setValueAtTime(
    analysis.quality === 'normal' ? -16 : -18,
    now,
  );
  graph.compressor.ratio.setValueAtTime(
    analysis.quality === 'very-quiet' ? 20 : 16,
    now,
  );
  // Makeup loudness, pushed hard for quiet field recordings.
  graph.makeup.gain.setValueAtTime(
    linearGainFromDb(
      analysis.quality === 'normal' ? 8 : analysis.quality === 'quiet' ? 11 : 13,
    ),
    now,
  );
}

export async function analyzeAudioUrl(
  url: string,
  context: AudioContext,
  signal: AbortSignal,
) {
  const cached = audioAnalysisCache.get(url);
  if (cached) {
    return cached;
  }

  const response = await fetch(url, { cache: 'force-cache', signal });
  if (!response.ok) {
    throw new Error(`Audio analysis fetch failed with status ${response.status}`);
  }

  const encoded = await response.arrayBuffer();
  if (signal.aborted) {
    throw new DOMException('Audio analysis aborted', 'AbortError');
  }

  const decoded = await context.decodeAudioData(encoded.slice(0));
  const mono = extractMonoSamples(decoded);
  let energy = 0;
  for (let index = 0; index < mono.length; index += 1) {
    const sample = mono[index];
    energy += sample * sample;
  }

  const rmsDbfs = dbfsFromLinear(Math.sqrt(energy / Math.max(mono.length, 1)));
  const dominantHz = estimateDominantHz(mono, decoded.sampleRate);
  const analysis = {
    gainDb: clamp(TARGET_RMS_DBFS - rmsDbfs, 0, MAX_GAIN_DB),
    quality: resolveAudioQuality(rmsDbfs),
    rmsDbfs,
    dominantHz,
    playbackRate: playbackRateForDominant(dominantHz),
  } satisfies AudioAnalysis;

  audioAnalysisCache.set(url, analysis);
  return analysis;
}

// Bird calls often peak well above the band that small speakers and human
// hearing reproduce well, so when a clip's dominant frequency is high we slow
// playback with pitch-tracking disabled — lowering the actual pitch into the
// audible range (and stretching the call out, which also helps it read). Calls
// already in a comfortable range get rate 1 and play normally.
function applyPlaybackPitch(mediaEl: HTMLMediaElement, playbackRate: number) {
  const el = mediaEl as HTMLMediaElement & {
    preservesPitch?: boolean;
    mozPreservesPitch?: boolean;
    webkitPreservesPitch?: boolean;
  };
  // Let rate change pitch instead of time-stretching to preserve it.
  el.preservesPitch = false;
  el.mozPreservesPitch = false;
  el.webkitPreservesPitch = false;
  // defaultPlaybackRate so the rate survives a later src load; playbackRate for
  // the case where the same clip is replayed without reloading.
  mediaEl.defaultPlaybackRate = playbackRate;
  mediaEl.playbackRate = playbackRate;
}

export async function prepareHearingAssist(
  mediaEl: HTMLMediaElement,
  url: string,
  graphRef: { current: AudioGraph | null },
  signal: AbortSignal,
) {
  const graph = await ensureAudioGraph(mediaEl, graphRef);
  if (!graph) {
    return defaultAudioAnalysis;
  }

  const analysis = await analyzeAudioUrl(url, graph.context, signal);
  applyAudioAnalysis(graph, analysis);
  applyPlaybackPitch(mediaEl, analysis.playbackRate);
  return analysis;
}

export function buildAudioInsight(analysis: AudioAnalysis): AudioInsight | null {
  if (analysis.quality === 'very-quiet') {
    return { label: 'very quiet source', tone: 'warning' };
  }
  if (analysis.gainDb >= 6) {
    return { label: `hear +${Math.round(analysis.gainDb)}dB`, tone: 'boost' };
  }
  return null;
}