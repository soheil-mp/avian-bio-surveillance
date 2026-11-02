"""Tests for privileged station summary refresh orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from avian_biosurveillance.orchestration.station_summary_flow import (
    _STATION_SUMMARY_DB_HOST_ENV,
    _STATION_SUMMARY_DB_NAME_ENV,
    _STATION_SUMMARY_DB_PASSWORD_ENV,
    _STATION_SUMMARY_DB_PORT_ENV,
    _STATION_SUMMARY_DB_USER_ENV,
    refresh_station_summary_task,
)


class TestStationSummaryRefreshTask:
    @patch("avian_biosurveillance.orchestration.station_summary_flow.ConnectionPool")
    def test_uses_maintenance_dsn_when_configured(
        self,
        mock_pool: MagicMock,
        monkeypatch: MagicMock,
    ) -> None:
        monkeypatch.setenv(_STATION_SUMMARY_DB_USER_ENV, "root")
        monkeypatch.setenv(_STATION_SUMMARY_DB_PASSWORD_ENV, "s3cr@t/with;chars")
        monkeypatch.setenv(_STATION_SUMMARY_DB_HOST_ENV, "timescaledb")
        monkeypatch.setenv(_STATION_SUMMARY_DB_PORT_ENV, "5432")
        monkeypatch.setenv(
            _STATION_SUMMARY_DB_NAME_ENV,
            "avian_biosurveillance",
        )

        cursor = MagicMock()
        connection = MagicMock()
        connection.autocommit = False
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        pool = MagicMock()
        pool.connection.return_value.__enter__.return_value = connection
        pool.connection.return_value.__exit__.return_value = False
        mock_pool.return_value = pool

        refresh_station_summary_task()

        mock_pool.assert_called_once_with(
            dsn=(
                "postgresql://root:s3cr%40t%2Fwith%3Bchars@timescaledb:5432/"
                "avian_biosurveillance"
            ),
            min_conn=1,
            max_conn=2,
        )
        assert (
            cursor.execute.call_args_list[0]
            .args[0]
            .startswith("CALL refresh_continuous_aggregate")
        )
        assert cursor.execute.call_args_list[1].args[0] == (
            "SELECT refresh_station_summary()"
        )
        assert connection.autocommit is False
        pool.close.assert_called_once()
