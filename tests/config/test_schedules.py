"""Tests for avian_biosurveillance.config.schedules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avian_biosurveillance.config.schedules import (
    DeploymentSchedule,
    ScheduleRegistry,
    load_schedule_registry,
    schedule_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_schedules(path: Path, deployments: list[dict]) -> None:
    path.write_text(json.dumps({"deployments": deployments}), encoding="utf-8")


def _minimal_deployment(**overrides) -> dict:
    base = {
        "name": "test-flow",
        "flow": "avian_biosurveillance.orchestration.test_flow:test_flow",
        "cron": "0 6 * * *",
        "tags": ["test"],
        "description": "Test deployment",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DeploymentSchedule dataclass
# ---------------------------------------------------------------------------


class TestDeploymentSchedule:
    def test_minimal_construction(self):
        dep = DeploymentSchedule(
            name="foo",
            flow="module:flow",
            cron="0 0 * * *",
            tags=["a"],
            description="desc",
        )
        assert dep.name == "foo"
        assert dep.concurrency_limit == 1
        assert dep.enabled is True
        assert dep.run_on_startup is False
        assert dep.execution_modes == ["cron"]
        assert dep.depends_on == []
        assert dep.parameters == {}

    def test_custom_fields(self):
        dep = DeploymentSchedule(
            name="bar",
            flow="module:flow",
            cron="*/5 * * * *",
            tags=["x", "y"],
            description="custom",
            concurrency_limit=3,
            enabled=False,
            run_on_startup=True,
            execution_modes=["manual", "startup"],
            depends_on=["upstream-a"],
            parameters={"key": "value"},
        )
        assert dep.concurrency_limit == 3
        assert dep.enabled is False
        assert dep.run_on_startup is True
        assert dep.execution_modes == ["manual", "startup"]
        assert dep.depends_on == ["upstream-a"]
        assert dep.parameters["key"] == "value"

    def test_frozen(self):
        dep = DeploymentSchedule(
            name="x", flow="m:f", cron="0 * * * *", tags=[], description=""
        )
        with pytest.raises((AttributeError, TypeError)):
            dep.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ScheduleRegistry
# ---------------------------------------------------------------------------


def _make_registry(*deps: dict) -> ScheduleRegistry:
    parsed = [
        DeploymentSchedule(
            name=d["name"],
            flow=d.get("flow", "m:f"),
            cron=d.get("cron", "0 * * * *"),
            tags=d.get("tags", []),
            description=d.get("description", ""),
            enabled=d.get("enabled", True),
            run_on_startup=d.get("run_on_startup", False),
            execution_modes=d.get("execution_modes", ["cron"]),
            depends_on=d.get("depends_on", []),
        )
        for d in deps
    ]
    return ScheduleRegistry(parsed)


class TestScheduleRegistry:
    def test_len(self):
        reg = _make_registry({"name": "a"}, {"name": "b"})
        assert len(reg) == 2

    def test_iter(self):
        reg = _make_registry({"name": "a"}, {"name": "b"})
        names = [d.name for d in reg]
        assert names == ["a", "b"]

    def test_getitem(self):
        reg = _make_registry({"name": "a"}, {"name": "b"})
        assert reg[0].name == "a"
        assert reg[1].name == "b"

    def test_get_existing(self):
        reg = _make_registry({"name": "daily"}, {"name": "hourly"})
        dep = reg.get("daily")
        assert dep is not None
        assert dep.name == "daily"

    def test_get_missing(self):
        reg = _make_registry({"name": "daily"})
        assert reg.get("nonexistent") is None

    def test_enabled_filters(self):
        reg = _make_registry(
            {"name": "active", "enabled": True},
            {"name": "inactive", "enabled": False},
        )
        assert len(reg.enabled) == 1
        assert reg.enabled[0].name == "active"

    def test_startup_deployments(self):
        reg = _make_registry(
            {"name": "boot", "enabled": True, "run_on_startup": True},
            {"name": "scheduled", "enabled": True, "run_on_startup": False},
            {"name": "disabled", "enabled": False, "run_on_startup": True},
        )
        startup = reg.startup_deployments
        assert [dep.name for dep in startup] == ["boot", "disabled"]


# ---------------------------------------------------------------------------
# load_schedule_registry
# ---------------------------------------------------------------------------


class TestLoadScheduleRegistry:
    def test_loads_from_explicit_path(self, tmp_path):
        f = tmp_path / "schedules.json"
        _write_schedules(f, [_minimal_deployment()])
        reg = load_schedule_registry(f)
        assert len(reg) == 1
        assert reg[0].name == "test-flow"
        assert reg[0].cron == "0 6 * * *"

    def test_loads_multiple_deployments(self, tmp_path):
        f = tmp_path / "schedules.json"
        _write_schedules(
            f,
            [
                _minimal_deployment(name="flow-a"),
                _minimal_deployment(name="flow-b"),
            ],
        )
        reg = load_schedule_registry(f)
        assert len(reg) == 2

    def test_parses_optional_fields(self, tmp_path):
        f = tmp_path / "schedules.json"
        _write_schedules(
            f,
            [
                _minimal_deployment(name="upstream-a"),
                _minimal_deployment(name="upstream-b"),
                _minimal_deployment(
                    concurrency_limit=1,
                    enabled=False,
                    run_on_startup=True,
                    execution_modes=["sensor", "startup"],
                    depends_on=["upstream-a", "upstream-b"],
                    parameters={"days": 7},
                ),
            ],
        )
        reg = load_schedule_registry(f)
        dep = reg.get("test-flow")
        assert dep is not None
        assert dep.concurrency_limit == 1
        assert dep.enabled is False
        assert dep.run_on_startup is True
        assert dep.execution_modes == ["sensor", "startup"]
        assert dep.depends_on == ["upstream-a", "upstream-b"]
        assert dep.parameters["days"] == 7

    def test_raises_value_error_on_unsupported_concurrency_limit(self, tmp_path):
        f = tmp_path / "schedules.json"
        _write_schedules(f, [_minimal_deployment(concurrency_limit=2)])

        with pytest.raises(ValueError, match="Unsupported concurrency_limit"):
            load_schedule_registry(f)

    def test_raises_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no config/schedules.json in tmp_path
        with pytest.raises(FileNotFoundError, match="Schedule config not found"):
            load_schedule_registry(tmp_path / "no_such.json")

    def test_raises_value_error_on_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "bad.json"
        f.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_schedule_registry(f)

    def test_raises_value_error_on_empty_deployments(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "empty.json"
        _write_schedules(f, [])
        with pytest.raises(ValueError, match="no 'deployments' entries"):
            load_schedule_registry(f)

    def test_raises_value_error_on_unknown_dependency(self, tmp_path):
        f = tmp_path / "schedules.json"
        _write_schedules(
            f,
            [
                _minimal_deployment(name="upstream-a"),
                _minimal_deployment(
                    name="downstream-b",
                    depends_on=["missing-upstream"],
                ),
            ],
        )

        with pytest.raises(ValueError, match="depends_on unknown deployment"):
            load_schedule_registry(f)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestScheduleRegistrySingleton:
    def test_singleton_loaded(self):
        assert len(schedule_registry) > 0

    def test_all_have_names(self):
        for dep in schedule_registry:
            assert dep.name
            assert dep.cron

    def test_repairs_are_manual_and_catchups_own_automation(self):
        repairs = [dep for dep in schedule_registry if "repair" in dep.tags]
        catchups = [dep for dep in schedule_registry if "catchup" in dep.tags]

        assert repairs
        assert catchups

        for dep in repairs:
            assert dep.run_on_startup is False
            assert dep.has_execution_mode("manual")
            assert not dep.has_execution_mode("startup")

        sensor_owned = {
            dep.name for dep in catchups if dep.has_execution_mode("sensor")
        }
        assert sensor_owned == {"birdweather-catchup", "historical-catchup"}

        bundle_sync = schedule_registry.get("birdweather-bundle-sync")
        assert bundle_sync is not None
        assert bundle_sync.has_execution_mode("sensor")
        assert bundle_sync.run_on_startup is False

        birdweather = schedule_registry.get("birdweather-catchup")
        assert birdweather is not None
        assert birdweather.run_on_startup is False
        assert birdweather.has_execution_mode("manual")

        historical = schedule_registry.get("historical-catchup")
        assert historical is not None
        assert historical.run_on_startup is False
        assert historical.has_execution_mode("manual")

        startup_catchups = {
            dep.name
            for dep in schedule_registry.startup_deployments
            if "catchup" in dep.tags
        }
        assert startup_catchups == set()
