"""Background sound classification using EfficientAT mn10_as.

Classifies environmental / background sounds (rain, wind, traffic, speech,
etc.) in **raw** BirdWeather audio clips using the EfficientAT mn10_as model
(Schmid et al. 2023).  AudioSet covers 527 sound event classes — all labels
above a configurable confidence threshold are kept (default >= 10 %).

Why raw audio?
--------------
The processing pipeline applies a 250–10 kHz bandpass filter to isolate bird
vocalizations.  Background sounds (traffic 50–500 Hz, wind < 500 Hz, rain
broadband, speech 85–255 Hz) have most energy *below* that cutoff.  Running
the model on bandpassed audio would destroy the very features it needs.
Background conditions are a *recording property* — like GPS coordinates or
weather data — so they belong in the ingestion layer.

Architecture
------------
EfficientAT mn10_as (Schmid et al. 2023, ICASSP) is a MobileNet v3–based
audio tagger trained on AudioSet.  Input preprocessing uses ``AugmentMelSTFT``
(preemphasis + 128-bin log-mel spectrogram at 32 kHz, 800-sample window,
320-sample hop) from the EfficientAT repo.  Output: 527-class sigmoid
probabilities.

The EfficientAT codebase is loaded on demand from the torch hub cache
(``~/.cache/torch/hub/fschmid56_EfficientAT_main``).  Model weights
(``mn10_as.pt``) are downloaded once and stored under ``resources/`` in
that directory.

Usage — Python API
------------------
::

    from avian_biosurveillance.ingestion.background_sound_classification import (
        BackgroundSoundClassifier,
    )

    clf = BackgroundSoundClassifier(device="cpu", top_k=5, min_confidence=0.05)
    tags = clf.classify("audio.mp3")
    # [AmbientTag(label="Rain", score=0.82), AmbientTag(label="Wind", score=0.45)]

    # Batched for throughput:
    tag_lists = clf.classify_batch(["a.mp3", "b.mp3"], batch_size=8)

Usage — CLI enrichment
----------------------
::

    # Enrich a single date directory (already downloaded)
    python -m avian_biosurveillance.ingestion.background_sound_classification \\
        --input data/raw/birdweather/2025-01-01 \\
        --device cuda --batch-size 8

    # Enrich all date directories under the birdweather root
    python -m avian_biosurveillance.ingestion.background_sound_classification \\
        --input data/raw/birdweather \\
        --device cuda --batch-size 16

    # Force re-classification (ignore existing tags)
    python -m avian_biosurveillance.ingestion.background_sound_classification \\
        --input data/raw/birdweather/2025-01-01 --force

    # Dry-run: show what would be classified without writing
    python -m avian_biosurveillance.ingestion.background_sound_classification \\
        --input data/raw/birdweather --dry-run

CLI scenarios -- Handled automatically
--------------------------------------
1. **Fresh data** (no enrichment exists): classifies all audio, writes
   tags back into ``detections.jsonl``.
2. **Partially enriched** (previous run interrupted or new detections):
   classifies only un-tagged detections.  Non-terminal statuses
   (``audio_not_downloaded``, ``decode_failed``, ``empty_audio``) are
   retried automatically.
3. **Fully enriched** (``--force`` not set): skips with a log message.
4. **No audio directory**: writes ``no_source_audio`` or
   ``audio_not_downloaded`` status for each detection (no early abort).
5. **Audio downloaded but detection has no matching file**: marks with
   ``audio_not_downloaded`` or ``no_source_audio`` depending on whether
   the detection has a ``soundscape.url``.
6. **Date range** over parent dir: iterates all ``YYYY-MM-DD`` subdirs.
7. **Dry-run**: reports counts without modifying files.

References
----------
- Schmid, F. et al. (2023). Efficient Large-Scale Audio Tagging via
  Transformer-to-CNN Knowledge Distillation. ICASSP 2023.
- AudioSet ontology: https://research.google.com/audioset/ontology/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import librosa
import numpy as np

from avian_biosurveillance.utils.progress import tqdm
from avian_biosurveillance.utils.noisy_runtime import suppress_third_party_output


logger = logging.getLogger(__name__)

_EFFICIENTAT_BOOTSTRAP_WARNING_PATTERNS = (
    r".*ConvNormActivation directly.*",
)
_EFFICIENTAT_RUNTIME_WARNING_PATTERNS = (
    r".*stft with return_complex=False is deprecated.*",
    r".*autocast.*deprecated.*",
)

# EfficientAT mn10_as expects 32 kHz mono input
_MODEL_SR = 32_000

# EfficientAT mn10_as mel-spectrogram parameters (must match training config)
_MEL_WIN_LENGTH = 800  # samples at 32 kHz (~25 ms)
_MEL_HOP_LENGTH = 320  # samples at 32 kHz (~10 ms)
_MEL_N_MELS = 128


def _ensure_writable_directory(path: Path) -> bool:
    """Create *path* when needed and report whether the runtime can write it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK | os.X_OK)


