"""Tests for Sovon orchestration flows."""

from __future__ import annotations

from pathlib import Path

import pytest

from avian_biosurveillance.orchestration import sovon_flow


def test_download_sovon_charts_reuses_persistent_cache_for_database_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database-target runs should not stage Sovon charts in a temp directory."""
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        sovon_flow,
        "sovon_download_charts",
        lambda raw_dir=None: calls.append({"raw_dir": raw_dir}),
    )

    sovon_flow.download_sovon_charts(target="database")

    assert calls == [{"raw_dir": None}]


def test_extract_sovon_data_cleans_explicit_database_stage_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit temporary staging should still be cleaned up after extraction."""
    stage_root = tmp_path / "sovon-stage"
    recorded_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        sovon_flow,
        "sovon_extract_data",
        lambda **kwargs: recorded_calls.append(dict(kwargs)),
    )

    raw_dir = stage_root / "raw"
    raw_dir.mkdir(parents=True)

    sovon_flow.extract_sovon_data(target="database", stage_root=str(stage_root))

    assert recorded_calls == [
        {
            "target": "database",
            "raw_dir": str(raw_dir),
            "processed_dir": str(stage_root / "processed"),
            "cache_dir": str(stage_root / "cache"),
        }
    ]
    assert not stage_root.exists()
