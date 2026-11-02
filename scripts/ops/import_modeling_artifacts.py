"""Import legacy filesystem modeling artifacts into Postgres.

This script is intentionally the only application-owned path that reads the old
``data/processed/modeling`` outputs. It is idempotent: each imported run deletes
and replaces its own child rows.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.modeling import (
    persist_modeling_run,
    persist_tuning_leaderboard,
)


logger = logging.getLogger(__name__)

DEFAULT_MODELING_DIR = Path("data/processed/modeling")

_SUMMARY_RE = re.compile(r"^species_week_anomaly_scores_(?P<run>.+)\.json$")
_REPORT_RE = re.compile(r"^species_week_validation_report_(?P<run>.+)\.json$")
_TUNING_RE = re.compile(r"^model_tuning_leaderboard_(?P<run>.+)\.json$")
_SCORE_FILE_PATTERNS = (
    ("species_week_anomaly_scores", "json"),
    ("species_week_anomaly_scores", "csv"),
    ("species_week_features", "csv"),
    ("species_week_embedding_clusters", "csv"),
    ("species_week_validation_report", "json"),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import legacy modeling CSV/JSON artifacts into Postgres.",
    )
    parser.add_argument(
        "--modeling-dir",
        type=Path,
        default=DEFAULT_MODELING_DIR,
        help="Legacy modeling artifact directory.",
    )
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Skipping malformed JSON: %s", path)
        return {}
    return value if isinstance(value, dict) else {}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _latest_or_run_path(
    directory: Path,
    prefix: str,
    run_id: str,
    suffix: str,
    *,
    allow_latest_fallback: bool,
) -> Path:
    run_path = directory / f"{prefix}_{run_id}.{suffix}"
    if run_path.is_file():
        return run_path
    if allow_latest_fallback:
        return directory / f"{prefix}_latest.{suffix}"
    return run_path


def _collect_score_runs(modeling_dir: Path) -> dict[str, bool]:
    """Return run ids and whether each id may use legacy ``latest`` fallbacks."""
    run_ids: dict[str, bool] = {}
    if not modeling_dir.is_dir():
        return {}
    latest_summary_path = modeling_dir / "species_week_anomaly_scores_latest.json"
    latest_summary = _read_json(latest_summary_path)
    latest_run_id = str(latest_summary.get("run_id") or "imported_latest")
    for prefix, suffix in _SCORE_FILE_PATTERNS:
        pattern = f"{prefix}_*.{suffix}"
        regex = re.compile(rf"^{re.escape(prefix)}_(?P<run>.+)\.{suffix}$")
        for path in modeling_dir.glob(pattern):
            match = regex.match(path.name)
            if not match:
                continue
            run_id = match.group("run")
            allow_latest_fallback = run_id == "latest"
            if run_id == "latest":
                run_id = latest_run_id
            run_ids[run_id] = run_ids.get(run_id, False) or allow_latest_fallback
    return dict(sorted(run_ids.items()))


def _import_score_runs(
    *,
    pool: ConnectionPool,
    modeling_dir: Path,
) -> int:
    imported = 0
    for run_id, allow_latest_fallback in _collect_score_runs(modeling_dir).items():
        summary_path = _latest_or_run_path(
            modeling_dir,
            "species_week_anomaly_scores",
            run_id,
            "json",
            allow_latest_fallback=allow_latest_fallback,
        )
        summary = _read_json(summary_path)
        run_id = str(summary.get("run_id") or run_id)
        report = _read_json(
            _latest_or_run_path(
                modeling_dir,
                "species_week_validation_report",
                run_id,
                "json",
                allow_latest_fallback=allow_latest_fallback,
            )
        )
        features = _read_csv(
            _latest_or_run_path(
                modeling_dir,
                "species_week_features",
                run_id,
                "csv",
                allow_latest_fallback=allow_latest_fallback,
            )
        )
        scores = _read_csv(
            _latest_or_run_path(
                modeling_dir,
                "species_week_anomaly_scores",
                run_id,
                "csv",
                allow_latest_fallback=allow_latest_fallback,
            )
        )
        clusters = _read_csv(
            _latest_or_run_path(
                modeling_dir,
                "species_week_embedding_clusters",
                run_id,
                "csv",
                allow_latest_fallback=allow_latest_fallback,
            )
        )
        generated_at = pd.to_datetime(
            summary.get("generated_at"),
            errors="coerce",
            utc=True,
        )
        if pd.isna(generated_at):
            generated = datetime.now(UTC)
        else:
            generated = generated_at.to_pydatetime()
        persist_modeling_run(
            run_id=run_id,
            run_kind="anomaly_scoring",
            generated_at=generated,
            completed_at=generated,
            config=summary.get("config") if isinstance(summary.get("config"), dict) else {},
            stats=summary.get("stats") if isinstance(summary.get("stats"), dict) else {},
            source_coverage=(
                summary.get("source_coverage")
                if isinstance(summary.get("source_coverage"), dict)
                else {}
            ),
            validation_report=report or None,
            features=features,
            scores=scores,
            clusters=clusters,
            pool=pool,
        )
        logger.info("Imported modeling run %s.", run_id)
        imported += 1
    return imported


def _import_tuning_runs(*, pool: ConnectionPool, modeling_dir: Path) -> int:
    if not modeling_dir.is_dir():
        return 0
    imported = 0
    for path in sorted(modeling_dir.glob("model_tuning_leaderboard_*.json")):
        match = _TUNING_RE.match(path.name)
        if not match:
            continue
        run_id = match.group("run")
        payload = _read_json(path)
        if run_id == "latest":
            run_id = str(payload.get("run_id") or "imported_tuning_latest")
        csv_path = modeling_dir / f"model_tuning_leaderboard_{run_id}.csv"
        if not csv_path.is_file():
            csv_path = modeling_dir / "model_tuning_leaderboard_latest.csv"
        leaderboard = _read_csv(csv_path)
        generated_at = pd.to_datetime(payload.get("generated_at"), errors="coerce", utc=True)
        generated = (
            datetime.now(UTC)
            if pd.isna(generated_at)
            else generated_at.to_pydatetime()
        )
        persist_tuning_leaderboard(
            tuning_run_id=run_id,
            generated_at=generated,
            mode=str(payload.get("mode") or ""),
            objective_weights=(
                payload.get("objective_weights")
                if isinstance(payload.get("objective_weights"), dict)
                else {}
            ),
            negative_control_penalty=float(payload.get("negative_control_penalty") or 0.0),
            top_k=int(payload.get("top_k") or 0),
            leaderboard=leaderboard,
            payload=payload,
            pool=pool,
        )
        logger.info("Imported tuning run %s.", run_id)
        imported += 1
    return imported


def import_artifacts(
    *,
    modeling_dir: Path = DEFAULT_MODELING_DIR,
    pool: ConnectionPool,
) -> dict[str, int]:
    """Import all legacy modeling artifact families into Postgres."""
    score_runs = _import_score_runs(pool=pool, modeling_dir=modeling_dir)
    tuning_runs = _import_tuning_runs(pool=pool, modeling_dir=modeling_dir)
    return {
        "score_runs": score_runs,
        "tuning_runs": tuning_runs,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        counts = import_artifacts(modeling_dir=args.modeling_dir, pool=pool)
    finally:
        pool.close()
    logger.info(
        "Import complete: %d score run(s), %d tuning run(s).",
        counts["score_runs"],
        counts["tuning_runs"],
    )


if __name__ == "__main__":
    main()
