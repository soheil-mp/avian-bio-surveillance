#!/usr/bin/env python
"""Phase 1 evaluation fixes (no model change).

Corrects what the evaluation MEASURES so later tuning optimises the right thing:

  1. The reference is SOVON mortality, treated as a continuous monthly burden
     score rather than a point-event label.
  2. PER-SPECIES lead-lag scan with a POSITIVE-LAG-ONLY early-warning metric:
     best Spearman rho at lag >= 0 (anomaly leads or is coincident). The best
     correlation over ALL lags is reported too, but only as a diagnostic -- a
     peak at negative lag (anomaly FOLLOWS the event) is never credited as
     early warning. Convention: target = reference.shift(-lag), so lag > 0 means
     the anomaly leads the reference by that many periods.
  3. THREE signals per species: continuous bioacoustic score, amber+ alert rate,
     red-only alert rate (plus the fusion score for reference).

Read-only against a persisted run. Usage:
    .venv/bin/python scripts/ops/phase1_evaluation.py --run-id <run_id>
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.modeling import load_score_payload_frame
from avian_biosurveillance.modeling.evaluation import (
    _series_correlation,
    _top_decile_mean,
)

MONTHLY_LAGS = tuple(range(-2, 3))     # months; >0 => anomaly leads reference
MIN_PAIRS = 5                          # minimum overlapping points for a rho

# signal label -> (source column, aggregation across cells in a period)
SIGNALS: dict[str, tuple[str, str]] = {
    "bioacoustic_mean": ("embedding_drift_score", "mean"),
    "bioacoustic_topdecile": ("embedding_drift_score", "topdecile"),
    "population_mean": ("population_anomaly_score", "mean"),
    "population_topdecile": ("population_anomaly_score", "topdecile"),
    "amber_plus_rate": ("_amber_plus", "mean"),
    "red_rate": ("_red", "mean"),
    "fusion_mean": ("final_score", "mean"),
}

# Project design split. config/target_species.json (SDD §1.2) lists all 11 as
# the scored set; within it these 4 are the negative-control species and the
# other 7 are targets. Used only for labelling/interpretation here (the model
# scores all 11 identically).
CONTROL_BIRDWEATHER_IDS = {"62", "81", "25", "76"}


def _num(frame: pd.DataFrame, col: str) -> pd.Series:
    if col not in frame.columns:
        return pd.Series(0.0, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[col], errors="coerce").fillna(0.0)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["period_start"] = pd.to_datetime(work["period_start"], errors="coerce").dt.normalize()
    work = work.loc[work["period_start"].notna()].copy()
    work["species_id"] = work["species_id"].astype(str)
    level = work.get("alert_level", pd.Series("", index=work.index)).astype(str)
    work["_amber_plus"] = level.isin(["amber", "red"]).astype(float)
    work["_red"] = (level == "red").astype(float)
    return work


def _agg(series: pd.Series, how: str) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0.0
    if how == "mean":
        return float(s.mean())
    if how == "max":
        return float(s.max())
    if how == "topdecile":
        return _top_decile_mean(s)
    raise ValueError(how)


def period_frame(df_sp: pd.DataFrame, *, grain: str) -> pd.DataFrame:
    """National per-period frame for one species: signals + the reference."""
    work = df_sp.copy()
    if grain == "monthly":
        work["period"] = work["period_start"].dt.to_period("M").dt.to_timestamp()
    else:
        work["period"] = work["period_start"]
    periods = sorted(work["period"].unique())
    rows = []
    for period in periods:
        block = work.loc[work["period"] == period]
        rec: dict[str, Any] = {"period": period, "n_cells": int(len(block))}
        for label, (col, how) in SIGNALS.items():
            rec[label] = _agg(block[col], how) if col in block.columns else 0.0
        # reference
        rec["ref_mortality"] = float(_num(block, "sovon_mortality_score").max())  # monthly burden
        rows.append(rec)
    out = pd.DataFrame(rows).sort_values("period").reset_index(drop=True)
    return out


def lead_lag(signal: pd.Series, reference: pd.Series, lags) -> list[dict[str, Any]]:
    """rho(signal_t, reference_{t+lag}); lag>0 => signal leads reference."""
    sig = signal.reset_index(drop=True)
    ref = reference.reset_index(drop=True)
    rows = []
    for lag in lags:
        target = ref.shift(-lag)
        mask = sig.notna() & target.notna()
        n = int(mask.sum())
        rho = (
            _series_correlation(sig[mask], target[mask], method="spearman")
            if n >= MIN_PAIRS
            else None
        )
        rows.append({"lag": int(lag), "rho": rho, "n_pairs": n})
    return rows


def summarize_scan(scan: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [(r["lag"], r["rho"]) for r in scan if r["rho"] is not None]
    pos = [(lag, rho) for lag, rho in valid if lag >= 0]
    best_pos = max(pos, key=lambda x: x[1]) if pos else (None, None)
    best_any = max(valid, key=lambda x: x[1]) if valid else (None, None)
    lag0 = next((r["rho"] for r in scan if r["lag"] == 0), None)
    return {
        "early_warning_rho": best_pos[1],          # POSITIVE-LAG-ONLY headline
        "early_warning_lag": best_pos[0],
        "lag0_rho": lag0,
        "diagnostic_best_rho_any_lag": best_any[1],
        "diagnostic_best_lag": best_any[0],
        "reverse_lag_trap": (
            best_any[0] is not None and best_any[0] < 0
            and (best_pos[1] is None or best_any[1] > (best_pos[1] or -1))
        ),
    }


def objective_table(work: pd.DataFrame, *, grain: str, reference_col: str) -> pd.DataFrame:
    lags = MONTHLY_LAGS
    out_rows = []
    for species, df_sp in work.groupby("species_id"):
        pf = period_frame(df_sp, grain=grain)
        ref = pf[reference_col]
        n_pos = int((pd.to_numeric(ref, errors="coerce").fillna(0) > 0).sum())
        for signal_label in SIGNALS:
            scan = lead_lag(pf[signal_label], ref, lags)
            summ = summarize_scan(scan)
            out_rows.append(
                {
                    "species_id": species,
                    "role": "control" if species in CONTROL_BIRDWEATHER_IDS else "target",
                    "signal": signal_label,
                    "n_periods": int(len(pf)),
                    "n_ref_positive": n_pos,
                    **summ,
                    "_scan": scan,
                }
            )
    return pd.DataFrame(out_rows)


def _fmt(value: Any) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "."
    if isinstance(value, float):
        return f"{value:+.2f}"
    return str(value)


def _lag(value: Any, unit: str) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "."
    return f"{int(value)}{unit}"


def print_objective(title: str, table: pd.DataFrame, *, grain: str) -> None:
    unit = "mo" if grain == "monthly" else "wk"
    print(f"\n{'='*78}\n{title}\n{'='*78}")
    print(f"{'species':<9}{'role':<8}{'signal':<22}{'n':>4}{'pos':>5}"
          f"{'EW_rho':>8}{'@lag':>6}{'lag0':>7}{'best_any':>10}{'@lag':>6}  flag")
    for _, r in table.sort_values(["signal", "species_id"]).iterrows():
        flag = "REVERSE-LAG" if r["reverse_lag_trap"] else ""
        print(
            f"{r['species_id']:<9}{r['role']:<8}{r['signal']:<22}"
            f"{r['n_periods']:>4}{r['n_ref_positive']:>5}"
            f"{_fmt(r['early_warning_rho']):>8}{_lag(r['early_warning_lag'], unit):>6}"
            f"{_fmt(r['lag0_rho']):>7}"
            f"{_fmt(r['diagnostic_best_rho_any_lag']):>10}{_lag(r['diagnostic_best_lag'], unit):>6}  {flag}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        df = load_score_payload_frame(run_id=args.run_id, pool=pool)
    finally:
        pool.close()
    if df.empty:
        raise SystemExit(f"Run {args.run_id} has no score rows.")
    work = prepare(df)

    print(f"\nPHASE 1 EVALUATION  run_id={args.run_id}")
    print(f"rows={len(work)}  species={work['species_id'].nunique()}  "
          f"weeks={work['period_start'].nunique()}  "
          f"period={work['period_start'].min().date()}..{work['period_start'].max().date()}")
    print("signals: " + ", ".join(SIGNALS))

    mort_tbl = objective_table(work, grain="monthly", reference_col="ref_mortality")

    print_objective(
        "OBJECTIVE -- SOVON mortality alignment (monthly; continuous burden score)",
        mort_tbl, grain="monthly",
    )

    # persist artifacts
    out_dir = Path(args.out_dir) if args.out_dir else Path(
        "evaluation_runs/phase1"
    ) / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    mort_tbl.drop(columns=["_scan"]).to_csv(out_dir / "mortality_lead_lag.csv", index=False)
    payload = {
        "run_id": args.run_id,
        "phase": "phase1_evaluation_fixes",
        "config": {
            "monthly_lags": list(MONTHLY_LAGS),
            "min_pairs": MIN_PAIRS,
        },
        "mortality_objective_monthly": mort_tbl.drop(columns=["_scan"]).to_dict(orient="records"),
    }
    (out_dir / "phase1_evaluation.json").write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nartifacts -> {out_dir}/  (mortality_lead_lag.csv, phase1_evaluation.json)")


if __name__ == "__main__":
    main()
