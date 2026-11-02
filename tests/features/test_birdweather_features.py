from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast

import numpy as np
import pytest

import avian_biosurveillance.features.birdweather_features as features_module
from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError
from avian_biosurveillance.features.birdweather_features import (
    _load_feature_candidates,
    engineer_birdweather_audio_features,
)
from avian_biosurveillance.features.embeddings import EmbeddingResult
from avian_biosurveillance.processing.birdweather_audio import BirdWeatherAudioCandidate


@pytest.fixture(autouse=True)
def _stub_embedding_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(features_module, "warm_embedding_models", lambda *_a, **_k: {})


def _embedded_results() -> dict[str, EmbeddingResult]:
    return {
        "birdnet": EmbeddingResult(
            provider="birdnet",
            status="embedded",
            values=[1.0, 2.0],
        ),
        "perch_v2": EmbeddingResult(
            provider="perch_v2",
            status="embedded",
            values=[3.0, 4.0],
        ),
    }


class _RecordingCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: list[Any] = []

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self.sql = sql
        self.params = params if params is not None else []

    def fetchall(self) -> list[dict[str, Any]]:
        return []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        return False


class _RecordingPool:
    def __init__(self) -> None:
        self.cursor_instance = _RecordingCursor()

    def cursor(self, cursor_factory: Any = None) -> _RecordingCursor:
        _ = cursor_factory
        return self.cursor_instance


def test_load_feature_candidates_retries_rows_with_missing_embeddings() -> None:
    pool = cast(Any, _RecordingPool())

    _load_feature_candidates(pool, lookback_days=2, limit=10, force=False)

    assert "classification_failed" in pool.cursor_instance.sql
    assert "birdnet_embedding IS NULL" in pool.cursor_instance.sql
    assert "perch_v2_embedding IS NULL" in pool.cursor_instance.sql


def test_load_feature_candidates_applies_detection_shard_filter() -> None:
    pool = cast(Any, _RecordingPool())

    _load_feature_candidates(
        pool,
        lookback_days=2,
        limit=10,
        force=False,
        shard_count=4,
        shard_index=1,
    )

    assert "hashtext(detection_id)" in pool.cursor_instance.sql
    assert "detected_at >= (CURRENT_DATE - %s::int)::timestamptz" in pool.cursor_instance.sql
    assert pool.cursor_instance.params == [2, 4, 1, 10]


def test_update_feature_row_targets_chunk_directly() -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        def __init__(self) -> None:
            self.rowcount = 1

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            calls.append((sql, params))

        def fetchone(self) -> tuple[str, str]:
            return ("_timescaledb_internal._hyper_1_2_chunk", "(0,1)")

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
            return False

    class _Pool:
        def cursor(self, **_kwargs: Any) -> _Cursor:
            return _Cursor()

    updated = features_module._update_feature_row(
        cast(Any, _Pool()),
        row,
        background_payload={"status": "classified", "tags": []},
        embedding_results={
            "birdnet": EmbeddingResult(
                provider="birdnet",
                status="embedded",
                values=[1.0, 2.0],
            ),
            "perch_v2": EmbeddingResult(
                provider="perch_v2",
                status="embedded",
                values=[3.0, 4.0],
            ),
        },
        artifact_manifest={"processed_clip": "processed-audio/example.flac"},
    )

    assert updated is True
    assert len(calls) == 2

    select_sql, select_params = calls[0]
    assert "SELECT tableoid::regclass::text AS chunk_name" in select_sql
    assert "detected_at >= %s" in select_sql
    assert "detected_at < %s" in select_sql
    assert select_params == (
        datetime(2026, 5, 5, tzinfo=UTC),
        datetime(2026, 5, 6, tzinfo=UTC),
        date(2026, 5, 5),
        "det-1",
        datetime(2026, 5, 5, tzinfo=UTC),
    )

    update_sql, update_params = calls[1]
    assert 'UPDATE "_timescaledb_internal"."_hyper_1_2_chunk"' in update_sql
    assert "WHERE ctid = %s::tid" in update_sql
    assert update_params[-3:] == (
        "(0,1)",
        "det-1",
        datetime(2026, 5, 5, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("shard_count", "shard_index"),
    [
        (None, 0),
        (4, None),
        (0, 0),
        (4, -1),
        (4, 4),
    ],
)
def test_load_feature_candidates_rejects_invalid_shard_args(
    shard_count: int | None,
    shard_index: int | None,
) -> None:
    pool = cast(Any, _RecordingPool())

    with pytest.raises(ValueError):
        _load_feature_candidates(
            pool,
            lookback_days=2,
            limit=10,
            force=False,
            shard_count=shard_count,
            shard_index=shard_index,
        )


def test_engineer_birdweather_audio_features_raises_when_storage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )

    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: [row],
    )

    class _Store:
        def get_object_if_exists(self, *_args: object, **_kwargs: object) -> bytes:
            raise AudioStoreUnavailableError("MinIO unavailable")

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    with pytest.raises(AudioStoreUnavailableError):
        engineer_birdweather_audio_features(
            store=store,
            pool=pool,
            limit=1,
        )


