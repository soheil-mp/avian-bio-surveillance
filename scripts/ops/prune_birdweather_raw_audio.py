#!/usr/bin/env python3
"""Compatibility wrapper for the mounted BirdWeather raw-audio prune module."""

# ruff: noqa: E402

from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.ops.prune_birdweather_raw_audio import logger, main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("BirdWeather raw-audio prune failed.")
        raise