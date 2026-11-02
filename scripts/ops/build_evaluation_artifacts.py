"""Regenerate modeling evaluation reports from the latest persisted DB run."""

from __future__ import annotations

import argparse
import logging

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.modeling import (
    load_latest_modeling_run,
    load_score_payload_frame,
    update_modeling_validation_report,
)
from avian_biosurveillance.modeling.anomaly_model import (
    BehaviorAnomalyModelConfig,
    score_behavior_anomalies_walk_forward,
)
from avian_biosurveillance.modeling.evaluation import build_validation_report
from avian_biosurveillance.modeling.settings import SETTINGS


logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild the validation report from the latest persisted modeling "
            "run."
        ),
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Specific modeling run id. Defaults to the latest complete anomaly run.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=25,
        help="Top-k cutoff for precision@k metrics.",
    )
    parser.add_argument(
        "--scoring-mode",
        choices=("retrospective", "walk_forward"),
        default="retrospective",
        help=(
            "retrospective: evaluate scores as stored. walk_forward: re-score "
            "the fusion prospectively from existing feature columns before "
            "evaluating."
        ),
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        run = (
            {"run_id": args.run_id}
            if args.run_id
            else load_latest_modeling_run(pool=pool)
        )
        if not run:
            raise SystemExit("No persisted anomaly-scoring run found.")
        run_id = str(run["run_id"])
        scored = load_score_payload_frame(run_id=run_id, pool=pool)
        if scored.empty:
            raise SystemExit(f"Run {run_id} has no persisted score rows.")

        if args.scoring_mode == "walk_forward":
            logger.info("Re-scoring run %s prospectively (%d rows).", run_id, len(scored))
            scored = score_behavior_anomalies_walk_forward(
                scored,
                config=BehaviorAnomalyModelConfig(
                    min_model_rows=SETTINGS.anomaly_model["min_model_rows"],
                    contamination=SETTINGS.anomaly_model["contamination"],
                    random_state=SETTINGS.anomaly_model["random_state"],
                    amber_quantile=SETTINGS.anomaly_model["amber_quantile"],
                    red_quantile=SETTINGS.anomaly_model["red_quantile"],
                ),
            )

        logger.info("Building validation report for run %s (%d rows).", run_id, len(scored))
        report = {
            "run_id": run_id,
            "scoring_mode": args.scoring_mode,
            "validation": build_validation_report(scored, top_k=args.top_k),
        }
        update_modeling_validation_report(run_id=run_id, validation_report=report, pool=pool)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
