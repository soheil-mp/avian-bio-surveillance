from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

import avian_biosurveillance.ops.ensure_historical_window as window_module
from avian_biosurveillance.db.minio_client import (
    BUCKET_FEATURES,
    BUCKET_PROCESSED_AUDIO,
    BUCKET_RAW_AUDIO,
)
from avian_biosurveillance.ops.ensure_historical_window import (
    _birdweather_object_refs,
    _date_windows,
    _feature_object_refs,
    _raise_for_birdweather_audio_processing_failures,
    _raise_for_birdweather_raw_audio_failures,
    OperationState,
    expand_phases,
    parse_sources,
)


def test_parse_sources_normalizes_comma_list() -> None:
    assert parse_sources("birdweather, knmi,ebird") == (
        "birdweather",
        "knmi",
        "ebird",
    )


def test_parse_sources_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown source"):
        parse_sources("birdweather,unknown")


def test_expand_phases_all() -> None:
    assert expand_phases("all") == (
        "audit",
        "cleanup",
        "backfill",
        "audio",
        "features",
        "refresh",
        "verify",
    )


def test_date_windows_are_inclusive() -> None:
    windows = list(_date_windows(date(2024, 1, 1), date(2024, 1, 10), 4))

    assert [(window.start, window.end) for window in windows] == [
        (date(2024, 1, 1), date(2024, 1, 4)),
        (date(2024, 1, 5), date(2024, 1, 8)),
        (date(2024, 1, 9), date(2024, 1, 10)),
    ]


def test_feature_object_refs_extracts_known_bucket_paths() -> None:
    refs = _feature_object_refs(
        {
            "processed_clip": "processed-audio/birdweather/2024-01-01/s1/d1.flac",
            "spectrogram": "features/spectrograms/birdweather/2024-01-01/s1/d1.png",
            "waveform": "waveforms/birdweather/2024-01-01/s1/d1.png",
            "ignored_url": "https://example.invalid/file.png",
        }
    )

    assert refs == [
        (BUCKET_PROCESSED_AUDIO, "birdweather/2024-01-01/s1/d1.flac"),
        (BUCKET_FEATURES, "spectrograms/birdweather/2024-01-01/s1/d1.png"),
        (BUCKET_FEATURES, "waveforms/birdweather/2024-01-01/s1/d1.png"),
    ]


def test_birdweather_object_refs_include_raw_processed_and_features() -> None:
    row = {
        "detection_id": "det-1",
        "detected_at": datetime(2024, 1, 1, tzinfo=UTC),
        "observation_date": date(2024, 1, 1),
        "station_id": "station-1",
        "species_id": "35",
        "audio_filename": "det-1_35.mp3",
        "audio_url": "https://cdn.example/audio.mp3",
        "has_processed_clip": True,
        "feature_artifacts": {
            "spectrogram": "features/spectrograms/birdweather/2024-01-01/station-1/det-1.png"
        },
    }

    refs = _birdweather_object_refs(row)

    assert (BUCKET_RAW_AUDIO, "2024-01-01/audio/det-1_35.mp3") in refs
    assert (
        BUCKET_PROCESSED_AUDIO,
        "birdweather/2024-01-01/station-1/det-1.flac",
    ) in refs
    assert (
        BUCKET_FEATURES,
        "spectrograms/birdweather/2024-01-01/station-1/det-1.png",
    ) in refs
    assert (
        BUCKET_FEATURES,
        "waveforms/birdweather/2024-01-01/station-1/det-1_smooth.png",
    ) in refs


def test_raise_for_birdweather_raw_audio_failures_ignores_archived_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")

    _raise_for_birdweather_raw_audio_failures(
        "BirdWeather raw audio 2024-01-01..2024-01-07",
        SimpleNamespace(
            failed=0,
            upload_failed=0,
            archived=6464,
            missing_file=0,
            not_found=0,
            forbidden=0,
            server_error=0,
            timeout=0,
            download_error=0,
            max_retries=0,
        ),
    )

    assert "continuing historical repair" in caplog.text


