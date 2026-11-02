"""Unified logging helpers for ingestion & orchestration.

Provides compact, scannable log-message formatters that all modules
can use for a consistent visual style in Docker / Dagster output.

The Dagster task runner already prepends timestamps and levels, so these
helpers only format the *message body*.  Callers use their own
:class:`logging.Logger`::

    from avian_biosurveillance.utils.log_utils import fmt

    logger.info(fmt.phase("INGESTION"))
    logger.info(fmt.start("birdweather", "last 24 h, 15 species"))
    logger.info(fmt.done("birdweather", "1 234 detections", 12.3))
"""

from __future__ import annotations


__all__ = ["fmt"]

_W = 60  # line width for phase headers


class _Fmt:
    """Namespace of message-formatting helpers."""

    @staticmethod
    def phase(label: str) -> str:
        """Compact phase header: ``─── LABEL ──────────────``."""
        tag = f" {label} "
        pad = max(0, _W - 4 - len(tag))
        return f"─── {tag}{'─' * pad}"

    @staticmethod
    def start(source: str, detail: str = "") -> str:
        """Source starting: ``▸ source       │ detail``."""
        msg = f"▸ {source:<18s}│ {detail}" if detail else f"▸ {source}"
        return msg

    @staticmethod
    def done(source: str, detail: str = "", elapsed_s: float | None = None) -> str:
        """Source completed: ``✓ source       │ detail (12.3s)``."""
        time_str = f" ({elapsed_s:.1f}s)" if elapsed_s is not None else ""
        if detail:
            return f"✓ {source:<18s}│ {detail}{time_str}"
        return f"✓ {source}{time_str}"

    @staticmethod
    def fail(source: str, detail: str = "") -> str:
        """Source failed: ``✗ source       │ detail``."""
        if detail:
            return f"✗ {source:<18s}│ {detail}"
        return f"✗ {source}"

    @staticmethod
    def skip(source: str, reason: str = "") -> str:
        """Source skipped: ``– source       │ reason``."""
        if reason:
            return f"– {source:<18s}│ {reason}"
        return f"– {source}"

    @staticmethod
    def kv(key: str, value: object) -> str:
        """Key-value detail line: ``  key:  value``."""
        return f"  {key + ':':<16s}{value}"

    @staticmethod
    def summary_line(items: list[tuple[str, object]]) -> str:
        """Inline summary: ``key1=val1  key2=val2  …``."""
        return "  ".join(f"{k}={v}" for k, v in items)


fmt = _Fmt()
