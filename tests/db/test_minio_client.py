"""Tests for avian_biosurveillance.db.minio_client module.

Covers:
- AudioStore construction from env vars
- Bucket existence checks and creation
- Upload / download / exists / list operations
- Object key generation helpers
- Healthcheck
"""

# ruff: noqa: S105, S106

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import ANY, MagicMock, patch

import pytest
from urllib3.exceptions import MaxRetryError

from avian_biosurveillance.db.minio_client import (
    BUCKET_FEATURES,
    BUCKET_PROCESSED_AUDIO,
    BUCKET_RAW_AUDIO,
    BUCKET_SPECTROGRAMS,
    AudioStore,
    AudioStoreUnavailableError,
)


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    """Verify AudioStore reads env vars correctly."""

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_defaults(self, mock_minio_cls):
        with patch.dict(
            "os.environ",
            {
                "MINIO_ENDPOINT": "minio-host:9000",
                "MINIO_ACCESS_KEY": "testkey",
                "MINIO_SECRET_KEY": "testsecret",
            },
        ):
            AudioStore()
        mock_minio_cls.assert_called_once_with(
            "minio-host:9000",
            access_key="testkey",
            secret_key="testsecret",
            secure=False,
            http_client=ANY,
        )

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_explicit_args(self, mock_minio_cls):
        AudioStore(
            endpoint="custom:9999",
            access_key="ak",
            secret_key="sk",
            secure=True,
        )
        mock_minio_cls.assert_called_once_with(
            "custom:9999",
            access_key="ak",
            secret_key="sk",
            secure=True,
            http_client=ANY,
        )

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_fallback_to_root_user_env(self, mock_minio_cls):
        with patch.dict(
            "os.environ",
            {
                "MINIO_ROOT_USER": "rootuser",
                "MINIO_ROOT_PASSWORD": "rootpass",
            },
            clear=False,
        ):
            import os

            os.environ.pop("MINIO_ACCESS_KEY", None)
            os.environ.pop("MINIO_SECRET_KEY", None)
            AudioStore()
        _, kwargs = mock_minio_cls.call_args
        assert kwargs["access_key"] == "rootuser"
        assert kwargs["secret_key"] == "rootpass"

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_local_root_bypasses_minio_client(self, mock_minio_cls, tmp_path):
        store = AudioStore(local_root=tmp_path)

        assert store._local_root == tmp_path.resolve()
        mock_minio_cls.assert_not_called()


# =============================================================================
# Bucket operations
# =============================================================================


