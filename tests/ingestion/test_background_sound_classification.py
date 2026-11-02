"""Tests for ingestion.background_sound_classification — AST tag enrichment."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from avian_biosurveillance.ingestion.background_sound_classification import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    AmbientTag,
    BackgroundSoundClassifier,
    _discover_date_dirs,
    _load_detections,
    _load_existing_tags,
    _resolve_audio_files,
    _resolve_background_weights_dir,
    _resolve_torch_hub_dir,
    enrich_detections,
    main,
)


SR = 22050


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _make_mock_pipe_results(
    labels: list[tuple[str, float]],
) -> list[dict[str, Any]]:
    """Build HF pipeline output: list of {label, score} dicts."""
    return [{"label": lbl, "score": sc} for lbl, sc in labels]


def _mock_pipeline_factory(
    return_value: list[dict[str, Any]],
) -> MagicMock:
    """Create a mock HuggingFace pipeline callable."""
    pipe = MagicMock()

    def side_effect(inputs, **kwargs):
        if isinstance(inputs, list):
            return [return_value for _ in inputs]
        return return_value

    pipe.side_effect = side_effect
    return pipe


def _make_clf_with_mock(
    results: list[dict[str, Any]],
    top_k: int | None = None,
    min_confidence: float = 0.01,
) -> BackgroundSoundClassifier:
    """Create a classifier with a mocked HF pipeline."""
    clf = BackgroundSoundClassifier(
        device="cpu", top_k=top_k, min_confidence=min_confidence
    )
    clf._pipe = _mock_pipeline_factory(results)
    return clf


def _setup_date_dir(
    tmp_path: Path,
    date: str = "2025-01-01",
    n_detections: int = 3,
    create_audio: bool = True,
) -> Path:
    """Set up a mock date directory with detections.jsonl and audio/.

    Each detection has a ``soundscape.url`` so the classifier knows
    audio *should* exist (``audio_not_downloaded`` vs ``no_source_audio``).
    """
    date_dir = tmp_path / date
    date_dir.mkdir(parents=True, exist_ok=True)

    if create_audio:
        audio_dir = date_dir / "audio"
        audio_dir.mkdir(exist_ok=True)

    detections: list[dict] = []
    for i in range(n_detections):
        det_id = str(1000 + i)
        det = {
            "id": det_id,
            "timestamp": f"2025-01-01T12:00:0{i}+01:00",
            "confidence": 0.8,
            "species": {"commonName": "Dunnock", "id": "44"},
            "speciesId": "44",
            "soundscape": {
                "url": f"https://example.com/audio/{det_id}.mp3",
                "downloadFilename": f"station-20250101-12000{i}.mp3",
            },
            "station": {"id": "100", "name": "Test Station"},
        }
        detections.append(det)

        if create_audio:
            audio_file = audio_dir / f"{det_id}_44.wav"
            audio = 0.1 * np.random.default_rng(i).standard_normal(SR).astype(
                np.float32
            )
            sf.write(str(audio_file), audio, SR)

    with open(date_dir / "detections.jsonl", "w", encoding="utf-8") as f:
        for det in detections:
            f.write(json.dumps(det) + "\n")

    return date_dir


# ──────────────────────────────────────────────────────────────────────────
# AmbientTag
# ──────────────────────────────────────────────────────────────────────────


class TestAmbientTag:
    def test_creation(self):
        tag = AmbientTag(label="Rain", score=0.82)
        assert tag.label == "Rain"
        assert tag.score == 0.82

    def test_frozen(self):
        tag = AmbientTag(label="Rain", score=0.82)
        with pytest.raises(AttributeError):
            tag.label = "Wind"  # type: ignore[misc]

    def test_asdict(self):
        tag = AmbientTag(label="Wind", score=0.5)
        d = asdict(tag)
        assert d == {"label": "Wind", "score": 0.5}


# ──────────────────────────────────────────────────────────────────────────
# Status constants
# ──────────────────────────────────────────────────────────────────────────


class TestStatusConstants:
    def test_terminal_statuses_is_frozenset(self):
        assert isinstance(TERMINAL_STATUSES, frozenset)

    def test_classified_is_terminal(self):
        assert "classified" in TERMINAL_STATUSES

    def test_no_source_audio_is_terminal(self):
        assert "no_source_audio" in TERMINAL_STATUSES

    def test_non_terminal_statuses_exist(self):
        assert "audio_not_downloaded" in NON_TERMINAL_STATUSES
        assert "decode_failed" in NON_TERMINAL_STATUSES
        assert "empty_audio" in NON_TERMINAL_STATUSES


# ──────────────────────────────────────────────────────────────────────────
# BackgroundSoundClassifier — unit tests (mocked HF pipeline)
# ──────────────────────────────────────────────────────────────────────────


class TestClassifierInit:
    def test_explicit_device(self):
        clf = BackgroundSoundClassifier(device="cpu")
        assert clf.device == "cpu"

    def test_default_top_k(self):
        clf = BackgroundSoundClassifier(device="cpu")
        assert clf.top_k is None

    def test_default_min_confidence(self):
        clf = BackgroundSoundClassifier(device="cpu")
        assert clf.min_confidence == 0.10

    def test_lazy_loading(self):
        clf = BackgroundSoundClassifier(device="cpu")
        assert clf._pipe is None


class TestClassifierFiltering:
    """Test _filter_results method in isolation.

    The current classifier keeps ALL 527 AudioSet labels — filtering
    is only by ``min_confidence`` and ``top_k``.
    """

    def test_keeps_all_labels_above_threshold(self):
        """All labels above min_confidence are kept (no label-set filtering)."""
        clf = _make_clf_with_mock([], min_confidence=0.01)
        raw = _make_mock_pipe_results(
            [
                ("Rain", 0.80),
                ("Tabla", 0.70),
                ("Wind", 0.60),
                ("Banjo", 0.50),
            ]
        )
        tags = clf._filter_results(raw)
        labels = {t.label for t in tags}
        assert labels == {"Rain", "Tabla", "Wind", "Banjo"}

    def test_respects_min_confidence(self):
        clf = BackgroundSoundClassifier(device="cpu", top_k=None, min_confidence=0.5)
        raw = _make_mock_pipe_results(
            [
                ("Rain", 0.80),
                ("Wind", 0.30),
                ("Speech", 0.10),
            ]
        )
        tags = clf._filter_results(raw)
        assert len(tags) == 1
        assert tags[0].label == "Rain"

    def test_respects_top_k(self):
        clf = BackgroundSoundClassifier(device="cpu", top_k=2, min_confidence=0.01)
        raw = _make_mock_pipe_results(
            [
                ("Rain", 0.80),
                ("Wind", 0.60),
                ("Speech", 0.50),
                ("Vehicle", 0.40),
            ]
        )
        tags = clf._filter_results(raw)
        assert len(tags) == 2
        assert tags[0].label == "Rain"
        assert tags[1].label == "Wind"

    def test_sorted_by_score_descending(self):
        clf = _make_clf_with_mock([], min_confidence=0.01)
        raw = _make_mock_pipe_results(
            [
                ("Wind", 0.40),
                ("Rain", 0.80),
                ("Speech", 0.60),
            ]
        )
        tags = clf._filter_results(raw)
        scores = [t.score for t in tags]
        assert scores == sorted(scores, reverse=True)

    def test_empty_input(self):
        clf = BackgroundSoundClassifier(device="cpu")
        tags = clf._filter_results([])
        assert tags == []

    def test_all_below_threshold(self):
        clf = BackgroundSoundClassifier(device="cpu", min_confidence=0.90)
        raw = _make_mock_pipe_results([("Banjo", 0.80), ("Tabla", 0.70)])
        tags = clf._filter_results(raw)
        assert tags == []

    def test_score_rounding(self):
        clf = _make_clf_with_mock([], min_confidence=0.01)
        raw = [{"label": "Rain", "score": 0.12345678}]
        tags = clf._filter_results(raw)
        assert tags[0].score == 0.1235


class TestClassifierClassify:
    """Test classify / classify_array / classify_batch with mocked model."""

    def test_classify_file(self, tmp_path: Path):
        wav_path = tmp_path / "test.wav"
        t = np.linspace(0, 1.0, SR, dtype=np.float32)
        sf.write(str(wav_path), 0.5 * np.sin(2 * np.pi * 3000 * t), SR)

        clf = _make_clf_with_mock(
            _make_mock_pipe_results(
                [
                    ("Rain", 0.85),
                    ("Wind", 0.60),
                    ("Banjo", 0.30),
                ]
            ),
            min_confidence=0.01,
        )
        tags = clf.classify(wav_path)
        assert len(tags) == 3
        assert tags[0].label == "Rain"

    def test_classify_array(self):
        audio = np.zeros(SR, dtype=np.float32)
        clf = _make_clf_with_mock(
            _make_mock_pipe_results(
                [
                    ("Speech", 0.70),
                    ("Vehicle", 0.40),
                ]
            ),
            min_confidence=0.01,
        )
        tags = clf.classify_array(audio, sample_rate=SR)
        assert len(tags) == 2
        assert tags[0].label == "Speech"

    def test_classify_batch(self, tmp_path: Path):
        wav_path = tmp_path / "test.wav"
        sf.write(str(wav_path), np.zeros(SR, dtype=np.float32), SR)

        clf = _make_clf_with_mock(
            _make_mock_pipe_results(
                [
                    ("Rain", 0.90),
                    ("Traffic noise, roadway noise", 0.50),
                ]
            ),
            min_confidence=0.01,
        )
        result = clf.classify_batch([wav_path, wav_path], batch_size=2)
        assert len(result) == 2
        for tags in result:
            assert tags[0].label == "Rain"

    def test_classify_batch_empty_list(self):
        clf = BackgroundSoundClassifier(device="cpu")
        result = clf.classify_batch([])
        assert result == []

    def test_classify_array_silence(self):
        clf = _make_clf_with_mock(
            _make_mock_pipe_results([("Silence", 0.90)]),
            min_confidence=0.01,
        )
        tags = clf.classify_array(np.zeros(SR, dtype=np.float32))
        assert len(tags) == 1
        assert tags[0].label == "Silence"


class TestClassifierLazyLoad:
    """Test lazy model loading behavior."""

    def test_second_call_does_not_reload(self):
        clf = BackgroundSoundClassifier(device="cpu")
        clf._pipe = MagicMock()
        original_pipe = clf._pipe
        clf._ensure_loaded()
        assert clf._pipe is original_pipe


class TestClassifierRuntimeDirectories:
    def test_torch_hub_dir_falls_back_when_torch_home_is_unwritable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        mounted_torch_home = tmp_path / "mounted_torch_home"
        mounted_torch_home.mkdir()
        mounted_torch_home.chmod(0o555)

        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("TORCH_HOME", str(mounted_torch_home))

        resolved = _resolve_torch_hub_dir()

        assert resolved == home_dir / ".cache" / "torch" / "hub"

    def test_background_weights_dir_falls_back_when_weights_mount_is_unwritable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        mounted_weights_dir = tmp_path / "mounted_weights"
        mounted_weights_dir.mkdir()
        mounted_weights_dir.chmod(0o555)

        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("AVIAN_WEIGHTS_DIR", str(mounted_weights_dir))

        resolved = _resolve_background_weights_dir()

        assert resolved == (home_dir / ".cache" / "avian_biosurveillance" / "weights")


# ──────────────────────────────────────────────────────────────────────────
# Audio file resolution
# ──────────────────────────────────────────────────────────────────────────


class TestResolveAudioFiles:
    def test_maps_detection_id_to_file(self, tmp_path: Path):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / "1000_44.mp3").write_bytes(b"\x00" * 100)
        (audio_dir / "1001_44.flac").write_bytes(b"\x00" * 100)

        detections = [{"id": "1000"}, {"id": "1001"}, {"id": "9999"}]
        result = _resolve_audio_files(detections, audio_dir)
        assert "1000" in result
        assert "1001" in result
        assert "9999" not in result

    def test_missing_audio_dir(self, tmp_path: Path):
        result = _resolve_audio_files(
            [{"id": "1"}],
            tmp_path / "nonexistent",
        )
        assert result == {}

    def test_ignores_non_audio_files(self, tmp_path: Path):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / "1000_44.txt").write_bytes(b"hello")
        (audio_dir / "readme.md").write_bytes(b"# test")

        result = _resolve_audio_files([{"id": "1000"}], audio_dir)
        assert result == {}


# ──────────────────────────────────────────────────────────────────────────
# JSONL loading helpers
# ──────────────────────────────────────────────────────────────────────────


class TestLoadDetections:
    def test_loads_valid_jsonl(self, tmp_path: Path):
        p = tmp_path / "dets.jsonl"
        p.write_text('{"id": "1"}\n{"id": "2"}\n', encoding="utf-8")
        dets = _load_detections(p)
        assert len(dets) == 2

    def test_skips_blank_lines(self, tmp_path: Path):
        p = tmp_path / "dets.jsonl"
        p.write_text('{"id": "1"}\n\n{"id": "2"}\n\n', encoding="utf-8")
        dets = _load_detections(p)
        assert len(dets) == 2


class TestLoadExistingTags:
    """Test _load_existing_tags which takes a list[dict] of detections."""

    def test_loads_terminally_tagged_detections(self):
        detections = [
            {
                "id": "1",
                "background_sound_classification_status": "classified",
                "background_sound_classification_tags": [
                    {"label": "Rain", "score": 0.8}
                ],
            }
        ]
        tags = _load_existing_tags(detections)
        assert "1" in tags
        assert tags["1"][0]["label"] == "Rain"

    def test_skips_non_terminal_statuses(self):
        detections = [
            {
                "id": "1",
                "background_sound_classification_status": "decode_failed",
                "background_sound_classification_tags": [],
            }
        ]
        tags = _load_existing_tags(detections)
        assert "1" not in tags

    def test_skips_untagged_detections(self):
        detections = [{"id": "1"}]
        tags = _load_existing_tags(detections)
        assert "1" not in tags

    def test_legacy_non_empty_tags_treated_as_classified(self):
        """Detections with non-empty tags but no status field are treated as classified."""
        detections = [
            {
                "id": "1",
                "background_sound_classification_tags": [
                    {"label": "Wind", "score": 0.7}
                ],
            }
        ]
        tags = _load_existing_tags(detections)
        assert "1" in tags

    def test_empty_list_returns_empty(self):
        tags = _load_existing_tags([])
        assert tags == {}


# ──────────────────────────────────────────────────────────────────────────
# discover_date_dirs
# ──────────────────────────────────────────────────────────────────────────


class TestDiscoverDateDirs:
    def test_single_date_dir(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path)
        dirs = _discover_date_dirs(date_dir)
        assert dirs == [date_dir]

    def test_parent_dir_discovers_children(self, tmp_path: Path):
        _setup_date_dir(tmp_path, "2025-01-01")
        _setup_date_dir(tmp_path, "2025-01-02")
        dirs = _discover_date_dirs(tmp_path)
        assert len(dirs) == 2
        assert dirs[0].name == "2025-01-01"
        assert dirs[1].name == "2025-01-02"

    def test_empty_parent_returns_empty(self, tmp_path: Path):
        dirs = _discover_date_dirs(tmp_path)
        assert dirs == []


# ──────────────────────────────────────────────────────────────────────────
# enrich_detections — scenarios
# ──────────────────────────────────────────────────────────────────────────


class TestEnrichDetections:
    """Test the core enrichment scenarios.

    The enrichment pipeline:
    1. Reads ``detections.jsonl``
    2. Classifies audio files
    3. Writes enriched tags back into ``detections.jsonl`` (atomic replace)
    """

    def _patch_and_enrich(self, date_dir: Path, mock_results, **kwargs):
        """Helper to run enrich_detections with a mocked classifier."""
        with patch.object(BackgroundSoundClassifier, "_ensure_loaded"):
            clf = BackgroundSoundClassifier(device="cpu")
            clf._pipe = _mock_pipeline_factory(mock_results)

            with patch(
                "avian_biosurveillance.ingestion.background_sound_classification.BackgroundSoundClassifier",
                return_value=clf,
            ):
                return enrich_detections(date_dir, **kwargs)

    # ── Scenario 1: Fresh data ────────────────────────────────────────

    def test_fresh_enrichment_tags_written(self, tmp_path: Path):
        """Fresh enrichment writes tags back into detections.jsonl."""
        date_dir = _setup_date_dir(tmp_path)
        results = _make_mock_pipe_results([("Rain", 0.80)])
        self._patch_and_enrich(date_dir, results)

        output = date_dir / "detections.jsonl"
        assert output.exists()

    def test_fresh_enrichment_all_detections_tagged(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=3)
        results = _make_mock_pipe_results([("Rain", 0.80)])
        stats = self._patch_and_enrich(date_dir, results)

        assert stats.classified == 3
        assert stats.skipped_existing == 0

        with open(date_dir / "detections.jsonl", encoding="utf-8") as f:
            for line in f:
                det = json.loads(line)
                assert "background_sound_classification_tags" in det
                assert det["background_sound_classification_status"] == "classified"

    # ── Scenario 2: Partially enriched ────────────────────────────────

    def test_partial_enrichment_only_classifies_new(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=3)

        # Pre-tag detection 1000 directly in detections.jsonl
        det_path = date_dir / "detections.jsonl"
        dets = []
        with open(det_path, encoding="utf-8") as f:
            for line in f:
                dets.append(json.loads(line))
        dets[0]["background_sound_classification_status"] = "classified"
        dets[0]["background_sound_classification_tags"] = [
            {"label": "Rain", "score": 0.9}
        ]
        with open(det_path, "w", encoding="utf-8") as f:
            for det in dets:
                f.write(json.dumps(det) + "\n")

        results = _make_mock_pipe_results([("Wind", 0.50)])
        stats = self._patch_and_enrich(date_dir, results)

        assert stats.skipped_existing == 1
        assert stats.classified == 2

    # ── Scenario 3: Fully enriched (skip) ─────────────────────────────

    def test_fully_enriched_skips(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=2)

        # Pre-tag ALL detections in detections.jsonl
        det_path = date_dir / "detections.jsonl"
        dets = []
        with open(det_path, encoding="utf-8") as f:
            for line in f:
                dets.append(json.loads(line))
        for det in dets:
            det["background_sound_classification_status"] = "classified"
            det["background_sound_classification_tags"] = [
                {"label": "Rain", "score": 0.9}
            ]
        with open(det_path, "w", encoding="utf-8") as f:
            for det in dets:
                f.write(json.dumps(det) + "\n")

        stats = enrich_detections(date_dir)  # No mock needed — should skip
        assert stats.classified == 0
        assert stats.skipped_existing == 2

    # ── Scenario 4: Force re-classification ───────────────────────────

    def test_force_reclassifies_everything(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=2)

        # Pre-tag ALL detections
        det_path = date_dir / "detections.jsonl"
        dets = []
        with open(det_path, encoding="utf-8") as f:
            for line in f:
                dets.append(json.loads(line))
        for det in dets:
            det["background_sound_classification_status"] = "classified"
            det["background_sound_classification_tags"] = [
                {"label": "Rain", "score": 0.9}
            ]
        with open(det_path, "w", encoding="utf-8") as f:
            for det in dets:
                f.write(json.dumps(det) + "\n")

        results = _make_mock_pipe_results([("Wind", 0.70)])
        stats = self._patch_and_enrich(date_dir, results, force=True)

        assert stats.classified == 2
        assert stats.skipped_existing == 0

    # ── Scenario 5: No audio directory ────────────────────────────────

    def test_no_audio_dir_marks_not_downloaded(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, create_audio=False)
        stats = enrich_detections(date_dir)
        assert stats.classified == 0
        assert stats.audio_matched == 0
        # Detections have soundscape.url so they get "audio_not_downloaded"
        assert stats.audio_not_downloaded == 3

    # ── Scenario 6: Detection has no matching audio ───────────────────

    def test_unmatched_detection_gets_status(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=3)

        # Delete one audio file so 1 detection has no match
        audio_files = list((date_dir / "audio").glob("1000_*"))
        for f in audio_files:
            f.unlink()

        results = _make_mock_pipe_results([("Rain", 0.80)])
        stats = self._patch_and_enrich(date_dir, results)

        # Detection 1000 has soundscape.url but no local file → audio_not_downloaded
        assert stats.audio_not_downloaded == 1
        assert stats.classified == 2

        # Check the unmatched detection has correct status
        with open(date_dir / "detections.jsonl", encoding="utf-8") as f:
            for line in f:
                det = json.loads(line)
                if det["id"] == "1000":
                    assert det["background_sound_classification_tags"] == []
                    assert (
                        det["background_sound_classification_status"]
                        == "audio_not_downloaded"
                    )

    # ── Scenario 7: Dry-run ───────────────────────────────────────────

    def test_dry_run_does_not_modify(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path)

        # Read original content
        with open(date_dir / "detections.jsonl", encoding="utf-8") as f:
            original = f.read()

        stats = enrich_detections(date_dir, dry_run=True)

        # File should be unchanged
        with open(date_dir / "detections.jsonl", encoding="utf-8") as f:
            after = f.read()
        assert original == after

    # ── Error handling ────────────────────────────────────────────────

    def test_missing_detections_jsonl_raises(self, tmp_path: Path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            enrich_detections(empty_dir)

    # ── Stats ─────────────────────────────────────────────────────────

    def test_stats_fields(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=2)
        results = _make_mock_pipe_results([("Rain", 0.80)])
        stats = self._patch_and_enrich(date_dir, results)

        assert stats.total_detections == 2
        assert stats.audio_matched == 2
        assert stats.classified == 2
        assert stats.elapsed_s > 0

    def test_avg_tags_calculation(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path, n_detections=2)
        results = _make_mock_pipe_results([("Rain", 0.80), ("Wind", 0.60)])
        stats = self._patch_and_enrich(date_dir, results)
        assert stats.avg_tags_per_detection == 2.0

    # ── Progress cache / resume ───────────────────────────────────────

    def test_progress_cache_cleaned_after_success(self, tmp_path: Path):
        """Progress cache file is deleted after successful enrichment."""
        date_dir = _setup_date_dir(tmp_path, n_detections=2)
        results = _make_mock_pipe_results([("Rain", 0.80)])
        self._patch_and_enrich(date_dir, results)
        assert not (date_dir / ".classification_progress.jsonl").exists()


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_main_with_missing_input_exits(self, tmp_path: Path):
        with pytest.raises(SystemExit) as exc_info:
            main(["--input", str(tmp_path / "nonexistent")])
        assert exc_info.value.code == 1

    def test_main_with_no_date_dirs_exits(self, tmp_path: Path):
        # Empty directory with no detections.jsonl
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SystemExit) as exc_info:
            main(["--input", str(empty)])
        assert exc_info.value.code == 1

    def test_main_dry_run_succeeds(self, tmp_path: Path):
        date_dir = _setup_date_dir(tmp_path)

        # Read original content
        with open(date_dir / "detections.jsonl", encoding="utf-8") as f:
            original = f.read()

        # dry-run should complete without needing the model
        main(["--input", str(date_dir), "--dry-run"])

        # File should be unchanged
        with open(date_dir / "detections.jsonl", encoding="utf-8") as f:
            after = f.read()
        assert original == after


# ──────────────────────────────────────────────────────────────────────────
# BirdWeather integration (_run_background_classification)
# ──────────────────────────────────────────────────────────────────────────


class TestBirdWeatherIntegration:
    """Test the hook in birdweather.py."""

    def test_run_background_classification_callable(self):
        from avian_biosurveillance.ingestion.birdweather import (
            _run_background_classification,
        )

        assert callable(_run_background_classification)

    def test_run_background_classification_handles_missing_dir(self, tmp_path: Path):
        from avian_biosurveillance.ingestion.birdweather import (
            _run_background_classification,
        )

        # Should not raise — graceful error handling
        _run_background_classification(tmp_path / "nonexistent")
