"""Tests for BirdWeather replay/check helper behavior."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast


class _ReplayModule(Protocol):
    class EmbeddingWindowSnapshot: ...

    def _behavior_failures(
        self,
        before: object,
        after: object,
        *,
        replay_executed: bool,
        expect_embeddings: bool,
    ) -> list[str]: ...

    def _resolve_snapshot_limit(self, args: object) -> int | None: ...


def _load_replay_module() -> _ReplayModule:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ops"
        / "replay_birdweather_pipeline.py"
    )
    spec = importlib.util.spec_from_file_location(
        "replay_birdweather_pipeline",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load replay_birdweather_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_ReplayModule, module)


replay = _load_replay_module()


def _snapshot(**overrides: int) -> object:
    values = {
        "rows_total": 5,
        "rows_with_audio_url": 5,
        "rows_with_audio_filename": 5,
        "rows_with_processed_clip": 5,
        "rows_with_feature_artifacts": 5,
        "rows_with_birdnet_embedding": 5,
        "rows_with_perch_v2_embedding": 5,
        "rows_fully_embedded": 5,
    }
    values.update(overrides)
    return replay.EmbeddingWindowSnapshot(**values)


def test_behavior_failures_allow_windows_without_audio() -> None:
    failures = replay._behavior_failures(
        _snapshot(rows_total=0, rows_with_audio_url=0),
        _snapshot(rows_total=0, rows_with_audio_url=0),
        replay_executed=False,
        expect_embeddings=True,
    )

    assert failures == []


def test_behavior_failures_flag_missing_embeddings_when_expected() -> None:
    failures = replay._behavior_failures(
        _snapshot(
            rows_fully_embedded=0,
            rows_with_birdnet_embedding=0,
            rows_with_perch_v2_embedding=0,
        ),
        _snapshot(
            rows_fully_embedded=0,
            rows_with_birdnet_embedding=0,
            rows_with_perch_v2_embedding=0,
        ),
        replay_executed=True,
        expect_embeddings=True,
    )

    assert any("BirdNET and Perch2 embeddings" in failure for failure in failures)


def test_behavior_failures_pass_when_embeddings_exist() -> None:
    failures = replay._behavior_failures(
        _snapshot(rows_fully_embedded=2),
        _snapshot(rows_fully_embedded=5),
        replay_executed=True,
        expect_embeddings=True,
    )

    assert failures == []


def test_resolve_snapshot_limit_defaults_to_replay_slice_size() -> None:
    args = SimpleNamespace(
        snapshot_limit=None,
        check_only=False,
        raw_limit=25,
        processing_limit=40,
        feature_limit=10,
    )

    assert replay._resolve_snapshot_limit(args) == 40


def test_resolve_snapshot_limit_uses_fast_default_for_check_only() -> None:
    args = SimpleNamespace(
        snapshot_limit=None,
        check_only=True,
        raw_limit=25,
        processing_limit=40,
        feature_limit=10,
    )

    assert replay._resolve_snapshot_limit(args) == 500
