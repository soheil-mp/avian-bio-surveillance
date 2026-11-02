"""Drop-in tqdm replacement that degrades gracefully in non-TTY environments.

In Docker containers (and other non-interactive pipes) tqdm's ``\\r``-based
progress bar produces one line per update, flooding the log with thousands of
nearly identical lines.  This module exposes a ``tqdm`` callable with the same
API: when ``sys.stderr`` is a real terminal it delegates to the upstream tqdm;
otherwise it emits a compact log line every *N* iterations (default: 10 % of
total, minimum interval 30 s).
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterable, Iterator
from typing import Any, TypeVar

from tqdm import tqdm as _real_tqdm


__all__ = ["tqdm"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_LOG_MIN_INTERVAL_S = 30
"""Minimum seconds between log lines in non-TTY mode."""


class _LogProgressBar:
    """Lightweight progress tracker that writes to the Python logger."""

    def __init__(
        self,
        iterable: Iterable[T] | None = None,
        *,
        desc: str = "",
        total: int | None = None,
        unit: str = "it",
        **_kwargs: Any,
    ) -> None:
        self._iterable = iterable
        self._desc = desc or "Progress"
        self._total = (
            total
            if total is not None
            else (
                len(iterable)
                if iterable is not None and hasattr(iterable, "__len__")
                else None  # type: ignore[arg-type]
            )
        )
        self._unit = unit
        self._n = 0
        self._start = time.monotonic()
        self._last_log = 0.0
        self._log_step = max(1, (self._total // 10)) if self._total else 50

    @property
    def n(self) -> int:
        """Current iteration count (matches tqdm.n)."""
        return self._n

    @n.setter
    def n(self, value: int) -> None:
        self._n = value

    # -- context manager (used with `with tqdm(...) as pbar:`) ----------------

    def __enter__(self) -> _LogProgressBar:
        self._start = time.monotonic()
        self._log_start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._log_done()

    # -- iterator (used as `for x in tqdm(items):`) ---------------------------

    def __iter__(self) -> Iterator[T]:
        self._start = time.monotonic()
        self._log_start()
        if self._iterable is None:
            return
        for item in self._iterable:
            yield item
            self._n += 1
            self._maybe_log()
        self._log_done()

    # -- manual update (`pbar.update(n)`) -------------------------------------

    def update(self, n: int = 1) -> None:
        self._n += n
        self._maybe_log()

    def set_postfix(self, *_args: Any, **_kwargs: Any) -> None:
        pass  # silently ignore — no bar to decorate

    def set_description(self, desc: str | None = None, **_kw: Any) -> None:
        if desc:
            self._desc = desc

    def close(self) -> None:
        pass

    # -- internal -------------------------------------------------------------

    def _maybe_log(self) -> None:
        now = time.monotonic()
        if (
            self._n % self._log_step == 0
            and (now - self._last_log) >= _LOG_MIN_INTERVAL_S
        ):
            self._last_log = now
            elapsed = now - self._start
            rate = self._n / elapsed if elapsed > 0 else 0
            if self._total:
                pct = self._n * 100 // self._total
                logger.info(
                    "%s: %d/%d %s (%d%%) [%.0fs, %.1f %s/s]",
                    self._desc,
                    self._n,
                    self._total,
                    self._unit,
                    pct,
                    elapsed,
                    rate,
                    self._unit,
                )
            else:
                logger.info(
                    "%s: %d %s [%.0fs, %.1f %s/s]",
                    self._desc,
                    self._n,
                    self._unit,
                    elapsed,
                    rate,
                    self._unit,
                )

    def _log_start(self) -> None:
        if self._total:
            logger.info("%s: starting (%d %s)", self._desc, self._total, self._unit)
        else:
            logger.info("%s: starting", self._desc)

    def _log_done(self) -> None:
        elapsed = time.monotonic() - self._start
        logger.info(
            "%s: done — %d %s in %.1fs",
            self._desc,
            self._n,
            self._unit,
            elapsed,
        )


def tqdm(iterable: Iterable[T] | None = None, **kwargs: Any) -> Any:
    """Smart tqdm: real progress bar on TTY, periodic log lines otherwise."""
    if sys.stderr.isatty():
        return _real_tqdm(iterable, **kwargs)
    return _LogProgressBar(iterable, **kwargs)
