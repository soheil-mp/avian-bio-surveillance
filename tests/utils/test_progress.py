"""Tests for avian_biosurveillance.utils.progress."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

from avian_biosurveillance.utils.progress import _LogProgressBar, tqdm


class TestLogProgressBarInit:
    def test_with_list(self):
        items = [1, 2, 3]
        bar = _LogProgressBar(items, desc="test", total=3)
        assert bar._total == 3
        assert bar._desc == "test"

    def test_total_inferred_from_len(self):
        items = list(range(10))
        bar = _LogProgressBar(items)
        assert bar._total == 10

    def test_no_iterable(self):
        bar = _LogProgressBar(desc="manual")
        assert bar._iterable is None
        assert bar._total is None

    def test_n_property(self):
        bar = _LogProgressBar()
        assert bar.n == 0
        bar.n = 5
        assert bar.n == 5


class TestLogProgressBarIteration:
    def test_iterates_all_items(self):
        items = [10, 20, 30]
        result = list(_LogProgressBar(items, desc="test"))
        assert result == items

    def test_increments_n(self):
        items = list(range(5))
        bar = _LogProgressBar(items, desc="test")
        for _ in bar:
            pass
        assert bar.n == 5

    def test_empty_iterable(self):
        result = list(_LogProgressBar([], desc="empty"))
        assert result == []

    def test_logs_start_and_done(self, caplog):
        with caplog.at_level(logging.INFO):
            list(_LogProgressBar([1, 2], desc="iter_test", total=2))
        messages = [r.message for r in caplog.records]
        assert any("starting" in m for m in messages)
        assert any("done" in m for m in messages)


class TestLogProgressBarContextManager:
    def test_enter_exit(self, caplog):
        with caplog.at_level(logging.INFO):
            with _LogProgressBar(desc="ctx_test", total=5) as bar:
                bar.update(3)
                bar.update(2)
        messages = [r.message for r in caplog.records]
        assert any("starting" in m for m in messages)
        assert any("done" in m for m in messages)


class TestLogProgressBarUpdate:
    def test_update_increments_n(self):
        bar = _LogProgressBar(desc="test", total=100)
        bar.update(10)
        assert bar.n == 10
        bar.update(5)
        assert bar.n == 15

    def test_update_default_one(self):
        bar = _LogProgressBar(desc="test", total=10)
        bar.update()
        assert bar.n == 1


class TestLogProgressBarMisc:
    def test_set_postfix_is_noop(self):
        bar = _LogProgressBar(desc="test")
        bar.set_postfix(ordered=True, key="val")  # Should not raise

    def test_set_description(self):
        bar = _LogProgressBar(desc="old")
        bar.set_description("new")
        assert bar._desc == "new"

    def test_set_description_none(self):
        bar = _LogProgressBar(desc="unchanged")
        bar.set_description(None)
        assert bar._desc == "unchanged"

    def test_close_is_noop(self):
        bar = _LogProgressBar(desc="test")
        bar.close()  # Should not raise


class TestMaybeLog:
    def test_logs_at_step_boundary_after_interval(self, caplog):
        bar = _LogProgressBar(desc="log_test", total=10)
        bar._log_step = 1
        bar._start = time.monotonic() - 31  # fake 31s elapsed
        bar._last_log = 0.0
        with caplog.at_level(logging.INFO):
            bar._n = 1
            bar._maybe_log()
        assert any("log_test" in r.message for r in caplog.records)

    def test_skips_log_within_min_interval(self, caplog):
        bar = _LogProgressBar(desc="no_log", total=10)
        bar._log_step = 1
        bar._start = time.monotonic()
        bar._last_log = time.monotonic()  # just logged
        with caplog.at_level(logging.INFO):
            bar._n = 1
            bar._maybe_log()
        assert not any("no_log" in r.message for r in caplog.records)


class TestTqdmWrapper:
    def test_returns_real_tqdm_on_tty(self):
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = True
        with patch("avian_biosurveillance.utils.progress.sys") as mock_sys:
            mock_sys.stderr = mock_stderr
            result = tqdm([1, 2, 3], desc="test")
        # Real tqdm — not our wrapper
        assert not isinstance(result, _LogProgressBar)

    def test_returns_log_bar_on_non_tty(self):
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = False
        with patch("avian_biosurveillance.utils.progress.sys") as mock_sys:
            mock_sys.stderr = mock_stderr
            result = tqdm([1, 2, 3], desc="test")
        assert isinstance(result, _LogProgressBar)