def test_raise_for_birdweather_raw_audio_failures_still_raises_hard_failures() -> None:
    with pytest.raises(RuntimeError, match="upload_failed"):
        _raise_for_birdweather_raw_audio_failures(
            "BirdWeather raw audio 2024-01-01..2024-01-07",
            SimpleNamespace(
                failed=0,
                upload_failed=1,
                archived=10,
                missing_file=0,
                not_found=0,
                forbidden=0,
                server_error=0,
                timeout=0,
                download_error=0,
                max_retries=0,
            ),
        )


def test_raise_for_birdweather_audio_processing_failures_ignores_missing_raw(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")

    _raise_for_birdweather_audio_processing_failures(
        "BirdWeather audio processing 2024-01-01",
        SimpleNamespace(
            missing_raw=260,
            invalid_window=0,
            decode_failed=0,
            upload_failed=0,
        ),
    )

    assert "continuing historical repair" in caplog.text


def test_raise_for_birdweather_audio_processing_failures_still_raises_upload_failures() -> None:
    with pytest.raises(RuntimeError, match="upload_failed"):
        _raise_for_birdweather_audio_processing_failures(
            "BirdWeather audio processing 2024-01-01",
            SimpleNamespace(
                missing_raw=10,
                invalid_window=0,
                decode_failed=0,
                upload_failed=1,
            ),
        )


def test_operation_state_merges_concurrent_writes(tmp_path: pytest.TempPathFactory) -> None:
    state_path = tmp_path / "state.json"
    first = OperationState.load(state_path)
    second = OperationState.load(state_path)

    first.mark_completed("audio:birdweather:2024-01-01..2024-01-07")
    second.mark_completed("features:birdweather:2024-03-08..2024-03-08")

    merged = OperationState.load(state_path)

    assert merged.is_completed("audio:birdweather:2024-01-01..2024-01-07")
    assert merged.is_completed("features:birdweather:2024-03-08..2024-03-08")


def test_audio_phase_leaves_window_incomplete_when_storage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    monkeypatch.setattr(
        window_module,
        "ConnectionPool",
        lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_raw_audio.download_birdweather_raw_audio_objects",
        lambda **_kwargs: SimpleNamespace(
            failed=0,
            upload_failed=0,
            archived=0,
            missing_file=0,
            not_found=0,
            forbidden=0,
            server_error=0,
            timeout=0,
            download_error=0,
            max_retries=0,
        ),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_audio.process_birdweather_audio_clips",
        lambda **_kwargs: SimpleNamespace(
            missing_raw=0,
            invalid_window=0,
            decode_failed=0,
            upload_failed=0,
            storage_unavailable=1,
        ),
    )

    state_path = tmp_path / "state.json"
    runner = window_module.HistoricalWindowRunner(
        window_module.HistoricalWindowConfig(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            sources=("birdweather",),
            batch_days=7,
            workers=4,
            delete_off_target=False,
            target="database",
            grid_size_km=5.0,
            state_path=state_path,
        )
    )
    runner._birdweather_audio_day_materialized = lambda day: False
    try:
        runner.audio()
    finally:
        runner.close()

    assert runner.state.payload["completed"] == {}
    assert not state_path.exists()


def test_audio_phase_leaves_window_incomplete_when_raw_upload_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    process_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        window_module,
        "ConnectionPool",
        lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_raw_audio.download_birdweather_raw_audio_objects",
        lambda **_kwargs: SimpleNamespace(
            failed=0,
            upload_failed=2,
            archived=0,
            missing_file=0,
            not_found=0,
            forbidden=0,
            server_error=0,
            timeout=0,
            download_error=0,
            max_retries=0,
        ),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_audio.process_birdweather_audio_clips",
        lambda **kwargs: process_calls.append(kwargs) or SimpleNamespace(),
    )

    state_path = tmp_path / "state.json"
    runner = window_module.HistoricalWindowRunner(
        window_module.HistoricalWindowConfig(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            sources=("birdweather",),
            batch_days=7,
            workers=4,
            delete_off_target=False,
            target="database",
            grid_size_km=5.0,
            state_path=state_path,
        )
    )
    runner._birdweather_audio_day_materialized = lambda day: False

    try:
        runner.audio()
    finally:
        runner.close()

    assert process_calls == []
    assert runner.state.payload["completed"] == {}
    assert not state_path.exists()


