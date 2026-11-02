#!/usr/bin/env python3
"""Generate realistic BirdWeather-structured sample data for 2026-03-06.

Produces:
  data/raw/birdweather/2026-03-06/
    detections.jsonl   - 63 detection records
    metadata.json      - collection stats
    audio/             - 63 x 3-second FLAC files with realistic bird+noise signals

Noise types match the full BirdWeather taxonomy:
  wind, rain, traffic, biophony, impulsive, water, hvac, urban, clean
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt


random.seed(42)
np.random.seed(42)
rng = np.random.default_rng(42)

_REPO_ROOT = Path(__file__).resolve().parents[2]
SR = 22050
DURATION_S = 3.0
DATE = "2026-03-06"
OUT_DIR = _REPO_ROOT / "data" / "raw" / "birdweather" / DATE
AUDIO_DIR = OUT_DIR / "audio"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

SPECIES = [
    {
        "id": "6",
        "commonName": "Common Blackbird",
        "scientificName": "Turdus merula",
        "freq_hz": [3200, 4100],
    },
    {
        "id": "11",
        "commonName": "Eurasian Robin",
        "scientificName": "Erithacus rubecula",
        "freq_hz": [3800, 5200],
    },
    {
        "id": "22",
        "commonName": "Great Tit",
        "scientificName": "Parus major",
        "freq_hz": [3500, 5500],
    },
    {
        "id": "31",
        "commonName": "Blue Tit",
        "scientificName": "Cyanistes caeruleus",
        "freq_hz": [4500, 7000],
    },
    {
        "id": "44",
        "commonName": "Eurasian Wren",
        "scientificName": "Troglodytes troglodytes",
        "freq_hz": [5000, 7500],
    },
    {
        "id": "55",
        "commonName": "Common Chaffinch",
        "scientificName": "Fringilla coelebs",
        "freq_hz": [3000, 5000],
    },
    {
        "id": "67",
        "commonName": "Eurasian Nuthatch",
        "scientificName": "Sitta europaea",
        "freq_hz": [2800, 4500],
    },
    {
        "id": "78",
        "commonName": "Song Thrush",
        "scientificName": "Turdus philomelos",
        "freq_hz": [3600, 5800],
    },
    {
        "id": "89",
        "commonName": "Garden Warbler",
        "scientificName": "Sylvia borin",
        "freq_hz": [4200, 7000],
    },
    {
        "id": "99",
        "commonName": "Common Wood Pigeon",
        "scientificName": "Columba palumbus",
        "freq_hz": [700, 1500],
    },
]

NOISE_TYPES = (
    ["wind"] * 5
    + ["rain"] * 5
    + ["thunder"] * 4
    + ["traffic"] * 5
    + ["aircraft"] * 4
    + ["biophony"] * 5
    + ["frog_chorus"] * 4
    + ["impulsive"] * 5
    + ["water"] * 5
    + ["hvac"] * 5
    + ["urban"] * 5
    + ["clean"] * 5
)
random.shuffle(NOISE_TYPES)
N_SAMPLES = len(NOISE_TYPES)

STATION_IDS = [f"NL-{1000 + i}" for i in range(8)]


def make_bird_signal(freq_low, freq_high, sr=SR):
    """Synthesise a realistic bird call."""
    t = np.linspace(0, DURATION_S, int(DURATION_S * sr), dtype=np.float32)
    n = len(t)
    signal = np.zeros(n, dtype=np.float32)
    fund = rng.uniform(freq_low, (freq_low + freq_high) / 2)
    am_freq = rng.uniform(10, 25)
    am = 0.5 * (1 + np.sin(2 * np.pi * am_freq * t))
    for harmonic in range(1, 5):
        freq_h = fund * harmonic
        if freq_h > sr / 2:
            break
        phase_offset = rng.uniform(0, 2 * np.pi)
        fm_depth = rng.uniform(0.01, 0.05) * freq_h
        fm_rate = rng.uniform(5, 20)
        inst_freq = freq_h + fm_depth * np.sin(2 * np.pi * fm_rate * t)
        phase = np.cumsum(inst_freq) / sr
        signal += (1.0 / harmonic) * am * np.sin(2 * np.pi * phase + phase_offset)
    mx = np.max(np.abs(signal)) + 1e-10
    return (signal / mx * 0.8).astype(np.float32)


def make_noise(noise_type, n_samp=int(DURATION_S * SR), sr=SR):
    """Synthesise realistic background noise for each noise family."""
    t = np.linspace(0, n_samp / sr, n_samp)

    if noise_type == "wind":
        gust_env = np.abs(np.sin(2 * np.pi * 1.5 * t)) * rng.uniform(0.5, 1.5, n_samp)
        sos = butter(3, 300 / (sr / 2), btype="low", output="sos")
        noise = sosfilt(sos, rng.standard_normal(n_samp) * gust_env)

    elif noise_type == "rain":
        noise = rng.standard_normal(n_samp) * 0.35
        sos_h = butter(
            4, [4400 / (sr / 2), 5600 / (sr / 2)], btype="band", output="sos"
        )
        noise += sosfilt(sos_h, rng.standard_normal(n_samp)) * 0.4
        n_drops = int(n_samp * 0.005)
        drop_idx = rng.integers(0, n_samp, size=n_drops)
        noise[drop_idx] += rng.standard_normal(n_drops) * 1.2

    elif noise_type == "thunder":
        noise = rng.standard_normal(n_samp) * 0.02
        crack_len = int(0.008 * sr)
        noise[:crack_len] += rng.standard_normal(crack_len) * rng.uniform(2.0, 3.5)
        decay = np.exp(-np.linspace(0, 4, n_samp))
        sos_r = butter(3, 250 / (sr / 2), btype="low", output="sos")
        noise += sosfilt(sos_r, rng.standard_normal(n_samp) * decay * 0.9)
        noise = noise.astype(np.float32)

    elif noise_type == "traffic":
        sos = butter(4, [200 / (sr / 2), 800 / (sr / 2)], btype="band", output="sos")
        env = 0.6 + 0.4 * np.abs(np.sin(2 * np.pi * 0.3 * t))
        noise = sosfilt(sos, rng.standard_normal(n_samp)) * env * 0.7

    elif noise_type == "biophony":
        noise = np.zeros(n_samp, dtype=np.float32)
        for f_cic in [2200, 3500, 5000, 6500, 8000]:
            if f_cic < sr / 2:
                am = 0.95 + 0.05 * np.sin(2 * np.pi * rng.uniform(0.5, 2) * t)
                phi = rng.uniform(0, 2 * np.pi)
                noise += (0.2 * am * np.sin(2 * np.pi * f_cic * t + phi)).astype(
                    np.float32
                )
        chirp_rate = rng.uniform(3, 8)
        chirp_env = (np.sin(2 * np.pi * chirp_rate * t) > 0).astype(float)
        sos_c = butter(
            4, [4000 / (sr / 2), 8000 / (sr / 2)], btype="band", output="sos"
        )
        noise += (
            sosfilt(sos_c, rng.standard_normal(n_samp)) * chirp_env * 0.15
        ).astype(np.float32)
        noise = noise.astype(np.float32)

    elif noise_type == "frog_chorus":
        noise = np.zeros(n_samp, dtype=np.float32)
        for f_frog in [600, 900, 1200, 1800, 2200]:
            if f_frog < sr / 2:
                am = 0.9 + 0.1 * np.sin(2 * np.pi * rng.uniform(1, 3) * t)
                phi = rng.uniform(0, 2 * np.pi)
                noise += (0.25 * am * np.sin(2 * np.pi * f_frog * t + phi)).astype(
                    np.float32
                )
        noise = noise.astype(np.float32)

    elif noise_type == "aircraft":
        flyover_center = rng.uniform(0.8, 2.2)
        flyover_width = rng.uniform(0.3, 0.7)
        env = np.exp(-((t - flyover_center) ** 2) / flyover_width)
        sos_ac = butter(
            4, [100 / (sr / 2), 1500 / (sr / 2)], btype="band", output="sos"
        )
        noise = (sosfilt(sos_ac, rng.standard_normal(n_samp)) * env * 0.8).astype(
            np.float32
        )
        noise = rng.standard_normal(n_samp) * 0.02
        n_events = rng.integers(2, 6)
        event_len = int(0.005 * sr)
        for _ in range(n_events):
            start = rng.integers(0, max(1, n_samp - event_len))
            amplitude = rng.uniform(1.5, 3.0)
            impulse_shape = amplitude * np.exp(-np.linspace(0, 5, event_len))
            noise[start : start + event_len] += impulse_shape * rng.choice([-1, 1])
        noise = noise.astype(np.float32)

    elif noise_type == "water":
        white = rng.standard_normal(n_samp)
        fft_w = np.fft.rfft(white)
        freqs_fft = np.fft.rfftfreq(n_samp, 1 / sr)
        psd = np.where(freqs_fft > 0, 1 / (freqs_fft**0.5 + 1e-10), 1.0)
        pink = np.fft.irfft(fft_w * psd, n=n_samp).astype(np.float32)
        sos_hp = butter(2, 50 / (sr / 2), btype="high", output="sos")
        pink = sosfilt(sos_hp, pink)
        noise = (pink / (np.max(np.abs(pink)) + 1e-10) * 0.6).astype(np.float32)
        noise *= 0.7 + 0.3 * np.abs(np.sin(2 * np.pi * 0.2 * t))

    elif noise_type == "hvac":
        noise = np.zeros(n_samp, dtype=np.float32)
        for harmonic in [50, 100, 150, 200]:
            if harmonic < sr / 2:
                amp = 0.5 / (harmonic / 50)
                noise += (
                    amp * np.sin(2 * np.pi * harmonic * t + rng.uniform(0, 2 * np.pi))
                ).astype(np.float32)
        sos_f = butter(4, [120 / (sr / 2), 180 / (sr / 2)], btype="band", output="sos")
        noise += (sosfilt(sos_f, rng.standard_normal(n_samp)) * 0.2).astype(np.float32)

    elif noise_type == "urban":
        sos_v = butter(3, [300 / (sr / 2), 3400 / (sr / 2)], btype="band", output="sos")
        sos_d = butter(3, [500 / (sr / 2), 2000 / (sr / 2)], btype="band", output="sos")
        noise = 0.35 * sosfilt(sos_v, rng.standard_normal(n_samp)) + 0.25 * sosfilt(
            sos_d, rng.standard_normal(n_samp)
        )

    else:
        noise = rng.standard_normal(n_samp) * 0.02

    return np.asarray(noise, dtype=np.float32)


print(f"Generating {N_SAMPLES} synthetic BirdWeather detections for {DATE}...")
detections = []
base_time = datetime(2026, 3, 6, 5, 30, 0, tzinfo=UTC)

for i, noise_type in enumerate(NOISE_TYPES):
    species = random.choice(SPECIES)
    det_time = base_time + timedelta(seconds=random.randint(0, 3600 * 8))
    det_id = 2000000 + i
    station_id = random.choice(STATION_IDS)
    confidence = round(random.uniform(0.70, 0.99), 3)
    lat = round(52.0 + random.uniform(-0.8, 0.8), 4)
    lon = round(4.9 + random.uniform(-1.0, 1.0), 4)

    detection = {
        "id": str(det_id),
        "timestamp": det_time.isoformat(),
        "speciesId": species["id"],
        "confidence": confidence,
        "species": {
            "id": species["id"],
            "commonName": species["commonName"],
            "scientificName": species["scientificName"],
            "imageUrl": f"https://example.com/img/{species['id']}.jpg",
        },
        "station": {
            "id": station_id,
            "name": f"Station {station_id}",
            "coords": {"lat": lat, "lon": lon},
            "timezone": "Europe/Amsterdam",
        },
        "stationId": station_id,
        "soundscape": {
            "url": f"https://example.com/audio/{det_id}.flac",
            "startTime": 0.0,
            "endTime": DURATION_S,
            "duration": DURATION_S,
        },
        "_collected_at": datetime.now(UTC).isoformat(),
        "_noise_type": noise_type,
    }
    detections.append(detection)

    n_samp = int(DURATION_S * SR)
    bird = make_bird_signal(species["freq_hz"][0], species["freq_hz"][1])
    noise = make_noise(noise_type, n_samp)

    target_snr = random.uniform(5, 20)
    bird_rms = np.sqrt(np.mean(bird**2)) + 1e-10
    noise_rms = np.sqrt(np.mean(noise**2)) + 1e-10
    desired_noise_rms = bird_rms / (10 ** (target_snr / 20))
    noise_scaled = noise * (desired_noise_rms / noise_rms)

    mixture = np.clip(bird + noise_scaled, -1, 1).astype(np.float32)
    sf.write(str(AUDIO_DIR / f"{det_id}_{species['id']}.flac"), mixture, SR)

with open(OUT_DIR / "detections.jsonl", "w", encoding="utf-8") as file_handle:
    for detection in detections:
        file_handle.write(json.dumps(detection) + "\n")

species_seen = {}
for detection in detections:
    for species in SPECIES:
        if species["id"] == detection["speciesId"]:
            species_seen[species["id"]] = species["commonName"]

noise_counts = Counter(NOISE_TYPES)
metadata = {
    "date": DATE,
    "detections_count": N_SAMPLES,
    "audio_count": N_SAMPLES,
    "species_list": [
        {"id": key, "commonName": value} for key, value in species_seen.items()
    ],
    "country": "Netherlands",
    "min_confidence": 0.7,
    "generated_synthetic": True,
    "noise_type_distribution": dict(noise_counts),
}
with open(OUT_DIR / "metadata.json", "w", encoding="utf-8") as file_handle:
    json.dump(metadata, file_handle, indent=2)

print(f"✓ Generated {N_SAMPLES} detections + audio")
print(f"  Location: {OUT_DIR}")
print(f"  Audio files: {len(list(AUDIO_DIR.glob('*.flac')))}")
print(f"  Noise distribution: {dict(noise_counts)}")
