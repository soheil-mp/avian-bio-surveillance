"""Tests for avian_biosurveillance.utils.log_utils."""

from __future__ import annotations

from avian_biosurveillance.utils.log_utils import fmt


class TestPhase:
    def test_contains_label(self):
        result = fmt.phase("INGESTION")
        assert "INGESTION" in result

    def test_starts_with_dashes(self):
        result = fmt.phase("X")
        assert result.startswith("───")

    def test_long_label_no_error(self):
        # Should not raise even if label is longer than line width
        result = fmt.phase("A" * 100)
        assert "A" * 100 in result


class TestStart:
    def test_with_detail(self):
        result = fmt.start("birdweather", "last 24 h")
        assert "birdweather" in result
        assert "last 24 h" in result
        assert result.startswith("▸")

    def test_without_detail(self):
        result = fmt.start("birdweather")
        assert "birdweather" in result
        assert result.startswith("▸")
        assert "│" not in result

    def test_separator_only_when_detail(self):
        with_detail = fmt.start("src", "detail")
        without_detail = fmt.start("src")
        assert "│" in with_detail
        assert "│" not in without_detail


class TestDone:
    def test_full_args(self):
        result = fmt.done("birdweather", "1234 records", 12.3)
        assert "birdweather" in result
        assert "1234 records" in result
        assert "12.3s" in result
        assert result.startswith("✓")

    def test_no_elapsed(self):
        result = fmt.done("birdweather", "1234 records")
        assert "1234 records" in result
        assert "s)" not in result

    def test_minimal(self):
        result = fmt.done("birdweather")
        assert "birdweather" in result
        assert result.startswith("✓")

    def test_elapsed_zero(self):
        result = fmt.done("src", elapsed_s=0.0)
        assert "0.0s" in result


class TestFail:
    def test_with_detail(self):
        result = fmt.fail("birdweather", "timeout")
        assert "birdweather" in result
        assert "timeout" in result
        assert result.startswith("✗")

    def test_without_detail(self):
        result = fmt.fail("birdweather")
        assert "birdweather" in result
        assert result.startswith("✗")
        assert "│" not in result


class TestSkip:
    def test_with_reason(self):
        result = fmt.skip("ebird", "no new data")
        assert "ebird" in result
        assert "no new data" in result
        assert result.startswith("–")

    def test_without_reason(self):
        result = fmt.skip("ebird")
        assert "ebird" in result
        assert result.startswith("–")
        assert "│" not in result


class TestKv:
    def test_format(self):
        result = fmt.kv("stations", 42)
        assert "stations" in result
        assert "42" in result
        assert result.startswith("  ")

    def test_colon_separator(self):
        result = fmt.kv("key", "value")
        assert ":" in result


class TestSummaryLine:
    def test_single_pair(self):
        result = fmt.summary_line([("records", 100)])
        assert "records=100" in result

    def test_multiple_pairs(self):
        result = fmt.summary_line([("a", 1), ("b", 2)])
        assert "a=1" in result
        assert "b=2" in result

    def test_empty(self):
        result = fmt.summary_line([])
        assert result == ""
