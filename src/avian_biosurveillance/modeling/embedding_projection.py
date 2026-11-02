"""Global PCA + kNN cosine-isolation alert score precompute for BirdWeather embeddings.

This module is the offline counterpart to a hot path that previously lived in
the alerts API: the frontend used to pull every detection's raw embedding text
(~10-20 KB per row) over the wire, run a 28-iteration power-iteration PCA in
Node, and compute kNN cosine isolation against a 256-vector reference set on
every request. None of that compute depended on the user's filters — it just
ran on whatever slice happened to be selected.

We replace that with a once-per-pipeline-run global compute, keyed on the
entire embedding population. For each backend (``birdnet``, ``perch_v2``) we
write three columns back to ``birdweather_tbl`` (``{backend}_pc1``,
``{backend}_pc2``, ``{backend}_alert_score``). The frontend then selects those
columns directly — no embedding text, no PCA, no kNN on the request path.

Tradeoff: global PCA means filtering to one species no longer rescales the
canvas axes to capture within-species variance. Points sit where they would
sit in the global space, so a single species typically appears as a tight
sub-region rather than spread across the canvas. This is intentional — it
makes slices comparable across queries and the axes stable over time.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import psycopg2.extras  # type: ignore[import-untyped]
from numpy.typing import NDArray
from sklearn.decomposition import PCA

from avian_biosurveillance.db.database import ConnectionPool, get_pool


logger = logging.getLogger(__name__)


PROJECTION_BACKENDS: tuple[str, ...] = ("birdnet", "perch_v2")
# Matches the frontend's reference-set sampling cap so the precomputed alert
# scores stay numerically comparable to the original per-slice path.
ALERT_REFERENCE_CAP: int = 256
# k for the kNN cosine isolation step. Frontend originally used 5.
ALERT_NEIGHBOUR_COUNT: int = 5
# How many rows to UPSERT per DB round-trip.
UPSERT_BATCH_SIZE: int = 1_000
# Below this population the projection collapses to noise; skip and leave the
# columns NULL so the frontend treats the backend as not yet projected.
MINIMUM_POPULATION: int = 8


@dataclass(frozen=True, slots=True)
class EmbeddingProjectionConfig:
    """Tuning knobs for the projection + alert-score precompute."""

    reference_cap: int = ALERT_REFERENCE_CAP
    neighbour_count: int = ALERT_NEIGHBOUR_COUNT
    minimum_population: int = MINIMUM_POPULATION


@dataclass(frozen=True, slots=True)
class BackendProjections:
    """Per-detection projection results for a single embedding backend."""

    detection_ids: NDArray[Any]
    detected_ats: NDArray[Any]
    pc1: NDArray[np.float32]
    pc2: NDArray[np.float32]
    alert_score: NDArray[np.float32]

    def __len__(self) -> int:
        return int(self.detection_ids.shape[0])


# ---------------------------------------------------------------------------
# Pure compute (importable without DB)
# ---------------------------------------------------------------------------


def _l2_normalize_rows(matrix: NDArray[np.float32]) -> NDArray[np.float32]:
    """Row-wise L2 normalize; zero rows survive as zero rows."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return np.asarray(matrix / norms, dtype=np.float32)


def _scale_to_unit_window(values: NDArray[np.float32]) -> NDArray[np.float32]:
    """Map an arbitrary 1-D float vector into [0.08, 0.92].

    Mirrors the ``0.08 + (x - min) / (max - min) * 0.84`` transform that the
    frontend was applying to per-slice PCA output, so canvas placement stays
    visually consistent with the previous behaviour.
    """
    if values.size == 0:
        return values.astype(np.float32, copy=False)
    min_value = float(np.min(values))
    max_value = float(np.max(values))
    span = max_value - min_value
    if span <= 1e-9:
        return np.full_like(values, 0.5, dtype=np.float32)
    return np.asarray(
        0.08 + (values - min_value) / span * 0.84,
        dtype=np.float32,
    )


def _build_reference_indices(population: int, reference_cap: int) -> NDArray[np.int64]:
    """Stride-sample a deterministic reference subset; matches frontend behaviour."""
    if population <= reference_cap:
        return np.arange(population, dtype=np.int64)
    step = population / float(reference_cap)
    clipped = np.minimum(
        population - 1,
        np.floor(np.arange(reference_cap) * step).astype(np.int64),
    )
    return np.asarray(clipped, dtype=np.int64)


