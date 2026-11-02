#!/usr/bin/env python
"""Comprehensive evaluation of the lean bioacoustic anomaly signal.

Reads the live ``species_week_anomaly_tbl`` (lean, bioacoustic-primary). Its
``payload`` carries the full v3 per-row context, so every signal (bioacoustic
drift + outlier-share, population, fusion) and the SOVON mortality reference are
available from this single table -- no deprecated tables.

Breakdowns
----------
National (per-period lead-lag vs reference; positive-lag-only early warning):
  * all species pooled   * per species   * by role (targets vs controls)
  SOVON mortality: monthly, lags -2..+2.
Signal comparison: distribution of early-warning rho per signal across
  targets and (the seasonal caveat) controls.

Methodology mirrors scripts/ops/phase1_evaluation.py; low-level metric helpers
are copied verbatim from avian_biosurveillance.modeling.evaluation so numbers
stay consistent with the report. Read-only.

Usage:
    .venv/bin/python scripts/ops/comprehensive_evaluation.py [--out <dir-name>]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    """Populate os.environ from .env (DB creds) + config/runtime.env (host/port).

    Parsed in Python so the password's shell-special characters never matter.
    """
    sources = (
        (".env", {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}),
        ("config/runtime.env", None),
    )
    for fname, keep in sources:
        path = REPO / fname
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if len(val) >= 2 and val[0] in "'\"" and val[-1] == val[0]:
                val = val[1:-1]
            if keep is None or key in keep:
                os.environ[key] = val
    os.environ.setdefault("POSTGRES_HOST", "localhost")
    os.environ.setdefault("TSDB_PORT", "15432")


_load_env()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402

# --------------------------------------------------------------------------
# Low-level metric helpers -- copied verbatim from modeling/evaluation.py
# --------------------------------------------------------------------------


def _safe_float(value):
    result = float(value)
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return round(result, 6)


def _top_decile_mean(values):
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return 0.0
    threshold = float(numeric.quantile(0.90))
    return float(numeric.loc[numeric >= threshold].mean())


def _series_correlation(left, right, *, method):
    aligned = pd.DataFrame(
        {
            "left": pd.to_numeric(left, errors="coerce"),
            "right": pd.to_numeric(right, errors="coerce"),
        }
    ).dropna()
    if len(aligned) < 2:
        return None
    if (
        aligned["left"].min() == aligned["left"].max()
        or aligned["right"].min() == aligned["right"].max()
    ):
        return None
    return _safe_float(aligned["left"].corr(aligned["right"], method=method))


# --------------------------------------------------------------------------
# Config (mirrors phase1_evaluation.py)
# --------------------------------------------------------------------------

MONTHLY_LAGS = tuple(range(-2, 3))
MIN_PAIRS = 5
CONTROL_IDS = {"62", "81", "25", "76"}  # negative-control species

SIGNALS = {
    "bioacoustic_mean": ("embedding_drift_score", "mean"),
    "bioacoustic_topdecile": ("embedding_drift_score", "topdecile"),
    "bio_outliershare_mean": ("embedding_outlier_share", "mean"),
    "bio_outliershare_topdecile": ("embedding_outlier_share", "topdecile"),
    "population_mean": ("population_anomaly_score", "mean"),
    "population_topdecile": ("population_anomaly_score", "topdecile"),
    "fusion_mean": ("final_score", "mean"),
    "amber_plus_rate": ("_amber_plus", "mean"),
    "red_rate": ("_red", "mean"),
}

SQL = """
SELECT species_id, species_english, location_type, location_id, grid_cell_id,
       period_start, drift_score, outlier_share, clip_count, alert_level,
       payload->>'embedding_drift_score'        AS embedding_drift_score,
       payload->>'embedding_outlier_share'      AS embedding_outlier_share,
       payload->>'embedding_clip_count_scored'  AS embedding_clip_count_scored,
       payload->>'population_anomaly_score'     AS population_anomaly_score,
       payload->>'final_score'                  AS final_score,
       payload->>'sovon_mortality_score'        AS sovon_mortality_score