def test_audio_phase_marks_window_complete_when_minio_prefixes_already_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    downloads: list[dict[str, object]] = []
    processes: list[dict[str, object]] = []

    monkeypatch.setattr(
        window_module,
        "ConnectionPool",
        lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_raw_audio.download_birdweather_raw_audio_objects",
        lambda **kwargs: downloads.append(kwargs) or SimpleNamespace(),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.processing.birdweather_audio.process_birdweather_audio_clips",
        lambda **kwargs: processes.append(kwargs) or SimpleNamespace(),
    )

    state_path = tmp_path / "state.json"
    runner = window_module.HistoricalWindowRunner(
        window_module.HistoricalWindowConfig(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
            sources=("birdweather",),
            batch_days=7,
            workers=4,
            delete_off_target=False,
            target="database",
            grid_size_km=5.0,
            state_path=state_path,
        )
    )
    runner._birdweather_audio_day_materialized = lambda day: True

    try:
        runner.audio()
    finally:
        runner.close()

    assert downloads == []
    assert processes == []
    assert runner.state.is_completed("audio:birdweather:2024-01-01..2024-01-02")


def test_features_phase_marks_day_complete_when_minio_prefixes_already_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    feature_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        window_module,
        "ConnectionPool",
        lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        "avian_biosurveillance.features.birdweather_features.engineer_birdweather_audio_features",
        lambda **kwargs: feature_calls.append(kwargs) or SimpleNamespace(),
    )

    state_path = tmp_path / "state.json"
    runner = window_module.HistoricalWindowRunner(
        window_module.HistoricalWindowConfig(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
            sources=("birdweather",),
            batch_days=7,
            workers=4,
            delete_off_target=False,
            target="database",
            grid_size_km=5.0,
            state_path=state_path,
        )
    )
    runner._birdweather_feature_day_materialized = lambda day: True

    try:
        runner.features()
    finally:
        runner.close()

    assert feature_calls == []
    assert runner.state.is_completed("features:birdweather:2024-01-01..2024-01-01")
    assert runner.state.is_completed("features:birdweather:2024-01-02..2024-01-02")


def test_birdweather_audio_day_materialized_requires_db_and_prefixes() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    target_day = date(2024, 1, 7)
    runner._birdweather_audio_day_status = lambda day: {
        "rows_with_audio_url": 3,
        "missing_raw_filename": 0,
        "raw_retention_required_rows": 3,
        "processable_rows": 2,
        "missing_processed_clip": 0,
    }
    calls: list[tuple[str, str]] = []
    runner._prefix_has_objects = lambda prefix, *, bucket: calls.append((bucket, prefix)) or True

    assert runner._birdweather_audio_day_materialized(target_day) is True
    assert calls == [
        (BUCKET_RAW_AUDIO, "2024-01-07/audio/"),
        (BUCKET_PROCESSED_AUDIO, "birdweather/2024-01-07/"),
    ]


def test_birdweather_audio_day_materialized_allows_pruned_raw_after_processing() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    target_day = date(2024, 1, 7)
    runner._birdweather_audio_day_status = lambda day: {
        "rows_with_audio_url": 3,
        "missing_raw_filename": 0,
        "raw_retention_required_rows": 0,
        "processable_rows": 2,
        "missing_processed_clip": 0,
    }
    calls: list[tuple[str, str]] = []

    def fake_has_objects(prefix: str, *, bucket: str) -> bool:
        calls.append((bucket, prefix))
        return bucket == BUCKET_PROCESSED_AUDIO

    runner._prefix_has_objects = fake_has_objects

    assert runner._birdweather_audio_day_materialized(target_day) is True
    assert calls == [
        (BUCKET_PROCESSED_AUDIO, "birdweather/2024-01-07/"),
    ]


