#!/usr/bin/env python
"""Reader-facing national evaluation table, read from evaluation_grain_tbl.

The grain table (built by build_eval_table.py) already joins the lean anomaly to
the reference: mortality from raw SOVON monthly dead_bird_count. This script
writes NO files to the repo -- results (including the full threshold sweep) go
to evaluation_results_tbl.

TABLE 1 -- NATIONAL (monthly), raw Spearman, NOT deseasonalised.
  Reference: SOVON mortality. Lags 0..+L months, anomaly LEADS.
  Signal three ways: continuous (mean outlier_share), amber rule (% of cell-weeks
  with outlier_share >= 0.50), optimised threshold (% flagged maximising rho).
  Scopes: all / target / control / each of the 11 species.

Run:  .venv/bin/python scripts/ops/final_evaluation_tables.py
      (add --no-store to skip writing evaluation_results_tbl)
"""
from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
_b = importlib.util.spec_from_file_location("bet", REPO / "scripts/ops/build_eval_table.py")
bet = importlib.util.module_from_spec(_b); _b.loader.exec_module(bet)

AMBER_TAU = 0.50
TAU_GRID = [round(x, 2) for x in np.arange(0.10, 0.96, 0.05)]
MONTH_LAGS = (0, 1, 2, 3, 4)
MIN_PAIRS = 5
CONTROL_IDS = {"62", "81", "25", "76"}

def role_of(s):
    return "control" if str(s) in CONTROL_IDS else "target"


def _spear(a, b):
    m = a.notna() & b.notna()
    if int(m.sum()) < MIN_PAIRS or a[m].nunique() < 2 or b[m].nunique() < 2:
        return None
    return float(a[m].corr(b[m], method="spearman"))


def lead_corr(signal, ref, lags=MONTH_LAGS):
    s = signal.reset_index(drop=True); r = ref.reset_index(drop=True)
    best, profile = (None, None), {}
    for lag in lags:
        rho = _spear(s, r.shift(-lag))
        profile[lag] = rho
        if rho is not None and (best[1] is None or rho > best[1]):
            best = (lag, rho)
    return best, profile


def load():
    df = bet.read_sql(bet.connect(), "SELECT * FROM evaluation_grain_tbl")
    df["period_start"] = pd.to_datetime(df["period_start"])
    df["month"] = df["period_start"].dt.to_period("M").dt.to_timestamp()
    df["outlier_share"] = pd.to_numeric(df["outlier_share"], errors="coerce")
    mort_sp = df.groupby(["species_id", "month"])["sovon_dead_bird_count"].max()
    return df, mort_sp


def scope_rows(df):
    ids = sorted(df["species_id"].unique(), key=lambda x: int(x) if str(x).isdigit() else 10**9)
    eng = df.groupby("species_id")["species_english"].first().to_dict()
    yield "all species", ids, "—"
    yield "target", [s for s in ids if role_of(s) == "target"], "—"
    yield "control", [s for s in ids if role_of(s) == "control"], "—"
    for s in ids:
        yield eng.get(s, s), [s], role_of(s)


# ----------------------------- national -----------------------------

def national_rows(df, mort_sp):
    out = {"mort": []}
    profiles = {"mort": {}}
    for name, ids, role in scope_rows(df):
        sub = df[df["species_id"].isin(ids)]
        months = pd.Index(sorted(sub["month"].dropna().unique()))
        cont = sub.groupby("month")["outlier_share"].mean().reindex(months)
        amber = sub.assign(a=sub["outlier_share"] >= AMBER_TAU).groupby("month")["a"].mean().reindex(months)
        amber_pct = float((sub["outlier_share"] >= AMBER_TAU).mean() * 100)
        refs = {
            "mort": mort_sp[mort_sp.index.get_level_values(0).isin(ids)].groupby("month").sum().reindex(months),
        }
        for k in ("mort",):
            ref = refs[k]
            (c_lag, c_rho), prof = lead_corr(cont, ref)
            (a_lag, a_rho), _ = lead_corr(amber, ref)
            opt = {"rho": None, "lag": None, "pct": None, "tau": None}
            tau_profile = {}
            for tau in TAU_GRID:
                rate = sub.assign(a=sub["outlier_share"] >= tau).groupby("month")["a"].mean().reindex(months)
                (lag, rho), _ = lead_corr(rate, ref)
                tau_profile[tau] = {"rho": rho, "lag": lag,
                                    "pct": float((sub["outlier_share"] >= tau).mean() * 100)}
                if rho is not None and (opt["rho"] is None or rho > opt["rho"]):
                    opt = {"rho": rho, "lag": lag,
                           "pct": float((sub["outlier_share"] >= tau).mean() * 100), "tau": tau}
            out[k].append({
                "scope": name, "role": role,
                "months": int(ref.notna().sum()),
                "ref_pos_months": int((pd.to_numeric(ref, errors="coerce").fillna(0) > 0).sum()),
                "cont_rho": c_rho, "cont_lag": c_lag,
                "amber_tau": AMBER_TAU,
                "amber_pct": round(amber_pct, 2), "amber_rho": a_rho, "amber_lag": a_lag,
                "opt_tau": opt["tau"],
                "opt_pct": None if opt["pct"] is None else round(opt["pct"], 2),
                "opt_rho": opt["rho"], "opt_lag": opt["lag"],
                "cont_profile": prof,
                "tau_profile": tau_profile,
            })
            if name in ("all species", "target", "control"):
                profiles[k][name] = prof
    return out, profiles


# ----------------------------- output -----------------------------

def _r(v):
    return "   .  " if v is None or (isinstance(v, float) and v != v) else f"{v:+.3f}"


