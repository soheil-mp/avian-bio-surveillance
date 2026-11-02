"""Materialize saved BirdWeather waveform and spectrogram artifacts on demand."""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import PurePosixPath
from typing import Any

import numpy as np
import psycopg2.extras
import soundfile as sf
from minio.error import S3Error

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.minio_client import (
    BUCKET_FEATURES,
    BUCKET_PROCESSED_AUDIO,
    AudioStore,
    AudioStoreUnavailableError,
)
from avian_biosurveillance.features.audio_artifacts import (
    render_bar_waveform_png,
    render_smooth_waveform_png,
    render_spectrogram_png,
)
from avian_biosurveillance.processing.birdweather_audio import (
    BirdWeatherAudioCandidate,
    processed_clip_key,
)


logger = logging.getLogger(__name__)

_QUALIFIED_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(slots=True)
class VisualArtifactMaterializationResult:
    """Structured result for one on-demand BirdWeather visual artifact run."""

    detection_id: str
    status: str
    processed_clip: str | None = None
    waveform: str | None = None
    spectrogram: str | None = None
    smooth_waveform: str | None = None
    updated_db: bool = False
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def materialize_birdweather_visual_artifacts(
    *,
    detection_id: str,
    force: bool = False,
    store: AudioStore | None = None,
    pool: ConnectionPool | None = None,
) -> VisualArtifactMaterializationResult:
    """Generate canonical waveform and spectrogram PNGs for one detection."""
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=2)
    store = store or AudioStore()
    manifest: dict[str, str | None] | None = None

    try:
        loaded = _load_detection_candidate(pool, detection_id)
        if loaded is None:
            return VisualArtifactMaterializationResult(
                detection_id=detection_id,
                status="not_found",
            )

        row = loaded
        manifest = _canonical_artifact_manifest(row)

        if not force and _artifacts_exist(store, manifest):
            updated_db = _merge_feature_artifacts(pool, row, manifest)
            return VisualArtifactMaterializationResult(
                detection_id=row.detection_id,
                status="existing",
                updated_db=updated_db,
                **manifest,
            )

        clip_key = processed_clip_key(row)
        if not store.exists(clip_key, bucket=BUCKET_PROCESSED_AUDIO):
            return VisualArtifactMaterializationResult(
                detection_id=row.detection_id,
                status="missing_clip",
                **manifest,
            )

        clip_bytes = store.get_object(clip_key, bucket=BUCKET_PROCESSED_AUDIO)
        audio, sample_rate = _decode_processed_clip(clip_bytes)
        uploaded_manifest = _upload_visual_artifacts(
            store,
            row,
            audio,
            sample_rate,
            processed_clip_object=manifest["processed_clip"]
            or f"{BUCKET_PROCESSED_AUDIO}/{clip_key}",
        )
        updated_db = _merge_feature_artifacts(pool, row, uploaded_manifest)
        return VisualArtifactMaterializationResult(
            detection_id=row.detection_id,
            status="materialized",
            updated_db=updated_db,
            **uploaded_manifest,
        )
    except (
        AudioStoreUnavailableError,
        OSError,
        RuntimeError,
        S3Error,
        TypeError,
        ValueError,
        sf.LibsndfileError,
    ) as exc:
        logger.exception(
            "Failed to materialize BirdWeather visual artifacts for %s",
            detection_id,
        )
        return VisualArtifactMaterializationResult(
            detection_id=detection_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            **(manifest or {}),
        )
    finally:
        if own_pool:
            pool.close()


