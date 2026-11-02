"""Loader for the modeling-phase configuration file.

The modeling phase reads tunable thresholds from ``config/modeling.json``.
The file is the single source of truth for baseline windows, embedding-drift
parameters, and the unsupervised anomaly model consumed by the daily Dagster
job.

The loader reads the checked-in config file directly and raises immediately
if it is missing or malformed so the runtime never silently diverges from the
repository configuration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_CONFIG_RELATIVE = Path("config/modeling.json")
_REMOVED_SECTIONS = frozenset(
    {
        "count_baseline",
        "environmental_context",
        "reference_context",
        "fusion",
        "review_queue",
        "artifacts",
    }
)


def _repo_root_from_source() -> Path:
    """Return the repository root when running from an editable checkout."""
    return Path(__file__).resolve().parents[3]


def _config_candidates(config_path: Path | None) -> list[Path]:
    if config_path is not None:
        return [config_path.expanduser()]
    candidates: list[Path] = []
    candidates.extend(
        [
            Path.cwd() / _CONFIG_RELATIVE,
            _repo_root_from_source() / _CONFIG_RELATIVE,
        ]
    )
    deduplicated: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved not in seen:
            deduplicated.append(resolved)
            seen.add(resolved)
    return deduplicated


@dataclass(frozen=True, slots=True)
class ModelingSettings:
    """Read-only view over the modeling configuration file."""

    feature_set: dict[str, Any]
    embedding_drift: dict[str, Any]
    anomaly_model: dict[str, Any]
    source_path: Path | None


def load_modeling_settings(config_path: Path | None = None) -> ModelingSettings:
    """Load modeling settings from JSON.

    Args:
        config_path: Optional explicit path to a modeling JSON config. When
            given, it is tried before the working-directory and source-checkout
            copies.

    Returns:
        A populated :class:`ModelingSettings` instance.

    Raises:
        FileNotFoundError: If no modeling config file can be found.
        ValueError: If the config file is invalid or missing required sections.
    """
    candidate_paths = _config_candidates(config_path)
    for candidate in candidate_paths:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = f"Invalid JSON in modeling config {candidate}: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(payload, dict):
            msg = f"Modeling config {candidate} did not contain a JSON object."
            raise ValueError(msg)
        cleaned = _clean_payload(payload)
        settings = _validate_and_build_settings(cleaned, candidate)
        logger.info("Loaded modeling settings from %s", candidate)
        return settings

    searched = ", ".join(str(p) for p in candidate_paths)
    msg = (
        "Modeling config not found. Searched: "
        f"{searched}. Create config/modeling.json or pass an explicit path."
    )
    raise FileNotFoundError(msg)


def _clean_payload(value: Any) -> Any:
    """Recursively strip underscore-prefixed comment keys from JSON payloads."""
    if isinstance(value, dict):
        return {
            str(key): _clean_payload(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_clean_payload(item) for item in value]
    return value


def _require_mapping(
    payload: dict[str, Any],
    section: str,
    *,
    config_path: Path,
) -> dict[str, Any]:
    value = payload.get(section)
    if not isinstance(value, dict):
        msg = f"Modeling config {config_path} is missing object section {section!r}."
        raise ValueError(msg)
    return value


def _validate_and_build_settings(
    payload: dict[str, Any],
    config_path: Path,
) -> ModelingSettings:
    removed = sorted(section for section in _REMOVED_SECTIONS if section in payload)
    if removed:
        msg = (
            f"Modeling config {config_path} contains removed section(s): "
            f"{', '.join(removed)}."
        )
        raise ValueError(msg)
    return ModelingSettings(
        feature_set=_require_mapping(payload, "feature_set", config_path=config_path),
        embedding_drift=_require_mapping(
            payload, "embedding_drift", config_path=config_path
        ),
        anomaly_model=_require_mapping(
            payload, "anomaly_model", config_path=config_path
        ),
        source_path=config_path,
    )


SETTINGS: ModelingSettings = load_modeling_settings()
"""Module-level singleton populated from ``config/modeling.json`` at import time."""


__all__ = [
    "SETTINGS",
    "ModelingSettings",
    "load_modeling_settings",
]
