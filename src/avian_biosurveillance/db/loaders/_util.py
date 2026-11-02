"""Shared helpers for bulk-upsert loaders."""

from collections.abc import Generator, Sequence
from typing import TypeVar


T = TypeVar("T")


def chunked(seq: Sequence[T], size: int) -> Generator[Sequence[T], None, None]:
    """Yield successive *size*-length slices of *seq*."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
