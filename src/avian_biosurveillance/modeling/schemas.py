"""Shared schemas for modeling artifacts and run metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ModelingRunStats:
    """Summary emitted by the daily modeling scoring pipeline."""

    run_id: str = ""
    database_tables: str = ""
    rows_loaded: int = 0
    feature_rows: int = 0
    rows_scored: int = 0
    alerts_green: int = 0
    alerts_amber: int = 0
    alerts_red: int = 0
    sparse_baseline_rows: int = 0
    cluster_rows: int = 0
    cluster_exemplar_rows: int = 0

    def to_metadata(self) -> dict[str, int | str]:
        """Return Dagster-friendly metadata values."""
        return asdict(self)
