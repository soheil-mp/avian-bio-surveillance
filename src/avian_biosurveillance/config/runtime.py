"""Runtime configuration loader for service topology.

The canonical non-secret runtime values live in ``config/runtime.env`` so
Docker Compose, Singularity, frontend, and Python consumers can share the same
source of truth. Secrets remain in the ignored/encrypted repo-root ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_RUNTIME_CONFIG_ENV = "AVIAN_RUNTIME_CONFIG"
_RUNTIME_CONFIG_RELATIVE = Path("config/runtime.env")
_SECRETS_ENV_RELATIVE = Path(".env")


def _repo_root_from_source() -> Path:
    """Return the repository root when running from an editable checkout."""
    return Path(__file__).resolve().parents[3]


def runtime_config_path() -> Path:
    """Resolve the runtime topology file path.

    Resolution order:
        1. ``AVIAN_RUNTIME_CONFIG`` environment variable.
        2. ``config/runtime.env`` relative to the current working directory.
        3. ``config/runtime.env`` relative to the source checkout.

    Raises:
        FileNotFoundError: If no runtime config file can be found.
    """
    explicit = os.environ.get(_RUNTIME_CONFIG_ENV)
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path.cwd() / _RUNTIME_CONFIG_RELATIVE,
        _repo_root_from_source() / _RUNTIME_CONFIG_RELATIVE,
    ]

    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate

    searched = ", ".join(str(p) for p in candidates if p is not None)
    msg = (
        "Runtime config not found. Set AVIAN_RUNTIME_CONFIG or create "
        f"{_RUNTIME_CONFIG_RELATIVE}. Searched: {searched}"
    )
    raise FileNotFoundError(msg)


def load_runtime_env(*, include_secrets: bool = True, override: bool = False) -> Path:
    """Load runtime config, then optional secrets, into ``os.environ``.

    Runtime config is loaded first. Secrets are loaded second with the same
    ``override`` policy, so the default behavior lets ``config/runtime.env`` win
    over stale duplicate non-secret values in a local ``.env``.

    Args:
        include_secrets: Also load repo-root ``.env`` when present.
        override: Passed through to :func:`dotenv.load_dotenv`.

    Returns:
        Path to the runtime config file that was loaded.
    """
    config_path = runtime_config_path()
    load_dotenv(config_path, override=override)

    if include_secrets:
        secrets_path = config_path.parents[1] / _SECRETS_ENV_RELATIVE
        if secrets_path.is_file():
            load_dotenv(secrets_path, override=override)

    return config_path


def _require_env(name: str) -> str:
    """Read a required runtime variable after loading ``config/runtime.env``."""
    value = os.environ.get(name)
    if value:
        return value
    msg = f"{name} must be set in {_RUNTIME_CONFIG_RELATIVE}"
    raise RuntimeError(msg)


def _get_int_env(name: str) -> int:
    """Read an integer environment variable with a clear validation error."""
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}"
        raise ValueError(msg) from exc


@dataclass(frozen=True)
class RuntimeConfig:
    """Typed view over the shared runtime topology."""

    postgres_host: str
    postgres_internal_host: str
    postgres_internal_port: int
    tsdb_port: int
    minio_api_port: int
    minio_use_ssl: bool
    dagster_code_server_host: str
    dagster_code_server_port: int
    dagster_location_name: str
    app_internal_url: str


def load_runtime_config() -> RuntimeConfig:
    """Load and return the typed runtime topology config."""
    load_runtime_env()
    return RuntimeConfig(
        postgres_host=_require_env("POSTGRES_HOST"),
        postgres_internal_host=_require_env("POSTGRES_INTERNAL_HOST"),
        postgres_internal_port=_get_int_env("POSTGRES_INTERNAL_PORT"),
        tsdb_port=_get_int_env("TSDB_PORT"),
        minio_api_port=_get_int_env("MINIO_API_PORT"),
        minio_use_ssl=_require_env("MINIO_USE_SSL").strip().lower()
        in {"1", "true", "yes", "on"},
        dagster_code_server_host=_require_env("DAGSTER_CODE_SERVER_HOST"),
        dagster_code_server_port=_get_int_env("DAGSTER_CODE_SERVER_PORT"),
        dagster_location_name=_require_env("DAGSTER_LOCATION_NAME"),
        app_internal_url=_require_env("INTERNAL_URL"),
    )