def _resolve_runtime_directory(
    preferred: Path,
    *,
    fallback: Path,
    purpose: str,
) -> Path:
    """Return a writable runtime directory, falling back to the app user's home."""
    preferred = preferred.expanduser()
    fallback = fallback.expanduser()

    if _ensure_writable_directory(preferred):
        return preferred
    if preferred != fallback and _ensure_writable_directory(fallback):
        logger.warning(
            "Configured %s directory %s is not writable; using %s instead.",
            purpose,
            preferred,
            fallback,
        )
        return fallback

    msg = (
        f"{purpose} directory is not writable: {preferred} "
        f"(fallback {fallback} is also unavailable)"
    )
    raise PermissionError(msg)


def _resolve_torch_hub_dir() -> Path:
    configured_torch_home = os.environ.get("TORCH_HOME")
    preferred = (
        Path(configured_torch_home).expanduser() / "hub"
        if configured_torch_home
        else Path.home() / ".cache" / "torch" / "hub"
    )
    fallback = Path.home() / ".cache" / "torch" / "hub"
    return _resolve_runtime_directory(
        preferred,
        fallback=fallback,
        purpose="EfficientAT torch hub cache",
    )


def _resolve_background_weights_dir() -> Path:
    configured_weights_dir = os.environ.get("AVIAN_WEIGHTS_DIR")
    preferred = (
        Path(configured_weights_dir).expanduser()
        if configured_weights_dir
        else Path(__file__).parents[3] / "weights"
    )
    fallback = Path.home() / ".cache" / "avian_biosurveillance" / "weights"
    return _resolve_runtime_directory(
        preferred,
        fallback=fallback,
        purpose="EfficientAT model weights",
    )


def _decode_with_ffmpeg(
    source: str | Path,
    sr: int = _MODEL_SR,
) -> np.ndarray:
    """Decode any audio format to mono float32 PCM via ``ffmpeg``.

    Handles M4A, AAC, MP3, FLAC, WAV, OGG, and every other format that
    ffmpeg supports.  Resampling to *sr* is done inside ffmpeg in a
    single pass — no Python-side resampling needed.

    Args:
        source: Path to an audio file.
        sr: Target sample rate.

    Returns:
        1-D float32 numpy array.

    Raises:
        RuntimeError: If ffmpeg is not installed or fails to decode.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(source),
            "-f",
            "f32le",  # raw float32 little-endian PCM
            "-acodec",
            "pcm_f32le",
            "-ac",
            "1",  # mono
            "-ar",
            str(sr),
            "-v",
            "quiet",
            "pipe:1",
        ],
        capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) == 0:
        raise RuntimeError(
            f"ffmpeg failed to decode {source} (exit {result.returncode})"
        )
    return np.frombuffer(result.stdout, dtype=np.float32).copy()


# Peak-normalize target; matches the process pipeline convention
_NORM_PEAK = 0.9

# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

MODEL_ID = "mn10_as"
"""EfficientAT model name (AudioSet, MobileNet v3)."""

# ──────────────────────────────────────────────────────────────────────────
# Status model
# ──────────────────────────────────────────────────────────────────────────

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "classified",
        "no_source_audio",
    }
)
"""Statuses that represent a final outcome — skipped on rerun.

- ``"classified"``: audio successfully decoded and classified (tags may
  be empty if nothing exceeded ``min_confidence``).
- ``"no_source_audio"``: the BirdWeather detection has no
  ``soundscape.url`` — there is nothing to download or classify.
"""

NON_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "audio_not_downloaded",
        "decode_failed",
        "empty_audio",
    }
)
"""Statuses that represent a retryable state — re-attempted on rerun.

- ``"audio_not_downloaded"``: the detection has a ``soundscape.url`` but
  no local audio file was found.
- ``"decode_failed"``: a local audio file exists but could not be decoded
  (corrupt, unsupported codec, environment issue).
- ``"empty_audio"``: the file decoded successfully but produced a
  zero-length or zero-energy array.
