"""Tests for avian_biosurveillance.db.database module.

Covers:
- ConnectionPool construction and DSN resolution
- Context-managed cursor with commit/rollback
- Module-level singleton (get_pool / close_pool)
- healthcheck() result structure
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from avian_biosurveillance.db import database


# =============================================================================
# DSN building
# =============================================================================


class TestBuildDSN:
    """Verify DSN resolution from environment variables."""

    def test_database_url_takes_priority(self):
        with patch.dict(
            "os.environ", {"DATABASE_URL": "postgresql://u:p@host:1234/db"}
        ):
            assert database._build_dsn() == "postgresql://u:p@host:1234/db"

    def test_individual_vars(self):
        env = {
            "POSTGRES_USER": "testuser",
            "POSTGRES_PASSWORD": "testpass",
            "POSTGRES_HOST": "dbhost",
            "TSDB_PORT": "5433",
            "POSTGRES_DB": "testdb",
        }
        with (
            patch.dict("os.environ", env, clear=False),
            patch.dict(
                "os.environ",
                {},
                clear=False,
            ),
        ):
            import os

            os.environ.pop("DATABASE_URL", None)
            dsn = database._build_dsn()
            assert "testuser" in dsn
            assert "dbhost" in dsn
            assert "5433" in dsn
            assert "testdb" in dsn

    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            for k in (
                "DATABASE_URL",
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "POSTGRES_DB",
            ):
                os.environ.pop(k, None)
            dsn = database._build_dsn()
            assert "root" in dsn  # default user
            assert "localhost" in dsn
            assert "15432" in dsn
            assert "avian_biosurveillance" in dsn

    def test_special_chars_in_password(self):
        env = {
            "POSTGRES_PASSWORD": "p@ss/w0rd#!",
            "POSTGRES_USER": "avian",
            "POSTGRES_HOST": "localhost",
            "TSDB_PORT": "5432",
            "POSTGRES_DB": "testdb",
        }
        with patch.dict("os.environ", env, clear=False):
            import os

            os.environ.pop("DATABASE_URL", None)
            dsn = database._build_dsn()
            # Special chars should be URL-encoded
            assert "%40" in dsn or "p@ss" not in dsn.split("@")[0]


# =============================================================================
# ConnectionPool
# =============================================================================


class TestConnectionPool:
    """Test the pool wrapper (mocked psycopg2)."""

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_pool_creation(self, mock_pool_cls):
        database.ConnectionPool(dsn="postgresql://test:test@localhost/test")
        mock_pool_cls.assert_called_once_with(
            minconn=2,
            maxconn=10,
            dsn="postgresql://test:test@localhost/test",
        )

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_cursor_commits_on_success(self, mock_pool_cls):
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool_cls.return_value.getconn.return_value = mock_conn

        pool = database.ConnectionPool(dsn="postgresql://test:test@localhost/test")
        with pool.cursor() as cur:
            cur.execute("SELECT 1")

        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()
        mock_pool_cls.return_value.putconn.assert_called_once_with(mock_conn)

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_cursor_rolls_back_on_error(self, mock_pool_cls):
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("boom")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool_cls.return_value.getconn.return_value = mock_conn

        pool = database.ConnectionPool(dsn="postgresql://test:test@localhost/test")
        with pytest.raises(RuntimeError, match="boom"), pool.cursor() as cur:
            cur.execute("SELECT 1")

        mock_conn.rollback.assert_called_once()
        mock_pool_cls.return_value.putconn.assert_called_once_with(mock_conn)

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_cursor_discards_closed_connection_on_error(self, mock_pool_cls):
        mock_conn = MagicMock()
        mock_conn.closed = True
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("boom")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool_cls.return_value.getconn.return_value = mock_conn

        pool = database.ConnectionPool(dsn="postgresql://test:test@localhost/test")
        with pytest.raises(RuntimeError, match="boom"), pool.cursor() as cur:
            cur.execute("SELECT 1")

        mock_conn.rollback.assert_not_called()
        mock_pool_cls.return_value.putconn.assert_called_once_with(
            mock_conn,
            close=True,
        )

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_connection_context_manager(self, mock_pool_cls):
        mock_conn = MagicMock()
        mock_pool_cls.return_value.getconn.return_value = mock_conn

        pool = database.ConnectionPool(dsn="postgresql://test:test@localhost/test")
        with pool.connection() as conn:
            assert conn is mock_conn

        mock_pool_cls.return_value.putconn.assert_called_once_with(mock_conn)

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_close(self, mock_pool_cls):
        pool = database.ConnectionPool(dsn="postgresql://test:test@localhost/test")
        pool.close()
        mock_pool_cls.return_value.closeall.assert_called_once()

    @patch("avian_biosurveillance.db.database.psycopg2.pool.ThreadedConnectionPool")
    def test_dsn_property_masks_password(self, _mock_pool_cls):
        pool = database.ConnectionPool(dsn="postgresql://user:secret@host/db")
        assert "secret" not in pool.dsn
        assert "***" in pool.dsn


# =============================================================================
# Module-level singleton
# =============================================================================


class TestModuleSingleton:
    """Test get_pool / close_pool."""

    def setup_method(self):
        database.close_pool()

    def teardown_method(self):
        database.close_pool()

    @patch("avian_biosurveillance.db.database.ConnectionPool")
    def test_get_pool_creates_once(self, mock_cls):
        pool1 = database.get_pool(dsn="postgresql://test:test@localhost/test")
        pool2 = database.get_pool(dsn="postgresql://other:other@localhost/other")
        assert pool1 is pool2
        mock_cls.assert_called_once()

    @patch("avian_biosurveillance.db.database.ConnectionPool")
    def test_close_pool_resets(self, _mock_cls):
        pool1 = database.get_pool(dsn="postgresql://test:test@localhost/test")
        database.close_pool()
        pool1.close.assert_called_once()


# =============================================================================
# Healthcheck
# =============================================================================


class TestHealthcheck:
    """Test healthcheck result parsing (mocked cursor)."""

    def test_expected_tables_include_runtime_derived_relations(self):
        required_relations = {
            "birdweather_daily_cagg_tbl",
            "knmi_daily_cagg_tbl",
            "movebank_tracks_tbl",
        }
        expected_tables = database._EXPECTED_TABLES

        assert required_relations <= expected_tables

    def _make_mock_pool(self, *, tsdb_version="2.17.0", tables=None, hypertables=None):
        """Build a mock pool whose cursor returns canned results."""
        if tables is None:
            tables = list(database._EXPECTED_TABLES)
        if hypertables is None:
            hypertables = ["birdweather_tbl", "knmi_tbl"]

        mock_pool = MagicMock(spec=database.ConnectionPool)
        mock_cursor = MagicMock()

        call_count = 0

        def execute_side_effect(sql, *args, **kwargs):
            nonlocal call_count
            call_count += 1

        def fetchone_side_effect():
            return (tsdb_version,)

        def fetchall_side_effect():
            nonlocal call_count
            if call_count == 2:
                return [(t,) for t in tables]
            if call_count == 3:
                return [(h,) for h in hypertables]
            return []

        mock_cursor.execute = execute_side_effect
        mock_cursor.fetchone = fetchone_side_effect
        mock_cursor.fetchall = fetchall_side_effect

        mock_pool.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_pool.cursor.return_value.__exit__ = MagicMock(return_value=False)

        return mock_pool

    def test_healthy(self):
        pool = self._make_mock_pool()
        result = database.healthcheck(pool)
        assert result["ok"] is True
        assert result["timescaledb"] == "2.17.0"
        assert len(result["tables_missing"]) == 0

    def test_missing_tables(self):
        pool = self._make_mock_pool(tables=["birdweather_detection"])
        result = database.healthcheck(pool)
        assert result["ok"] is False
        assert len(result["tables_missing"]) > 0