def compute_backend_projection(
    detection_ids: Sequence[Any],
    detected_ats: Sequence[Any],
    embeddings: NDArray[np.float32],
    *,
    config: EmbeddingProjectionConfig | None = None,
) -> BackendProjections | None:
    """Run global PCA → pc1, pc2 plus kNN cosine isolation alert score.

    Returns ``None`` when the population is too small for a meaningful
    projection (caller should leave the corresponding DB columns NULL).
    """
    resolved_config = config or EmbeddingProjectionConfig()
    population = embeddings.shape[0]
    if population < resolved_config.minimum_population:
        return None
    if population != len(detection_ids) or population != len(detected_ats):
        msg = (
            f"detection_ids/detected_ats/embeddings length mismatch: "
            f"{len(detection_ids)}/{len(detected_ats)}/{population}"
        )
        raise ValueError(msg)

    matrix = np.asarray(embeddings, dtype=np.float32)
    normalized = _l2_normalize_rows(matrix)

    # --- PCA → 2D projection, scaled to [0.08, 0.92] like the frontend ----
    pca = PCA(n_components=2, svd_solver="auto", random_state=0)
    components = pca.fit_transform(normalized).astype(np.float32)
    pc1_scaled = _scale_to_unit_window(components[:, 0])
    pc2_raw_scaled = _scale_to_unit_window(components[:, 1])
    # Frontend uses (1 - y) so positive PC2 reads visually "up"; mirror it.
    pc2_scaled = (1.0 - pc2_raw_scaled).astype(np.float32)

    # --- kNN cosine-isolation alert score against a sampled reference set --
    reference_indices = _build_reference_indices(
        population, resolved_config.reference_cap
    )
    reference = normalized[reference_indices]
    # Cosine similarity = dot product (vectors are already unit length).
    similarities = normalized @ reference.T  # shape: (N, R)

    # Mask self-matches when the row is in the reference subset.
    if reference_indices.size > 0:
        row_positions = np.arange(reference_indices.size, dtype=np.int64)
        similarities[reference_indices, row_positions] = -np.inf

    neighbour_count = min(resolved_config.neighbour_count, reference.shape[0] - 1)
    if neighbour_count <= 0:
        return None
    top_similarities = np.partition(similarities, kth=-neighbour_count, axis=1)[
        :, -neighbour_count:
    ]
    average_similarity = np.clip(top_similarities.mean(axis=1), -1.0, 1.0)
    alert_score = np.clip(1.0 - average_similarity, 0.0, 1.0).astype(np.float32)

    return BackendProjections(
        detection_ids=np.asarray(detection_ids, dtype=object),
        detected_ats=np.asarray(detected_ats, dtype=object),
        pc1=pc1_scaled,
        pc2=pc2_scaled,
        alert_score=alert_score,
    )


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------


_FETCH_SQL = """
    SELECT
        detection_id,
        detected_at,
        {column}::text AS embedding_text
    FROM birdweather_tbl
    WHERE has_processed_clip
      AND {column} IS NOT NULL
"""


def _parse_pgvector_text(value: str | None) -> list[float] | None:
    """Parse pgvector's textual ``[v1,v2,...]`` form into a Python list."""
    if not value:
        return None
    text = value.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return None
    inner = text[1:-1].strip()
    if not inner:
        return None
    try:
        return [float(entry) for entry in inner.split(",")]
    except ValueError:
        return None


def _fetch_backend_rows(
    pool: ConnectionPool,
    backend: str,
    *,
    since: str | None = None,
) -> tuple[list[Any], list[Any], NDArray[np.float32]]:
    """Stream embedded detections for ``backend`` into a preallocated matrix.

    Vectors are parsed straight into a preallocated ``float32`` array rather
    than a Python list-of-lists, keeping peak memory near the final matrix size
    (``N * dim * 4`` bytes) instead of ~8x that — the list-of-lists form made
    even a single month of perch_v2 (dim 1536) blow past host RAM. ``since``
    bounds the scan to a recent window (``detected_at >= since``) so the full
    multi-million-row corpus need not be materialised at once.
    """
    column = f"{backend}_embedding"
    where = ["has_processed_clip", f"{column} IS NOT NULL"]
    params: list[Any] = []
    if since:
        where.append("detected_at >= %s")
        params.append(since)
    where_sql = " AND ".join(where)

    with pool.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM birdweather_tbl WHERE {where_sql}", params)
        total = int(cur.fetchone()[0])

    if total == 0:
        return [], [], np.zeros((0, 0), dtype=np.float32)

    detection_ids: list[Any] = []
    detected_ats: list[Any] = []
    matrix: NDArray[np.float32] | None = None
    fill = 0

    fetch_sql = (
        f"SELECT detection_id, detected_at, {column}::text AS embedding_text "
        f"FROM birdweather_tbl WHERE {where_sql}"
    )
    with pool.cursor() as cur:
        cur.itersize = 5_000
        cur.execute(fetch_sql, params)
        for detection_id, detected_at, embedding_text in cur:
            vector = _parse_pgvector_text(embedding_text)
            if vector is None:
                continue
            if matrix is None:
                matrix = np.empty((total, len(vector)), dtype=np.float32)
            matrix[fill, :] = vector
            detection_ids.append(detection_id)
            detected_ats.append(detected_at)
            fill += 1

    if matrix is None or fill == 0:
        return [], [], np.zeros((0, 0), dtype=np.float32)

    return detection_ids, detected_ats, matrix[:fill]


