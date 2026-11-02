"""Tests for global PCA + kNN cosine-isolation alert score precompute."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from avian_biosurveillance.modeling.embedding_projection import (
    BackendProjections,
    EmbeddingProjectionConfig,
    _build_reference_indices,
    _l2_normalize_rows,
    _parse_pgvector_text,
    _scale_to_unit_window,
    compute_backend_projection,
)


def _make_embeddings(
    n: int,
    dim: int,
    *,
    cluster_center: NDArray[np.float32] | None = None,
    spread: float = 0.05,
    seed: int = 0,
) -> NDArray[np.float32]:
    """Create a tight Gaussian cluster around ``cluster_center`` (or origin)."""
    rng = np.random.default_rng(seed)
    if cluster_center is None:
        cluster_center = np.zeros(dim, dtype=np.float32)
    sample = cluster_center + rng.normal(0.0, spread, size=(n, dim))
    return np.asarray(sample, dtype=np.float32)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_l2_normalize_rows_unit_length() -> None:
    matrix = np.array([[3.0, 4.0], [0.0, 0.0], [1.0, 1.0]], dtype=np.float32)
    normalized = _l2_normalize_rows(matrix)
    norms = np.linalg.norm(normalized, axis=1)
    # Non-zero rows have unit norm; the zero row stays zero (norm == 0).
    assert np.allclose(norms[0], 1.0)
    assert norms[1] == 0.0
    assert np.allclose(norms[2], 1.0)


def test_scale_to_unit_window_constant_input() -> None:
    values = np.full(5, 2.5, dtype=np.float32)
    scaled = _scale_to_unit_window(values)
    assert np.allclose(scaled, 0.5)


def test_scale_to_unit_window_clips_into_canvas_padding() -> None:
    values = np.array([-1.0, 0.0, 1.0], dtype=np.float32)
    scaled = _scale_to_unit_window(values)
    assert pytest.approx(0.08, abs=1e-6) == float(scaled.min())
    assert pytest.approx(0.92, abs=1e-6) == float(scaled.max())


def test_build_reference_indices_stride_samples_at_cap() -> None:
    indices = _build_reference_indices(population=1000, reference_cap=4)
    # Stride = 250, expect 0, 250, 500, 750.
    assert indices.tolist() == [0, 250, 500, 750]


def test_build_reference_indices_passes_through_small_population() -> None:
    indices = _build_reference_indices(population=3, reference_cap=10)
    assert indices.tolist() == [0, 1, 2]


# ---------------------------------------------------------------------------
# pgvector text parsing
# ---------------------------------------------------------------------------


def test_parse_pgvector_text_happy_path() -> None:
    parsed = _parse_pgvector_text("[0.1,0.2,0.3]")
    assert parsed == [0.1, 0.2, 0.3]


def test_parse_pgvector_text_rejects_unbracketed() -> None:
    assert _parse_pgvector_text("0.1,0.2,0.3") is None
    assert _parse_pgvector_text("") is None
    assert _parse_pgvector_text(None) is None


def test_parse_pgvector_text_rejects_garbage() -> None:
    assert _parse_pgvector_text("[abc, def]") is None


# ---------------------------------------------------------------------------
# compute_backend_projection — end-to-end pure-compute behaviour
# ---------------------------------------------------------------------------


def test_compute_returns_none_below_minimum_population() -> None:
    embeddings = _make_embeddings(n=4, dim=8)
    out = compute_backend_projection(
        detection_ids=[f"d{i}" for i in range(4)],
        detected_ats=[None] * 4,
        embeddings=embeddings,
        config=EmbeddingProjectionConfig(minimum_population=8),
    )
    assert out is None


def test_compute_validates_length_mismatch() -> None:
    embeddings = _make_embeddings(n=12, dim=8)
    with pytest.raises(ValueError, match="length mismatch"):
        compute_backend_projection(
            detection_ids=["a", "b", "c"],
            detected_ats=[None, None, None],
            embeddings=embeddings,
        )


def test_compute_outputs_canvas_ready_coordinates() -> None:
    # Two well-separated clusters in a low-dim space → PCA should spread them.
    dim = 8
    cluster_a = _make_embeddings(
        n=20,
        dim=dim,
        cluster_center=np.eye(dim, dtype=np.float32)[0] * 5.0,
        seed=1,
    )
    cluster_b = _make_embeddings(
        n=20,
        dim=dim,
        cluster_center=np.eye(dim, dtype=np.float32)[1] * 5.0,
        seed=2,
    )
    embeddings = np.vstack([cluster_a, cluster_b]).astype(np.float32)

    out = compute_backend_projection(
        detection_ids=[f"d{i}" for i in range(embeddings.shape[0])],
        detected_ats=[None] * embeddings.shape[0],
        embeddings=embeddings,
    )

    assert isinstance(out, BackendProjections)
    assert len(out) == embeddings.shape[0]
    # All projection coords land inside the canvas padding band.
    assert float(out.pc1.min()) >= 0.08 - 1e-6
    assert float(out.pc1.max()) <= 0.92 + 1e-6
    assert float(out.pc2.min()) >= 0.08 - 1e-6
    assert float(out.pc2.max()) <= 0.92 + 1e-6
    # Alert scores are clipped to [0, 1] and not all identical (the population
    # is mixed; some points are denser-neighboured than others).
    assert float(out.alert_score.min()) >= 0.0
    assert float(out.alert_score.max()) <= 1.0
    assert float(out.alert_score.std()) > 0.0


def test_compute_alert_score_flags_isolated_point_above_dense_cluster() -> None:
    # 30 tight-cluster points + 1 far outlier — the outlier should rank highest
    # on alert_score (max cosine distance from the reference set).
    dim = 16
    cluster = _make_embeddings(
        n=30,
        dim=dim,
        cluster_center=np.eye(dim, dtype=np.float32)[0] * 3.0,
        seed=42,
    )
    outlier = (
        (np.eye(dim, dtype=np.float32)[5] * 3.0).reshape(1, dim).astype(np.float32)
    )
    embeddings = np.vstack([cluster, outlier]).astype(np.float32)

    out = compute_backend_projection(
        detection_ids=[f"d{i}" for i in range(embeddings.shape[0])],
        detected_ats=[None] * embeddings.shape[0],
        embeddings=embeddings,
    )

    assert out is not None
    # The outlier was appended last.
    outlier_score = float(out.alert_score[-1])
    cluster_scores = out.alert_score[:-1]
    assert outlier_score > float(cluster_scores.max())
