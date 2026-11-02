"""MinIO / S3-compatible object storage client for audio files.

Wraps the ``minio`` SDK with convenience methods for the three buckets
defined in the Docker Compose init container:

* ``raw-audio``         — original WAV/FLAC files from BirdWeather or ARUs.
* ``processed-audio``   — high-pass-filtered, segmented audio clips.
* ``features``          — spectrogram and waveform image artifacts.

Configuration is read from ``config/runtime.env`` plus secret env overrides.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NoReturn
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager, Timeout
from urllib3.exceptions import HTTPError

from avian_biosurveillance.config.runtime import load_runtime_env


load_runtime_env()

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _build_minio_http_client() -> PoolManager:
    """Build a urllib3 pool manager with bounded timeouts for MinIO.

    The minio SDK otherwise defaults to a 300 s read timeout, which turns a
    transient stall into a multi-minute hang during large backfills. The
    connect/read timeouts are configurable via ``MINIO_CONNECT_TIMEOUT_SECONDS``
    and ``MINIO_READ_TIMEOUT_SECONDS``.
    """
    connect_timeout = _env_float("MINIO_CONNECT_TIMEOUT_SECONDS", 10.0)
    read_timeout = _env_float("MINIO_READ_TIMEOUT_SECONDS", 120.0)
    timeout = Timeout(connect=connect_timeout, read=read_timeout)
    pool_size = int(_env_float("MINIO_CONNECTION_POOL_SIZE", 32.0))
    return PoolManager(
        timeout=timeout,
        maxsize=pool_size,
        retries=False,
    )

# Default bucket names (must match minio-init in docker-compose.yml)
BUCKET_RAW_AUDIO = "raw-audio"
BUCKET_PROCESSED_AUDIO = "processed-audio"
BUCKET_FEATURES = "features"
BUCKET_SPECTROGRAMS = BUCKET_FEATURES

_ALL_BUCKETS = (
    BUCKET_RAW_AUDIO,
    BUCKET_PROCESSED_AUDIO,
    BUCKET_FEATURES,
)
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NoSuchVersion"})


class AudioStoreUnavailableError(RuntimeError):
    """Raised when MinIO cannot complete a storage operation."""


class AudioStore:
    """S3-compatible client for the AudioDB (MinIO) container.

    Args:
        endpoint: MinIO host:port.  Defaults to ``MINIO_ENDPOINT`` env var.
        access_key: MinIO access key. Defaults to ``MINIO_ACCESS_KEY`` /
            ``MINIO_ROOT_USER`` env var.
        secret_key: MinIO secret key. Defaults to ``MINIO_SECRET_KEY`` /
            ``MINIO_ROOT_PASSWORD`` env var.
        secure: Use HTTPS.  Defaults to ``False`` for local Docker.
        local_root: Optional local bucket root (e.g. ``data/minio``). When set,
            storage operations use the filesystem directly instead of the MinIO
            API. Defaults to ``MINIO_LOCAL_ROOT`` when present.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        *,
        secure: bool | None = None,
        local_root: str | os.PathLike[str] | None = None,
    ) -> None:
        local_root_value = local_root or os.environ.get("MINIO_LOCAL_ROOT")
        self._local_root = (
            Path(local_root_value).expanduser().resolve()
            if local_root_value
            else None
        )

        if self._local_root is not None:
            self._endpoint = "local-filesystem"
            self._access_key = access_key or ""
            self._secret_key = secret_key or ""
            self._client: Minio | None = None
            logger.info("AudioStore using local filesystem root %s", self._local_root)
            return

        endpoint_value = endpoint or os.environ.get("MINIO_ENDPOINT")
        if not endpoint_value:
            api_port = os.environ["MINIO_API_PORT"]
            endpoint_value = f"localhost:{api_port}"

        if "://" in endpoint_value:
            parsed = urlparse(endpoint_value)
            resolved_endpoint = parsed.netloc or parsed.path
            resolved_secure = parsed.scheme == "https"
        else:
            resolved_endpoint = endpoint_value
            if secure is None:
                secure_env = os.environ.get(
                    "MINIO_SECURE",
                    os.environ.get("MINIO_USE_SSL", "false"),
                )
                resolved_secure = str(secure_env).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            else:
                resolved_secure = secure

        if secure is not None and "://" in endpoint_value:
            resolved_secure = secure

        self._endpoint = resolved_endpoint
        self._access_key = access_key or os.environ.get(
            "MINIO_ACCESS_KEY",
            os.environ.get("MINIO_ROOT_USER", "avian-minio"),
        )
        self._secret_key = secret_key or os.environ.get(
            "MINIO_SECRET_KEY",
            os.environ.get("MINIO_ROOT_PASSWORD", ""),
        )
        self._client = Minio(
            self._endpoint,
            access_key=self._access_key,
            secret_key=self._secret_key,
            secure=resolved_secure,
            http_client=_build_minio_http_client(),
        )
        logger.info(
            "AudioStore connected to %s (%s)",
            self._endpoint,
            "https" if resolved_secure else "http",
        )

    # -- Bucket operations ------------------------------------------

    def ensure_buckets(self) -> list[str]:
        """Create default buckets if they don't already exist.

        Returns:
            List of bucket names that were created (empty if all existed).
        """
        created: list[str] = []
        for bucket in _ALL_BUCKETS:
            if self._local_root is not None:
                bucket_dir = self._bucket_dir(bucket)
                if not bucket_dir.exists():
                    bucket_dir.mkdir(parents=True, exist_ok=True)
                    created.append(bucket)
                    logger.info("Created local bucket directory: %s", bucket_dir)
                continue
            try:
                exists = self._client.bucket_exists(bucket)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _raise_storage_unavailable(exc, operation="probe bucket", bucket=bucket)
            if not exists:
                try:
                    self._client.make_bucket(bucket)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    _raise_storage_unavailable(
                        exc, operation="create bucket", bucket=bucket
                    )
                created.append(bucket)
                logger.info("Created bucket: %s", bucket)
        return created

    # -- Upload -----------------------------------------------------

    def upload_audio(
        self,
        object_name: str,
        data: BinaryIO | bytes,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
        content_type: str = "audio/mpeg",
        length: int = -1,
    ) -> str:
        """Upload an audio file to a bucket.

        Args:
            object_name: Object key (e.g. ``2024-01-15/station_123/det_456.mp3``).
            data: File-like object or raw bytes to upload.
            bucket: Target bucket name.
            content_type: MIME type.
            length: Content length. If *data* is ``bytes`` this is computed
                automatically; for streams, pass the known length or ``-1``
                for unknown (uses 5 MiB part size).

        Returns:
            The full object path ``bucket/object_name``.
        """
        if isinstance(data, bytes):
            length = len(data)
            data = io.BytesIO(data)

        if self._local_root is not None:
            object_path = self._object_path(bucket, object_name)
            object_path.parent.mkdir(parents=True, exist_ok=True)
            payload = data.read()
            object_path.write_bytes(payload)
            path = f"{bucket}/{object_name}"
            logger.debug("Wrote %s (%d bytes) to local audio store", path, len(payload))
            return path

        part_size = 0 if length >= 0 else 5 * 1024 * 1024  # 5 MiB
        try:
            self._client.put_object(
                bucket,
                object_name,
                data,
                length=length,
                content_type=content_type,
                part_size=part_size,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="upload object",
                bucket=bucket,
                object_name=object_name,
            )
        path = f"{bucket}/{object_name}"
        logger.debug("Uploaded %s (%d bytes)", path, length)
        return path

    def upload_feature(
        self,
        object_name: str,
        data: BinaryIO | bytes,
        *,
        content_type: str = "application/octet-stream",
        length: int = -1,
    ) -> str:
        """Upload a feature artifact into the ``features`` bucket."""
        return self.upload_audio(
            object_name,
            data,
            bucket=BUCKET_FEATURES,
            content_type=content_type,
            length=length,
        )

    def upload_spectrogram(
        self,
        object_name: str,
        data: BinaryIO | bytes,
        *,
        content_type: str = "image/png",
        length: int = -1,
    ) -> str:
        """Upload a spectrogram image under ``features/spectrograms``."""
        key = _prefixed_feature_key("spectrograms", object_name)
        return self.upload_feature(
            key,
            data,
            content_type=content_type,
            length=length,
        )

    def upload_waveform(
        self,
        object_name: str,
        data: BinaryIO | bytes,
        *,
        content_type: str = "image/png",
        length: int = -1,
    ) -> str:
        """Upload a waveform image under ``features/waveforms``."""
        key = _prefixed_feature_key("waveforms", object_name)
        return self.upload_feature(
            key,
            data,
            content_type=content_type,
            length=length,
        )

    # -- Download / Read --------------------------------------------

    def get_object(
        self,
        object_name: str,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
    ) -> bytes:
        """Download an object and return its raw bytes.

        Args:
            object_name: Object key.
            bucket: Source bucket.

        Returns:
            The object contents.
        """
        if self._local_root is not None:
            object_path = self._object_path(bucket, object_name)
            try:
                return object_path.read_bytes()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _raise_local_storage_unavailable(
                    exc,
                    operation="download object",
                    bucket=bucket,
                    object_name=object_name,
                )

        try:
            response = self._client.get_object(bucket, object_name)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="download object",
                bucket=bucket,
                object_name=object_name,
            )
        try:
            return response.read()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="read object response",
                bucket=bucket,
                object_name=object_name,
            )
        finally:
            response.close()
            response.release_conn()

    def get_object_if_exists(
        self,
        object_name: str,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
    ) -> bytes | None:
        """Download an object and return ``None`` when it is absent."""
        if self._local_root is not None:
            object_path = self._object_path(bucket, object_name)
            try:
                return object_path.read_bytes()
            except FileNotFoundError:
                return None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _raise_local_storage_unavailable(
                    exc,
                    operation="download object",
                    bucket=bucket,
                    object_name=object_name,
                )

        try:
            response = self._client.get_object(bucket, object_name)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if _is_missing_object_error(exc):
                return None
            _raise_storage_unavailable(
                exc,
                operation="download object",
                bucket=bucket,
                object_name=object_name,
            )
        try:
            return response.read()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="read object response",
                bucket=bucket,
                object_name=object_name,
            )
        finally:
            response.close()
            response.release_conn()

    def download_to_file(
        self,
        object_name: str,
        dest_path: str,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
    ) -> str:
        """Download an object to a local file.

        Args:
            object_name: Object key.
            dest_path: Local file path to write.
            bucket: Source bucket.

        Returns:
            The destination path.
        """
        if self._local_root is not None:
            object_path = self._object_path(bucket, object_name)
            destination = Path(dest_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                destination.write_bytes(object_path.read_bytes())
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _raise_local_storage_unavailable(
                    exc,
                    operation="download object to file",
                    bucket=bucket,
                    object_name=object_name,
                )
            logger.debug("Downloaded %s/%s → %s from local audio store", bucket, object_name, dest_path)
            return dest_path

        try:
            self._client.fget_object(bucket, object_name, dest_path)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="download object to file",
                bucket=bucket,
                object_name=object_name,
            )
        logger.debug("Downloaded %s/%s → %s", bucket, object_name, dest_path)
        return dest_path

    # -- Existence / listing ----------------------------------------

    def exists(
        self,
        object_name: str,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
    ) -> bool:
        """Check whether an object exists in a bucket."""
        if self._local_root is not None:
            return self._object_path(bucket, object_name).is_file()

        try:
            self._client.stat_object(bucket, object_name)
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if _is_missing_object_error(exc):
                return False
            _raise_storage_unavailable(
                exc,
                operation="stat object",
                bucket=bucket,
                object_name=object_name,
            )

        return False

    def list_objects(
        self,
        prefix: str = "",
        *,
        bucket: str = BUCKET_RAW_AUDIO,
        recursive: bool = True,
    ) -> list[str]:
        """List object keys under a prefix.

        Args:
            prefix: Key prefix to filter on.
            bucket: Bucket to list.
            recursive: Recurse into sub-prefixes.

        Returns:
            List of object names.
        """
        if self._local_root is not None:
            bucket_dir = self._bucket_dir(bucket)
            if not bucket_dir.exists():
                return []

            iterator = bucket_dir.rglob("*") if recursive else bucket_dir.glob("*")
            return sorted(
                path.relative_to(bucket_dir).as_posix()
                for path in iterator
                if path.is_file() and path.relative_to(bucket_dir).as_posix().startswith(prefix)
            )

        try:
            return [
                obj.object_name
                for obj in self._client.list_objects(
                    bucket,
                    prefix=prefix,
                    recursive=recursive,
                )
                if obj.object_name is not None
            ]
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="list objects",
                bucket=bucket,
                object_name=prefix or None,
            )

    def has_objects(
        self,
        prefix: str = "",
        *,
        bucket: str = BUCKET_RAW_AUDIO,
        recursive: bool = True,
    ) -> bool:
        """Return ``True`` when a bucket contains at least one object for a prefix."""
        if self._local_root is not None:
            bucket_dir = self._bucket_dir(bucket)
            if not bucket_dir.exists():
                return False

            iterator = bucket_dir.rglob("*") if recursive else bucket_dir.glob("*")
            return any(
                path.is_file()
                and path.relative_to(bucket_dir).as_posix().startswith(prefix)
                for path in iterator
            )

        try:
            for obj in self._client.list_objects(
                bucket,
                prefix=prefix,
                recursive=recursive,
            ):
                if obj.object_name is not None:
                    return True
            return False
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _raise_storage_unavailable(
                exc,
                operation="list objects",
                bucket=bucket,
                object_name=prefix or None,
            )

        return False

    def delete_object(
        self,
        object_name: str,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
        missing_ok: bool = True,
    ) -> bool:
        """Delete one object from a bucket.

        Returns ``True`` when an object was deleted and ``False`` when it was
        already absent and *missing_ok* is true.
        """
        if self._local_root is not None:
            object_path = self._object_path(bucket, object_name)
            try:
                object_path.unlink()
                return True
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _raise_local_storage_unavailable(
                    exc,
                    operation="delete object",
                    bucket=bucket,
                    object_name=object_name,
                )

        try:
            self._client.remove_object(bucket, object_name)
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if missing_ok and _is_missing_object_error(exc):
                return False
            _raise_storage_unavailable(
                exc,
                operation="delete object",
                bucket=bucket,
                object_name=object_name,
            )

    def delete_objects(
        self,
        object_names: list[str],
        *,
        bucket: str = BUCKET_RAW_AUDIO,
        missing_ok: bool = True,
    ) -> int:
        """Delete multiple objects from a bucket and return deleted count."""
        deleted = 0
        for object_name in object_names:
            if self.delete_object(
                object_name,
                bucket=bucket,
                missing_ok=missing_ok,
            ):
                deleted += 1
        return deleted

    # -- Presigned URL ----------------------------------------------

    def presigned_url(
        self,
        object_name: str,
        *,
        bucket: str = BUCKET_RAW_AUDIO,
        expires_secs: int = 3600,
    ) -> str:
        """Generate a presigned GET URL for an object.

        Args:
            object_name: Object key.
            bucket: Bucket containing the object.
            expires_secs: URL validity in seconds (default 1 hour).

        Returns:
            Presigned URL string.
        """
        if self._local_root is not None:
            return self._object_path(bucket, object_name).as_uri()

        from datetime import timedelta

        return self._client.presigned_get_object(
            bucket,
            object_name,
            expires=timedelta(seconds=expires_secs),
        )

    # -- Healthcheck ------------------------------------------------

    def healthcheck(self) -> dict[str, object]:
        """Verify MinIO connectivity and bucket existence.

        Returns:
            Dict with ``ok``, ``buckets_found``, ``buckets_missing``.
        """
        found: list[str] = []
        missing: list[str] = []
        for bucket in _ALL_BUCKETS:
            if self._local_root is not None:
                if self._bucket_dir(bucket).exists():
                    found.append(bucket)
                else:
                    missing.append(bucket)
                continue
            if self._client.bucket_exists(bucket):
                found.append(bucket)
            else:
                missing.append(bucket)
        return {
            "ok": len(missing) == 0,
            "buckets_found": found,
            "buckets_missing": missing,
        }

    def _bucket_dir(self, bucket: str) -> Path:
        if self._local_root is None:
            raise RuntimeError("local filesystem storage is not configured")
        return self._local_root / bucket

    def _object_path(self, bucket: str, object_name: str) -> Path:
        return self._bucket_dir(bucket) / PurePosixPath(object_name)

    # -- Build object key helpers -----------------------------------

    @staticmethod
    def detection_key(
        date_str: str,
        station_id: str,
        detection_id: str,
        ext: str = "flac",
    ) -> str:
        """Build a standard object key for a BirdWeather audio file.

        Format: ``{date}/{station_id}/{detection_id}.{ext}``

        Args:
            date_str: Date string (``YYYY-MM-DD``).
            station_id: BirdWeather station ID.
            detection_id: Detection ID.
            ext: File extension (default ``flac``).

        Returns:
            Object key string.
        """
        return str(PurePosixPath(date_str, station_id, f"{detection_id}.{ext}"))


