"""Helpers for suppressing noisy third-party bootstrap output."""

from __future__ import annotations

import io
import os
import sys
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager


@contextmanager
def suppress_third_party_output(
    *,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
    warning_messages: Iterable[str] = (),
) -> Iterator[None]:
    """Temporarily hide known noisy bootstrap output from third-party runtimes."""

    devnull = None
    saved_streams: list[tuple[int, int]] = []
    try:
        with warnings.catch_warnings():
            for pattern in warning_messages:
                warnings.filterwarnings("ignore", message=pattern)

            devnull = open(os.devnull, "w", encoding="utf-8")
            for stream, enabled in (
                (sys.stdout, capture_stdout),
                (sys.stderr, capture_stderr),
            ):
                if not enabled:
                    continue
                fileno = _safe_fileno(stream)
                if fileno is None:
                    continue
                _flush_stream(stream)
                saved_streams.append((fileno, os.dup(fileno)))
                os.dup2(devnull.fileno(), fileno)

            yield
    finally:
        for fileno, duplicate in reversed(saved_streams):
            os.dup2(duplicate, fileno)
            os.close(duplicate)
        if devnull is not None:
            devnull.close()


def _safe_fileno(stream: object) -> int | None:
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        return None
    try:
        return int(fileno())
    except (io.UnsupportedOperation, OSError, ValueError):
        return None


def _flush_stream(stream: object) -> None:
    flush = getattr(stream, "flush", None)
    if callable(flush):
        try:
            flush()
        except OSError:
            return