FROM species_week_anomaly_tbl
"""


def role_of(species_id: str) -> str:
    return "control" if str(species_id) in CONTROL_IDS else "target"


def num(frame, col):
    if col not in frame.columns:
        return pd.Series(0.0, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[col], errors="coerce").fillna(0.0)


def agg(series, how):
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


# --------------------------------------------------------------------------
# Load + prepare
# --------------------------------------------------------------------------


def load_frame() -> pd.DataFrame:
    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("TSDB_PORT", "15432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DB", "avian_biosurveillance"),
    )
    try:
        cur = conn.cursor()
        cur.execute(SQL)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()
    return pd.DataFrame(rows, columns=cols)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    w = df.copy()
    w["period_start"] = pd.to_datetime(w["period_start"], errors="coerce").dt.normalize()
    w = w.loc[w["period_start"].notna()].copy()
    w["species_id"] = w["species_id"].astype(str)
    numeric_cols = [
        "embedding_drift_score", "embedding_outlier_share", "embedding_clip_count_scored",
        "population_anomaly_score", "final_score", "sovon_mortality_score",
    ]
    for c in numeric_cols:
        if c in w.columns:
            w[c] = pd.to_numeric(w[c], errors="coerce")
    level = w.get("alert_level", pd.Series("", index=w.index)).astype(str)
    w["_amber_plus"] = level.isin(["amber", "red"]).astype(float)
    w["_red"] = (level == "red").astype(float)
    return w


# --------------------------------------------------------------------------
# National lead-lag (generalised over any row subset)
# --------------------------------------------------------------------------


def national_period_frame(work: pd.DataFrame, *, grain: str) -> pd.DataFrame:
    w = work.copy()
    if grain == "monthly":
        w["period"] = w["period_start"].dt.to_period("M").dt.to_timestamp()
    else:
        w["period"] = w["period_start"]
    # national mortality for a (possibly multi-species) group: per-species value
    # (max over its cells) summed across species in the group.
    mort_sp = w.groupby(["species_id", "period"])["sovon_mortality_score"].max()
    mort_by_period = mort_sp.groupby("period").sum()
    rows = []
    for period, block in w.groupby("period"):
        rec = {
            "period": period,
            "n_cells": int(len(block)),
            "n_species": int(block["species_id"].nunique()),
        }
        for label, (col, how) in SIGNALS.items():
            rec[label] = agg(block[col], how) if col in block.columns else 0.0
        rec["ref_mortality"] = float(mort_by_period.get(period, 0.0))
        rows.append(rec)
    return pd.DataFrame(rows).sort_values("period").reset_index(drop=True)


def lead_lag(signal, reference, lags):
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


def summarize_scan(scan):
    valid = [(r["lag"], r["rho"]) for r in scan if r["rho"] is not None]
    pos = [(lag, rho) for lag, rho in valid if lag >= 0]
    best_pos = max(pos, key=lambda x: x[1]) if pos else (None, None)
    best_any = max(valid, key=lambda x: x[1]) if valid else (None, None)
    lag0 = next((r["rho"] for r in scan if r["lag"] == 0), None)
    return {
        "early_warning_rho": best_pos[1],
        "early_warning_lag": best_pos[0],
        "lag0_rho": lag0,
        "diagnostic_best_rho_any_lag": best_any[1],
        "diagnostic_best_lag": best_any[0],
        "reverse_lag_trap": (
            best_any[0] is not None and best_any[0] < 0
            and (best_pos[1] is None or best_any[1] > (best_pos[1] or -1))
        ),
    }


def national_objective(work: pd.DataFrame, *, grain: str, reference_col: str) -> pd.DataFrame:
    lags = MONTHLY_LAGS
    pf = national_period_frame(work, grain=grain)
    ref = pf[reference_col]
    n_pos = int((pd.to_numeric(ref, errors="coerce").fillna(0) > 0).sum())
    out = []
    for sig in SIGNALS:
        out.append(
            {
                "signal": sig,
                "n_periods": int(len(pf)),
                "n_ref_positive": n_pos,
                **summarize_scan(lead_lag(pf[sig], ref, lags)),
            }
        )
    return pd.DataFrame(out)


# --------------------------------------------------------------------------
# Signal comparison
# --------------------------------------------------------------------------


def signal_comparison(ps_mort: dict) -> pd.DataFrame:
    recs = []
    for objective, table_map in (("mortality", ps_mort),):
        for role in ("target", "control"):
            sids = [s for s in table_map if role_of(s) == role]
            for sig in SIGNALS:
                vals = []
                for s in sids:
                    df = table_map[s]
                    row = df.loc[df["signal"] == sig]
                    if row.empty:
                        continue
                    rho = row["early_warning_rho"].iloc[0]
                    npos = row["n_ref_positive"].iloc[0]
                    if npos > 0 and rho is not None and not (
                        isinstance(rho, float) and math.isnan(rho)
                    ):
                        vals.append(float(rho))
                arr = np.array(vals, dtype=float)
                recs.append({
                    "objective": objective, "role": role, "signal": sig,
                    "n_species_with_signal": len(vals),
                    "median_ew_rho": float(np.median(arr)) if len(arr) else None,
                    "mean_ew_rho": float(np.mean(arr)) if len(arr) else None,
                    "frac_positive": float((arr > 0).mean()) if len(arr) else None,
                })
    return pd.DataFrame(recs)


# --------------------------------------------------------------------------
# Console helpers
# --------------------------------------------------------------------------


def _f(v):
    if v is None or (isinstance(v, float) and v != v):
        return "   . "
    return f"{v:+.3f}"


def print_objective(title, table, unit):
    print(f"\n{'='*92}\n{title}\n{'='*92}")
    print(f"{'signal':<28}{'n':>4}{'pos':>5}{'EW_rho':>9}{'@lag':>6}"
          f"{'lag0':>9}{'best_any':>10}{'@lag':>6}  flag")
    for _, r in table.iterrows():
        flag = "REVERSE-LAG" if r["reverse_lag_trap"] else ""
        ewl = f"{int(r['early_warning_lag'])}{unit}" if r["early_warning_lag"] is not None else " . "
        bal = f"{int(r['diagnostic_best_lag'])}{unit}" if r["diagnostic_best_lag"] is not None else " . "
        print(f"{r['signal']:<28}{r['n_periods']:>4}{r['n_ref_positive']:>5}"
              f"{_f(r['early_warning_rho']):>9}{ewl:>6}{_f(r['lag0_rho']):>9}"
              f"{_f(r['diagnostic_best_rho_any_lag']):>10}{bal:>6}  {flag}")


def per_species_group_summary(ps_tables: dict, group_fn, *, objective: str) -> pd.DataFrame:
    """Honest grouped view: the distribution of *per-species* early-warning rho
    within a group -- NOT an aggregate-then-correlate pooled correlation.

    Pooling a heterogeneous group (e.g. all 7 targets, which span Wren -0.29 to
    Wood-Pigeon +0.65 on mortality and include mutually anti-correlated species)
    collapses signals that peak in different months and is dominated by whichever
    species carries the reference mass. That distorts the result badly: pooled
    targets read ~0 while the coherent controls are reinforced. Per-species
    medians are the trustworthy summary.
    """
    groups: dict = {}
    for sid, df in ps_tables.items():
        groups.setdefault(group_fn(sid), []).append((sid, df))
    recs = []
    for group, members in groups.items():
        for sig in SIGNALS:
            vals = []
            for _sid, df in members:
                row = df.loc[df["signal"] == sig]
                if row.empty:
                    continue
                rho = row["early_warning_rho"].iloc[0]
                npos = row["n_ref_positive"].iloc[0]
                if npos > 0 and rho is not None and not (
                    isinstance(rho, float) and math.isnan(rho)
                ):
                    vals.append(float(rho))
            arr = np.array(vals, dtype=float)
            recs.append({
                "objective": objective, "group": group, "signal": sig,
                "n_species": len(vals),
                "median_ew_rho": float(np.median(arr)) if len(arr) else None,
                "mean_ew_rho": float(np.mean(arr)) if len(arr) else None,
                "min_ew_rho": float(np.min(arr)) if len(arr) else None,
                "max_ew_rho": float(np.max(arr)) if len(arr) else None,
                "frac_positive": float((arr > 0).mean()) if len(arr) else None,
            })
    return pd.DataFrame(recs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="comprehensive_lean_v3")
    args = ap.parse_args()

    print("loading species_week_anomaly_tbl ...")
    work = prepare(load_frame())
    species_ids = sorted(
        work["species_id"].unique(), key=lambda x: int(x) if x.isdigit() else 10**9
    )
    english = (
        work.dropna(subset=["species_english"])
        .groupby("species_id")["species_english"].first().to_dict()
    )
    print(f"rows={len(work)}  species={len(species_ids)}  "
          f"weeks={work['period_start'].nunique()}  "
          f"period={work['period_start'].min().date()}..{work['period_start'].max().date()}")
    n_amber = int((work['alert_level'] == 'amber').sum())
    n_red = int((work['alert_level'] == 'red').sum())
    n_green = int((work['alert_level'] == 'green').sum())
    print(f"lean alerts: amber={n_amber} red={n_red} green={n_green} "
          f"(alert_rate={ (n_amber+n_red)/max(len(work),1):.3%})")

    # ---- National: pooled all-species ----
    pooled_mort = national_objective(work, grain="monthly", reference_col="ref_mortality")
    print_objective("NATIONAL -- ALL SPECIES POOLED -- SOVON mortality (monthly)",
                    pooled_mort, "m")

    # ---- National: per species (trustworthy base for all grouped views) ----
    ps_mort = {}
    for s in species_ids:
        sub = work.loc[work["species_id"] == s]
        ps_mort[s] = national_objective(sub, grain="monthly", reference_col="ref_mortality")

    # ---- National: by role -- as PER-SPECIES MEDIANS (the honest grouped view) ----
    role_summary_mort = per_species_group_summary(ps_mort, role_of, objective="mortality_monthly")
    # Pooled role kept ONLY as a labelled diagnostic, to document the artifact.
    role_pooled_diag = {}
    for role in ("target", "control"):
        sub = work.loc[work["species_id"].isin([s for s in species_ids if role_of(s) == role])]
        role_pooled_diag[role] = {
            "mortality": national_objective(sub, grain="monthly", reference_col="ref_mortality"),
        }

    print(f"\n{'='*92}\nNATIONAL -- BY ROLE, mortality (PER-SPECIES MEDIANS -- honest grouped view)"
          f"\n{'='*92}")
    print(f"  {'signal':<28}{'target med':>11}{'n':>4}{'target range':>20}{'control med':>13}{'n':>4}")
    for sig in SIGNALS:
        t = role_summary_mort[(role_summary_mort.group == "target") & (role_summary_mort.signal == sig)]
        c = role_summary_mort[(role_summary_mort.group == "control") & (role_summary_mort.signal == sig)]
        tm = t["median_ew_rho"].iloc[0] if len(t) else None
        tn = int(t["n_species"].iloc[0]) if len(t) else 0
        rng = f"[{_f(t['min_ew_rho'].iloc[0] if len(t) else None)},{_f(t['max_ew_rho'].iloc[0] if len(t) else None)}]"
        cm = c["median_ew_rho"].iloc[0] if len(c) else None
        cn = int(c["n_species"].iloc[0]) if len(c) else 0
        print(f"  {sig:<28}{_f(tm):>11}{tn:>4}{rng:>20}{_f(cm):>13}{cn:>4}")

    print(f"\n  [DIAGNOSTIC] aggregate-then-correlate pooled role -- MISLEADS on heterogeneous groups:")
    for role in ("target", "control"):
        rm = role_pooled_diag[role]["mortality"]
        sh = rm[rm.signal == "bio_outliershare_topdecile"].iloc[0]
        med = role_summary_mort[(role_summary_mort.group == role)
                                & (role_summary_mort.signal == "bio_outliershare_topdecile")]["median_ew_rho"].iloc[0]
        print(f"    {role:<8} pooled EW={_f(sh['early_warning_rho'])}@{sh['early_warning_lag']}m"
              f"   vs per-species median={_f(med)}")

    # ---- Signal comparison ----
    sig_cmp = signal_comparison(ps_mort)
    print(f"\n{'='*92}\nSIGNAL COMPARISON -- median early-warning rho across species "
          f"(ref-positive species only)\n{'='*92}")
    for objective in ("mortality",):
        print(f"\n  [{objective}]  {'signal':<28}{'target med':>12}{'tgt n':>7}"
              f"{'ctrl med':>11}{'ctrl n':>8}")
        for sig in SIGNALS:
            t = sig_cmp[(sig_cmp.objective == objective) & (sig_cmp.role == "target")
                        & (sig_cmp.signal == sig)]
            c = sig_cmp[(sig_cmp.objective == objective) & (sig_cmp.role == "control")
                        & (sig_cmp.signal == sig)]
            tm = t["median_ew_rho"].iloc[0] if len(t) else None
            tn = int(t["n_species_with_signal"].iloc[0]) if len(t) else 0
            cm = c["median_ew_rho"].iloc[0] if len(c) else None
            cn = int(c["n_species_with_signal"].iloc[0]) if len(c) else 0
            print(f"  {'':<8}{sig:<28}{_f(tm):>12}{tn:>7}{_f(cm):>11}{cn:>8}")

    # ---- Persist ----
    out_dir = REPO / "evaluation_runs" / "phase1" / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    def tag(df, **cols):
        df = df.copy()
        for i, (k, v) in enumerate(cols.items()):
            df.insert(i, k, v)
        return df

    national_all = tag(pooled_mort, scope="all_species", objective="mortality_monthly")
    role_pooled = pd.concat(
        [tag(tbls["mortality"], scope=f"{role}_pooled_DIAGNOSTIC", objective="mortality_monthly")
         for role, tbls in role_pooled_diag.items()],
        ignore_index=True,
    )

    per_species_mort = pd.concat(
        [tag(ps_mort[s], species_id=s, species_english=english.get(s, ""), role=role_of(s))
         for s in species_ids], ignore_index=True)

    national_all.to_csv(out_dir / "national_all_species.csv", index=False)
    role_summary_mort.to_csv(out_dir / "national_by_role_per_species_median.csv", index=False)
    role_pooled.to_csv(out_dir / "national_by_role_pooled_DIAGNOSTIC.csv", index=False)
    per_species_mort.to_csv(out_dir / "national_per_species_mortality.csv", index=False)
    sig_cmp.to_csv(out_dir / "signal_comparison.csv", index=False)

    payload = {
        "source_table": "species_week_anomaly_tbl",
        "rows": int(len(work)),
        "species": len(species_ids),
        "weeks": int(work["period_start"].nunique()),
        "period": [str(work["period_start"].min().date()), str(work["period_start"].max().date())],
        "lean_alerts": {"amber": n_amber, "red": n_red, "green": n_green},
        "config": {
            "monthly_lags": list(MONTHLY_LAGS),
            "min_pairs": MIN_PAIRS, "control_ids": sorted(CONTROL_IDS), "signals": list(SIGNALS),
        },
        "national_all_species": {
            "mortality_monthly": pooled_mort.to_dict("records"),
        },
        "national_by_role_per_species_median": {
            "_note": "Per-species medians within each role -- the correct grouped "
                     "view. Pooled aggregate-then-correlate is unreliable here.",
            "mortality_monthly": role_summary_mort.to_dict("records"),
        },
        "national_by_role_pooled_diagnostic": {
            "_caveat": "AGGREGATE-THEN-CORRELATE on a heterogeneous group. Pooled "
                       "targets read ~0 only because species peak in different "
                       "months and Wren is anti-correlated; do NOT headline this. "
                       "See national_by_role_per_species_median.",
            **{role: {"mortality_monthly": t["mortality"].to_dict("records")}
               for role, t in role_pooled_diag.items()},
        },
        "national_per_species": {
            s: {"english": english.get(s, ""), "role": role_of(s),
                "mortality_monthly": ps_mort[s].to_dict("records")}
            for s in species_ids
        },
        "signal_comparison": sig_cmp.to_dict("records"),
    }
    (out_dir / "comprehensive_evaluation.json").write_text(
        json.dumps(payload, indent=2, default=str)
    )
    print(f"\nartifacts -> {out_dir}/")
    print("  comprehensive_evaluation.json + 5 CSVs "
          "(role view is per-species medians; pooled kept as *_DIAGNOSTIC)")


if __name__ == "__main__":
    main()