def _prefixed_feature_key(prefix: str, object_name: str) -> str:
    clean_name = str(PurePosixPath(object_name))
    if clean_name == prefix or clean_name.startswith(f"{prefix}/"):
        return clean_name
    return str(PurePosixPath(prefix, clean_name))


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def _raise_local_storage_unavailable(
    exc: BaseException,
    *,
    operation: str,
    bucket: str,
    object_name: str | None = None,
) -> NoReturn:
    target = bucket if object_name is None else f"{bucket}/{object_name}"
    raise AudioStoreUnavailableError(
        f"Audio storage could not {operation} for {target}"
    ) from exc


def _is_missing_object_error(exc: BaseException) -> bool:
    return any(
        isinstance(node, S3Error) and node.code in _MISSING_OBJECT_CODES
        for node in _exception_chain(exc)
    )


def _raise_storage_unavailable(
    exc: BaseException,
    *,
    operation: str,
    bucket: str,
    object_name: str | None = None,
) -> NoReturn:
    if isinstance(exc, AudioStoreUnavailableError):
        raise exc

    if not any(
        isinstance(node, (S3Error, HTTPError, OSError, TimeoutError))
        for node in _exception_chain(exc)
    ):
        raise exc

    object_path = bucket if object_name is None else f"{bucket}/{object_name}"
    raise AudioStoreUnavailableError(
        f"MinIO could not {operation} for {object_path}"
    ) from exc