def test_birdweather_feature_day_materialized_requires_feature_prefix() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    target_day = date(2024, 1, 7)
    runner._birdweather_feature_day_status = lambda day: {
        "processed_rows": 2,
        "missing_features": 0,
    }
    calls: list[tuple[str, str]] = []

    def fake_has_objects(prefix: str, *, bucket: str) -> bool:
        calls.append((bucket, prefix))
        return prefix.startswith("waveforms/")

    runner._prefix_has_objects = fake_has_objects

    assert runner._birdweather_feature_day_materialized(target_day) is True
    assert calls == [
        (BUCKET_FEATURES, "spectrograms/birdweather/2024-01-07/"),
        (BUCKET_FEATURES, "waveforms/birdweather/2024-01-07/"),
    ]


def test_birdweather_audio_day_status_uses_detected_at_day_bounds() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)

    class FakeCursor:
        def __init__(self) -> None:
            self.sql: str | None = None
            self.params: tuple[object, ...] | None = None

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            self.sql = sql
            self.params = params

        def fetchone(self) -> tuple[int, int, int, int, int]:
            return (0, 0, 0, 0, 0)

    fake_cursor = FakeCursor()
    runner.pool = SimpleNamespace(cursor=lambda **kwargs: nullcontext(fake_cursor))

    runner._birdweather_audio_day_status(date(2024, 1, 7))

    assert fake_cursor.sql is not None
    assert "detected_at >= %s" in fake_cursor.sql
    assert "detected_at < %s" in fake_cursor.sql
    assert fake_cursor.params == (
        datetime(2024, 1, 7, tzinfo=UTC),
        datetime(2024, 1, 8, tzinfo=UTC),
        date(2024, 1, 7),
    )


def test_verify_birdweather_objects_allows_pruned_raw_when_processed_clip_exists() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    target_day = date(2024, 1, 7)
    row = {
        "detection_id": "det-1",
        "observation_date": target_day,
        "station_id": "station-1",
        "species_id": "35",
        "audio_filename": "det-1_35.mp3",
        "audio_url": "https://cdn.example/audio.mp3",
        "audio_start_time_s": 0.0,
        "audio_end_time_s": 1.0,
        "has_processed_clip": True,
        "feature_artifacts": {
            "spectrogram": "features/spectrograms/birdweather/2024-01-07/station-1/det-1.png",
            "waveform": "waveforms/birdweather/2024-01-07/station-1/det-1_smooth.png",
        },
    }

    def fake_list_objects(prefix: str, *, bucket: str) -> list[str]:
        if bucket == BUCKET_RAW_AUDIO:
            return []
        if bucket == BUCKET_PROCESSED_AUDIO:
            return ["birdweather/2024-01-07/station-1/det-1.flac"]
        if prefix.startswith("spectrograms/"):
            return ["spectrograms/birdweather/2024-01-07/station-1/det-1.png"]
        return ["waveforms/birdweather/2024-01-07/station-1/det-1_smooth.png"]

    runner._store = SimpleNamespace(list_objects=fake_list_objects)
    runner.config = SimpleNamespace(start_date=target_day, end_date=target_day)
    runner._birdweather_object_rows = lambda day: [row]

    failures: list[dict[str, object]] = []
    report = runner._verify_birdweather_objects(failures)

    assert report["raw_missing"] == 0
    assert report["processed_missing"] == 0
    assert report["feature_artifact_missing"] == 0
    assert failures == []


