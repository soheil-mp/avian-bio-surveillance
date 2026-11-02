"""Embedding provider adapters for BirdWeather feature engineering.

The pipeline stores BirdNET and Perch v2 embeddings when a real backend is
configured. It does not synthesize placeholder vectors when model packages are
missing; the caller receives explicit ``backend_unavailable`` statuses instead.
"""

from __future__ import annotations

import importlib
import importlib.resources
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import librosa
import numpy as np

from avian_biosurveillance.utils.noisy_runtime import suppress_third_party_output


logger = logging.getLogger(__name__)

BIRDNET_MODEL_VERSION = "2.4"
BIRDNET_MODEL_RESOURCE = (
    "models",
    "analyzer",
    "BirdNET_GLOBAL_6K_V2.4_Model_FP32.tflite",
)
BIRDNET_SAMPLE_RATE = 48_000
BIRDNET_WINDOW_SECONDS = 3.0
PERCH_V2_SAMPLE_RATE = 32_000
PERCH_V2_WINDOW_SECONDS = 5.0
DEFAULT_WEIGHTS_DIR = "weights"
NATIVE_MODEL_CACHE = Path("embedding_models")
PERCH_V2_MODEL_HANDLES = {
    "perch_v2": "google/bird-vocalization-classifier/tensorFlow2/perch_v2/2",
    "perch_v2_cpu": "google/bird-vocalization-classifier/tensorFlow2/perch_v2_cpu/1",
}
# "auto" picks the GPU SavedModel when TensorFlow can see a CUDA device at load
# time and falls back to the CPU SavedModel otherwise. This makes the same
# image/config self-adjust whether or not the host exposes a GPU.
DEFAULT_PERCH_V2_VARIANT = "auto"
AUTO_PERCH_V2_VARIANT = "auto"

_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS = (
    r".*tf\.lite\.Interpreter is deprecated.*",
)
_KAGGLEHUB_BOOTSTRAP_WARNING_PATTERNS = (
    r".*outdated `kagglehub` version.*",
)
_KAGGLEHUB_CACHE_WARNED: set[str] = set()
_PERCH_V2_MODEL_FAILURES: dict[tuple[str, str, str], str] = {}


