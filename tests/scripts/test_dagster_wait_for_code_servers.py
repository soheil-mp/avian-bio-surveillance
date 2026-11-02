"""Tests for Dagster code-server readiness probing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest


class _WaitModule(Protocol):
    def _wait_for_endpoint(
        self,
        host: str,
        port: int,
        label: str,
        attempts: int,
        delay_seconds: float,
    ) -> None: ...


def _load_wait_module() -> _WaitModule:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ops"
        / "dagster_wait_for_code_servers.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dagster_wait_for_code_servers",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load dagster_wait_for_code_servers.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_WaitModule, module)


wait_module = _load_wait_module()


def test_wait_for_endpoint_requires_loaded_repositories(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Client:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def get_server_id(self, timeout: float) -> str:
            assert timeout > 0
            return "server-id"

    monkeypatch.setattr(wait_module, "DagsterGrpcClient", _Client)
    monkeypatch.setattr(
        wait_module,
        "sync_list_repositories_grpc",
        lambda _client: SimpleNamespace(
            repository_symbols=[
                SimpleNamespace(repository_name="__repository__", attribute="defs")
            ]
        ),
    )

    wait_module._wait_for_endpoint(
        "ingestion",
        4000,
        "ingestion code-server",
        attempts=1,
        delay_seconds=0.01,
    )

    output = capsys.readouterr().out
    assert "with repositories: __repository__@defs" in output


def test_wait_for_endpoint_retries_when_repository_list_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def get_server_id(self, timeout: float) -> str:
            assert timeout > 0
            return "server-id"

    monkeypatch.setattr(wait_module, "DagsterGrpcClient", _Client)
    monkeypatch.setattr(wait_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        wait_module,
        "sync_list_repositories_grpc",
        lambda _client: SimpleNamespace(repository_symbols=[]),
    )

    with pytest.raises(
        RuntimeError, match="Timed out waiting for ingestion code-server"
    ):
        wait_module._wait_for_endpoint(
            "ingestion",
            4000,
            "ingestion code-server",
            attempts=2,
            delay_seconds=0.01,
        )