def test_engineer_birdweather_audio_features_keeps_embeddings_when_artifacts_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        features_module,
        "_decode_processed_clip",
        lambda *_args, **_kwargs: (np.zeros(16_000, dtype=np.float32), 32_000),
    )
    monkeypatch.setattr(
        features_module,
        "_upload_feature_artifacts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AudioStoreUnavailableError("MinIO unavailable")
        ),
    )
    monkeypatch.setattr(
        features_module,
        "compute_embedding_results_batch",
        lambda clips, **_kwargs: [_embedded_results() for _clip in clips],
    )

    class _Classifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def classify_array(self, *_args: object, **_kwargs: object) -> list[Any]:
            return []

    def _fake_update(
        _pool: Any,
        _row: Any,
        *,
        background_payload: dict[str, Any],
        embedding_results: dict[str, EmbeddingResult],
        artifact_manifest: dict[str, Any],
    ) -> bool:
        captured["background_payload"] = background_payload
        captured["embedding_results"] = embedding_results
        captured["artifact_manifest"] = artifact_manifest
        return True

    monkeypatch.setattr(features_module, "BackgroundSoundClassifier", _Classifier)
    monkeypatch.setattr(features_module, "_update_feature_row", _fake_update)

    class _Store:
        def get_object_if_exists(self, *_args: object, **_kwargs: object) -> bytes:
            return b"processed-audio"

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = engineer_birdweather_audio_features(
        store=store,
        pool=pool,
        limit=1,
    )

    assert stats.artifact_upload_failed == 1
    assert stats.embeddings_written == 2
    assert stats.db_updated == 1
    assert captured["artifact_manifest"]["artifact_upload_status"] == "failed"
    assert captured["artifact_manifest"]["processed_clip"].endswith("det-1.flac")


