from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pytest

import avian_biosurveillance.ops.prune_birdweather_raw_audio as prune_module


def _seed(day: date, filename: str) -> dict[str, object]:
    return {
        "observation_date": day,
        "resolved_audio_filename": filename,
    }


def _row(
    day: date,
    detection_id: str,
    station_id: str,
    audio_filename: str,
    *,
    processed: bool,
    size: int,
    species_id: str = "35",
) -> dict[str, object]:
    return {
        "observation_date": day,
        "detection_id": detection_id,
        "species_id": species_id,
        "station_id": station_id,
        "audio_url": f"https://cdn.example/{audio_filename}",
        "audio_filename": audio_filename,
        "audio_filesize_bytes": size,
        "has_processed_clip": processed,
    }


def test_parse_args_requires_scope_limiter_for_apply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        prune_module._parse_args(["--apply"])

    assert excinfo.value.code == 2
    assert "--apply requires a scope limiter" in capsys.readouterr().err


def test_load_candidates_groups_only_fully_processed_raw_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_batches = [
        [
            _seed(date(2024, 1, 1), "shared.mp3"),
            _seed(date(2024, 1, 3), "mixed.mp3"),
        ],
        [],
    ]

    monkeypatch.setattr(
        prune_module,
        "_load_candidate_seed_rows",
        lambda pool, *, start_date, end_date, cursor, limit: seed_batches.pop(0),
    )
    monkeypatch.setattr(
        prune_module,
        "_load_candidate_rows_for_seed",
        lambda pool, *, observation_date, resolved_audio_filename: {
            (date(2024, 1, 1), "shared.mp3"): [
                _row(
                    date(2024, 1, 1),
                    "det-1",
                    "station-1",
                    "shared.mp3",
                    processed=True,
                    size=100,
                )
            ],
            (date(2024, 1, 3), "mixed.mp3"): [
                _row(
                    date(2024, 1, 3),
                    "det-3",
                    "station-3",
                    "mixed.mp3",
                    processed=True,
                    size=80,
                ),
                _row(
                    date(2024, 1, 3),
                    "det-4",
                    "station-4",
                    "mixed.mp3",
                    processed=False,
                    size=80,
                ),
            ],
        }[(observation_date, resolved_audio_filename)],
    )

    args = argparse.Namespace(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        before_date=None,
        limit=None,
        preview=5,
        chunk_days=30,
        apply=False,
    )

    candidates = prune_module._load_candidates(object(), args)

    assert candidates == [
        prune_module.PruneCandidate(
            raw_object_key="2024-01-01/audio/shared.mp3",
            row_count=1,
            processed_clip_keys=(
                "birdweather/2024-01-01/station-1/det-1.flac",
            ),
            first_observation_date=date(2024, 1, 1),
            last_observation_date=date(2024, 1, 1),
            raw_size_bytes=100,
        ),
    ]


def test_load_candidates_respects_limit_while_paging_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_batches = [
        [
            _seed(date(2024, 1, 1), "a.mp3"),
            _seed(date(2024, 1, 2), "b.mp3"),
        ],
        [
            _seed(date(2024, 1, 3), "c.mp3"),
        ],
        [],
    ]
    loaded_keys: list[tuple[date, str]] = []

    monkeypatch.setattr(
        prune_module,
        "_load_candidate_seed_rows",
        lambda pool, *, start_date, end_date, cursor, limit: seed_batches.pop(0),
    )
    monkeypatch.setattr(
        prune_module,
        "_load_candidate_rows_for_seed",
        lambda pool, *, observation_date, resolved_audio_filename: loaded_keys.append(
            (observation_date, resolved_audio_filename)
        )
        or {
            (date(2024, 1, 1), "a.mp3"): [
                _row(
                    date(2024, 1, 1),
                    "det-1",
                    "station-1",
                    "a.mp3",
                    processed=False,
                    size=10,
                )
            ],
            (date(2024, 1, 2), "b.mp3"): [
                _row(
                    date(2024, 1, 2),
                    "det-2",
                    "station-2",
                    "b.mp3",
                    processed=True,
                    size=20,
                )
            ],
            (date(2024, 1, 3), "c.mp3"): [
                _row(
                    date(2024, 1, 3),
                    "det-3",
                    "station-3",
                    "c.mp3",
                    processed=True,
                    size=30,
                )
            ],
        }[(observation_date, resolved_audio_filename)],
    )

    args = argparse.Namespace(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        before_date=None,
        limit=2,
        preview=5,
        chunk_days=30,
        apply=False,
    )

    candidates = prune_module._load_candidates(object(), args)

    assert [candidate.raw_object_key for candidate in candidates] == [
        "2024-01-02/audio/b.mp3",
        "2024-01-03/audio/c.mp3",
    ]
    assert loaded_keys == [
        (date(2024, 1, 1), "a.mp3"),
        (date(2024, 1, 2), "b.mp3"),
        (date(2024, 1, 3), "c.mp3"),
    ]