class TestBuckets:
    """Test ensure_buckets and bucket creation."""

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_ensure_buckets_creates_missing(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_client.bucket_exists.side_effect = lambda b: b == BUCKET_RAW_AUDIO
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        created = store.ensure_buckets()

        assert BUCKET_RAW_AUDIO not in created
        assert BUCKET_PROCESSED_AUDIO in created
        assert BUCKET_SPECTROGRAMS in created

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_ensure_buckets_all_exist(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        created = store.ensure_buckets()
        assert created == []


# =============================================================================
# Upload
# =============================================================================


class TestUpload:
    """Test upload operations."""

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_upload_bytes(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        path = store.upload_audio("2024-06-15/s1/d1.mp3", b"audiodata")

        assert path == "raw-audio/2024-06-15/s1/d1.mp3"
        mock_client.put_object.assert_called_once()
        args, kwargs = mock_client.put_object.call_args
        assert args[0] == BUCKET_RAW_AUDIO
        assert args[1] == "2024-06-15/s1/d1.mp3"
        assert kwargs["length"] == 9

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_upload_spectrogram(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        path = store.upload_spectrogram("spec.png", b"imgdata")

        assert path == f"{BUCKET_SPECTROGRAMS}/spectrograms/spec.png"
        args, kwargs = mock_client.put_object.call_args
        assert args[0] == BUCKET_SPECTROGRAMS
        assert args[1] == "spectrograms/spec.png"
        assert kwargs["content_type"] == "image/png"

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_upload_waveform(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        path = store.upload_waveform("wave.png", b"imgdata")

        assert path == f"{BUCKET_FEATURES}/waveforms/wave.png"
        args, kwargs = mock_client.put_object.call_args
        assert args[0] == BUCKET_FEATURES
        assert args[1] == "waveforms/wave.png"
        assert kwargs["content_type"] == "image/png"


# =============================================================================
# Download / exists / list
# =============================================================================


class TestReadOps:
    """Test download, exists, and list operations."""

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_get_object(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.read.return_value = b"audio-bytes"
        mock_client.get_object.return_value = mock_response
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        data = store.get_object("test.mp3")

        assert data == b"audio-bytes"
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_exists_true(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        assert store.exists("file.mp3") is True

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_exists_false(self, mock_minio_cls):
        from minio.error import S3Error

        mock_client = MagicMock()
        mock_client.stat_object.side_effect = S3Error(
            cast(Any, MagicMock()),
            "NoSuchKey",
            "msg",
            "resource",
            "req",
            "host",
            "raw-audio",
            "missing.mp3",
        )
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        assert store.exists("missing.mp3") is False

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_exists_raises_when_storage_is_unavailable(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_client.stat_object.side_effect = MaxRetryError(
            cast(Any, MagicMock()),
            "/raw-audio/file.mp3",
            Exception("too many 503 error responses"),
        )
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")

        with pytest.raises(AudioStoreUnavailableError, match=r"raw-audio/file\.mp3"):
            store.exists("file.mp3")

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_list_objects(self, mock_minio_cls):
        mock_client = MagicMock()
        obj1 = MagicMock()
        obj1.object_name = "a/b.mp3"
        obj2 = MagicMock()
        obj2.object_name = "a/c.mp3"
        mock_client.list_objects.return_value = [obj1, obj2]
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        result = store.list_objects(prefix="a/")
        assert result == ["a/b.mp3", "a/c.mp3"]

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_delete_object(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")

        assert store.delete_object("a/b.mp3") is True
        mock_client.remove_object.assert_called_once_with(BUCKET_RAW_AUDIO, "a/b.mp3")


class TestLocalFilesystemBackend:
    """Test the explicit local-filesystem storage mode."""

    def test_round_trips_audio_objects_via_local_root(self, tmp_path):
        store = AudioStore(local_root=tmp_path)

        created = store.ensure_buckets()
        assert created == [BUCKET_RAW_AUDIO, BUCKET_PROCESSED_AUDIO, BUCKET_FEATURES]

        path = store.upload_audio("2024-06-15/s1/d1.mp3", b"audiodata")
        assert path == "raw-audio/2024-06-15/s1/d1.mp3"
        assert (tmp_path / BUCKET_RAW_AUDIO / Path("2024-06-15/s1/d1.mp3")).read_bytes() == b"audiodata"
        assert store.exists("2024-06-15/s1/d1.mp3") is True
        assert store.get_object("2024-06-15/s1/d1.mp3") == b"audiodata"
        assert store.list_objects(prefix="2024-06-15/") == ["2024-06-15/s1/d1.mp3"]
        assert store.delete_object("2024-06-15/s1/d1.mp3") is True
        assert store.exists("2024-06-15/s1/d1.mp3") is False
        assert store.delete_object("2024-06-15/s1/d1.mp3") is False

        output_path = tmp_path / "downloaded.mp3"
        store.upload_audio("2024-06-15/s1/d1.mp3", b"audiodata")
        assert store.download_to_file("2024-06-15/s1/d1.mp3", str(output_path)) == str(
            output_path
        )
        assert output_path.read_bytes() == b"audiodata"

        health = store.healthcheck()
        assert health["ok"] is True
        assert health["buckets_missing"] == []


# =============================================================================
# Object key helper
# =============================================================================


class TestDetectionKey:
    """Test the static key-building helper."""

    def test_standard_key(self):
        key = AudioStore.detection_key("2024-06-15", "station1", "det123")
        assert key == "2024-06-15/station1/det123.flac"

    def test_custom_extension(self):
        key = AudioStore.detection_key("2024-06-15", "s1", "d1", ext="wav")
        assert key == "2024-06-15/s1/d1.wav"


# =============================================================================
# Healthcheck
# =============================================================================


class TestHealthcheck:
    """Test the healthcheck method."""

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_healthy(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        result = store.healthcheck()
        assert result["ok"] is True
        assert len(result["buckets_missing"]) == 0

    @patch("avian_biosurveillance.db.minio_client.Minio")
    def test_missing_buckets(self, mock_minio_cls):
        mock_client = MagicMock()
        mock_client.bucket_exists.side_effect = lambda b: b == BUCKET_RAW_AUDIO
        mock_minio_cls.return_value = mock_client

        store = AudioStore(endpoint="h:9000", access_key="a", secret_key="s")
        result = store.healthcheck()
        assert result["ok"] is False
        assert BUCKET_PROCESSED_AUDIO in result["buckets_missing"]
