"""Helpers for locally staged BirdWeather raw audio files."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


DEFAULT_LOCAL_BIRDWEATHER_RAW_ROOT = Path("data/raw/birdweather")

# Smallest size (in bytes) for a usable BirdWeather raw-audio object. The CDN
# occasionally returns an HTTP 200 with an empty body under parallel SSL/timeout
# churn; those land on disk as 0-byte files that no decoder can open. Anything
# below this floor is treated as "not present" so it is re-downloaded rather
# than persisted and repeatedly failing to decode. (Truncated-but-non-empty
# bodies are caught later, at decode time, as UndecodableAudioError.)
MIN_VALID_RAW_AUDIO_BYTES = 1


def resolve_local_birdweather_raw_root(
    raw_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the configured local BirdWeather raw-audio root."""
    if raw_root is None:
        return DEFAULT_LOCAL_BIRDWEATHER_RAW_ROOT
    return Path(raw_root).expanduser()


def local_raw_audio_path_from_key(
    object_key: str,
    *,
    raw_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve one canonical BirdWeather raw-audio key to a local file path."""
    return resolve_local_birdweather_raw_root(raw_root) / PurePosixPath(object_key)


def local_raw_audio_is_present(
    object_key: str,
    *,
    raw_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Return True only when a usable (non-empty) local raw-audio file exists.

    A 0-byte (or truncated, sub-``MIN_VALID_RAW_AUDIO_BYTES``) file is treated
    as absent so that callers re-download it instead of mistaking a failed CDN
    response for a staged object.
    """
    path = local_raw_audio_path_from_key(object_key, raw_root=raw_root)
    try:
        return path.is_file() and path.stat().st_size >= MIN_VALID_RAW_AUDIO_BYTES
    except OSError:
        return False