_UPSERT_SQL = """
    UPDATE birdweather_tbl AS b SET
        {backend}_pc1 = data.pc1,
        {backend}_pc2 = data.pc2,
        {backend}_alert_score = data.alert_score
    FROM (VALUES %s) AS data (detection_id, detected_at, pc1, pc2, alert_score)
    WHERE b.detection_id = data.detection_id
      AND b.detected_at = data.detected_at
"""


def write_backend_projection(
    pool: ConnectionPool,
    backend: str,
    projection: BackendProjections,
    *,
    batch_size: int = UPSERT_BATCH_SIZE,
) -> int:
    """Persist ``projection`` to ``birdweather_tbl`` in batches of ``batch_size``."""
    if backend not in PROJECTION_BACKENDS:
        msg = f"unknown backend {backend!r}; expected one of {PROJECTION_BACKENDS}"
        raise ValueError(msg)

    sql = _UPSERT_SQL.format(backend=backend)
    rows = [
        (
            projection.detection_ids[i],
            projection.detected_ats[i],
            float(projection.pc1[i]),
            float(projection.pc2[i]),
            float(projection.alert_score[i]),
        )
        for i in range(len(projection))
    ]
    if not rows:
        return 0

    written = 0
    with pool.cursor() as cur:
        for offset in range(0, len(rows), batch_size):
            chunk = rows[offset : offset + batch_size]
            psycopg2.extras.execute_values(cur, sql, chunk, page_size=batch_size)
            written += len(chunk)
    return written


def run(
    *,
    pool: ConnectionPool | None = None,
    config: EmbeddingProjectionConfig | None = None,
    backends: Iterable[str] = PROJECTION_BACKENDS,
    since: str | None = None,
) -> dict[str, int]:
    """Recompute and persist projections for the requested backends.

    Returns ``{backend: rows_written}``. A zero value means the backend was
    either empty or fell below ``config.minimum_population``; corresponding
    DB columns remain NULL. ``since`` bounds the embedding scan to a recent
    window so the large perch_v2 corpus fits in memory.
    """
    pool = pool or get_pool()
    resolved_config = config or EmbeddingProjectionConfig()
    written_per_backend: dict[str, int] = {}

    for backend in backends:
        if backend not in PROJECTION_BACKENDS:
            msg = f"unknown backend {backend!r}; expected one of {PROJECTION_BACKENDS}"
            raise ValueError(msg)

        logger.info(
            "[embedding_projection] reading %s embeddings%s",
            backend,
            f" (since {since})" if since else "",
        )
        detection_ids, detected_ats, matrix = _fetch_backend_rows(
            pool, backend, since=since
        )

        if matrix.shape[0] == 0:
            logger.info("[embedding_projection] %s: no embedded detections", backend)
            written_per_backend[backend] = 0
            continue

        logger.info(
            "[embedding_projection] %s: %d detections, dim=%d — fitting PCA + kNN",
            backend,
            matrix.shape[0],
            matrix.shape[1],
        )
        projection = compute_backend_projection(
            detection_ids=detection_ids,
            detected_ats=detected_ats,
            embeddings=matrix,
            config=resolved_config,
        )
        if projection is None:
            logger.warning(
                "[embedding_projection] %s: population %d below minimum %d, skipped",
                backend,
                matrix.shape[0],
                resolved_config.minimum_population,
            )
            written_per_backend[backend] = 0
            continue

        logger.info(
            "[embedding_projection] %s: writing %d rows to birdweather_tbl",
            backend,
            len(projection),
        )
        written_per_backend[backend] = write_backend_projection(
            pool, backend, projection
        )

    return written_per_backend


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        action="append",
        choices=PROJECTION_BACKENDS,
        help="Restrict to a single backend (may be repeated). Default: all.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Only project detections with detected_at >= this date (YYYY-MM-DD). "
            "Bounds peak memory for the large perch_v2 corpus. Default: all rows."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    backends = tuple(args.backend) if args.backend else PROJECTION_BACKENDS
    counts = run(backends=backends, since=args.since)
    for backend, n in counts.items():
        sys.stdout.write(f"{backend}: {n} rows updated\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