def _configure_quiet_tensorflow_runtime() -> None:
    defaults = {
        "TF_CPP_MIN_LOG_LEVEL": "2",
        "ABSL_MIN_LOG_LEVEL": "2",
        "GLOG_minloglevel": "2",
        "AUTOGRAPH_VERBOSITY": "0",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


_configure_quiet_tensorflow_runtime()


_GPU_RUNTIME_CONFIGURED = False


def _configure_tensorflow_gpu(tensorflow: Any) -> None:
    """Enable per-process GPU memory growth so TF does not pre-grab all VRAM.

    On modest GPUs (for example a 6 GB laptop card) TensorFlow's default
    behaviour of reserving the entire device can crowd out other processes or
    fail outright. Memory growth lets the allocation expand on demand. The call
    is idempotent and a no-op when no GPU is visible or when disabled via
    ``AVIAN_TF_GPU_MEMORY_GROWTH=0``.
    """
    global _GPU_RUNTIME_CONFIGURED
    if _GPU_RUNTIME_CONFIGURED:
        return
    _GPU_RUNTIME_CONFIGURED = True
    if os.environ.get("AVIAN_TF_GPU_MEMORY_GROWTH", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    try:
        gpus = tensorflow.config.list_physical_devices("GPU")
        for gpu in gpus:
            tensorflow.config.experimental.set_memory_growth(gpu, True)
    except (AttributeError, RuntimeError, ValueError):
        # Device list already initialized, or running on a TF build without GPU
        # support; either way memory growth cannot be (re)configured here.
        pass


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Result from one embedding backend."""

    provider: str
    status: str
    values: list[float] | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def dimensions(self) -> int | None:
        return len(self.values) if self.values is not None else None

    def manifest(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        if self.error:
            payload["error"] = self.error
        if self.metadata:
            payload.update(self.metadata)
        return payload


def _native_embedding_providers() -> tuple[
    BirdNetEmbeddingProvider, PerchV2EmbeddingProvider
]:
    return (
        BirdNetEmbeddingProvider(
            provider="birdnet",
            target_sample_rate=BIRDNET_SAMPLE_RATE,
            target_seconds=BIRDNET_WINDOW_SECONDS,
        ),
        PerchV2EmbeddingProvider(
            provider="perch_v2",
            target_sample_rate=PERCH_V2_SAMPLE_RATE,
            target_seconds=PERCH_V2_WINDOW_SECONDS,
        ),
    )


def compute_embedding_results(
    audio: np.ndarray,
    sample_rate: int,
) -> dict[str, EmbeddingResult]:
    """Compute BirdNET and Perch v2 embeddings for a single clip."""
    return compute_embedding_results_batch([(audio, sample_rate)])[0]


def compute_embedding_results_batch(
    clips: Sequence[tuple[np.ndarray, int]],
    *,
    providers: Sequence[str] | None = None,
) -> list[dict[str, EmbeddingResult]]:
    """Compute BirdNET and Perch v2 embeddings for a batch of clips.

    Each clip's framed windows are stacked across the batch and passed through
    the underlying model in a single call, which keeps a GPU (or multi-core
    CPU) saturated instead of issuing one tiny inference per clip. Results are
    numerically identical to computing each clip independently because every
    frame is embedded in isolation and only aggregated per clip afterwards.

    ``providers`` optionally restricts which backends run (by provider name,
    e.g. ``("perch_v2",)``); the default of ``None`` computes every backend.
    """
    if not clips:
        return []
    results: list[dict[str, EmbeddingResult]] = [{} for _ in clips]
    selected = _native_embedding_providers()
    if providers is not None:
        wanted = set(providers)
        selected = tuple(p for p in selected if p.provider in wanted)
    for provider in selected:
        provider_results = provider.embed_batch(clips)
        for index, result in enumerate(provider_results):
            results[index][provider.provider] = result
    return results


class BirdNetEmbeddingProvider:
    """Native BirdNET embedding adapter backed by the packaged TFLite model."""

    def __init__(
        self,
        *,
        provider: str,
        target_sample_rate: int,
        target_seconds: float,
        model_path: str | Path | None = None,
        num_threads: int | None = None,
    ) -> None:
        self.provider = provider
        self.target_sample_rate = target_sample_rate
        self.target_seconds = target_seconds
        self.model_path = Path(model_path) if model_path is not None else None
        self.num_threads = num_threads or int(
            os.environ.get("AVIAN_BIRDNET_TFLITE_THREADS", "4")
        )

    def embed(self, audio: np.ndarray, sample_rate: int) -> EmbeddingResult:
        return self.embed_batch([(audio, sample_rate)])[0]

    def warm(self) -> EmbeddingResult:
        try:
            _load_birdnet_interpreter(
                self._configured_model_path(),
                self.num_threads,
            )
        except (
            FileNotFoundError,
            ImportError,
            ModuleNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            return EmbeddingResult(
                provider=self.provider,
                status="backend_unavailable",
                error=(
                    "Install the BirdNET embedding runtime for the ingestion worker "
                    "(TensorFlow plus birdnetlib's packaged model). "
                    f"{type(exc).__name__}: {exc}"
                ),
                metadata=self._metadata(),
            )
        return EmbeddingResult(
            provider=self.provider,
            status="ready",
            metadata=self._metadata(),
        )

    def embed_batch(
        self,
        clips: Sequence[tuple[np.ndarray, int]],
    ) -> list[EmbeddingResult]:
        try:
            interpreter = _load_birdnet_interpreter(
                self._configured_model_path(),
                self.num_threads,
            )
        except (
            FileNotFoundError,
            ImportError,
            ModuleNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            unavailable = EmbeddingResult(
                provider=self.provider,
                status="backend_unavailable",
                error=(
                    "Install the BirdNET embedding runtime for the ingestion worker "
                    "(TensorFlow plus birdnetlib's packaged model). "
                    f"{type(exc).__name__}: {exc}"
                ),
                metadata=self._metadata(),
            )
            return [unavailable for _ in clips]

        return _embed_clips_with_model(
            clips,
            provider=self.provider,
            metadata=self._metadata(),
            prepare=lambda audio, sample_rate: frame_model_audio(
                prepare_stream_audio(
                    audio,
                    sample_rate,
                    target_sample_rate=self.target_sample_rate,
                ),
                target_sample_rate=self.target_sample_rate,
                target_seconds=self.target_seconds,
            ),
            run=lambda frames: _embed_with_birdnet_interpreter(interpreter, frames),
        )

    def _configured_model_path(self) -> str:
        if self.model_path is not None:
            return str(self.model_path)
        return os.environ.get("AVIAN_BIRDNET_MODEL_PATH", "").strip()

    def _metadata(self) -> dict[str, Any]:
        configured_model_path = self._configured_model_path()
        return {
            "backend": "birdnet_native",
            "model_version": BIRDNET_MODEL_VERSION,
            "model_path": configured_model_path
            or f"birdnetlib:{'/'.join(BIRDNET_MODEL_RESOURCE)}",
            "target_sample_rate": self.target_sample_rate,
            "target_seconds": self.target_seconds,
            "num_threads": self.num_threads,
        }


class PerchV2EmbeddingProvider:
    """Native Perch v2 embedding adapter backed by KaggleHub SavedModels."""

    def __init__(
        self,
        *,
        provider: str,
        target_sample_rate: int,
        target_seconds: float,
        weights_dir: str | Path | None = None,
        model_path: str | Path | None = None,
        model_variant: str | None = None,
    ) -> None:
        self.provider = provider
        self.target_sample_rate = target_sample_rate
        self.target_seconds = target_seconds
        self.weights_dir = Path(
            weights_dir or os.environ.get("AVIAN_WEIGHTS_DIR", DEFAULT_WEIGHTS_DIR)
        )
        self.model_path = Path(model_path) if model_path is not None else None
        self.model_variant = model_variant or os.environ.get(
            "AVIAN_PERCH_V2_VARIANT",
            DEFAULT_PERCH_V2_VARIANT,
        )

    @property
    def cache_dir(self) -> Path:
        return self.weights_dir / NATIVE_MODEL_CACHE / "kagglehub"

    def embed(self, audio: np.ndarray, sample_rate: int) -> EmbeddingResult:
        return self.embed_batch([(audio, sample_rate)])[0]

    def warm(self) -> EmbeddingResult:
        try:
            _model, resolved_model_path = _load_perch_v2_model(
                self.model_variant,
                str(self.cache_dir),
                self._configured_model_path(),
            )
        except (
            FileNotFoundError,
            ImportError,
            ModuleNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            return EmbeddingResult(
                provider=self.provider,
                status="backend_unavailable",
                error=(
                    "Install the Perch v2 embedding runtime for the ingestion worker "
                    "(TensorFlow plus kagglehub) and allow access to the official "
                    "model. "
                    f"{type(exc).__name__}: {exc}"
                ),
                metadata=self._metadata(),
            )
        return EmbeddingResult(
            provider=self.provider,
            status="ready",
            metadata=self._metadata(resolved_model_path),
        )

    def embed_batch(
        self,
        clips: Sequence[tuple[np.ndarray, int]],
    ) -> list[EmbeddingResult]:
        try:
            model, resolved_model_path = _load_perch_v2_model(
                self.model_variant,
                str(self.cache_dir),
                self._configured_model_path(),
            )
        except (
            FileNotFoundError,
            ImportError,
            ModuleNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            unavailable = EmbeddingResult(
                provider=self.provider,
                status="backend_unavailable",
                error=(
                    "Install the Perch v2 embedding runtime for the ingestion worker "
                    "(TensorFlow plus kagglehub) and allow access to the official "
                    "model. "
                    f"{type(exc).__name__}: {exc}"
                ),
                metadata=self._metadata(),
            )
            return [unavailable for _ in clips]

        return _embed_clips_with_model(
            clips,
            provider=self.provider,
            metadata=self._metadata(resolved_model_path),
            prepare=lambda audio, sample_rate: frame_model_audio(
                prepare_stream_audio(
                    audio,
                    sample_rate,
                    target_sample_rate=self.target_sample_rate,
                ),
                target_sample_rate=self.target_sample_rate,
                target_seconds=self.target_seconds,
            ),
            run=lambda frames: _embed_with_perch_saved_model(model, frames),
        )

    def _configured_model_path(self) -> str:
        if self.model_path is not None:
            return str(self.model_path)
        return os.environ.get("AVIAN_PERCH_V2_MODEL_PATH", "").strip()

    def _metadata(self, resolved_model_path: str | None = None) -> dict[str, Any]:
        configured_model_path = self._configured_model_path()
        payload: dict[str, Any] = {
            "backend": "perch_v2_native",
            "model_variant": self.model_variant,
            "cache_dir": str(_effective_kagglehub_cache_dir(self.cache_dir)),
            "target_sample_rate": self.target_sample_rate,
            "target_seconds": self.target_seconds,
        }
        if configured_model_path:
            payload["model_path"] = configured_model_path
            payload["model_source"] = "local_path"
        else:
            payload["model_handle"] = PERCH_V2_MODEL_HANDLES.get(self.model_variant)
            payload["model_source"] = "kagglehub"
            if resolved_model_path is not None:
                payload["resolved_model_path"] = resolved_model_path
        return payload


def prepare_stream_audio(
    audio: np.ndarray,
    sample_rate: int,
    *,
    target_sample_rate: int,
) -> np.ndarray:
    """Convert audio to mono, resample, and peak-normalize the full clip."""
    prepared = np.asarray(audio, dtype=np.float32)
    if prepared.ndim > 1:
        prepared = np.mean(prepared, axis=1, dtype=np.float32)
    if prepared.size == 0:
        raise ValueError("audio must not be empty")

    if sample_rate != target_sample_rate:
        prepared = librosa.resample(
            prepared,
            orig_sr=sample_rate,
            target_sr=target_sample_rate,
        )

    peak = float(np.max(np.abs(prepared)))
    if peak > 1e-10:
        prepared = prepared / peak * 0.9
    return np.asarray(prepared, dtype=np.float32)


def frame_model_audio(
    audio: np.ndarray,
    *,
    target_sample_rate: int,
    target_seconds: float,
    hop_seconds: float | None = None,
) -> np.ndarray:
    """Split normalized audio into fixed-size model windows with right padding."""
    target_samples = max(1, round(target_sample_rate * target_seconds))
    hop_samples = max(1, round(target_sample_rate * (hop_seconds or target_seconds)))

    if len(audio) <= target_samples:
        padded = np.pad(audio, (0, target_samples - len(audio)))
        return np.asarray([padded], dtype=np.float32)

    frames: list[np.ndarray] = []
    start = 0
    while start < len(audio):
        end = start + target_samples
        frame = audio[start:end]
        if len(frame) < target_samples:
            frame = np.pad(frame, (0, target_samples - len(frame)))
        frames.append(np.asarray(frame, dtype=np.float32))
        if end >= len(audio):
            break
        start += hop_samples

    return np.asarray(frames, dtype=np.float32)


def _coerce_embedding_values(values: Sequence[float] | np.ndarray) -> list[float]:
    array = np.asarray(values, dtype=np.float32).squeeze()
    if array.ndim > 1:
        array = np.mean(array.reshape(-1, array.shape[-1]), axis=0)
    array = array.reshape(-1)
    if array.size == 0:
        raise ValueError("embedding vector must not be empty")
    if not np.all(np.isfinite(array)):
        raise ValueError("embedding vector contains non-finite values")
    return [round(float(value), 8) for value in array]


def warm_embedding_models(
    providers: Sequence[str] | None = None,
) -> dict[str, EmbeddingResult]:
    """Resolve and load native embedding models before row processing starts.

    ``providers`` optionally restricts which backends are warmed.
    """
    selected = _native_embedding_providers()
    if providers is not None:
        wanted = set(providers)
        selected = tuple(p for p in selected if p.provider in wanted)
    return {provider.provider: provider.warm() for provider in selected}


def _resolve_embedding_inference_chunk() -> int:
    """Resolve the max frames per single model inference call from the env."""
    raw = os.environ.get("AVIAN_EMBEDDING_INFERENCE_CHUNK", "32").strip()
    try:
        chunk = int(raw)
    except ValueError:
        chunk = 32
    return max(1, chunk)


def _run_in_frame_chunks(
    run: Callable[[np.ndarray], np.ndarray],
    frames: np.ndarray,
) -> np.ndarray:
    """Run inference over ``frames`` in fixed-size chunks.

    A single huge stacked inference can exhaust a modest GPU (for example a
    6 GB laptop card). Splitting the frames into bounded chunks keeps each GPU
    allocation small while staying numerically identical to one large call,
    since every frame is embedded independently. Tune the chunk size via
    ``AVIAN_EMBEDDING_INFERENCE_CHUNK`` (default 32).
    """
    total = int(frames.shape[0])
    chunk = _resolve_embedding_inference_chunk()
    if total <= chunk:
        return np.asarray(run(frames), dtype=np.float32)
    parts: list[np.ndarray] = []
    for start in range(0, total, chunk):
        parts.append(np.asarray(run(frames[start : start + chunk]), dtype=np.float32))
    return np.concatenate(parts, axis=0)


def _embed_clips_with_model(
    clips: Sequence[tuple[np.ndarray, int]],
    *,
    provider: str,
    metadata: dict[str, Any],
    prepare: Callable[[np.ndarray, int], np.ndarray],
    run: Callable[[np.ndarray], np.ndarray],
) -> list[EmbeddingResult]:
    """Frame each clip, run the model once across the whole batch, aggregate.

    Frames from every clip are concatenated and embedded in a single ``run``
    call. Per-clip results are then sliced back out and aggregated, so the
    output is identical to embedding each clip independently while issuing far
    fewer (and larger) inference calls.
    """
    results: list[EmbeddingResult | None] = [None] * len(clips)
    valid: list[tuple[int, np.ndarray]] = []
    for index, (audio, sample_rate) in enumerate(clips):
        try:
            valid.append((index, np.asarray(prepare(audio, sample_rate))))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            results[index] = EmbeddingResult(
                provider=provider,
                status="embedding_failed",
                error=f"{type(exc).__name__}: {exc}",
                metadata=metadata,
            )

    if valid:
        valid_indexes = [index for index, _ in valid]
        frames_list = [frames for _, frames in valid]
        counts = [int(frames.shape[0]) for frames in frames_list]
        segments: list[np.ndarray | BaseException] | None = None
        try:
            stacked = np.concatenate(frames_list, axis=0)
            raw = _run_in_frame_chunks(run, stacked)
            if raw.shape[0] == sum(counts):
                split_points = np.cumsum(counts)[:-1]
                segments = list(np.split(raw, split_points))
        except Exception:  # noqa: BLE001 - any batch inference failure -> per-clip retry
            segments = None

        if segments is None:
            # Fall back to per-clip inference so one bad clip cannot fail the
            # batch; this also bounds each GPU allocation to a single clip.
            segments = []
            for frames in frames_list:
                try:
                    segments.append(_run_in_frame_chunks(run, frames))
                except Exception as exc:  # noqa: BLE001 - record per-clip failure
                    segments.append(exc)

        for index, segment in zip(valid_indexes, segments, strict=True):
            if isinstance(segment, BaseException):
                results[index] = EmbeddingResult(
                    provider=provider,
                    status="embedding_failed",
                    error=f"{type(segment).__name__}: {segment}",
                    metadata=metadata,
                )
                continue
            try:
                values = _coerce_embedding_values(segment)
            except ValueError as exc:
                results[index] = EmbeddingResult(
                    provider=provider,
                    status="embedding_failed",
                    error=f"{type(exc).__name__}: {exc}",
                    metadata=metadata,
                )
                continue
            results[index] = EmbeddingResult(
                provider=provider,
                status="embedded",
                values=values,
                metadata=metadata,
            )

    return [
        result
        if result is not None
        else EmbeddingResult(
            provider=provider,
            status="embedding_failed",
            error="embedding result was not produced",
            metadata=metadata,
        )
        for result in results
    ]


@lru_cache(maxsize=4)
def _load_birdnet_interpreter(configured_model_path: str, num_threads: int) -> Any:
    _configure_quiet_tensorflow_runtime()
    with suppress_third_party_output(
        capture_stdout=False,
        warning_messages=_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS,
    ):
        tensorflow = importlib.import_module("tensorflow")
    _configure_tensorflow_gpu(tensorflow)

    if configured_model_path:
        with suppress_third_party_output(
            capture_stdout=False,
            warning_messages=_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS,
        ):
            interpreter = _create_tflite_interpreter(
                tensorflow,
                configured_model_path,
                num_threads,
            )
            interpreter.allocate_tensors()
        return interpreter

    package_model = importlib.resources.files("birdnetlib").joinpath(
        *BIRDNET_MODEL_RESOURCE
    )
    with importlib.resources.as_file(package_model) as resolved_model_path:
        with suppress_third_party_output(
            capture_stdout=False,
            warning_messages=_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS,
        ):
            interpreter = _create_tflite_interpreter(
                tensorflow,
                str(resolved_model_path),
                num_threads,
            )
            interpreter.allocate_tensors()
        return interpreter


def _create_tflite_interpreter(
    tensorflow: Any,
    model_path: str,
    num_threads: int,
) -> Any:
    lite = tensorflow.lite
    kwargs: dict[str, Any] = {
        "model_path": model_path,
        "num_threads": num_threads,
    }
    experimental = getattr(lite, "experimental", None)
    op_resolver_type = getattr(experimental, "OpResolverType", None)
    if op_resolver_type is not None:
        kwargs["experimental_op_resolver_type"] = (
            op_resolver_type.BUILTIN_WITHOUT_DEFAULT_DELEGATES
        )
    return lite.Interpreter(**kwargs)


def _embed_with_birdnet_interpreter(
    interpreter: Any,
    framed_audio: np.ndarray,
) -> np.ndarray:
    frames = np.asarray(framed_audio, dtype=np.float32)
    if frames.ndim == 1:
        frames = frames[np.newaxis, :]

    input_details = interpreter.get_input_details()[0]
    interpreter.resize_tensor_input(
        input_details["index"],
        [frames.shape[0], frames.shape[-1]],
        strict=False,
    )
    interpreter.allocate_tensors()

    output_details = interpreter.get_output_details()[0]
    embedding_index = output_details["index"] - 1
    interpreter.set_tensor(input_details["index"], frames)
    interpreter.invoke()

    return np.asarray(interpreter.get_tensor(embedding_index), dtype=np.float32)


@lru_cache(maxsize=4)
def _load_perch_v2_model(
    model_variant: str,
    cache_dir: str,
    configured_model_path: str,
) -> tuple[Any, str]:
    cache_key = (model_variant, cache_dir, configured_model_path)
    if cached_failure := _PERCH_V2_MODEL_FAILURES.get(cache_key):
        raise RuntimeError(cached_failure)

    try:
        _configure_quiet_tensorflow_runtime()
        with suppress_third_party_output(
            capture_stdout=False,
            warning_messages=_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS,
        ):
            tensorflow = importlib.import_module("tensorflow")
        _configure_tensorflow_gpu(tensorflow)

        if configured_model_path:
            resolved_model_path = configured_model_path
        else:
            resolved_variant = _resolve_perch_v2_variant(model_variant, tensorflow)
            model_handle = PERCH_V2_MODEL_HANDLES.get(resolved_variant)
            if model_handle is None:
                raise ValueError(
                    "AVIAN_PERCH_V2_VARIANT must be one of: "
                    f"{', '.join(sorted(PERCH_V2_MODEL_HANDLES))}, {AUTO_PERCH_V2_VARIANT}"
                )
            effective_cache_dir = _effective_kagglehub_cache_dir(Path(cache_dir))
            effective_cache_dir.mkdir(parents=True, exist_ok=True)
            os.environ["KAGGLEHUB_CACHE"] = str(effective_cache_dir)
            logger.info(
                "Resolving Perch v2 model %s via KaggleHub cache %s.",
                resolved_variant,
                effective_cache_dir,
            )
            with suppress_third_party_output(
                warning_messages=_KAGGLEHUB_BOOTSTRAP_WARNING_PATTERNS,
            ):
                kagglehub = importlib.import_module("kagglehub")
                resolved_model_path = str(kagglehub.model_download(model_handle))
            logger.info("Resolved Perch v2 model at %s.", resolved_model_path)

        with suppress_third_party_output(
            capture_stdout=False,
            warning_messages=_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS,
        ):
            model = tensorflow.saved_model.load(resolved_model_path)
        return model, resolved_model_path
    except (
        FileNotFoundError,
        ImportError,
        ModuleNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        _PERCH_V2_MODEL_FAILURES[cache_key] = f"{type(exc).__name__}: {exc}"
        raise


def _tensorflow_has_visible_gpu(tensorflow: Any) -> bool:
    """Return True when TensorFlow can enumerate at least one GPU device.

    A CPU-only TensorFlow build (or a host without the NVIDIA Container Toolkit)
    reports an empty device list, so this doubles as a safe "is the GPU actually
    usable from here" probe rather than merely "does a GPU exist on the host".
    """
    try:
        return bool(tensorflow.config.list_physical_devices("GPU"))
    except (AttributeError, RuntimeError, ValueError):
        return False


def _resolve_perch_v2_variant(model_variant: str, tensorflow: Any) -> str:
    """Map a configured variant to a concrete SavedModel key.

    ``auto`` (the default) selects the GPU SavedModel when a CUDA device is
    visible and the CPU SavedModel otherwise; any explicit variant is returned
    unchanged so operators can still pin a specific build.
    """
    normalized = (model_variant or AUTO_PERCH_V2_VARIANT).strip().lower()
    if normalized in {AUTO_PERCH_V2_VARIANT, ""}:
        return "perch_v2" if _tensorflow_has_visible_gpu(tensorflow) else "perch_v2_cpu"
    return normalized


def _embed_with_perch_saved_model(model: Any, framed_audio: np.ndarray) -> np.ndarray:
    signature = model.signatures.get("serving_default")
    if signature is None:
        raise ValueError("Perch v2 model is missing the serving_default signature")

    with suppress_third_party_output(
        capture_stdout=False,
        warning_messages=_TENSORFLOW_BOOTSTRAP_WARNING_PATTERNS,
    ):
        outputs = signature(inputs=np.asarray(framed_audio, dtype=np.float32))
    if hasattr(outputs, "keys"):
        embedding = outputs.get("embedding")
        if embedding is None:
            raise ValueError("Perch v2 output did not include an embedding tensor")
    elif len(outputs) == 2:
        _logits, embedding = outputs
    else:
        raise ValueError("Unexpected Perch v2 output shape")

    if hasattr(embedding, "numpy"):
        embedding = embedding.numpy()
    return np.asarray(embedding, dtype=np.float32)


def _effective_kagglehub_cache_dir(default_cache_dir: Path) -> Path:
    configured_cache_dir = os.environ.get("KAGGLEHUB_CACHE", "").strip()
    if configured_cache_dir:
        configured = Path(configured_cache_dir)
        if _cache_dir_is_writable(configured):
            return configured
        return _fallback_kagglehub_cache_dir(configured)

    if _cache_dir_is_writable(default_cache_dir):
        return default_cache_dir
    return _fallback_kagglehub_cache_dir(default_cache_dir)


def _fallback_kagglehub_cache_dir(unwritable_cache_dir: Path) -> Path:
    fallback = _default_kagglehub_fallback_cache_dir()
    warning_key = f"{unwritable_cache_dir}->{fallback}"
    if warning_key not in _KAGGLEHUB_CACHE_WARNED:
        _KAGGLEHUB_CACHE_WARNED.add(warning_key)
        logger.warning(
            "KaggleHub cache %s is not writable from this container; using %s instead.",
            unwritable_cache_dir,
            fallback,
        )
    return fallback


def _default_kagglehub_fallback_cache_dir() -> Path:
    configured = os.environ.get("AVIAN_KAGGLEHUB_FALLBACK_CACHE", "").strip()
    if configured:
        return Path(configured)

    dagster_home = os.environ.get("DAGSTER_HOME", "").strip()
    if dagster_home:
        return Path(dagster_home) / "storage" / "kagglehub"

    return Path(tempfile.gettempdir()) / "avian-biosurveillance" / "kagglehub"


def _cache_dir_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def clear_embedding_model_cache() -> None:
    """Clear cached native embedding model instances."""
    _PERCH_V2_MODEL_FAILURES.clear()
    _KAGGLEHUB_CACHE_WARNED.clear()
    for loader in (
        _load_birdnet_interpreter,
        _load_perch_v2_model,
    ):
        cache_clear = getattr(loader, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
