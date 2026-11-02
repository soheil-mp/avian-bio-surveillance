"""Tests for BirdWeather audio processing helpers."""

from __future__ import annotations

import io
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
import soundfile as sf

import avian_biosurveillance.processing.birdweather_audio as audio_module
from avian_biosurveillance.db.minio_client import AudioStoreUnavailableError
from avian_biosurveillance.processing.birdweather_audio import (
    BirdWeatherAudioCandidate,
    UndecodableAudioError,
    crop_audio_bytes,
    encode_flac_bytes,
    process_birdweather_audio_clips,
    processed_clip_key,
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


def _stereo_wav_bytes(sample_rate: int = 8_000) -> bytes:
    time_axis = np.linspace(0, 1.0, sample_rate, endpoint=False, dtype=np.float32)
    left = np.sin(2 * np.pi * 440 * time_axis)
    right = np.sin(2 * np.pi * 880 * time_axis)
    stereo = np.column_stack([left, right]).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, stereo, sample_rate, format="WAV")
    return buffer.getvalue()


def _mono_wav_bytes(sample_rate: int = 8_000) -> bytes:
    time_axis = np.linspace(0, 1.0, sample_rate, endpoint=False, dtype=np.float32)
    mono = np.sin(2 * np.pi * 440 * time_axis).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, mono, sample_rate, format="WAV")
    return buffer.getvalue()


def test_crop_audio_bytes_converts_stereo_to_mono() -> None:
    audio, sample_rate = crop_audio_bytes(
        _stereo_wav_bytes(),
        start_s=0.25,
        end_s=0.75,
    )

    assert sample_rate == 8_000
    assert audio.ndim == 1
    assert len(audio) == 4_000


def test_crop_audio_bytes_handles_mono_input() -> None:
    audio, sample_rate = crop_audio_bytes(
        _mono_wav_bytes(),
        start_s=0.1,
        end_s=0.2,
    )

    assert sample_rate == 8_000
    assert audio.ndim == 1
    assert len(audio) == 800