def test_engineer_birdweather_audio_features_continues_after_storage_check_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    second_row = BirdWeatherAudioCandidate(
        detection_id="det-2",
        detected_at=datetime(2026, 5, 5, 0, 0, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    processed_ids: list[str] = []

    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: [first_row, second_row],
    )
    monkeypatch.setattr(
        features_module,
        "_decode_processed_clip",
        lambda *_args, **_kwargs: (np.zeros(16_000, dtype=np.float32), 32_000),
    )
    monkeypatch.setattr(
        features_module,
        "_upload_feature_artifacts",
        lambda *_args, **_kwargs: {
            "processed_clip": "processed-audio/example.flac",
            "generated_at": "2026-05-05T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        features_module,
        "compute_embedding_results_batch",
        lambda clips, **_kwargs: [_embedded_results() for _clip in clips],
    )

    class _Classifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def classify_array(self, *_args: object, **_kwargs: object) -> list[Any]:
            return []

    def _fake_update(
        _pool: Any,
        row: BirdWeatherAudioCandidate,
        *,
        background_payload: dict[str, Any],
        embedding_results: dict[str, EmbeddingResult],
        artifact_manifest: dict[str, Any],
    ) -> bool:
        _ = background_payload, embedding_results, artifact_manifest
        processed_ids.append(row.detection_id)
        return True

    monkeypatch.setattr(features_module, "BackgroundSoundClassifier", _Classifier)
    monkeypatch.setattr(features_module, "_update_feature_row", _fake_update)

    class _Store:
        def get_object_if_exists(self, object_name: str, **_kwargs: object) -> bytes:
            if object_name.endswith("det-1.flac"):
                raise AudioStoreUnavailableError("MinIO unavailable")
            return b"processed-audio"

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = engineer_birdweather_audio_features(
        store=store,
        pool=pool,
        limit=2,
        continue_on_storage_unavailable=True,
    )

    assert stats.storage_unavailable == 1
    assert stats.db_updated == 1
    assert stats.embeddings_written == 2
    assert processed_ids == ["det-2"]


def test_engineer_birdweather_audio_features_batches_embedding_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        BirdWeatherAudioCandidate(
            detection_id=f"det-{index + 1}",
            detected_at=datetime(2026, 5, 5, 0, 0, index, tzinfo=UTC),
            observation_date=date(2026, 5, 5),
            station_id="station-7",
            species_id="sp-9",
            audio_filename=None,
            audio_start_time_s=0.0,
            audio_end_time_s=3.0,
        )
        for index in range(2)
    ]
    batch_sizes: list[int] = []
    processed_ids: list[str] = []

    monkeypatch.setenv("BIRDWEATHER_FEATURE_PREFETCH_WORKERS", "1")
    monkeypatch.setenv("BIRDWEATHER_EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: rows,
    )
    monkeypatch.setattr(
        features_module,
        "_decode_processed_clip",
        lambda *_args, **_kwargs: (np.zeros(16_000, dtype=np.float32), 32_000),
    )

    def _fake_embedding_batch(
        clips: list[tuple[np.ndarray, int]],
        **_kwargs: Any,
    ) -> list[dict[str, EmbeddingResult]]:
        batch_sizes.append(len(clips))
        return [_embedded_results() for _clip in clips]

    def _fake_update(
        _pool: Any,
        row: BirdWeatherAudioCandidate,
        *,
        background_payload: dict[str, Any] | None,
        embedding_results: dict[str, EmbeddingResult],
        artifact_manifest: dict[str, Any],
    ) -> bool:
        _ = background_payload, embedding_results, artifact_manifest
        processed_ids.append(row.detection_id)
        return True

    monkeypatch.setattr(
        features_module,
        "compute_embedding_results_batch",
        _fake_embedding_batch,
    )
    monkeypatch.setattr(features_module, "_update_feature_row", _fake_update)

    class _Store:
        def get_object_if_exists(self, *_args: object, **_kwargs: object) -> bytes:
            return b"processed-audio"

    stats = engineer_birdweather_audio_features(
        store=cast(Any, _Store()),
        pool=cast(Any, SimpleNamespace(close=lambda: None)),
        limit=2,
        include_background_classification=False,
        include_feature_artifacts=False,
    )

    # One batched inference call per selected provider (birdnet, then perch_v2).
    assert batch_sizes == [2, 2]
    assert processed_ids == ["det-1", "det-2"]
    assert stats.embeddings_written == 4
    assert stats.db_updated == 2


def test_engineer_birdweather_audio_features_warms_models_before_clip_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    events: list[str] = []

    monkeypatch.setenv("BIRDWEATHER_FEATURE_PREFETCH_WORKERS", "1")
    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        features_module,
        "warm_embedding_models",
        lambda *_a, **_k: events.append("warm") or {},
    )
    monkeypatch.setattr(
        features_module,
        "_decode_processed_clip",
        lambda *_args, **_kwargs: (np.zeros(16_000, dtype=np.float32), 32_000),
    )
    monkeypatch.setattr(
        features_module,
        "compute_embedding_results_batch",
        lambda clips, **_kwargs: [_embedded_results() for _clip in clips],
    )
    monkeypatch.setattr(
        features_module,
        "_update_feature_row",
        lambda *_args, **_kwargs: True,
    )

    class _Store:
        def get_object_if_exists(self, *_args: object, **_kwargs: object) -> bytes:
            events.append("fetch")
            return b"processed-audio"

    engineer_birdweather_audio_features(
        store=cast(Any, _Store()),
        pool=cast(Any, SimpleNamespace(close=lambda: None)),
        limit=1,
        include_background_classification=False,
        include_feature_artifacts=False,
    )

    assert events[:2] == ["warm", "fetch"]


