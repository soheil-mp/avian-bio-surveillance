"""Static SQL checks for the species tracker refresh routine."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTINE = REPO_ROOT / "db" / "routines" / "050_refresh_species_tracker.sql"


def test_refresh_species_tracker_uses_staging_table() -> None:
    sql = ROUTINE.read_text(encoding="utf-8")

    assert "CREATE TEMP TABLE IF NOT EXISTS species_tracker_tbl" in sql
    assert "SELECT * FROM public.species_tracker_tbl WITH NO DATA;" in sql


def test_refresh_species_tracker_cuts_over_live_table_at_end() -> None:
    sql = ROUTINE.read_text(encoding="utf-8")

    stage_create = sql.index("CREATE TEMP TABLE IF NOT EXISTS species_tracker_tbl")
    stage_insert = sql.index("INSERT INTO species_tracker_tbl (")
    live_lock = sql.index(
        "LOCK TABLE public.species_tracker_tbl IN ACCESS EXCLUSIVE MODE;"
    )
    live_insert = sql.index("INSERT INTO public.species_tracker_tbl (")

    assert stage_create < stage_insert < live_lock < live_insert
    assert "TRUNCATE TABLE public.species_tracker_tbl RESTART IDENTITY;" in sql


def test_refresh_species_tracker_supports_windowed_refresh() -> None:
    sql = ROUTINE.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION refresh_species_tracker(" in sql
    assert "p_window_start date" in sql
    assert "p_window_end date" in sql
    assert "DELETE FROM public.species_tracker_tbl" in sql
    assert "WHERE period_end >= p_window_start" in sql
    assert "AND period_start <= p_window_end" in sql
    assert "RETURN refresh_species_tracker(NULL::date, NULL::date);" in sql


def test_refresh_species_tracker_supports_modeling_scope() -> None:
    sql = ROUTINE.read_text(encoding="utf-8")

    assert "p_refresh_scope text" in sql
    assert "v_scope_mode text := lower(COALESCE(p_refresh_scope, 'full'))" in sql
    assert "v_scope_start := date_trunc('week', p_window_start::timestamp)::date;" in sql
    assert "RETURN refresh_species_tracker(p_window_start, p_window_end, 'full');" in sql
