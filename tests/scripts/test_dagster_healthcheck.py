"""Tests for the Dagster code-server healthcheck helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest


class _HealthcheckModule(Protocol):
    def main(self) -> int: ...


def _load_healthcheck_module() -> _HealthcheckModule:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ops"
        / "dagster_healthcheck.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dagster_healthcheck",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load dagster_healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_HealthcheckModule, module)


healthcheck_module = _load_healthcheck_module()


def test_healthcheck_main_requires_loaded_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self, host: str, port: int) -> None:
            assert host == "127.0.0.1"
            assert port == 4000

        def get_server_id(self, timeout: float) -> str:
            assert timeout == pytest.approx(5.0)
            return "server-id"

    monkeypatch.setenv("DAGSTER_CODE_SERVER_PORT", "4000")
    monkeypatch.setattr(healthcheck_module, "DagsterGrpcClient", _Client)
    monkeypatch.setattr(
        healthcheck_module,
        "sync_list_repositories_grpc",
        lambda _client: SimpleNamespace(
            repository_symbols=[
                SimpleNamespace(repository_name="__repository__", attribute="defs")
            ]
        ),
    )

    assert healthcheck_module.main() == 0


def test_healthcheck_main_rejects_empty_repository_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def get_server_id(self, timeout: float) -> str:
            assert timeout > 0
            return "server-id"

    monkeypatch.setenv("DAGSTER_CODE_SERVER_PORT", "4000")
    monkeypatch.setattr(healthcheck_module, "DagsterGrpcClient", _Client)
    monkeypatch.setattr(
        healthcheck_module,
        "sync_list_repositories_grpc",
        lambda _client: SimpleNamespace(repository_symbols=[]),
    )

    with pytest.raises(RuntimeError, match="reported no loadable repositories"):
        healthcheck_module.main()
