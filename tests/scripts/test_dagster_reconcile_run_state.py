"""Tests for Dagster orphaned-run reconciliation scoping."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest


class _ReconcileModule(Protocol):
    def should_reconcile_run(self, run: object) -> bool: ...


def _load_reconcile_module() -> _ReconcileModule:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ops"
        / "dagster_reconcile_run_state.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dagster_reconcile_run_state",
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load dagster_reconcile_run_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(_ReconcileModule, module)


reconcile = _load_reconcile_module()


def _run(tags: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(tags=tags)


def test_should_reconcile_all_locations_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIAN_DAGSTER_CODE_LOCATION", raising=False)

    assert reconcile.should_reconcile_run(_run({}))


def test_should_reconcile_only_matching_ingestion_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIAN_DAGSTER_CODE_LOCATION", "ingestion")

    assert reconcile.should_reconcile_run(
        _run({".dagster/grpc_info": '{"host": "ingestion", "port": 4000}'})
    )
    assert not reconcile.should_reconcile_run(
        _run({".dagster/grpc_info": '{"host": "model", "port": 4001}'})
    )


def test_should_fall_back_to_repository_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIAN_DAGSTER_CODE_LOCATION", "model")

    assert reconcile.should_reconcile_run(
        _run({".dagster/repository": "__repository__@models"})
    )
    assert not reconcile.should_reconcile_run(
        _run({".dagster/repository": "__repository__@pipelines"})
    )


def test_should_not_reconcile_scoped_run_without_identity_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIAN_DAGSTER_CODE_LOCATION", "ingestion")

    assert not reconcile.should_reconcile_run(_run({}))