def test_verify_birdweather_objects_still_requires_raw_for_unprocessed_rows() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    target_day = date(2024, 1, 7)
    row = {
        "detection_id": "det-1",
        "observation_date": target_day,
        "station_id": "station-1",
        "species_id": "35",
        "audio_filename": "det-1_35.mp3",
        "audio_url": "https://cdn.example/audio.mp3",
        "audio_start_time_s": None,
        "audio_end_time_s": None,
        "has_processed_clip": False,
        "feature_artifacts": None,
    }

    def fake_list_objects(prefix: str, *, bucket: str) -> list[str]:
        return []

    runner._store = SimpleNamespace(list_objects=fake_list_objects)
    runner.config = SimpleNamespace(start_date=target_day, end_date=target_day)
    runner._birdweather_object_rows = lambda day: [row]

    failures: list[dict[str, object]] = []
    report = runner._verify_birdweather_objects(failures)

    assert report["raw_missing"] == 1
    assert report["processed_missing"] == 0
    assert report["feature_artifact_missing"] == 0
    assert failures == [
        {
            "check": "birdweather_object_raw_missing",
            "count": 1,
            "details": {"sample": ["det-1"]},
        }
    ]


def test_delete_off_target_birdweather_day_fast_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    day = date(2024, 1, 7)
    calls: list[tuple[date, int]] = []
    batches = iter((50_000, 1_234, 0))

    def fake_batch(
        self: object,
        requested_day: date,
        batch_size: int,
    ) -> int:
        calls.append((requested_day, batch_size))
        return next(batches)

    monkeypatch.setattr(
        window_module.HistoricalWindowRunner,
        "_delete_off_target_birdweather_day_fast_batch",
        fake_batch,
    )

    deleted_rows = runner._delete_off_target_birdweather_day_fast(day)

    assert deleted_rows == 51_234
    assert calls == [
        (day, window_module._BIRDWEATHER_CLEANUP_BATCH_SIZE),
        (day, window_module._BIRDWEATHER_CLEANUP_BATCH_SIZE),
        (day, window_module._BIRDWEATHER_CLEANUP_BATCH_SIZE),
    ]


def test_delete_off_target_birdweather_day_batches_returned_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    day = date(2024, 1, 7)
    calls: list[tuple[date, int]] = []
    first_batch = [{"detection_id": "det-1"}]
    second_batch = [{"detection_id": "det-2"}, {"detection_id": "det-3"}]
    batches = iter((first_batch, second_batch, []))

    def fake_batch(
        self: object,
        requested_day: date,
        batch_size: int,
    ) -> list[dict[str, str]]:
        calls.append((requested_day, batch_size))
        return next(batches)

    monkeypatch.setattr(
        window_module.HistoricalWindowRunner,
        "_delete_off_target_birdweather_day_batch",
        fake_batch,
    )

    deleted_rows = runner._delete_off_target_birdweather_day(day)

    assert deleted_rows == first_batch + second_batch
    assert calls == [
        (day, window_module._BIRDWEATHER_CLEANUP_BATCH_SIZE),
        (day, window_module._BIRDWEATHER_CLEANUP_BATCH_SIZE),
        (day, window_module._BIRDWEATHER_CLEANUP_BATCH_SIZE),
    ]


def test_delete_off_target_birdweather_day_batch_selects_then_deletes() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    day = date(2024, 1, 7)
    rows = [
        {
            "detection_id": "det-1",
            "detected_at": datetime(2024, 1, 7, tzinfo=UTC),
            "observation_date": day,
            "station_id": "station-1",
            "species_id": "999",
            "audio_filename": "det-1_999.mp3",
            "audio_url": "https://cdn.example/det-1_999.mp3",
            "has_processed_clip": False,
            "feature_artifacts": None,
        }
    ]
    delete_calls: list[list[dict[str, object]]] = []
    captured: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            captured["query"] = query
            captured["params"] = params

        def fetchall(self) -> list[dict[str, object]]:
            return rows

    class FakePool:
        def cursor(self, cursor_factory: object | None = None) -> FakeCursor:
            captured["cursor_factory"] = cursor_factory
            return FakeCursor()

    def fake_delete(selected_rows: list[dict[str, object]]) -> int:
        delete_calls.append(selected_rows)
        return len(selected_rows)

    runner.pool = FakePool()
    runner.target_birdweather_ids = ("35", "61")
    runner._delete_birdweather_rows = fake_delete

    deleted_rows = runner._delete_off_target_birdweather_day_batch(day, 123)

    assert deleted_rows == rows
    assert delete_calls == [rows]
    assert captured["cursor_factory"] is window_module.psycopg2.extras.RealDictCursor
    assert captured["params"] == (
        datetime(2024, 1, 7, tzinfo=UTC),
        datetime(2024, 1, 8, tzinfo=UTC),
        day,
        runner.target_birdweather_ids,
        123,
    )
    assert "SELECT" in str(captured["query"])
    assert "ORDER BY detected_at, detection_id" in str(captured["query"])


