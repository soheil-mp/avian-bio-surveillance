"""Sovon Mortality Data Flow.

Two-phase pipeline:

1. **Download** — fetch bar-chart images from
   `portal.sovon.nl <https://portal.sovon.nl/>`_.
2. **Extract** — parse charts via a HuggingFace vision model into
   structured mortality rows and persist to ``sovon_tbl``.

Runs daily as part of the daily pipeline.  The download step is a no-op
when no new charts are available.  The standalone flow remains
importable for ad-hoc / manual runs.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import date

from avian_biosurveillance.ingestion.sovon import (
    download_charts as sovon_download_charts,
)
from avian_biosurveillance.ingestion.sovon import (
    extract_data as sovon_extract_data,
)


logger = logging.getLogger(__name__)


def _sovon_run_name() -> str:
    """Generate run name: ``sovon-ingest/YYYY-MM-DD``."""
    return f"sovon-ingest/{date.today().isoformat()}"


def download_sovon_charts(
    target: str = "database",
) -> None:
    """Download all mortality bar-chart images from the Sovon portal.

    Database-target runs reuse the default raw chart directory so later
    runs can skip already-downloaded images instead of re-fetching the
    entire historical corpus.
    """
    logger.info("Sovon download: starting (target=%s).", target)

    if target == "database":
        logger.info(
            "Sovon DB target: reusing the persistent raw chart cache to avoid "
            "full historical re-downloads."
        )

    sovon_download_charts()  # type: ignore[no-untyped-call]
    logger.info("Sovon download: complete.")


def extract_sovon_data(
    target: str = "database",
    stage_root: str | None = None,
) -> None:
    """Extract mortality counts from chart images via a vision model.

    Processes downloaded bar-chart images using a HuggingFace
    vision-language model to extract structured mortality data
    (species, date, count) and persists the results.

    Args:
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    logger.info("Sovon extraction: starting (target=%s).", target)
    raw_dir = None
    processed_dir = None
    cache_dir = None
    cleanup_stage_root = stage_root if target == "database" and stage_root else None
    if cleanup_stage_root:
        raw_dir = os.path.join(cleanup_stage_root, "raw")
        processed_dir = os.path.join(cleanup_stage_root, "processed")
        cache_dir = os.path.join(cleanup_stage_root, "cache")

    try:
        sovon_extract_data(
            target=target,
            raw_dir=raw_dir,
            processed_dir=processed_dir,
            cache_dir=cache_dir,
        )
        logger.info("Sovon extraction: complete.")
    finally:
        if cleanup_stage_root:
            shutil.rmtree(cleanup_stage_root, ignore_errors=True)
            logger.info(
                "Removed temporary Sovon staging directory %s.",
                cleanup_stage_root,
            )


def run_sovon_flow(
    target: str = "database",
) -> None:
    """Sovon mortality pipeline: download then extract."""
    download_sovon_charts(target=target)
    extract_sovon_data(target=target)