def test_crop_audio_bytes_falls_back_to_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = sf.read

    def _maybe_fail_soundfile(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        if isinstance(args[0], io.BytesIO) and args[0].getvalue() == b"not-audio":
            raise RuntimeError("format not recognised")
        return cast(tuple[Any, Any], original_read(*args, **kwargs))

    def _fake_run(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["input"] == b"not-audio"
        command = args[0]
        assert isinstance(command, list)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=_mono_wav_bytes(),
            stderr=b"",
        )

    monkeypatch.setattr(sf, "read", _maybe_fail_soundfile)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    audio, sample_rate = crop_audio_bytes(
        b"not-audio",
        start_s=0.0,
        end_s=0.1,
    )

    assert sample_rate == 8_000
    assert audio.ndim == 1
    assert len(audio) == 800


def test_crop_audio_bytes_rejects_empty_payload() -> None:
    # An empty/dropped CDN download (0-byte file) must raise the typed,
    # expected error rather than a bare decoder exception.
    with pytest.raises(UndecodableAudioError):
        crop_audio_bytes(b"", start_s=0.0, end_s=0.1)


def test_crop_audio_bytes_raises_undecodable_when_all_decoders_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _always_fail_soundfile(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        raise RuntimeError("format not recognised")

    def _failing_ffmpeg(
        *args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        command = args[0]
        assert isinstance(command, list)
        return subprocess.CompletedProcess(
            args=command,
            returncode=183,
            stdout=b"",
            stderr=b"Invalid data found when processing input",
        )

    monkeypatch.setattr(sf, "read", _always_fail_soundfile)
    monkeypatch.setattr(subprocess, "run", _failing_ffmpeg)

    # Non-empty but corrupt bytes: both decoders reject them, so the failure is
    # surfaced as a single typed UndecodableAudioError (no double traceback).
    with pytest.raises(UndecodableAudioError):
        crop_audio_bytes(b"corrupt-but-not-empty", start_s=0.0, end_s=0.1)


def test_encode_flac_bytes_round_trips() -> None:
    audio, sample_rate = crop_audio_bytes(
        _stereo_wav_bytes(),
        start_s=0.0,
        end_s=0.1,
    )
    encoded = encode_flac_bytes(audio, sample_rate)
    decoded, decoded_rate = sf.read(io.BytesIO(encoded), dtype="float32")

    assert decoded_rate == sample_rate
    assert len(decoded) == len(audio)


def test_processed_clip_key_is_stable() -> None:
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

    assert processed_clip_key(row) == "birdweather/2026-05-05/station-7/det-1.flac"


def test_load_candidates_uses_detected_at_bounds() -> None:
    pool = _RecordingPool()

    audio_module._load_candidates(
        pool,
        lookback_days=None,
        limit=10,
        force=False,
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


def test_process_birdweather_audio_clips_raises_when_storage_is_unavailable(
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

    monkeypatch.setattr(audio_module, "_load_candidates", lambda *args, **kwargs: [row])
    monkeypatch.setattr(audio_module, "_mark_processed", lambda *args, **kwargs: 0)

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            if bucket == audio_module.BUCKET_PROCESSED_AUDIO:
                return False
            return object_name == "2026-05-05/audio/det-1_sp-9.flac"

        def get_object(self, *_args: object, **_kwargs: object) -> bytes:
            raise AudioStoreUnavailableError("MinIO unavailable")

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    with pytest.raises(AudioStoreUnavailableError):
        process_birdweather_audio_clips(
            store=store,
            pool=pool,
            limit=1,
        )


def test_process_birdweather_audio_clips_uses_candidate_key_probes(
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
        audio_end_time_s=0.1,
    )
    uploaded: list[str] = []

    monkeypatch.setattr(audio_module, "_load_candidates", lambda *args, **kwargs: [row])
    monkeypatch.setattr(audio_module, "_mark_processed", lambda *args, **kwargs: 1)

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            if bucket == audio_module.BUCKET_PROCESSED_AUDIO:
                return False
            return object_name == "2026-05-05/audio/det-1_sp-9.flac"

        def get_object(self, *_args: object, **_kwargs: object) -> bytes:
            return _mono_wav_bytes()

        def upload_audio(
            self, object_name: str, *_args: object, **_kwargs: object
        ) -> str:
            uploaded.append(object_name)
            return object_name

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = process_birdweather_audio_clips(
        store=store,
        pool=pool,
        limit=1,
    )

    assert stats.processed == 1
    assert stats.db_updated == 1
    assert uploaded == ["birdweather/2026-05-05/station-7/det-1.flac"]


def test_process_birdweather_audio_clips_prefers_local_raw_audio_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    row = BirdWeatherAudioCandidate(
        detection_id="det-1",
        detected_at=datetime(2026, 5, 5, tzinfo=UTC),
        observation_date=date(2026, 5, 5),
        station_id="station-7",
        species_id="sp-9",
        audio_filename=None,
        audio_start_time_s=0.0,
        audio_end_time_s=0.1,
    )
    uploaded: list[str] = []
    local_path = tmp_path / "2026-05-05" / "audio" / "det-1_sp-9.flac"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(_mono_wav_bytes())

    monkeypatch.setattr(audio_module, "_load_candidates", lambda *args, **kwargs: [row])
    monkeypatch.setattr(audio_module, "_mark_processed", lambda *args, **kwargs: 1)

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            if bucket == audio_module.BUCKET_PROCESSED_AUDIO:
                return False
            raise AssertionError(
                f"raw MinIO lookup should not run when local audio exists: {object_name}"
            )

        def get_object(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("raw MinIO download should not run when local audio exists")

        def upload_audio(
            self, object_name: str, *_args: object, **_kwargs: object
        ) -> str:
            uploaded.append(object_name)
            return object_name

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = process_birdweather_audio_clips(
        store=store,
        pool=pool,
        limit=1,
        local_audio_root=tmp_path,
    )

    assert stats.processed == 1
    assert stats.db_updated == 1
    assert uploaded == ["birdweather/2026-05-05/station-7/det-1.flac"]


def test_process_birdweather_audio_clips_retries_transient_raw_key_probe(
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
        audio_end_time_s=0.1,
    )
    raw_probe_attempts = 0

    monkeypatch.setattr(audio_module, "_load_candidates", lambda *args, **kwargs: [row])
    monkeypatch.setattr(audio_module, "_mark_processed", lambda *args, **kwargs: 1)

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            nonlocal raw_probe_attempts
            if bucket == audio_module.BUCKET_PROCESSED_AUDIO:
                return False

            if object_name == "2026-05-05/audio/det-1_sp-9.flac":
                raw_probe_attempts += 1
                if raw_probe_attempts == 1:
                    raise AudioStoreUnavailableError("MinIO unavailable")
                return True

            return False

        def get_object(self, *_args: object, **_kwargs: object) -> bytes:
            return _mono_wav_bytes()

        def upload_audio(
            self, object_name: str, *_args: object, **_kwargs: object
        ) -> str:
            return object_name

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = process_birdweather_audio_clips(
        store=store,
        pool=pool,
        limit=1,
        storage_retry_attempts=1,
        storage_retry_backoff_seconds=0.0,
    )

    assert stats.processed == 1
    assert stats.storage_retries == 1


def test_process_birdweather_audio_clips_continues_when_storage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        BirdWeatherAudioCandidate(
            detection_id="det-1",
            detected_at=datetime(2026, 5, 5, tzinfo=UTC),
            observation_date=date(2026, 5, 5),
            station_id="station-7",
            species_id="sp-9",
            audio_filename=None,
            audio_start_time_s=0.0,
            audio_end_time_s=0.1,
        ),
        BirdWeatherAudioCandidate(
            detection_id="det-2",
            detected_at=datetime(2026, 5, 5, 0, 1, tzinfo=UTC),
            observation_date=date(2026, 5, 5),
            station_id="station-7",
            species_id="sp-9",
            audio_filename=None,
            audio_start_time_s=0.0,
            audio_end_time_s=0.1,
        ),
    ]

    monkeypatch.setattr(audio_module, "_load_candidates", lambda *args, **kwargs: rows)
    monkeypatch.setattr(audio_module, "_mark_processed", lambda *args, **kwargs: 1)

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            if bucket == audio_module.BUCKET_PROCESSED_AUDIO:
                return False
            if object_name.endswith("det-1_sp-9.flac"):
                raise AudioStoreUnavailableError("MinIO unavailable")
            return object_name.endswith("det-2_sp-9.flac")

        def get_object(self, *_args: object, **_kwargs: object) -> bytes:
            return _mono_wav_bytes()

        def upload_audio(
            self, object_name: str, *_args: object, **_kwargs: object
        ) -> str:
            return object_name

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = process_birdweather_audio_clips(
        store=store,
        pool=pool,
        limit=None,
        continue_on_storage_unavailable=True,
        storage_retry_attempts=0,
    )

    assert stats.storage_unavailable == 1
    assert stats.processed == 1
    assert stats.db_updated == 1


def test_process_birdweather_audio_clips_flushes_processed_marks_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        BirdWeatherAudioCandidate(
            detection_id=f"det-{index}",
            detected_at=datetime(2026, 5, 5, 0, index, tzinfo=UTC),
            observation_date=date(2026, 5, 5),
            station_id="station-7",
            species_id="sp-9",
            audio_filename=None,
            audio_start_time_s=0.0,
            audio_end_time_s=0.1,
        )
        for index in range(1, 4)
    ]
    mark_batches: list[list[tuple[str, datetime, date]]] = []

    monkeypatch.setattr(audio_module, "_PROCESSED_MARK_BATCH_SIZE", 2)
    monkeypatch.setattr(audio_module, "_load_candidates", lambda *args, **kwargs: rows)

    def _fake_mark_processed(
        _pool: object, batch: list[tuple[str, datetime, date]]
    ) -> int:
        mark_batches.append(list(batch))
        return len(batch)

    monkeypatch.setattr(audio_module, "_mark_processed", _fake_mark_processed)

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            if bucket == audio_module.BUCKET_PROCESSED_AUDIO:
                return False
            return object_name.endswith(".flac")

        def get_object(self, *_args: object, **_kwargs: object) -> bytes:
            return _mono_wav_bytes()

        def upload_audio(
            self, object_name: str, *_args: object, **_kwargs: object
        ) -> str:
            return object_name

    store: Any = _Store()
    pool: Any = SimpleNamespace(close=lambda: None)

    stats = process_birdweather_audio_clips(
        store=store,
        pool=pool,
        limit=None,
    )

    assert stats.processed == 3
    assert stats.db_updated == 3
    assert mark_batches == [
        [
            (rows[0].detection_id, rows[0].detected_at, rows[0].observation_date),
            (rows[1].detection_id, rows[1].detected_at, rows[1].observation_date),
        ],
        [(rows[2].detection_id, rows[2].detected_at, rows[2].observation_date)],
    ]


def test_mark_processed_targets_chunk_rows_directly() -> None:
    rows = [
        ("det-1", datetime(2026, 5, 5, 0, 1, tzinfo=UTC), date(2026, 5, 5)),
        ("det-2", datetime(2026, 5, 5, 0, 2, tzinfo=UTC), date(2026, 5, 5)),
        ("det-3", datetime(2026, 5, 6, 0, 3, tzinfo=UTC), date(2026, 5, 6)),
    ]
    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        rowcount = 0

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            calls.append((sql, params))
            if sql.lstrip().startswith("UPDATE "):
                self.rowcount = 1
            else:
                self.rowcount = 0

        def fetchone(self) -> tuple[str, str]:
            return ("_timescaledb_internal._hyper_1_2_chunk", "(0,1)")

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    class _Pool:
        def cursor(self) -> _Cursor:
            return _Cursor()

    updated = audio_module._mark_processed(cast(Any, _Pool()), rows)

    assert updated == 3
    assert len(calls) == 6

    first_select_sql, first_select_params = calls[0]
    assert "SELECT tableoid::regclass::text AS chunk_name" in first_select_sql
    assert first_select_params == (
        datetime(2026, 5, 5, tzinfo=UTC),
        datetime(2026, 5, 6, tzinfo=UTC),
        date(2026, 5, 5),
        "det-1",
        datetime(2026, 5, 5, 0, 1, tzinfo=UTC),
    )

    first_update_sql, first_update_params = calls[1]
    assert 'UPDATE "_timescaledb_internal"."_hyper_1_2_chunk" AS target' in first_update_sql
    assert "WHERE ctid = %s::tid" in first_update_sql
    assert first_update_params == (
        "(0,1)",
        date(2026, 5, 5),
        "det-1",
        datetime(2026, 5, 5, 0, 1, tzinfo=UTC),
    )

    third_select_sql, third_select_params = calls[4]
    assert "SELECT tableoid::regclass::text AS chunk_name" in third_select_sql
    assert third_select_params == (
        datetime(2026, 5, 6, tzinfo=UTC),
        datetime(2026, 5, 7, tzinfo=UTC),
        date(2026, 5, 6),
        "det-3",
        datetime(2026, 5, 6, 0, 3, tzinfo=UTC),
    )
