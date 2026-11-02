"""Tests for source-specific repair flow failure semantics."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from avian_biosurveillance.orchestration import earth_engine_flow, ebird_flow, knmi_flow


def test_ebird_repair_flow_raises_when_any_day_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_backfill(obs_date: date, **_kwargs: object) -> None:
        if obs_date == date(2024, 1, 2):
            raise RuntimeError("api down")

    monkeypatch.setattr(ebird_flow, "backfill_ebird_day", fake_backfill)

    with pytest.raises(RuntimeError, match="1 day"):
        ebird_flow.run_ebird_repair_flow(
            start_date="2024-01-01",
            end_date="2024-01-02",
        )


def test_ebird_backfill_day_raises_when_collector_reports_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCollector:
        def collect_historic(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(api_errors=1)

    monkeypatch.setattr(ebird_flow, "EBirdCollector", FakeCollector)

    with pytest.raises(RuntimeError, match="1 API error"):
        ebird_flow.backfill_ebird_day(obs_date=date(2024, 1, 1))


def test_knmi_repair_flow_uses_synchronous_day_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knmi_flow,
        "backfill_knmi_day",
        lambda date, **_kwargs: {
            "date": date.isoformat(),
            "skipped": False,
            "total_observations": 2,
            "station_count": 1,
        },
    )

    summary = knmi_flow.run_knmi_repair_flow(
        start_date="2024-01-01",
        end_date="2024-01-02",
        target="database",
    )

    assert summary["days_ingested"] == 2
    assert summary["days_failed"] == 0
    assert summary["total_observations"] == 4


def test_knmi_repair_flow_raises_when_any_day_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_backfill(date: date, **_kwargs: object) -> dict[str, object]:
        if date.isoformat() == "2024-01-02":
            raise RuntimeError("api down")
        return {
            "date": date.isoformat(),
            "skipped": False,
            "total_observations": 2,
            "station_count": 1,
        }

    monkeypatch.setattr(knmi_flow, "backfill_knmi_day", fake_backfill)

    with pytest.raises(RuntimeError, match="1 day"):
        knmi_flow.run_knmi_repair_flow(
            start_date="2024-01-01",
            end_date="2024-01-02",
            target="database",
        )


def test_knmi_repair_flow_reuses_single_ingestion_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_calls: list[object] = []

    class FakeIngestor:
        def __init__(self, config: object) -> None:
            init_calls.append(config)

        def ingest_day(
            self,
            *,
            date: date,
            target: str,
            force: bool,
        ) -> dict[str, object]:
            return {
                "date": date.isoformat(),
                "skipped": False,
                "total_observations": 2,
                "station_count": 1,
                "target": target,
                "force": force,
            }

    monkeypatch.setattr(knmi_flow, "KNMIConfig", lambda: object())
    monkeypatch.setattr(knmi_flow, "KNMIIngestionManager", FakeIngestor)

    summary = knmi_flow.run_knmi_repair_flow(
        start_date="2024-01-01",
        end_date="2024-01-03",
        target="database",
        force=True,
    )

    assert len(init_calls) == 1
    assert summary["days_ingested"] == 3
    assert summary["days_failed"] == 0


def test_earth_engine_grid_profiles_raise_on_failed_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCollector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def collect_date_range(self, **_kwargs: object) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(locations_profiled=10, locations_failed=1),
            ]

    monkeypatch.setattr(
        earth_engine_flow,
        "EarthEngineGridCollector",
        FakeCollector,
    )

    with pytest.raises(RuntimeError, match="failed"):
        earth_engine_flow.backfill_earth_engine_grid_profiles(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
