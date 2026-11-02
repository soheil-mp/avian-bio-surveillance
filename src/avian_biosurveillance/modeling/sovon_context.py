"""SOVON mortality context for retrospective anomaly interpretation."""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd


SOVON_CONTEXT_COLUMNS = [
    "sovon_species_english",
    "sovon_dead_bird_count",
    "sovon_n_total",
    "sovon_mortality_rate",
    "sovon_expected_dead_bird_count",
    "sovon_mortality_z",
    "sovon_mortality_score",
    "sovon_mortality_top_decile_flag",
    "sovon_lag_0_to_3mo_max_score",
    "sovon_lag_0_to_3mo_dead_birds",
    "sovon_has_context",
]


def build_sovon_mortality_context(rows: pd.DataFrame) -> pd.DataFrame:
    """Build monthly SOVON mortality context from raw ``sovon_tbl`` rows."""
    if rows.empty:
        return _empty_sovon_context()

    frame = rows.copy()
    missing = {"year", "month", "species_english", "dead_bird_count"} - set(
        frame.columns
    )
    if missing:
        msg = f"Missing required SOVON columns: {', '.join(sorted(missing))}"
        raise ValueError(msg)

    frame["year"] = pd.to_numeric(frame["year"], errors="coerce").astype("Int64")
    frame["month"] = pd.to_numeric(frame["month"], errors="coerce").astype("Int64")
    frame["dead_bird_count"] = (
        pd.to_numeric(frame["dead_bird_count"], errors="coerce").fillna(0).astype(float)
    )
    if "n_total" not in frame.columns:
        frame["n_total"] = np.nan
    frame["n_total"] = pd.to_numeric(frame["n_total"], errors="coerce")

    frame = frame.dropna(subset=["year", "month"])
    frame["month_start"] = pd.to_datetime(
        {
            "year": frame["year"].astype(int),
            "month": frame["month"].astype(int),
            "day": 1,
        }
    )
    frame["sovon_species_id"] = pd.to_numeric(
        frame.get("sovon_species_id"),
        errors="coerce",
    )
    frame["sovon_species_key"] = frame.apply(_sovon_species_key, axis="columns")
    frame["sovon_mortality_rate"] = frame["dead_bird_count"].div(
        frame["n_total"].where(frame["n_total"] > 0)
    )

    group_columns = ["sovon_species_key", "month"]
    grouped = frame.groupby(group_columns, dropna=False)["dead_bird_count"]
    frame["sovon_expected_dead_bird_count"] = grouped.transform("median")
    frame["sovon_mortality_scale"] = grouped.transform(_robust_scale)
    frame["sovon_mortality_z"] = (
        frame["dead_bird_count"] - frame["sovon_expected_dead_bird_count"]
    ).div(frame["sovon_mortality_scale"])
    frame["sovon_mortality_score"] = grouped.transform(_rank_score)
    frame["sovon_mortality_top_decile_flag"] = (
        frame["sovon_mortality_score"] >= 0.9
    ) & (frame["dead_bird_count"] > 0)

    context = frame[
        [
            "sovon_species_key",
            "month_start",
            "sovon_species_id",
            "species_english",
            "dead_bird_count",
            "n_total",
            "sovon_mortality_rate",
            "sovon_expected_dead_bird_count",
            "sovon_mortality_z",
            "sovon_mortality_score",
            "sovon_mortality_top_decile_flag",
        ]
    ].rename(
        columns={
            "species_english": "sovon_species_english",
            "dead_bird_count": "sovon_dead_bird_count",
            "n_total": "sovon_n_total",
        }
    )
    context = context.sort_values(["sovon_species_key", "month_start"])
    context["sovon_lag_0_to_3mo_max_score"] = context.groupby(
        "sovon_species_key",
        dropna=False,
    )["sovon_mortality_score"].transform(_rolling_max_3)
    context["sovon_lag_0_to_3mo_dead_birds"] = context.groupby(
        "sovon_species_key",
        dropna=False,
    )["sovon_dead_bird_count"].transform(_rolling_sum_3)
    context["sovon_has_context"] = True
    return context.reset_index(drop=True)


def attach_sovon_mortality_context(
    features: pd.DataFrame,
    context: pd.DataFrame,
) -> pd.DataFrame:
    """Attach monthly SOVON context to weekly species features."""
    frame = features.copy()
    if frame.empty:
        return _ensure_sovon_columns(frame)

    frame["month_start"] = (
        pd.to_datetime(frame["month_start"]).dt.to_period("M").dt.to_timestamp()
    )
    if context.empty:
        return _ensure_sovon_columns(frame)

    attach_columns = [
        "sovon_species_key",
        "month_start",
        *SOVON_CONTEXT_COLUMNS,
    ]
    merged = frame.merge(
        context[attach_columns].drop_duplicates(["sovon_species_key", "month_start"]),
        on=["sovon_species_key", "month_start"],
        how="left",
    )
    return _ensure_sovon_columns(merged)


def _ensure_sovon_columns(frame: pd.DataFrame) -> pd.DataFrame:
    defaults: dict[str, Any] = {
        "sovon_species_english": pd.NA,
        "sovon_dead_bird_count": 0.0,
        "sovon_n_total": np.nan,
        "sovon_mortality_rate": np.nan,
        "sovon_expected_dead_bird_count": np.nan,
        "sovon_mortality_z": 0.0,
        "sovon_mortality_score": 0.0,
        "sovon_mortality_top_decile_flag": False,
        "sovon_lag_0_to_3mo_max_score": 0.0,
        "sovon_lag_0_to_3mo_dead_birds": 0.0,
        "sovon_has_context": False,
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
    frame["sovon_has_context"] = frame["sovon_has_context"].apply(
        lambda value: False if pd.isna(value) else bool(value)
    )
    for column in (
        "sovon_dead_bird_count",
        "sovon_mortality_z",
        "sovon_mortality_score",
        "sovon_lag_0_to_3mo_max_score",
        "sovon_lag_0_to_3mo_dead_birds",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    return frame


def _sovon_species_key(row: pd.Series) -> str:
    species_id = row.get("sovon_species_id")
    if species_id is not None and not pd.isna(species_id):
        return f"sovon:{int(float(species_id))}"
    return f"english:{_slug(row.get('species_english'))}"


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")


def _robust_scale(values: pd.Series) -> float:
    median = float(values.median())
    mad = float((values - median).abs().median())
    if mad > 0:
        return 1.4826 * mad
    std = float(values.std(ddof=0))
    return std if std > 0 else 1.0


def _rank_score(values: pd.Series) -> pd.Series:
    if len(values) <= 1:
        return pd.Series(0.0, index=values.index)
    ranks = values.rank(method="max") - 1
    return ranks.div(len(values) - 1).astype(float)


def _rolling_max_3(values: pd.Series) -> pd.Series:
    return values.rolling(window=3, min_periods=1).max()


def _rolling_sum_3(values: pd.Series) -> pd.Series:
    return values.rolling(window=3, min_periods=1).sum()


def _empty_sovon_context() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "sovon_species_key",
            "month_start",
            "sovon_species_id",
            *SOVON_CONTEXT_COLUMNS,
        ]
    )