def _load_detection_candidate(
    pool: ConnectionPool,
    detection_id: str,
) -> BirdWeatherAudioCandidate | None:
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                detection_id,
                detected_at,
                observation_date,
                station_id,
                species_id,
                audio_filename,
                COALESCE(audio_start_time_s, 0.0) AS audio_start_time_s,
                COALESCE(audio_end_time_s, 0.0) AS audio_end_time_s
            FROM birdweather_tbl
            WHERE detection_id = %s
            ORDER BY detected_at DESC
            LIMIT 1
            """,
            (detection_id,),
        )
        record = cur.fetchone()

    if record is None:
        return None

    return BirdWeatherAudioCandidate(
        detection_id=str(record["detection_id"]),
        detected_at=record["detected_at"],
        observation_date=record["observation_date"],
        station_id=str(record.get("station_id") or "unknown_station"),
        species_id=str(record.get("species_id") or "unknown_species"),
        audio_filename=record.get("audio_filename"),
        audio_start_time_s=float(record["audio_start_time_s"]),
        audio_end_time_s=float(record["audio_end_time_s"]),
    )


def _decode_processed_clip(clip_bytes: bytes) -> tuple[np.ndarray, int]:
    audio, sample_rate = sf.read(
        io.BytesIO(clip_bytes),
        dtype="float32",
        always_2d=False,
    )
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1, dtype=np.float32)
    if audio.size == 0:
        raise ValueError("processed clip is empty")
    return np.asarray(audio, dtype=np.float32), sample_rate


def _upload_visual_artifacts(
    store: AudioStore,
    row: BirdWeatherAudioCandidate,
    audio: np.ndarray,
    sample_rate: int,
    *,
    processed_clip_object: str,
) -> dict[str, str]:
    base_key = str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}.png",
        )
    )
    smooth_key = str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}_smooth.png",
        )
    )

    spectrogram_object = store.upload_spectrogram(
        base_key,
        render_spectrogram_png(audio, sample_rate),
    )
    waveform_object = store.upload_waveform(
        base_key,
        render_bar_waveform_png(audio),
    )
    smooth_waveform_object = store.upload_waveform(
        smooth_key,
        render_smooth_waveform_png(audio),
    )

    return {
        "processed_clip": processed_clip_object,
        "spectrogram": spectrogram_object,
        "waveform": waveform_object,
        "smooth_waveform": smooth_waveform_object,
    }


def _canonical_artifact_manifest(
    row: BirdWeatherAudioCandidate,
) -> dict[str, str]:
    base_key = str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}.png",
        )
    )
    smooth_key = str(
        PurePosixPath(
            "birdweather",
            row.date_str,
            row.station_id or "unknown_station",
            f"{row.detection_id}_smooth.png",
        )
    )
    return {
        "processed_clip": f"{BUCKET_PROCESSED_AUDIO}/{processed_clip_key(row)}",
        "waveform": f"{BUCKET_FEATURES}/waveforms/{base_key}",
        "spectrogram": f"{BUCKET_FEATURES}/spectrograms/{base_key}",
        "smooth_waveform": f"{BUCKET_FEATURES}/waveforms/{smooth_key}",
    }


def _artifacts_exist(store: AudioStore, manifest: dict[str, str | None]) -> bool:
    waveform = _bucket_object_name(manifest.get("waveform"), BUCKET_FEATURES)
    spectrogram = _bucket_object_name(manifest.get("spectrogram"), BUCKET_FEATURES)
    smooth_waveform = _bucket_object_name(
        manifest.get("smooth_waveform"),
        BUCKET_FEATURES,
    )
    if not waveform or not spectrogram or not smooth_waveform:
        return False

    return all(
        store.exists(object_name, bucket=BUCKET_FEATURES)
        for object_name in (waveform, spectrogram, smooth_waveform)
    )


def _bucket_object_name(path: str | None, bucket: str) -> str | None:
    if not path:
        return None
    prefix = f"{bucket}/"
    if not path.startswith(prefix):
        return None
    object_name = path[len(prefix) :]
    return object_name or None


def _merge_feature_artifacts(
    pool: ConnectionPool,
    row: BirdWeatherAudioCandidate,
    artifact_manifest: dict[str, str | None],
) -> bool:
    day_start, day_end = _day_bounds(
        row.observation_date,
        tzinfo=row.detected_at.tzinfo,
    )

    with pool.cursor() as cur:
        cur.execute(
            """
            SELECT tableoid::regclass::text AS chunk_name, ctid::text AS chunk_tid
            FROM birdweather_tbl
            WHERE detected_at >= %s
              AND detected_at < %s
              AND observation_date = %s
              AND detection_id = %s
              AND detected_at = %s
            LIMIT 1
            """,
            (
                day_start,
                day_end,
                row.observation_date,
                row.detection_id,
                row.detected_at,
            ),
        )
        target_row = cur.fetchone()
        if target_row is None:
            return False

        chunk_name, chunk_tid = target_row
        cur.execute(
            f"""
            UPDATE {_quote_qualified_identifier(str(chunk_name))}
            SET feature_artifacts = COALESCE(feature_artifacts, '{{}}'::jsonb) || %s::jsonb,
                has_processed_clip = true
            WHERE ctid = %s::tid
              AND detection_id = %s
              AND detected_at = %s
            """,
            (
                json.dumps(artifact_manifest),
                chunk_tid,
                row.detection_id,
                row.detected_at,
            ),
        )
        return int(cur.rowcount or 0) > 0


def _day_bounds(
    value: date,
    *,
    tzinfo: tzinfo | None,
) -> tuple[datetime, datetime]:
    day_start = datetime(value.year, value.month, value.day, tzinfo=tzinfo or UTC)
    return day_start, day_start + timedelta(days=1)


def _quote_qualified_identifier(value: str) -> str:
    parts = value.split(".")
    if not parts or any(not _QUALIFIED_IDENTIFIER_RE.fullmatch(part) for part in parts):
        msg = f"unsafe SQL identifier: {value!r}"
        raise ValueError(msg)
    return ".".join(f'"{part}"' for part in parts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize canonical BirdWeather waveform and spectrogram artifacts.",
    )
    parser.add_argument("--detection-id", required=True, help="BirdWeather detection_id")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-render artifacts even when the expected PNGs already exist.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = materialize_birdweather_visual_artifacts(
        detection_id=args.detection_id,
        force=bool(args.force),
    )
    print(result.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())