def test_engineer_birdweather_audio_features_retries_storage_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    exists_attempts = 0

    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        features_module,
        "_decode_processed_clip",
        lambda *_args, **_kwargs: (np.zeros(16_000, dtype=np.float32), 32_000),
    )
    monkeypatch.setattr(
        features_module,
        "_upload_feature_artifacts",
        lambda *_args, **_kwargs: {
            "processed_clip": "processed-audio/example.flac",
            "generated_at": "2026-05-05T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        features_module,
        "compute_embedding_results_batch",
        lambda clips, **_kwargs: [_embedded_results() for _clip in clips],
    )
    monkeypatch.setattr(
        "avian_biosurveillance.features.birdweather_features.time.sleep",
        lambda *_args: None,
    )

    class _Classifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def classify_array(self, *_args: object, **_kwargs: object) -> list[Any]:
            return []

    monkeypatch.setattr(features_module, "BackgroundSoundClassifier", _Classifier)
    monkeypatch.setattr(
        features_module,
        "_update_feature_row",
        lambda *_args, **_kwargs: True,
    )

    class _Store:
        def get_object_if_exists(self, *_args: object, **_kwargs: object) -> bytes:
            nonlocal exists_attempts
            exists_attempts += 1
            if exists_attempts < 3:
                raise AudioStoreUnavailableError("MinIO unavailable")
            return b"processed-audio"

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = engineer_birdweather_audio_features(
        store=store,
        pool=pool,
        limit=1,
        continue_on_storage_unavailable=True,
        storage_retry_attempts=2,
        storage_retry_backoff_seconds=0.0,
    )

    assert exists_attempts == 3
    assert stats.storage_retries == 2
    assert stats.storage_unavailable == 0
    assert stats.db_updated == 1


def test_engineer_birdweather_audio_features_retries_processed_clip_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=3.0,
    )
    get_attempts = 0

    monkeypatch.setattr(
        features_module,
        "_load_feature_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        features_module,
        "_decode_processed_clip",
        lambda *_args, **_kwargs: (np.zeros(16_000, dtype=np.float32), 32_000),
    )
    monkeypatch.setattr(
        features_module,
        "_upload_feature_artifacts",
        lambda *_args, **_kwargs: {
            "processed_clip": "processed-audio/example.flac",
            "generated_at": "2026-05-05T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        features_module,
        "compute_embedding_results_batch",
        lambda clips, **_kwargs: [_embedded_results() for _clip in clips],
    )
    monkeypatch.setattr(
        "avian_biosurveillance.features.birdweather_features.time.sleep",
        lambda *_args: None,
    )

    class _Classifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs

        def classify_array(self, *_args: object, **_kwargs: object) -> list[Any]:
            return []

    monkeypatch.setattr(features_module, "BackgroundSoundClassifier", _Classifier)
    monkeypatch.setattr(
        features_module,
        "_update_feature_row",
        lambda *_args, **_kwargs: True,
    )

    class _Store:
        def get_object_if_exists(self, *_args: object, **_kwargs: object) -> bytes:
            nonlocal get_attempts
            get_attempts += 1
            if get_attempts < 3:
                raise AudioStoreUnavailableError("MinIO unavailable")
            return b"processed-audio"

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = engineer_birdweather_audio_features(
        store=store,
        pool=pool,
        limit=1,
        continue_on_storage_unavailable=True,
        storage_retry_attempts=2,
        storage_retry_backoff_seconds=0.0,
    )

    assert get_attempts == 3
    assert stats.storage_retries == 2
    assert stats.storage_unavailable == 0
    assert stats.db_updated == 1