"""


class EmptyAudioError(Exception):
    """Audio file decoded to a zero-length array."""


# ──────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AmbientTag:
    """Single ambient sound classification result."""

    label: str
    """AudioSet label name (e.g. ``"Rain"``, ``"Traffic noise, roadway noise"``)."""

    score: float
    """Confidence score from the model (0.0–1.0)."""


@dataclass
class EnrichmentStats:
    """Summary statistics for an enrichment run."""

    total_detections: int = 0
    audio_matched: int = 0
    classified: int = 0
    skipped_existing: int = 0
    no_source_audio: int = 0
    audio_not_downloaded: int = 0
    decode_failed: int = 0
    empty_audio: int = 0
    avg_tags_per_detection: float = 0.0
    elapsed_s: float = 0.0


# ──────────────────────────────────────────────────────────────────────────
# Classifier
# ──────────────────────────────────────────────────────────────────────────


class BackgroundSoundClassifier:
    """EfficientAT mn10_as classifier for ambient sound profiling.

    Wraps the ``mn10_as`` EfficientAT model (Schmid et al. 2023) with:
    - Lazy model loading (first call triggers download/load)
    - All 527 AudioSet labels preserved (filter downstream)
    - Batched inference support for throughput
    - Automatic device selection (CUDA > MPS > CPU)

    The EfficientAT codebase is sourced from the torch hub cache
    (``fschmid56/EfficientAT`` main branch).  ``AugmentMelSTFT`` is used
    for preprocessing to exactly match the training pipeline.

    Args:
        device: ``"cuda"``, ``"cpu"``, or ``None`` for auto-detect.
        top_k: Maximum tags to return per file (``None`` = all).
        min_confidence: Minimum score threshold (default ``0.10``).
        model_id: EfficientAT model name (override for testing).
    """

    def __init__(
        self,
        device: str | None = None,
        top_k: int | None = None,
        min_confidence: float = 0.10,
        model_id: str = MODEL_ID,
    ) -> None:
        self.device = device or self._detect_device()
        self.top_k = top_k
        self.min_confidence = min_confidence
        self.model_id = model_id
        self._model = None  # Lazy-loaded EfficientAT MobileNet
        self._mel_proc = None  # AugmentMelSTFT from EfficientAT
        self._labels: list[str] = []  # 527 AudioSet class names

    # ── Device detection ──────────────────────────────────────────────

    @staticmethod
    def _detect_device() -> str:
        """Auto-detect best available device (CUDA > MPS > CPU)."""
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    # ── Lazy model loading ────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Load EfficientAT model and preprocessor on first use."""
        if self._model is not None:
            return

        import csv
        import urllib.request
        import zipfile

        import torch

        hub_dir = _resolve_torch_hub_dir()
        torch.hub.set_dir(str(hub_dir))
        efficientat_dir = hub_dir / "fschmid56_EfficientAT_main"
        extracted_dir = hub_dir / "EfficientAT-main"

        # Download and unzip the EfficientAT repo if not already cached
        if not efficientat_dir.exists():
            if not extracted_dir.exists():
                zip_path = hub_dir / "_efficientat_main.zip"
                logger.info("Downloading EfficientAT repo to %s …", hub_dir)
                urllib.request.urlretrieve(
                    "https://github.com/fschmid56/EfficientAT/archive/main.zip",
                    str(zip_path),
                )
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(hub_dir)
                zip_path.unlink(missing_ok=True)
            if extracted_dir.exists():
                extracted_dir.replace(efficientat_dir)

        # Load AudioSet class labels from the metadata CSV
        csv_path = efficientat_dir / "metadata" / "class_labels_indices.csv"
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            labels = [row["display_name"] for row in reader]

        logger.info(
            "Loading EfficientAT model '%s' on device='%s'…",
            self.model_id,
            self.device,
        )
        t0 = time.perf_counter()

        # Project-level weights directory — all downloaded .pt files land here
        weights_dir = _resolve_background_weights_dir()

        # EfficientAT uses relative paths for CSV labels, so CWD must be the
        # repo root during import. Weights are redirected to weights_dir by
        # patching the module-level model_dir variable before get_model runs.
        orig_cwd = os.getcwd()
        efficientat_str = str(efficientat_dir)
        sys.path.insert(0, efficientat_str)
        os.chdir(efficientat_dir)
        try:
            with suppress_third_party_output(
                warning_messages=_EFFICIENTAT_BOOTSTRAP_WARNING_PATTERNS,
            ):
                import models.mn.model as _mn_model_module  # type: ignore[import]
                from helpers.utils import NAME_TO_WIDTH  # type: ignore[import]
                from models.mn.model import (
                    get_model as get_mobilenet,  # type: ignore[import]
                )
                from models.preprocess import AugmentMelSTFT  # type: ignore[import]

                # Redirect weight downloads from the hub-cache "resources/" folder
                # to the project's ./weights/ directory.
                _mn_model_module.model_dir = str(weights_dir)

                model = get_mobilenet(
                    width_mult=NAME_TO_WIDTH(self.model_id),
                    pretrained_name=self.model_id,
                )
                mel_proc = AugmentMelSTFT(
                    n_mels=_MEL_N_MELS,
                    sr=_MODEL_SR,
                    win_length=_MEL_WIN_LENGTH,
                    hopsize=_MEL_HOP_LENGTH,
                    freqm=0,  # no augmentation at inference
                    timem=0,  # no augmentation at inference
                )
        finally:
            os.chdir(orig_cwd)

        model.eval()
        model.to(self.device)
        mel_proc.eval()
        mel_proc.to(self.device)

        self._model = model
        self._mel_proc = mel_proc
        self._labels = labels

        elapsed = time.perf_counter() - t0
        logger.info("EfficientAT model loaded in %.1f s", elapsed)

    # ── Audio loading & normalization ──────────────────────────────────

    @staticmethod
    def _load_and_normalize(
        path: str | Path,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> np.ndarray:
        """Load audio, optionally crop, and peak-normalize for inference.

        BirdWeather raw recordings are extremely quiet (peaks at −26 to
        −46 dBFS).  Without normalization the mel-spectrogram is crushed
        near-zero and the model predicts *Silence* for everything.

        Decoding strategy (most audio from BirdWeather is M4A in a
        ``.mp3`` container, which neither PySoundFile nor mpg123 handle):

        1. ``soundfile.read`` — fast C path for FLAC / WAV / OGG.
        2. ``ffmpeg`` subprocess — universal fallback for M4A / AAC / etc.

        Args:
            path: Path to an audio file.
            start_sec: If given, crop audio starting at this second offset.
            end_sec: If given, crop audio ending at this second offset.

        Returns:
            Peak-normalized float32 numpy array at ``_MODEL_SR``.
        """
        import soundfile as sf

        audio: np.ndarray | None = None

        # Fast path: soundfile handles FLAC, WAV, OGG, AIFF natively
        try:
            data, sr = sf.read(str(path))
            if data.ndim > 1:
                data = data.mean(axis=1)
            if sr != _MODEL_SR:
                data = librosa.resample(data, orig_sr=sr, target_sr=_MODEL_SR)
            audio = data
        except Exception:
            pass

        # Fallback: ffmpeg decodes M4A, AAC, MP3, and everything else
        if audio is None:
            audio = _decode_with_ffmpeg(path, sr=_MODEL_SR)

        # Crop to the detection's BirdNET analysis window if specified.
        if start_sec is not None or end_sec is not None:
            start_sample = int((start_sec or 0.0) * _MODEL_SR)
            end_sample = int(end_sec * _MODEL_SR) if end_sec is not None else len(audio)
            start_sample = max(0, min(start_sample, len(audio)))
            end_sample = max(start_sample, min(end_sample, len(audio)))
            audio = audio[start_sample:end_sample]

        # Guard: zero-length audio (truncated file, empty stream, etc.)
        if len(audio) == 0:
            raise EmptyAudioError(f"Audio file decoded to zero-length array: {path}")

        # Peak-normalize so the model sees normally-leveled input
        peak = np.max(np.abs(audio))
        if peak > 1e-10:
            audio = audio / peak * _NORM_PEAK

        return audio.astype(np.float32)

    # ── Mel-spectrogram + inference helpers ───────────────────────────

    def _audio_to_mel(self, audio: np.ndarray) -> torch.Tensor:
        """Convert a raw float32 array to a log-mel spectrogram tensor.

        Uses EfficientAT's ``AugmentMelSTFT`` to exactly replicate the
        training-time preprocessing pipeline.

        Returns:
            Tensor of shape ``[1, n_mels, T]`` on CPU.
        """
        import torch

        waveform = (
            torch.from_numpy(audio).float().unsqueeze(0).to(self.device)
        )  # [1, samples]
        with torch.no_grad():
            with suppress_third_party_output(
                capture_stdout=False,
                capture_stderr=False,
                warning_messages=_EFFICIENTAT_RUNTIME_WARNING_PATTERNS,
            ):
                mel = self._mel_proc(waveform)  # [1, n_mels, T]
        return mel.cpu()

    def _infer_mels(self, mels: list[torch.Tensor]) -> list[list[dict]]:
        """Run EfficientAT on a list of ``[1, n_mels, T]`` CPU tensors.

        Pads all tensors to the same time length before batching, then
        applies sigmoid to logits for multi-label probabilities.

        Returns:
            One ``list[dict]`` of ``{"label": str, "score": float}``
            per input tensor.
        """
        import torch
        import torch.nn.functional as F

        max_T = max(m.shape[-1] for m in mels)
        padded = [F.pad(m, (0, max_T - m.shape[-1])) for m in mels]
        # Each mel is [1, n_mels, T] → stack → [B, 1, n_mels, T]
        batch = torch.stack(padded, dim=0).to(self.device)

        with torch.no_grad():
            with suppress_third_party_output(
                capture_stdout=False,
                capture_stderr=False,
                warning_messages=_EFFICIENTAT_RUNTIME_WARNING_PATTERNS,
            ):
                preds, _ = self._model(batch)  # [B, 527]
            probs = torch.sigmoid(preds.float()).cpu().numpy()

        return [
            [
                {"label": self._labels[i], "score": float(p[i])}
                for i in range(len(self._labels))
            ]
            for p in probs
        ]

    # ── Single-file classification ────────────────────────────────────

    def classify(
        self,
        audio_path: str | Path,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> list[AmbientTag]:
        """Classify ambient sounds in a single audio file.

        Args:
            audio_path: Path to audio file (MP3, FLAC, WAV, M4A, …).
            start_sec: If given, crop audio starting at this second offset.
            end_sec: If given, crop audio ending at this second offset.

        Returns:
            Sorted list of ``AmbientTag`` (highest score first),
            above ``min_confidence`` threshold.
        """
        self._ensure_loaded()
        assert self._model is not None

        audio = self._load_and_normalize(audio_path, start_sec, end_sec)
        mel = self._audio_to_mel(audio)
        results = self._infer_mels([mel])[0]
        return self._filter_results(results)

    def classify_array(
        self,
        audio: np.ndarray,
        sample_rate: int = _MODEL_SR,
    ) -> list[AmbientTag]:
        """Classify ambient sounds from an in-memory numpy array.

        Audio is resampled to 32 kHz and peak-normalized before inference.

        Args:
            audio: 1D float32 numpy array.
            sample_rate: Sample rate of the audio array.

        Returns:
            Sorted list of ``AmbientTag`` above ``min_confidence``.
        """
        self._ensure_loaded()
        assert self._model is not None

        if sample_rate != _MODEL_SR:
            audio = librosa.resample(
                audio.astype(np.float32),
                orig_sr=sample_rate,
                target_sr=_MODEL_SR,
            )

        peak = np.max(np.abs(audio))
        if peak > 1e-10:
            audio = audio / peak * _NORM_PEAK

        mel = self._audio_to_mel(audio.astype(np.float32))
        results = self._infer_mels([mel])[0]
        return self._filter_results(results)

    # ── Batched classification ────────────────────────────────────────

    def classify_batch(
        self,
        audio_paths: Sequence[str | Path],
        batch_size: int = 8,
        crop_windows: (
            Sequence[tuple[float | None, float | None] | None] | None
        ) = None,
    ) -> list[list[AmbientTag] | str]:
        """Classify ambient sounds in multiple audio files with batching.

        Args:
            audio_paths: Sequence of audio file paths.
            batch_size: Inference batch size.
            crop_windows: Optional parallel sequence
                of ``(start_sec, end_sec)``
                tuples aligned with *audio_paths*.  Each entry crops the
                loaded audio to the BirdNET detection window before
                classification.  Pass ``None`` per entry (or omit the
                argument entirely) to classify the full soundscape.

        Returns:
            List of results, one per input file.  Each element is a list
            of ``AmbientTag`` on success, or a *status string* on failure:
            ``"decode_failed"`` if the file could not be decoded, or
            ``"empty_audio"`` if decoding succeeded but produced a
            zero-length array.
        """
        if not audio_paths:
            return []

        self._ensure_loaded()
        assert self._model is not None

        all_results: list[list[AmbientTag] | str] = []

        for batch_start in range(0, len(audio_paths), batch_size):
            batch_paths = audio_paths[batch_start : batch_start + batch_size]
            batch_crops = (
                crop_windows[batch_start : batch_start + batch_size]
                if crop_windows is not None
                else [None] * len(batch_paths)
            )

            # Load + normalize each file; convert successful loads to mels
            loaded_indices: list[int] = []
            loaded_mels: list = []
            batch_results: list[list[AmbientTag] | str] = [
                "decode_failed" for _ in batch_paths
            ]

            for idx, p in enumerate(batch_paths):
                try:
                    crop = batch_crops[idx]
                    start_s, end_s = crop if crop is not None else (None, None)
                    audio = self._load_and_normalize(p, start_s, end_s)
                    loaded_indices.append(idx)
                    loaded_mels.append(self._audio_to_mel(audio))
                except EmptyAudioError:
                    batch_results[idx] = "empty_audio"
                    logger.warning("Empty audio %s — empty_audio", p)
                except Exception:
                    logger.warning("Cannot decode audio %s — decode_failed", p)

            if loaded_mels:
                infer_results = self._infer_mels(loaded_mels)
                for idx, raw in zip(loaded_indices, infer_results):
                    batch_results[idx] = self._filter_results(raw)

            all_results.extend(batch_results)

        return all_results

    # ── Result filtering ──────────────────────────────────────────────

    def _filter_results(
        self,
        raw_results: list[dict],
    ) -> list[AmbientTag]:
        """Filter model results above confidence threshold.

        All 527 AudioSet labels are preserved — only ``min_confidence``
        and ``top_k`` are applied.
        """
        tags = [
            AmbientTag(label=r["label"], score=round(r["score"], 4))
            for r in raw_results
            if r["score"] >= self.min_confidence
        ]
        tags.sort(key=lambda t: t.score, reverse=True)
        return tags[: self.top_k] if self.top_k is not None else tags


# ──────────────────────────────────────────────────────────────────────────
# Audio file resolution
# ──────────────────────────────────────────────────────────────────────────


def _resolve_audio_files(
    detections: list[dict],
    audio_dir: Path,
) -> dict[str, Path]:
    """Map detection IDs to audio file paths.

    BirdWeather audio follows the naming convention:
    ``{detection_id}_{species_id}.{ext}`` (mp3/flac/wav/m4a/aac/ogg).

    Args:
        detections: List of detection dicts (each has ``"id"``).
        audio_dir: Directory containing audio files.

    Returns:
        Mapping of detection_id → audio file path.
    """
    _AUDIO_SUFFIXES = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"}

    # Index audio directory once — O(n) scan instead of per-detection glob
    audio_index: dict[str, Path] = {}
    if audio_dir.exists():
        for f in audio_dir.iterdir():
            if f.is_file() and f.suffix.lower() in _AUDIO_SUFFIXES:
                # Filename format: {detection_id}_{species_id}.ext
                det_id = f.stem.split("_")[0]
                audio_index[det_id] = f

    result: dict[str, Path] = {}
    for det in detections:
        det_id = str(det.get("id", ""))
        if det_id and det_id in audio_index:
            result[det_id] = audio_index[det_id]

    return result


# ──────────────────────────────────────────────────────────────────────────
# JSONL enrichment
# ──────────────────────────────────────────────────────────────────────────


def _load_detections(path: Path) -> list[dict]:
    """Load detections from a JSONL file."""
    detections: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                detections.append(json.loads(line))
    return detections


def _load_existing_tags(detections: list[dict]) -> dict[str, list[dict]]:
    """Extract terminally processed classification results from detections.

    A detection is considered "done" (skip on rerun) when:

    1. It carries a ``background_sound_classification_status`` whose value
       is in ``TERMINAL_STATUSES`` (``"classified"`` or
       ``"no_source_audio"``).
    2. *Legacy*: it has non-empty ``background_sound_classification_tags``
       with *no* status field (treated as ``"classified"``).

    Detections with a *non-terminal* status (``"audio_not_downloaded"``,
    ``"decode_failed"``, ``"empty_audio"``) are **not** returned — they
    will be retried on the next run.

    Returns:
        Mapping of detection_id → background_sound_classification_tags list
        for every terminally processed detection.
    """
    tags: dict[str, list[dict]] = {}
    for det in detections:
        status = det.get("background_sound_classification_status")
        if status is not None:
            # Only terminal statuses are "done"
            if status in TERMINAL_STATUSES:
                tags[str(det["id"])] = det.get(
                    "background_sound_classification_tags",
                    [],
                )
        # Legacy fallback: non-empty tags imply classified (terminal)
        elif det.get("background_sound_classification_tags"):
            tags[str(det["id"])] = det["background_sound_classification_tags"]
    return tags


def enrich_detections(
    date_dir: str | Path,
    *,
    device: str | None = None,
    batch_size: int = 8,
    top_k: int | None = None,
    min_confidence: float = 0.10,
    force: bool = False,
    dry_run: bool = False,
) -> EnrichmentStats:
    """Enrich BirdWeather detections with ambient sound tags.

    Reads ``detections.jsonl`` from the given date directory, classifies
    each corresponding **raw** audio file, and writes the enriched tags
    back into ``detections.jsonl``.

    Status semantics
    ~~~~~~~~~~~~~~~~
    Every detection receives a ``background_sound_classification_status``:

    *Terminal* (skipped on rerun):
      - ``"classified"`` — audio decoded and classified (tags may be
        empty if nothing exceeded ``min_confidence``).
      - ``"no_source_audio"`` — detection has no ``soundscape.url``;
        there is nothing to download.

    *Non-terminal* (retried on rerun without ``--force``):
      - ``"audio_not_downloaded"`` — detection has a ``soundscape.url``
        but no local audio file was found.
      - ``"decode_failed"`` — local file exists but could not be decoded.
      - ``"empty_audio"`` — file decoded but produced a zero-length array.

    Args:
        date_dir: Path to a date folder (e.g. ``data/raw/birdweather/2025-01-01``).
        device: ``"cuda"``, ``"cpu"``, or ``None`` for auto-detect.
        batch_size: Batch size for inference.
        top_k: Maximum ambient tags per detection.
        min_confidence: Minimum tag confidence threshold.
        force: If True, re-classify all detections even if tags exist.
        dry_run: If True, log what would be done but don't write.

    Returns:
        ``EnrichmentStats`` with processing statistics.
    """
    date_dir = Path(date_dir)
    t0 = time.perf_counter()
    stats = EnrichmentStats()

    detections_path = date_dir / "detections.jsonl"
    audio_dir = date_dir / "audio"

    # ── Validate inputs ───────────────────────────────────────────────
    if not detections_path.exists():
        raise FileNotFoundError(f"No detections.jsonl in {date_dir}")

    # ── Load detections ───────────────────────────────────────────────
    detections = _load_detections(detections_path)
    stats.total_detections = len(detections)
    logger.info(
        "Loaded %d detections from %s",
        len(detections),
        detections_path,
    )

    # ── Check for existing enrichment ─────────────────────────────────
    existing_tags: dict[str, list[dict]] = {}
    if not force:
        existing_tags = _load_existing_tags(detections)
        if existing_tags and len(existing_tags) >= len(detections):
            logger.info(
                "All %d detections already enriched in %s — skipping "
                "(use --force to re-classify)",
                len(existing_tags),
                date_dir.name,
            )
            stats.skipped_existing = len(existing_tags)
            stats.elapsed_s = round(time.perf_counter() - t0, 3)
            return stats

    # ── Resolve audio files ───────────────────────────────────────────
    audio_map = _resolve_audio_files(detections, audio_dir)
    stats.audio_matched = len(audio_map)
    logger.info(
        "Matched %d/%d detections to audio files",
        len(audio_map),
        len(detections),
    )

    # ── Determine status for unmatched detections ─────────────────────
    # Build a lookup: does this detection have a soundscape URL?
    _has_source_audio: dict[str, bool] = {}
    for det in detections:
        det_id = str(det.get("id", ""))
        url = (det.get("soundscape") or {}).get("url", "")
        _has_source_audio[det_id] = bool(url)

    # ── Build classification work list ────────────────────────────────
    # Each item is (det_id, audio_path, crop_window) where crop_window
    # is (startTime, endTime) seconds from the soundscape or None.
    work: list[tuple[str, Path, tuple[float | None, float | None] | None]] = []
    no_audio_ids: dict[str, str] = {}  # det_id → status for unmatched
    for det in detections:
        det_id = str(det.get("id", ""))
        # Skip already-tagged detections (unless forced)
        if not force and det_id in existing_tags:
            stats.skipped_existing += 1
            continue
        # No local audio file
        if det_id not in audio_map:
            if _has_source_audio.get(det_id, False):
                no_audio_ids[det_id] = "audio_not_downloaded"
                stats.audio_not_downloaded += 1
            else:
                no_audio_ids[det_id] = "no_source_audio"
                stats.no_source_audio += 1
            continue
        # Extract BirdNET detection window for targeted classification
        sc = det.get("soundscape") or {}
        st, et = sc.get("startTime"), sc.get("endTime")
        crop = (st, et) if st is not None and et is not None else None
        work.append((det_id, audio_map[det_id], crop))

    logger.info(
        "Classification plan: %d to classify, %d pre-tagged, "
        "%d no-source-audio, %d audio-not-downloaded",
        len(work),
        stats.skipped_existing,
        stats.no_source_audio,
        stats.audio_not_downloaded,
    )

    if dry_run:
        logger.info("[DRY-RUN] Would classify %d files — exiting", len(work))
        stats.elapsed_s = round(time.perf_counter() - t0, 3)
        return stats

    # ── Classify in batches with progress bar and incremental save ───
    tags_by_id: dict[str, list[AmbientTag]] = {}
    failure_statuses: dict[str, str] = {}  # det_id → "decode_failed"|"empty_audio"

    # Resume support: load progress cache from any interrupted previous run
    progress_cache_path = date_dir / ".classification_progress.jsonl"
    if not force and progress_cache_path.exists():
        cached_ids: set[str] = set()
        with open(progress_cache_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                det_id = str(entry["id"])
                if entry["status"] == "classified":
                    tags_by_id[det_id] = [
                        AmbientTag(**t) for t in entry.get("tags", [])
                    ]
                else:
                    failure_statuses[det_id] = entry["status"]
                cached_ids.add(det_id)
        # Remove already-cached items from the work list
        prev_len = len(work)
        work = [(did, p, cw) for did, p, cw in work if did not in cached_ids]
        if prev_len > len(work):
            logger.info(
                "Resuming: %d already classified from progress cache, %d remaining",
                prev_len - len(work),
                len(work),
            )

    if work:
        clf = BackgroundSoundClassifier(
            device=device,
            top_k=top_k,
            min_confidence=min_confidence,
        )

        with (
            tqdm(
                total=len(work),
                desc="Classifying audio",
                unit="file",
                colour="green",
                bar_format=(
                    "{l_bar}{bar}| {n_fmt}/{total_fmt} "
                    "[{elapsed}<{remaining}, {rate_fmt}]"
                ),
            ) as pbar,
            open(progress_cache_path, "a", encoding="utf-8") as cache_f,
        ):
            for batch_start in range(0, len(work), batch_size):
                batch = work[batch_start : batch_start + batch_size]
                batch_ids, batch_paths, batch_crops = zip(*batch)
                batch_results = clf.classify_batch(
                    list(batch_paths),
                    batch_size=batch_size,
                    crop_windows=list(batch_crops),
                )

                for det_id, result in zip(batch_ids, batch_results):
                    if isinstance(result, str):
                        failure_statuses[det_id] = result
                        cache_entry = {
                            "id": det_id,
                            "status": result,
                            "tags": [],
                        }
                        if result == "empty_audio":
                            stats.empty_audio += 1
                        else:
                            stats.decode_failed += 1
                    else:
                        tags_by_id[det_id] = result
                        cache_entry = {
                            "id": det_id,
                            "status": "classified",
                            "tags": [asdict(t) for t in result],
                        }
                    cache_f.write(json.dumps(cache_entry) + "\n")

                cache_f.flush()
                pbar.update(len(batch))
                pbar.set_postfix(
                    classified=len(tags_by_id),
                    failed=len(failure_statuses),
                )

    stats.classified = len(tags_by_id)

    # ── Merge and write back to detections.jsonl (atomic) ────────────
    total_tags = 0
    tmp_path = detections_path.with_suffix(".jsonl.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for det in detections:
                det_id = str(det.get("id", ""))

                status_key = "background_sound_classification_status"
                tags_key = "background_sound_classification_tags"

                if det_id in tags_by_id:
                    det[tags_key] = [asdict(t) for t in tags_by_id[det_id]]
                    det[status_key] = "classified"
                elif det_id in failure_statuses:
                    det[tags_key] = []
                    det[status_key] = failure_statuses[det_id]
                elif det_id in no_audio_ids:
                    det[tags_key] = []
                    det[status_key] = no_audio_ids[det_id]
                elif det_id in existing_tags:
                    det[tags_key] = existing_tags[det_id]
                    # Legacy compat: backfill status
                    det.setdefault(status_key, "classified")
                else:
                    # Fallback: shouldn't normally happen
                    det[tags_key] = []
                    det[status_key] = "no_source_audio"

                total_tags += len(det.get(tags_key, []))
                f.write(json.dumps(det, ensure_ascii=False) + "\n")
        tmp_path.replace(detections_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # Clean up progress cache after successful merge
    progress_cache_path.unlink(missing_ok=True)

    if stats.total_detections > 0:
        stats.avg_tags_per_detection = round(
            total_tags / stats.total_detections,
            2,
        )

    stats.elapsed_s = round(time.perf_counter() - t0, 3)
    logger.info(
        "Enrichment complete: %d classified, %d existing, output → %s",
        stats.classified,
        stats.skipped_existing,
        detections_path,
    )
    return stats


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


def _discover_date_dirs(input_path: Path) -> list[Path]:
    """Find date directories to process.

    If ``input_path`` itself contains ``detections.jsonl``, return it.
    Otherwise, iterate subdirectories looking for ones with detections.

    Returns:
        Sorted list of date directories.
    """
    if (input_path / "detections.jsonl").exists():
        return [input_path]

    return sorted(
        d
        for d in input_path.iterdir()
        if d.is_dir() and (d / "detections.jsonl").exists()
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Enrich BirdWeather detections with ambient sound tags "
            "using EfficientAT mn10_as."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scenarios:
  1. Fresh data (no tags yet)       → classifies all audio, writes tags into detections.jsonl
  2. Partially enriched             → classifies only un-tagged detections
  3. Fully enriched (no --force)    → skips with log message
  4. Fully enriched + --force       → re-classifies everything
  5. No audio/ directory            → warns and skips
  6. --dry-run                      → reports counts, writes nothing

Examples:
  # Single date directory
  python -m avian_biosurveillance.ingestion.background_sound_classification \\
      --input data/raw/birdweather/2025-01-01

  # All dates under parent directory
  python -m avian_biosurveillance.ingestion.background_sound_classification \\
      --input data/raw/birdweather --device cuda --batch-size 16

  # Force re-classification
  python -m avian_biosurveillance.ingestion.background_sound_classification \\
      --input data/raw/birdweather/2025-01-01 --force

  # Dry-run to preview what will be processed
  python -m avian_biosurveillance.ingestion.background_sound_classification \\
      --input data/raw/birdweather --dry-run
        """,
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        required=True,
        help=(
            "Path to a BirdWeather date directory or the parent directory. "
            "If a parent is given, all date subdirectories are processed."
        ),
    )
    parser.add_argument(
        "--device",
        "-d",
        type=str,
        default=None,
        choices=["cuda", "cpu", "mps"],
        help="Device for inference (default: auto-detect).",
    )
    parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=8,
        help="Batch size for inference (default: 8).",
    )
    parser.add_argument(
        "--top-k",
        "-k",
        type=int,
        default=None,
        help="Maximum tags per detection (default: all).",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.10,
        help="Minimum tag confidence threshold (default: 0.10).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify all detections, ignoring existing tags.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be classified without writing anything.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point for background sound enrichment."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input path does not exist: %s", input_path)
        sys.exit(1)

    date_dirs = _discover_date_dirs(input_path)
    if not date_dirs:
        logger.error("No date directories with detections.jsonl found.")
        sys.exit(1)

    mode = "[DRY-RUN] " if args.dry_run else ""
    logger.info(
        "%sProcessing %d date director%s",
        mode,
        len(date_dirs),
        "y" if len(date_dirs) == 1 else "ies",
    )

    t0 = time.perf_counter()
    total_classified = 0
    total_skipped = 0

    for date_dir in date_dirs:
        logger.info("── %s ──", date_dir.name)
        try:
            stats = enrich_detections(
                date_dir,
                device=args.device,
                batch_size=args.batch_size,
                top_k=args.top_k,
                min_confidence=args.min_confidence,
                force=args.force,
                dry_run=args.dry_run,
            )
            total_classified += stats.classified
            total_skipped += stats.skipped_existing
            logger.info(
                "  → %d classified, %d pre-tagged, %d no-source, "
                "%d not-downloaded, %d decode-failed, %d empty, %.1fs",
                stats.classified,
                stats.skipped_existing,
                stats.no_source_audio,
                stats.audio_not_downloaded,
                stats.decode_failed,
                stats.empty_audio,
                stats.elapsed_s,
            )
        except FileNotFoundError as e:
            logger.warning("  → Skipped: %s", e)
        except Exception:
            logger.exception("  → Failed to process %s", date_dir)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Done: %d classified, %d pre-tagged in %.1f s (%.1f files/s)",
        total_classified,
        total_skipped,
        elapsed,
        total_classified / elapsed if elapsed > 0 else 0,
    )


if __name__ == "__main__":
    main()
