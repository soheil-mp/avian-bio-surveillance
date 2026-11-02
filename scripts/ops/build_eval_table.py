#!/usr/bin/env python
"""Build evaluation_grain_tbl -- the single, eval-ready SQL table.

Joins the lean anomaly signal to the CORRECT decoupled reference table, fixing a
bug found in the lean payload copies:
  mortality <- sovon_tbl (raw monthly dead_bird_count), not the degenerate
               derived score that was in the payload.

Grain: species x grid_cell x week.

Rebuild after a scoring run:
    .venv/bin/python scripts/ops/build_eval_table.py --rebuild
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ce", REPO / "scripts/ops/comprehensive_evaluation.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)  # runs env loading (.env + runtime.env)
sys.path.insert(0, str(REPO / "src"))
from avian_biosurveillance.config.species import registry  # noqa: E402

CONTROL_IDS = {"62", "81", "25", "76"}

DDL = """
DROP TABLE IF EXISTS evaluation_grain_tbl;
CREATE TABLE evaluation_grain_tbl (
  species_id text, species_english text, role text,
  grid_cell_id text, period_start date, month_start date,
  outlier_share double precision, drift_score double precision,
  clip_count integer, alert_level text, embedding_present boolean,
  sovon_dead_bird_count double precision, sovon_n_total double precision
);
"""

COLS = ["species_id", "species_english", "role", "grid_cell_id",
        "period_start", "month_start", "outlier_share", "drift_score", "clip_count",
        "alert_level", "embedding_present", "sovon_dead_bird_count", "sovon_n_total"]


def connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("TSDB_PORT", "15432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ.get("POSTGRES_DB", "avian_biosurveillance"))


def read_sql(conn, q):
    cur = conn.cursor(); cur.execute(q)
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def role_of(s):
    return "control" if str(s) in CONTROL_IDS else "target"


def build():
    conn = connect()
    print("loading anomaly (lean) ...")
    A = read_sql(conn, """
        SELECT species_id::text species_id, species_english, grid_cell_id::text grid_cell_id,
               period_start::date period_start, outlier_share::float8 outlier_share,
               drift_score::float8 drift_score, clip_count, alert_level
        FROM species_week_anomaly_tbl""")
    A["period_start"] = pd.to_datetime(A["period_start"])
    A["month_start"] = A["period_start"].dt.to_period("M").dt.to_timestamp()
    A["embedding_present"] = (pd.to_numeric(A["clip_count"], errors="coerce").fillna(0) > 0) | \
                             (pd.to_numeric(A["drift_score"], errors="coerce").fillna(0) > 0)

    print("loading mortality reference (sovon raw) ...")
    M = read_sql(conn, """
        SELECT sovon_species_id, year, month, sum(dead_bird_count)::float8 sovon_dead_bird_count,
               sum(n_total)::float8 sovon_n_total
        FROM sovon_tbl GROUP BY 1,2,3""")
    eng_to_bw = dict(zip(registry.english_names, registry.birdweather_ids))
    sovon_to_bw = {sid: eng_to_bw[eng] for eng, sid in registry.sovon_id_by_english.items()}
    M["species_id"] = M["sovon_species_id"].map(sovon_to_bw)
    M = M.dropna(subset=["species_id"]).copy()
    M["month_start"] = pd.to_datetime(dict(year=M["year"].astype(int),
                                           month=M["month"].astype(int), day=1))
    conn.close()

    print("merging ...")
    df = A.merge(M[["species_id", "month_start", "sovon_dead_bird_count", "sovon_n_total"]],
                 on=["species_id", "month_start"], how="left")
    df["role"] = df["species_id"].map(role_of)
    df["clip_count"] = pd.to_numeric(df["clip_count"], errors="coerce").fillna(0).astype(int)
    df["alert_level"] = df["alert_level"].fillna("")

    out = df[COLS].copy()
    out["period_start"] = out["period_start"].dt.strftime("%Y-%m-%d")
    out["month_start"] = out["month_start"].dt.strftime("%Y-%m-%d")

    print(f"writing evaluation_grain_tbl ({len(out)} rows) ...")
    conn = connect(); cur = conn.cursor()
    cur.execute(DDL); conn.commit()
    buf = io.StringIO(); out.to_csv(buf, index=False, header=False); buf.seek(0)
    cur.copy_expert("COPY evaluation_grain_tbl FROM STDIN WITH (FORMAT csv)", buf)
    conn.commit()
    cur.execute("CREATE INDEX ON evaluation_grain_tbl(species_id, period_start);")
    cur.execute("CREATE INDEX ON evaluation_grain_tbl(species_id, month_start);")
    conn.commit()
    cur.execute("SELECT count(*), count(DISTINCT species_id) FROM evaluation_grain_tbl")
    n, sp = cur.fetchone()
    conn.close()
    print(f"done: {n} rows, {sp} species, {len(COLS)} columns.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="drop and rebuild the table")
    ap.parse_args()
    build()


if __name__ == "__main__":
    main()