def test_delete_off_target_birdweather_day_fast_batch_selects_then_deletes() -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    day = date(2024, 6, 25)
    rows = [
        {
            "detection_id": "det-1",
            "detected_at": datetime(2024, 6, 25, tzinfo=UTC),
        },
        {
            "detection_id": "det-2",
            "detected_at": datetime(2024, 6, 25, 0, 1, tzinfo=UTC),
        },
    ]
    delete_calls: list[list[dict[str, object]]] = []
    captured: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            captured["query"] = query
            captured["params"] = params

        def fetchall(self) -> list[dict[str, object]]:
            return rows

    class FakePool:
        def cursor(self, cursor_factory: object | None = None) -> FakeCursor:
            captured["cursor_factory"] = cursor_factory
            return FakeCursor()

    def fake_delete(selected_rows: list[dict[str, object]]) -> int:
        delete_calls.append(selected_rows)
        return len(selected_rows)

    runner.pool = FakePool()
    runner.target_birdweather_ids = ("35", "61")
    runner._delete_birdweather_rows = fake_delete

    deleted_rows = runner._delete_off_target_birdweather_day_fast_batch(day, 123)

    assert deleted_rows == len(rows)
    assert delete_calls == [rows]
    assert captured["cursor_factory"] is window_module.psycopg2.extras.RealDictCursor
    assert captured["params"] == (
        datetime(2024, 6, 25, tzinfo=UTC),
        datetime(2024, 6, 26, tzinfo=UTC),
        day,
        runner.target_birdweather_ids,
        123,
    )
    assert "SELECT detection_id, detected_at" in str(captured["query"])
    assert "ORDER BY detected_at, detection_id" in str(captured["query"])


def test_delete_orphan_birdweather_stations_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    calls: list[int] = []
    batches = iter((5_000, 25, 0))

    def fake_batch(self: object, batch_size: int) -> int:
        calls.append(batch_size)
        return next(batches)

    monkeypatch.setattr(
        window_module.HistoricalWindowRunner,
        "_delete_orphan_birdweather_stations_batch",
        fake_batch,
    )

    deleted_rows = runner._delete_orphan_birdweather_stations()

    assert deleted_rows == 5_025
    assert calls == [
        window_module._BIRDWEATHER_STATION_CLEANUP_BATCH_SIZE,
        window_module._BIRDWEATHER_STATION_CLEANUP_BATCH_SIZE,
        window_module._BIRDWEATHER_STATION_CLEANUP_BATCH_SIZE,
    ]


def test_cleanup_off_target_birdweather_only_checks_days_with_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = object.__new__(window_module.HistoricalWindowRunner)
    clean_day = date(2024, 1, 6)
    target_day = date(2024, 1, 7)
    checked_days: list[date] = []

    monkeypatch.setattr(window_module, "_iter_days", lambda start, end: iter((clean_day, target_day)))
    runner.config = SimpleNamespace(start_date=clean_day, end_date=target_day)
    runner._has_off_target_birdweather_day = lambda day: day == target_day
    runner._existing_birdweather_refs = lambda day: checked_days.append(day) or {
        BUCKET_RAW_AUDIO: set(),
        BUCKET_PROCESSED_AUDIO: set(),
        BUCKET_FEATURES: set(),
    }
    runner._delete_off_target_birdweather_day_fast = lambda day: 12

    result = runner._cleanup_off_target_birdweather()

    assert checked_days == [target_day]
    assert result == {
        "scanned_rows": 12,
        "deleted_rows": 12,
        "deleted_minio_objects": 0,
    }