def print_national(title, rows):
    print(f"\n{'='*112}\n{title}\n{'='*112}")
    print("  threshold = outlier_share cutoff; %flag = share of cell-weeks flagged; "
          "amber = production rule (>=0.50); opt = correlation-maximising cutoff")
    print(f"{'scope':<18}{'mo':>4}{'pos':>4}{'cont r':>9}{'lag':>4}"
          f"{'amb_thr':>8}{'amb%':>7}{'amb r':>8}{'lag':>4}"
          f"{'opt_thr':>8}{'opt%':>7}{'opt r':>8}{'lag':>4}")
    for r in rows:
        cl = "" if r["cont_lag"] is None else str(r["cont_lag"])
        al = "" if r["amber_lag"] is None else str(r["amber_lag"])
        ol = "" if r["opt_lag"] is None else str(r["opt_lag"])
        othr = "" if r.get("opt_tau") is None else f"{r['opt_tau']:.2f}"
        opct = "" if r["opt_pct"] is None else f"{r['opt_pct']:.1f}"
        print(f"{r['scope']:<18}{r['months']:>4}{r['ref_pos_months']:>4}"
              f"{_r(r['cont_rho']):>9}{cl:>4}"
              f"{r['amber_tau']:>8.2f}{r['amber_pct']:>6.1f}%{_r(r['amber_rho']):>8}{al:>4}"
              f"{othr:>8}{opct:>6}%{_r(r['opt_rho']):>8}{ol:>4}")


def print_profiles(title, profiles):
    print(f"\n  lag profile ({title}, continuous signal) -- rho at each month lag (anomaly leads):")
    print(f"  {'scope':<14}" + "".join(f"{'+'+str(l)+'mo':>9}" for l in MONTH_LAGS))
    for scope, prof in profiles.items():
        print(f"  {scope:<14}" + "".join(f"{_r(prof.get(l)):>9}" for l in MONTH_LAGS))


def print_threshold_sweep(nat):
    grid = [0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80]
    print(f"\n{'='*112}\nTABLE 1b -- NATIONAL threshold sensitivity (pooled scopes): "
          f"Spearman rho at each alert threshold (anomaly leads)\n{'='*112}")
    print("  0.50 is the production alert rule; other thresholds are shown for sensitivity "
          "only (NOT selected against the references)")
    for label, key in (("mortality", "mort"),):
        by_scope = {r["scope"]: r for r in nat[key]}
        print(f"\n  {label}:")
        print(f"  {'threshold ->':<14}" + "".join(f"{t:>8.2f}" for t in grid))
        for scope in ("all species", "target", "control"):
            r = by_scope.get(scope)
            if not r:
                continue
            prof = r.get("tau_profile") or {}
            cells = []
            for t in grid:
                c = prof.get(t) or prof.get(round(t, 2))
                cells.append(_r(c["rho"]) if c and c.get("rho") is not None else "   .  ")
            print(f"  {scope:<14}" + "".join(f"{v:>8}" for v in cells))


def store_results(nat):
    rows = []
    ts = datetime.now(timezone.utc).isoformat()
    for ref_key, label in (("mort", "mortality"),):
        for r in nat[ref_key]:
            for sig, rho, lag, pct, thr in (
                ("continuous", r["cont_rho"], r["cont_lag"], None, None),
                ("amber", r["amber_rho"], r["amber_lag"], r["amber_pct"], r["amber_tau"]),
                ("optimized", r["opt_rho"], r["opt_lag"], r["opt_pct"], r["opt_tau"]),
            ):
                rows.append(("national", r["scope"], r["role"], label, sig,
                             thr, pct, lag, None, "spearman", rho, ts))
            rows.append(("national", r["scope"], r["role"], label, "\u2014",
                         None, None, None, None, "n_months", float(r["months"]), ts))
            rows.append(("national", r["scope"], r["role"], label, "\u2014",
                         None, None, None, None, "n_ref_pos_months", float(r["ref_pos_months"]), ts))
            for lag, rho in (r.get("cont_profile") or {}).items():
                if rho is not None:
                    rows.append(("national", r["scope"], r["role"], label, "continuous",
                                 None, None, int(lag), None, "spearman_lagprofile", float(rho), ts))
            for tau, cell in (r.get("tau_profile") or {}).items():
                if cell.get("rho") is not None:
                    rows.append(("national", r["scope"], r["role"], label, "threshold_sweep",
                                 float(tau), cell.get("pct"), cell.get("lag"), None,
                                 "spearman", float(cell["rho"]), ts))
    conn = bet.connect(); cur = conn.cursor()
    cur.execute("""
        DROP TABLE IF EXISTS evaluation_results_tbl;
        CREATE TABLE evaluation_results_tbl (
          kind text, scope text, role text, reference text, signal_or_threshold text,
          threshold double precision, flagged_pct double precision, lag_months integer,
          horizon_weeks integer, metric text, value double precision, computed_at timestamptz);""")
    cur.executemany("INSERT INTO evaluation_results_tbl VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    conn.commit(); conn.close()
    print(f"\nstored {len(rows)} rows -> evaluation_results_tbl (no files written to the repo)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-store", action="store_true")
    args = ap.parse_args()
    print("reading evaluation_grain_tbl ...")
    df, mort_sp = load()
    print(f"rows={len(df)} species={df['species_id'].nunique()} months={df['month'].nunique()} "
          f"(reference: mortality=raw SOVON monthly)")
    nat, profiles = national_rows(df, mort_sp)
    print_national("TABLE 1a -- NATIONAL monthly vs SOVON mortality (raw Spearman, anomaly leads)", nat["mort"])
    print_profiles("mortality", profiles["mort"])
    print_threshold_sweep(nat)
    if not args.no_store:
        store_results(nat)


if __name__ == "__main__":
    main()
