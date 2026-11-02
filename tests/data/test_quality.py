"""Focused regression tests for avian_biosurveillance.data.quality."""

from __future__ import annotations

from avian_biosurveillance.data.quality import (
    QualityReport,
    _assess_completeness,
    _assess_consistency,
)


def test_audio_start_before_end_flags_equal_timestamps(monkeypatch) -> None:
    """Equal audio start/end timestamps should be counted as invalid."""

    def fake_scalar(_cur, sql: str, params: tuple = ()) -> int:
        del params
        normalized_sql = " ".join(sql.split())
        if "WHERE audio_start_time_s IS NOT NULL AND audio_end_time_s IS NOT NULL" in normalized_sql:
            if "audio_start_time_s >= audio_end_time_s" in normalized_sql:
                return 2
            return 10
        if normalized_sql == "SELECT COUNT(*) FROM birdweather_tbl":
            return 0
        return 0

    monkeypatch.setattr(
        "avian_biosurveillance.data.quality._scalar",
        fake_scalar,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.data.quality._rows",
        lambda _cur, _sql, params=(): [],
    )

    report = QualityReport()

    _assess_consistency(cur=object(), report=report)

    metric = next(
        m for m in report.metrics if m.metric == "audio_start_before_end_pct"
    )
    assert metric.value == 80.0
    assert metric.status == "WARN"
    assert metric.detail == "2 rows with start >= end"


def test_target_species_and_volume_trend_are_info(monkeypatch) -> None:
    """Readiness-style BirdWeather completeness metrics should not affect scores."""

    def fake_scalar(_cur, sql: str, params: tuple = ()) -> int:
        del params
        normalized_sql = " ".join(sql.split())
        if normalized_sql == "SELECT COUNT(*) FROM birdweather_tbl":
            return 10
        if normalized_sql.startswith("SELECT COUNT(") and "FROM birdweather_tbl" in normalized_sql:
            return 10
        if "SELECT COUNT(DISTINCT b.species_id)" in normalized_sql:
            return 1
        return 0

    def fake_rows(_cur, sql: str, params: tuple = ()):
        del params
        normalized_sql = " ".join(sql.split())
        if "SELECT MIN(observation_date), MAX(observation_date), COUNT(DISTINCT observation_date) FROM birdweather_tbl" in normalized_sql:
            from datetime import date

            return [(date(2026, 1, 1), date(2026, 1, 10), 10)]
        if "SELECT (SELECT * FROM recent), (SELECT * FROM baseline)" in normalized_sql:
            return [(2.0, 4.0)]
        if "SELECT species_id, COUNT(*) FROM birdweather_tbl GROUP BY species_id" in normalized_sql:
            return [("sp1", 10)]
        return []

    monkeypatch.setattr(
        "avian_biosurveillance.data.quality._scalar",
        fake_scalar,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.data.quality._rows",
        fake_rows,
    )

    report = QualityReport()

    _assess_completeness(cur=object(), report=report)

    target_metric = next(
        m for m in report.metrics if m.metric == "target_species_coverage"
    )
    trend_metric = next(
        m for m in report.metrics if m.metric == "volume_trend_pct"
    )

    assert target_metric.status == "INFO"
    assert trend_metric.status == "INFO"


def test_consistency_adds_fk_coverage_metrics(monkeypatch) -> None:
    """BirdWeather consistency checks should report explicit FK coverage."""

    def fake_scalar(_cur, sql: str, params: tuple = ()) -> int:
        del params
        normalized_sql = " ".join(sql.split())
        if normalized_sql == "SELECT COUNT(*) FROM birdweather_tbl":
            return 10
        if "JOIN birdweather_stations_tbl s ON s.station_id = b.station_id" in normalized_sql:
            return 9
        if "JOIN species_tbl sp ON sp.species_id = b.species_id" in normalized_sql:
            return 8
        if "WHERE audio_start_time_s IS NOT NULL" in normalized_sql:
            return 0
        return 0

    monkeypatch.setattr(
        "avian_biosurveillance.data.quality._scalar",
        fake_scalar,
    )
    monkeypatch.setattr(
        "avian_biosurveillance.data.quality._rows",
        lambda _cur, _sql, params=(): [],
    )

    report = QualityReport()

    _assess_consistency(cur=object(), report=report)

    station_metric = next(
        m for m in report.metrics if m.metric == "detections_with_valid_station_fk_pct"
    )
    species_metric = next(
        m for m in report.metrics if m.metric == "detections_with_valid_species_fk_pct"
    )

    assert station_metric.value == 90.0
    assert station_metric.status == "WARN"
    assert species_metric.value == 80.0
    assert species_metric.status == "WARN"