def test_prune_birdweather_local_raw_audio_files_deletes_verified_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = prune_module.PruneCandidate(
        raw_object_key="2024-01-02/audio/shared.mp3",
        row_count=1,
        processed_clip_keys=("birdweather/2024-01-02/station-1/det-1.flac",),
        first_observation_date=date(2024, 1, 2),
        last_observation_date=date(2024, 1, 2),
        raw_size_bytes=10,
    )
    raw_path = tmp_path / "2024-01-02" / "audio" / "shared.mp3"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"raw")

    monkeypatch.setattr(
        prune_module,
        "_load_local_prune_candidates",
        lambda *_args, **_kwargs: [candidate],
    )

    class _Store:
        def exists(self, object_name: str, *, bucket: str) -> bool:
            assert bucket == "processed-audio"
            return object_name == "birdweather/2024-01-02/station-1/det-1.flac"

    stats, verified, missing = prune_module.prune_birdweather_local_raw_audio_files(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        raw_root=str(tmp_path),
        pool=object(),
        store=_Store(),
        apply=True,
    )

    assert stats.verified_objects == 1
    assert stats.deleted_objects == 1
    assert verified == [candidate]
    assert missing == []
    assert not raw_path.exists()


def test_prune_birdweather_local_raw_audio_files_prefetches_processed_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = prune_module.PruneCandidate(
        raw_object_key="2024-01-02/audio/shared-a.mp3",
        row_count=1,
        processed_clip_keys=("birdweather/2024-01-02/station-1/det-1.flac",),
        first_observation_date=date(2024, 1, 2),
        last_observation_date=date(2024, 1, 2),
        raw_size_bytes=10,
    )
    second = prune_module.PruneCandidate(
        raw_object_key="2024-01-02/audio/shared-b.mp3",
        row_count=1,
        processed_clip_keys=("birdweather/2024-01-02/station-2/det-2.flac",),
        first_observation_date=date(2024, 1, 2),
        last_observation_date=date(2024, 1, 2),
        raw_size_bytes=10,
    )
    for raw_key in (first.raw_object_key, second.raw_object_key):
        raw_path = tmp_path / raw_key
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(b"raw")

    monkeypatch.setattr(
        prune_module,
        "_load_local_prune_candidates",
        lambda *_args, **_kwargs: [first, second],
    )

    class _Store:
        def __init__(self) -> None:
            self.list_calls: list[tuple[str, str, bool]] = []

        def list_objects(
            self,
            prefix: str = "",
            *,
            bucket: str,
            recursive: bool = True,
        ) -> list[str]:
            self.list_calls.append((prefix, bucket, recursive))
            assert bucket == "processed-audio"
            return [
                "birdweather/2024-01-02/station-1/det-1.flac",
                "birdweather/2024-01-02/station-2/det-2.flac",
            ]

        def exists(self, object_name: str, *, bucket: str) -> bool:
            raise AssertionError(
                f"unexpected per-object exists call for {bucket}/{object_name}"
            )

    store = _Store()
    stats, verified, missing = prune_module.prune_birdweather_local_raw_audio_files(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        raw_root=str(tmp_path),
        pool=object(),
        store=store,
        apply=False,
    )

    assert stats.verified_objects == 2
    assert [candidate.raw_object_key for candidate in verified] == [
        "2024-01-02/audio/shared-a.mp3",
        "2024-01-02/audio/shared-b.mp3",
    ]
    assert missing == []
    assert store.list_calls == [
        ("birdweather/2024-01-02/", "processed-audio", True),
    ]


def test_load_local_prune_candidates_retries_transient_db_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg2

    observation_date = date(2024, 1, 2)
    row = _row(
        observation_date,
        "det-1",
        "station-1",
        "shared.mp3",
        processed=True,
        size=10,
    )
    sleep_calls: list[float] = []

    monkeypatch.setattr(prune_module.time, "sleep", sleep_calls.append)

    class _Cursor:
        def execute(self, _query: str, _params: tuple[object, ...]) -> None:
            return None

        def fetchall(self) -> list[dict[str, object]]:
            return [row]

    class _CursorContext:
        def __init__(self, *, fail: bool) -> None:
            self._fail = fail

        def __enter__(self) -> _Cursor:
            if self._fail:
                raise psycopg2.OperationalError("server closed the connection unexpectedly")
            return _Cursor()

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _Pool:
        def __init__(self) -> None:
            self.calls = 0

        def cursor(self, **_kwargs: object) -> _CursorContext:
            self.calls += 1
            return _CursorContext(fail=self.calls == 1)

    pool = _Pool()

    candidates = prune_module._load_local_prune_candidates(
        pool,
        start_date=observation_date,
        end_date=observation_date,
        limit=None,
    )

    assert [candidate.raw_object_key for candidate in candidates] == [
        "2024-01-02/audio/shared.mp3",
    ]
    assert pool.calls == 2
    assert sleep_calls == [prune_module._DATABASE_RETRY_BACKOFF_SECONDS]