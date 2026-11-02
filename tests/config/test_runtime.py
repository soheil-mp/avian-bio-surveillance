from __future__ import annotations

import os
from pathlib import Path

import pytest

from avian_biosurveillance.config.runtime import (
    load_runtime_config,
    load_runtime_env,
    runtime_config_path,
)


def test_runtime_config_path_uses_explicit_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "runtime.env"
    config_file.write_text("TSDB_PORT=16000\n", encoding="utf-8")

    monkeypatch.setenv("AVIAN_RUNTIME_CONFIG", str(config_file))

    assert runtime_config_path() == config_file


def test_load_runtime_env_reads_runtime_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "runtime.env"
    config_file.write_text(
        "\n".join(
            [
                "TSDB_PORT=16000",
                "DAGSTER_CODE_SERVER_PORT=4100",
                "DAGSTER_CODE_SERVER_HOST=127.0.0.1",
            ],
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AVIAN_RUNTIME_CONFIG", str(config_file))
    monkeypatch.delenv("TSDB_PORT", raising=False)
    monkeypatch.delenv("DAGSTER_CODE_SERVER_PORT", raising=False)

    loaded_path = load_runtime_env(include_secrets=False)

    assert loaded_path == config_file
    assert os.environ["TSDB_PORT"] == "16000"
    assert os.environ["DAGSTER_CODE_SERVER_PORT"] == "4100"


def test_load_runtime_config_returns_typed_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "runtime.env"
    config_file.write_text(
        "\n".join(
            [
                "POSTGRES_HOST=db.example",
                "POSTGRES_INTERNAL_HOST=timescaledb",
                "POSTGRES_INTERNAL_PORT=5432",
                "TSDB_PORT=16000",
                "MINIO_API_PORT=19100",
                "MINIO_USE_SSL=true",
                "DAGSTER_CODE_SERVER_HOST=127.0.0.1",
                "DAGSTER_CODE_SERVER_PORT=4100",
                "DAGSTER_LOCATION_NAME=test_location",
                "INTERNAL_URL=http://localhost:3100",
            ],
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AVIAN_RUNTIME_CONFIG", str(config_file))
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_INTERNAL_PORT",
        "TSDB_PORT",
        "MINIO_API_PORT",
        "MINIO_USE_SSL",
        "DAGSTER_CODE_SERVER_HOST",
        "DAGSTER_CODE_SERVER_PORT",
        "DAGSTER_LOCATION_NAME",
        "INTERNAL_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    config = load_runtime_config()

    assert config.postgres_host == "db.example"
    assert config.tsdb_port == 16000
    assert config.minio_api_port == 19100
    assert config.minio_use_ssl is True
    assert config.dagster_code_server_port == 4100
    assert config.dagster_location_name == "test_location"
