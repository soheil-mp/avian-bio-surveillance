"""Runtime path helpers for safe temporary-file placement.

These helpers prefer project-local temporary directories configured by the
deployment wrapper (for example Singularity's ``TMPDIR=/app/data/tmp``) and
only fall back to the platform default when no explicit safe temp root is
available.
"""

from __future__ import annotations

import os
from pathlib import Path


def preferred_temp_dir() -> str | None:
    """Return a writable temp directory, preferring deployment-local paths.

    Resolution order:

    1. ``TMPDIR``
    2. ``TEMP``
    3. ``TMP``
    4. ``/app/data/tmp`` when running in the containerized app layout

    Returns ``None`` when no explicit safe directory can be determined, letting
    the caller fall back to the standard library's default temp handling.
    """
    for env_var in ("TMPDIR", "TEMP", "TMP"):
        candidate = os.environ.get(env_var)
        if candidate:
            path = Path(candidate).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

    if os.name != "nt":
        app_root = Path("/app")
        if app_root.exists():
            path = app_root / "data" / "tmp"
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

    return None
