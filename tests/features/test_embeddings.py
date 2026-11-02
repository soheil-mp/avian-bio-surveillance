"""Tests for native audio embedding providers."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import avian_biosurveillance.features.embeddings as embeddings_module
from avian_biosurveillance.features.embeddings import (
    clear_embedding_model_cache,
    compute_embedding_results,
    compute_embedding_results_batch,
)


def _sine_clip(sample_rate: int = 8_000) -> np.ndarray:
    time_axis = np.linspace(0, 0.25, sample_rate // 4, endpoint=False)
    return (0.4 * np.sin(2 * np.pi * 1_000 * time_axis)).astype(np.float32)


def test_legacy_override_env_vars_do_not_replace_native_backends(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIAN_BIRDNET_EMBEDDING_FN", "legacy.module:birdnet")
    monkeypatch.setenv("AVIAN_PERCH_V2_EMBEDDING_FN", "legacy.module:perch")
    monkeypatch.setenv("AVIAN_WEIGHTS_DIR", str(tmp_path / "weights"))
    monkeypatch.setattr(
        embeddings_module,
        "_load_birdnet_interpreter",
        lambda _configured_model_path, _num_threads: object(),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_embed_with_birdnet_interpreter",
        lambda _interpreter, _framed_audio: np.asarray(
            [[1.0, 2.0, 3.0]], dtype=np.float32
        ),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_load_perch_v2_model",
        lambda _model_variant, _cache_dir, _configured_model_path: (
            object(),
            str(tmp_path / "perch_v2"),
        ),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_embed_with_perch_saved_model",
        lambda _model, _framed_audio: np.asarray(
            [[4.0, 5.0, 6.0, 7.0]],
            dtype=np.float32,
        ),
    )
    clear_embedding_model_cache()

    results = compute_embedding_results(_sine_clip(), 8_000)

    assert results["birdnet"].status == "embedded"
    assert results["birdnet"].manifest()["backend"] == "birdnet_native"
    assert results["perch_v2"].status == "embedded"
    assert results["perch_v2"].manifest()["backend"] == "perch_v2_native"


def test_compute_embedding_results_uses_native_backends_by_default(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIAN_WEIGHTS_DIR", str(tmp_path / "weights"))
    monkeypatch.setattr(
        embeddings_module,
        "_load_birdnet_interpreter",
        lambda _configured_model_path, _num_threads: object(),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_embed_with_birdnet_interpreter",
        lambda _interpreter, _framed_audio: np.asarray(
            [[1.0, 2.0, 3.0]], dtype=np.float32
        ),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_load_perch_v2_model",
        lambda _model_variant, _cache_dir, _configured_model_path: (
            object(),
            str(tmp_path / "perch_v2"),
        ),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_embed_with_perch_saved_model",
        lambda _model, _framed_audio: np.asarray(
            [[4.0, 5.0, 6.0, 7.0]],
            dtype=np.float32,
        ),
    )
    clear_embedding_model_cache()

    results = compute_embedding_results(_sine_clip(), 8_000)

    birdnet_result = results["birdnet"]
    assert birdnet_result.status == "embedded"
    assert birdnet_result.values == [1.0, 2.0, 3.0]
    assert birdnet_result.manifest()["backend"] == "birdnet_native"

    perch_result = results["perch_v2"]
    assert perch_result.status == "embedded"
    assert perch_result.values == [4.0, 5.0, 6.0, 7.0]
    assert perch_result.manifest()["backend"] == "perch_v2_native"
    assert perch_result.manifest()["cache_dir"] == str(
        tmp_path / "weights" / "embedding_models" / "kagglehub"
    )


def test_compute_embedding_results_batch_matches_per_clip(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIAN_WEIGHTS_DIR", str(tmp_path / "weights"))
    monkeypatch.setattr(
        embeddings_module,
        "_load_birdnet_interpreter",
        lambda _configured_model_path, _num_threads: object(),
    )
    # One embedding row per input frame, so batched (stacked) inference can be
    # sliced back into per-clip results that match independent computation.
    monkeypatch.setattr(
        embeddings_module,
        "_embed_with_birdnet_interpreter",
        lambda _interpreter, framed_audio: np.tile(
            np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32),
            (np.asarray(framed_audio).shape[0], 1),
        ),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_load_perch_v2_model",
        lambda _model_variant, _cache_dir, _configured_model_path: (
            object(),
            str(tmp_path / "perch_v2"),
        ),
    )
    monkeypatch.setattr(
        embeddings_module,
        "_embed_with_perch_saved_model",
        lambda _model, framed_audio: np.tile(
            np.asarray([[4.0, 5.0, 6.0, 7.0]], dtype=np.float32),
            (np.asarray(framed_audio).shape[0], 1),
        ),
    )
    clear_embedding_model_cache()

    clips = [(_sine_clip(), 8_000), (_sine_clip(), 8_000)]
    batch_results = compute_embedding_results_batch(clips)

    assert len(batch_results) == 2
    for results in batch_results:
        assert results["birdnet"].status == "embedded"
        assert results["birdnet"].values == [1.0, 2.0, 3.0]
        assert results["perch_v2"].status == "embedded"
        assert results["perch_v2"].values == [4.0, 5.0, 6.0, 7.0]

    # Batched results must equal computing each clip independently.
    single = compute_embedding_results(_sine_clip(), 8_000)
    assert batch_results[0]["birdnet"].values == single["birdnet"].values
    assert batch_results[0]["perch_v2"].values == single["perch_v2"].values


def test_perch_v2_kagglehub_download_noise_is_suppressed(
    monkeypatch: Any,
    tmp_path: Path,
    capfd: Any,
) -> None:
    class FakeTensorflow:
        class config:
            @staticmethod
            def list_physical_devices(_device_type: str) -> list[str]:
                return []

        class saved_model:
            @staticmethod
            def load(_path: str) -> object:
                return object()

    class FakeKaggleHub:
        @staticmethod
        def model_download(_model_handle: str) -> str:
            sys.stderr.write(
                "Warning: Looks like you're using an outdated `kagglehub` version",
            )
            sys.stderr.flush()
            sys.stderr.write("Downloading 6 files:   0%|          | 0/6")
            sys.stderr.flush()
            sys.stdout.write("kagglehub stdout progress")
            sys.stdout.flush()
            return str(tmp_path / "perch_v2")

    def fake_import_module(module_name: str) -> object:
        if module_name == "tensorflow":
            return FakeTensorflow
        if module_name == "kagglehub":
            return FakeKaggleHub
        raise AssertionError(f"Unexpected import: {module_name}")

    monkeypatch.setattr(embeddings_module.importlib, "import_module", fake_import_module)
    clear_embedding_model_cache()

    model, resolved_model_path = embeddings_module._load_perch_v2_model(
        "perch_v2_cpu",
        str(tmp_path / "cache"),
        "",
    )

    captured = capfd.readouterr()
    assert model is not None
    assert resolved_model_path == str(tmp_path / "perch_v2")
    assert "outdated `kagglehub` version" not in captured.err
    assert "Downloading 6 files" not in captured.err
    assert "kagglehub stdout progress" not in captured.out


def test_perch_v2_uses_fallback_cache_when_weights_cache_is_unwritable(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class FakeTensorflow:
        class config:
            @staticmethod
            def list_physical_devices(_device_type: str) -> list[str]:
                return []

        class saved_model:
            @staticmethod
            def load(_path: str) -> object:
                return object()

    class FakeKaggleHub:
        @staticmethod
        def model_download(_model_handle: str) -> str:
            assert os_environ_cache() == str(tmp_path / "fallback")
            return str(tmp_path / "perch_v2")

    def os_environ_cache() -> str | None:
        return embeddings_module.os.environ.get("KAGGLEHUB_CACHE")

    def fake_import_module(module_name: str) -> object:
        if module_name == "tensorflow":
            return FakeTensorflow
        if module_name == "kagglehub":
            return FakeKaggleHub
        raise AssertionError(f"Unexpected import: {module_name}")

    def fake_is_writable(path: Path) -> bool:
        return path == tmp_path / "fallback"

    monkeypatch.delenv("KAGGLEHUB_CACHE", raising=False)
    monkeypatch.setenv("AVIAN_KAGGLEHUB_FALLBACK_CACHE", str(tmp_path / "fallback"))
    monkeypatch.setattr(embeddings_module.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(embeddings_module, "_cache_dir_is_writable", fake_is_writable)
    clear_embedding_model_cache()

    _model, resolved_model_path = embeddings_module._load_perch_v2_model(
        "perch_v2_cpu",
        str(tmp_path / "unwritable"),
        "",
    )

    assert resolved_model_path == str(tmp_path / "perch_v2")
    assert os_environ_cache() == str(tmp_path / "fallback")


def test_perch_v2_model_load_failure_is_cached(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    download_attempts = 0

    class FakeTensorflow:
        class config:
            @staticmethod
            def list_physical_devices(_device_type: str) -> list[str]:
                return []

        class saved_model:
            @staticmethod
            def load(_path: str) -> object:
                return object()

    class FakeKaggleHub:
        @staticmethod
        def model_download(_model_handle: str) -> str:
            nonlocal download_attempts
            download_attempts += 1
            raise PermissionError("cache is not writable")

    def fake_import_module(module_name: str) -> object:
        if module_name == "tensorflow":
            return FakeTensorflow
        if module_name == "kagglehub":
            return FakeKaggleHub
        raise AssertionError(f"Unexpected import: {module_name}")

    monkeypatch.setattr(embeddings_module.importlib, "import_module", fake_import_module)
    clear_embedding_model_cache()

    with pytest.raises(PermissionError):
        embeddings_module._load_perch_v2_model(
            "perch_v2_cpu",
            str(tmp_path / "cache"),
            "",
        )
    with pytest.raises(RuntimeError, match="PermissionError: cache is not writable"):
        embeddings_module._load_perch_v2_model(
            "perch_v2_cpu",
            str(tmp_path / "cache"),
            "",
        )

    assert download_attempts == 1
