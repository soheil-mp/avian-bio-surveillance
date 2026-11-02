"""Tests for BirdWeather raw-audio queue helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import avian_biosurveillance.processing.birdweather_raw_audio as raw_audio_module
from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError
from avian_biosurveillance.processing.birdweather_raw_audio import (
    RawAudioCandidate,
    download_birdweather_raw_audio_objects,
    raw_audio_object_key,
)


class _RecordingCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: list[Any] = []

    def execute(self, sql: str, params: list[Any]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _RecordingPool:
    def __init__(self) -> None:
        self.cursor_instance = _RecordingCursor()

    def cursor(self, cursor_factory: Any = None) -> _RecordingCursor:
        _ = cursor_factory
        return self.cursor_instance


def _candidate(**overrides: object) -> RawAudioCandidate:
    values = {
        "detection_id": "det-1",
        "detected_at": datetime(2026, 5, 6, 12, 0, 0),
        "observation_date": date(2026, 5, 6),
        "station_id": "station-1",
        "species_id": "sp-1",
        "audio_url": "https://media.birdweather.com/soundscapes/hash",
        "audio_filename": None,
        "audio_filesize_bytes": None,
    }
    values.update(overrides)
    return RawAudioCandidate(**values)  # type: ignore[arg-type]


def test_raw_audio_object_key_defaults_extensionless_urls_to_flac() -> None:
    """BirdWeather CDN URLs often omit extensions but serve FLAC content."""
    key = raw_audio_object_key(_candidate())

    assert key == "2026-05-06/audio/det-1_sp-1.flac"


def test_raw_audio_object_key_preserves_download_filename_extension() -> None:
    """Existing BirdWeather download filenames remain canonical when present."""
    key = raw_audio_object_key(_candidate(audio_filename="original.wav"))

    assert key == "2026-05-06/audio/original.wav"


def test_load_raw_audio_candidates_uses_detected_at_bounds() -> None:
    pool = _RecordingPool()

    raw_audio_module._load_raw_audio_candidates(
        pool,
        lookback_days=None,
        limit=10,
        start_date=date(2026, 5, 6),
        end_date=date(2026, 5, 7),
    )

    assert "detected_at >= %s" in pool.cursor_instance.sql
    assert "detected_at < %s" in pool.cursor_instance.sql
    assert pool.cursor_instance.params == [
        datetime(2026, 5, 6, tzinfo=UTC),
        datetime(2026, 5, 8, tzinfo=UTC),
        10,
    ]


def test_load_raw_audio_candidates_can_skip_processed_rows() -> None:
    pool = _RecordingPool()

    raw_audio_module._load_raw_audio_candidates(
        pool,
        lookback_days=None,
        limit=10,
        start_date=date(2026, 5, 6),
        end_date=date(2026, 5, 7),
        skip_processed=True,
    )

    assert "NOT has_processed_clip" in pool.cursor_instance.sql


def test_download_birdweather_raw_audio_objects_counts_storage_unavailable_as_upload_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _candidate()

    monkeypatch.setattr(raw_audio_module.time, "sleep", lambda _seconds: None)

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 0
    )

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
        ) -> tuple[bool, int]:
            audio_path.write_bytes(b"raw-audio")
            return True, 200

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            raise AudioStoreUnavailableError("MinIO unavailable")

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=1,
    )

    assert stats.upload_failed == 1
    assert stats.uploaded == 0
    assert stats.db_updated == 0


def test_download_birdweather_raw_audio_objects_retries_transient_upload_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _candidate()
    sleep_calls: list[float] = []

    monkeypatch.setattr(raw_audio_module.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 1
    )

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
        ) -> tuple[bool, int]:
            audio_path.write_bytes(b"raw-audio")
            return True, 200

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def __init__(self) -> None:
            self.calls = 0

        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            self.calls += 1
            if self.calls < 3:
                raise AudioStoreUnavailableError("MinIO unavailable")
            return "ok"

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=1,
    )

    assert store.calls == 3
    assert sleep_calls == [1.0, 2.0]
    assert stats.upload_failed == 0
    assert stats.uploaded == 1
    assert stats.db_updated == 1


def test_download_birdweather_raw_audio_objects_can_persist_to_local_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    row = _candidate()

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 1
    )

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
        ) -> tuple[bool, int]:
            audio_path.write_bytes(b"raw-audio")
            return True, 200

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        pool=pool,
        workers=1,
        limit=1,
        local_audio_root=tmp_path,
    )

    assert stats.uploaded == 1
    assert stats.db_updated == 1
    assert (
        tmp_path / "2026-05-06" / "audio" / "det-1_sp-1.flac"
    ).read_bytes() == b"raw-audio"


def test_download_birdweather_raw_audio_objects_rejects_empty_body_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Reproduces the CDN failure mode: download_file reports success but the
    # body is empty (0-byte file). It must not be persisted or counted as
    # uploaded; instead it is retried as a transient failure and ultimately
    # recorded as failed.
    row = _candidate()

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 0
    )

    class _CollectorClient:
        def __init__(self) -> None:
            self.calls = 0

        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            self.calls += 1
            audio_path.write_bytes(b"")  # empty/dropped CDN body
            return True, "downloaded"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        pool=pool,
        workers=1,
        limit=1,
        local_audio_root=tmp_path,
    )

    assert stats.uploaded == 0
    assert stats.failed == 1
    assert stats.retry_attempted >= 1
    assert not (tmp_path / "2026-05-06" / "audio" / "det-1_sp-1.flac").exists()


def test_download_birdweather_raw_audio_objects_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _candidate()

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 1
    )

    class _CollectorClient:
        def __init__(self) -> None:
            self.calls = 0

        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            self.calls += 1
            if self.calls == 1:
                return False, "timeout"
            audio_path.write_bytes(b"raw-audio")
            return True, "downloaded"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            return "ok"

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=1,
    )

    assert stats.failed == 0
    assert stats.retry_attempted == 1
    assert stats.retry_recovered == 1
    assert stats.uploaded == 1
    assert stats.db_updated == 1


def test_download_birdweather_raw_audio_objects_counts_final_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _candidate()

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 0
    )

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            _audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            return False, "forbidden"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("hard download failures must not upload")

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=1,
    )

    assert stats.failed == 1
    assert stats.forbidden == 1
    assert stats.retry_attempted == 0
    assert stats.uploaded == 0


def test_download_birdweather_raw_audio_objects_audits_archived_without_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _candidate()

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 0
    )

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            _audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            return False, "archived"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("archived audio must not upload")

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=1,
    )

    assert stats.archived == 1
    assert stats.failed == 0
    assert stats.uploaded == 0


def test_download_birdweather_raw_audio_objects_lists_once_per_date_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _candidate(detection_id="det-existing", audio_filename="existing.flac")
    pending = _candidate(detection_id="det-pending", audio_filename="pending.flac")

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [existing, pending],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 1
    )

    class _CollectorClient:
        def __init__(self) -> None:
            self.calls = 0

        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            self.calls += 1
            audio_path.write_bytes(b"raw-audio")
            return True, "downloaded"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def __init__(self) -> None:
            self.prefixes: list[str] = []

        def list_objects(self, prefix: str = "", **_kwargs: object) -> list[str]:
            self.prefixes.append(prefix)
            return [raw_audio_object_key(existing)]

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            return "ok"

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=2,
    )

    assert stats.already_present == 1
    assert stats.attempted == 1
    assert stats.uploaded == 1
    assert store.prefixes == ["2026-05-06/audio/"]


def test_download_birdweather_raw_audio_objects_uploads_before_batch_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast = _candidate(detection_id="det-fast", audio_filename="fast.flac")
    slow = _candidate(detection_id="det-slow", audio_filename="slow.flac")

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [fast, slow],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 2
    )

    fast_upload_seen = Event()
    slow_download_wait_observed = {"value": False}

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            if audio_path.name == "fast.flac":
                audio_path.write_bytes(b"fast")
                return True, "downloaded"

            slow_download_wait_observed["value"] = fast_upload_seen.wait(timeout=1)
            audio_path.write_bytes(b"slow")
            return True, "downloaded"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def __init__(self) -> None:
            self.uploads: list[str] = []

        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, object_key: str, *_args: object, **_kwargs: object) -> str:
            self.uploads.append(object_key)
            if object_key.endswith("fast.flac"):
                fast_upload_seen.set()
            return "ok"

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=2,
        limit=2,
    )

    assert slow_download_wait_observed["value"] is True
    assert stats.uploaded == 2
    assert store.uploads[0].endswith("fast.flac")


def test_download_birdweather_raw_audio_objects_marks_existing_raw_audio_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _candidate(detection_id="det-existing", audio_filename="existing.flac")

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [existing],
    )

    marked_rows: list[list[str]] = []

    def _mark_audio_filename(
        _pool: object,
        rows: list[tuple[str, datetime, date, str]],
    ) -> int:
        marked_rows.append(
            [
                detection_id
                for detection_id, _detected_at, _observation_date, _key in rows
            ]
        )
        return len(rows)

    monkeypatch.setattr(raw_audio_module, "_mark_audio_filename", _mark_audio_filename)

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            _audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            raise AssertionError("existing raw-audio rows must not be re-downloaded")

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return [raw_audio_object_key(existing)]

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("existing raw-audio rows must not be re-uploaded")

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=1,
        limit=1,
    )

    assert stats.already_present == 1
    assert stats.attempted == 0
    assert stats.db_updated == 1
    assert marked_rows == [["det-existing"]]


def test_download_birdweather_raw_audio_objects_dedupes_duplicate_object_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _candidate(detection_id="det-1", audio_filename="shared.flac")
    second = _candidate(detection_id="det-2", audio_filename="shared.flac")

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [first, second],
    )

    marked_rows: list[list[str]] = []

    def _mark_audio_filename(
        _pool: object,
        rows: list[tuple[str, datetime, date, str]],
    ) -> int:
        marked_rows.append(
            [
                detection_id
                for detection_id, _detected_at, _observation_date, _key in rows
            ]
        )
        return len(rows)

    monkeypatch.setattr(raw_audio_module, "_mark_audio_filename", _mark_audio_filename)

    download_calls: list[str] = []

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            download_calls.append(audio_path.as_posix())
            audio_path.write_bytes(b"raw-audio")
            return True, "downloaded"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            return "ok"

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=2,
        limit=2,
    )

    assert len(download_calls) == 1
    assert stats.attempted == 1
    assert stats.uploaded == 1
    assert stats.failed == 0
    assert stats.missing_file == 0
    assert stats.db_updated == 2
    assert marked_rows == [["det-1", "det-2"]]


def test_download_birdweather_raw_audio_objects_uses_date_scoped_temp_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _candidate(audio_filename="shared.flac")
    second = _candidate(
        detection_id="det-2",
        observation_date=date(2026, 5, 7),
        audio_filename="shared.flac",
    )

    monkeypatch.setattr(
        raw_audio_module,
        "_load_raw_audio_candidates",
        lambda *_args, **_kwargs: [first, second],
    )
    monkeypatch.setattr(
        raw_audio_module, "_mark_audio_filename", lambda *_args, **_kwargs: 2
    )

    download_paths: list[str] = []

    class _CollectorClient:
        def download_file(
            self,
            _url: str,
            audio_path: Path,
            _expected_size: object,
            _max_retries: int = 3,
        ) -> tuple[bool, str]:
            download_paths.append(audio_path.as_posix())
            audio_path.write_bytes(b"raw-audio")
            return True, "downloaded"

    class _Collector:
        def __init__(self) -> None:
            self.client = _CollectorClient()

    class _Store:
        def list_objects(self, *_args: object, **_kwargs: object) -> list[str]:
            return []

        def upload_audio(self, *_args: object, **_kwargs: object) -> str:
            return "ok"

    monkeypatch.setattr(raw_audio_module, "AudioCollector", _Collector)
    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = download_birdweather_raw_audio_objects(
        store=store,
        pool=pool,
        workers=2,
        limit=2,
    )

    assert stats.attempted == 2
    assert stats.uploaded == 2
    assert len(download_paths) == 2
    assert len(set(download_paths)) == 2
    assert any(path.endswith("2026-05-06/audio/shared.flac") for path in download_paths)
    assert any(path.endswith("2026-05-07/audio/shared.flac") for path in download_paths)


def test_mark_audio_rows_in_batches_splits_cross_day_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _mark_audio_filename(
        _pool: object,
        rows: list[tuple[str, datetime, date, str]],
    ) -> int:
        calls.append(
            [
                detection_id
                for detection_id, _detected_at, _observation_date, _object_key in rows
            ]
        )
        return len(rows)

    monkeypatch.setattr(raw_audio_module, "_mark_audio_filename", _mark_audio_filename)

    updated = raw_audio_module._mark_audio_rows_in_batches(
        object(),
        [
            (
                "det-1",
                datetime(2026, 5, 6, 10, 0, 0),
                date(2026, 5, 6),
                "2026-05-06/audio/a.flac",
            ),
            (
                "det-2",
                datetime(2026, 5, 7, 10, 0, 0),
                date(2026, 5, 7),
                "2026-05-07/audio/b.flac",
            ),
            (
                "det-3",
                datetime(2026, 5, 6, 11, 0, 0),
                date(2026, 5, 6),
                "2026-05-06/audio/c.flac",
            ),
        ],
    )

    assert updated == 3
    assert calls == [["det-1", "det-3"], ["det-2"]]


def test_mark_audio_filename_targets_chunk_directly() -> None:
    calls: list[tuple[str, tuple[object, ...] | None]] = []

    class _Cursor:
        rowcount = 1

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            calls.append((sql, params))

        def fetchone(self) -> tuple[str, str]:
            return ("_timescaledb_internal._hyper_1_2_chunk", "(0,1)")

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    cursor = _Cursor()

    class _Pool:
        def cursor(self) -> _Cursor:
            return cursor

    updated = raw_audio_module._mark_audio_filename(
        _Pool(),
        [
            (
                "det-1",
                datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC),
                date(2026, 5, 6),
                "2026-05-06/audio/file.flac",
            )
        ],
    )

    assert updated == 1
    assert len(calls) == 2
    select_sql, select_params = calls[0]
    assert "SELECT tableoid::regclass::text AS chunk_name" in select_sql
    assert "detected_at >= %s" in select_sql
    assert "detected_at < %s" in select_sql
    assert "observation_date = %s" in select_sql
    assert select_params == (
        datetime(2026, 5, 6, tzinfo=UTC),
        datetime(2026, 5, 7, tzinfo=UTC),
        date(2026, 5, 6),
        "det-1",
        datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC),
    )

    update_sql, update_params = calls[1]
    assert 'UPDATE "_timescaledb_internal"."_hyper_1_2_chunk" AS target' in update_sql
    assert "WHERE ctid = %s::tid" in update_sql
    assert "target.observation_date = %s" in update_sql
    assert update_params == (
        "file.flac",
        "(0,1)",
        date(2026, 5, 6),
        "det-1",
        datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC),
    )


def test_effective_raw_audio_download_workers_clamps_to_default_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BIRDWEATHER_RAW_AUDIO_MAX_WORKERS", raising=False)
    effective_workers = raw_audio_module.resolve_raw_audio_download_workers

    assert effective_workers(4) == 4
    assert effective_workers(20) == 4


def test_effective_raw_audio_download_workers_respects_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIRDWEATHER_RAW_AUDIO_MAX_WORKERS", "12")
    effective_workers = raw_audio_module.resolve_raw_audio_download_workers

    assert effective_workers(20) == 12
