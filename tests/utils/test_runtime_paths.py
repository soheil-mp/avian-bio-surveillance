"""Tests for avian_biosurveillance.utils.runtime_paths."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from avian_biosurveillance.utils.runtime_paths import preferred_temp_dir


class TestPreferredTempDir:
    def test_returns_tmpdir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        result = preferred_temp_dir()
        assert result == str(tmp_path)

    def test_tmpdir_takes_priority_over_temp(self, tmp_path, monkeypatch):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("TEMP", str(other))
        result = preferred_temp_dir()
        assert result == str(tmp_path)

    def test_falls_back_to_temp(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.delenv("TMP", raising=False)
        result = preferred_temp_dir()
        assert result == str(tmp_path)

    def test_falls_back_to_tmp(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.setenv("TMP", str(tmp_path))
        result = preferred_temp_dir()
        assert result == str(tmp_path)

    def test_returns_none_when_no_env_and_not_container(self, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        # Patch os.name to 'nt' so the /app check is skipped
        monkeypatch.setattr(os, "name", "nt")
        result = preferred_temp_dir()
        assert result is None

    def test_creates_directory_if_missing(self, tmp_path, monkeypatch):
        new_dir = tmp_path / "nested" / "temp"
        monkeypatch.setenv("TMPDIR", str(new_dir))
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        result = preferred_temp_dir()
        assert result is not None
        assert new_dir.exists()

    def test_uses_app_data_tmp_on_posix(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TEMP", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.setattr(os, "name", "posix")
        fake_app = tmp_path / "app"
        fake_app.mkdir()
        # Patch Path("/app") to return our fake directory
        original_path = Path

        def fake_path_constructor(p):
            if str(p) == "/app":
                return fake_app
            return original_path(p)

        with patch(
            "avian_biosurveillance.utils.runtime_paths.Path",
            side_effect=fake_path_constructor,
        ):
            result = preferred_temp_dir()
        assert result is not None
        expected = fake_app / "data" / "tmp"
        assert result == str(expected)
