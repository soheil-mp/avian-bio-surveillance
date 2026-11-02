#!/usr/bin/env python3
"""CLI entry point for the data-quality audit.

Delegates to :mod:`avian_biosurveillance.data.quality`.

Usage::

    python scripts/ops/data_quality.py
    python scripts/ops/data_quality.py -o data/quality -v
    python scripts/ops/data_quality.py --fail-under 70
"""

from __future__ import annotations

import sys
from pathlib import Path


# Ensure src/ is importable when running the script directly
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.data.quality import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
