"""Static smoke checks for the database bootstrap layout.

These tests do **not** require a running PostgreSQL/TimescaleDB instance.
They guarantee that:

1. The folder layout documented in ``db/README.md`` exists.
2. ``db/bootstrap/init_database.sh`` is the only schema entry point and is
   referenced by the docker-compose / singularity bootstrap paths.
3. No file under ``db/`` references the now-removed ``db/schemas/`` or
   ``db/data model/`` paths (which would silently break migrations).
4. Every migration file follows the ``V<NNN>__<name>.sql`` naming convention.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DB_ROOT = REPO_ROOT / "db"

EXPECTED_SUBDIRS = (
    "bootstrap",
    "baseline",
    "functions",
    "migrations",
    "triggers",
    "routines",
    "caggs",
    "views",
    "grants",
    "erd",
    "seeds",
)

MIGRATION_PATTERN = re.compile(r"^V\d{3,}__[a-z0-9_]+\.sql$")

EXPECTED_BASELINE_FILES = (
    "010_extensions.sql",
    "020_birdweather.sql",
    "030_knmi.sql",
    "040_sovon.sql",
    "050_ebird.sql",
    "060_movebank.sql",
    "070_grid_cells.sql",
    "080_google_earth_engine.sql",
    "090_cds_drought.sql",
    "100_seismic.sql",
    "110_birdweather_stations.sql",
    "120_data_quality.sql",
    "130_species.sql",
    "140_movebank_tracks.sql",
    "150_species_tracker.sql",
    "160_modeling.sql",
)


def test_db_subfolders_exist() -> None:
    missing = [d for d in EXPECTED_SUBDIRS if not (DB_ROOT / d).is_dir()]
    assert not missing, f"Missing required db/ subfolders: {missing}"


def test_bootstrap_entry_point_present() -> None:
    entry = DB_ROOT / "bootstrap" / "init_database.sh"
    assert entry.is_file(), f"Missing single-source bootstrap script: {entry}"
    text = entry.read_text(encoding="utf-8")
    # Sanity: the script must reference every folder it claims to apply.
    for label in (
        "ensure_aux_roles.sh",
        "functions",
        "baseline",
        "migrations",
        "triggers",
        "routines",
        "caggs",
        "views",
        "grants",
    ):
        assert label in text, f"init_database.sh does not mention '{label}'"
    for step in range(1, 11):
        assert f"step {step}/10" in text, (
            f"init_database.sh does not report step {step}/10"
        )
    assert "--single-transaction" in text, (
        "migrations must be applied in one transaction with their ledger insert"
    )


def test_baseline_files_are_domain_split_and_ordered() -> None:
    baseline_files = tuple(sorted(p.name for p in (DB_ROOT / "baseline").glob("*.sql")))
    assert baseline_files == EXPECTED_BASELINE_FILES
    # Legacy / coalesced filenames should never come back.
    for legacy in (
        "001_create_tables.sql",
        "002_derived_tables.sql",
        "100_operational_tables.sql",
        "110_derived_tables.sql",
        "110_recording_stations.sql",
        "150_grid_cells_5km_v.sql",
        "150_legacy_audit_cleanup.sql",
    ):
        assert not (DB_ROOT / "baseline" / legacy).exists(), (
            f"Legacy baseline file resurfaced: {legacy}"
        )


def test_grid_cells_5km_view_removed() -> None:
    """5 km is canonical now, so the derived convenience view stays removed."""
    view_file = DB_ROOT / "views" / "012_grid_cells_5km.sql"
    assert not view_file.exists(), "Derived 5 km grid view should not be present"


def test_erd_generator_lives_in_db_erd() -> None:
    generator = DB_ROOT / "erd" / "generate_erd.py"
    assert generator.is_file()
    assert not (REPO_ROOT / "tools" / "db" / "generate_erd.py").exists()
    text = generator.read_text(encoding="utf-8")
    assert "db/baseline/" in text or '"baseline"' in text
    assert 'Column("detection_id"' not in text
    assert "_parse_table_defs" in text
    assert "LayoutHint" not in text
    assert "STACKS" not in text
    assert "LAYOUT = {" not in text
    assert "_layout_group" in text
    assert "source_path: Path" in text

    readme = (DB_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Hand-positioned ERD" not in readme
    assert "coords baked in" not in readme
    assert "file order and query dependencies" in readme


def test_legacy_paths_removed() -> None:
    legacy = ("db/schemas", "db/data model")
    leftover = [p for p in legacy if (REPO_ROOT / p).exists()]
    assert not leftover, f"Legacy db paths still on disk: {leftover}"


def test_migrations_follow_naming_convention() -> None:
    migrations_dir = DB_ROOT / "migrations"
    sql_files = sorted(p.name for p in migrations_dir.glob("*.sql"))
    bad = [name for name in sql_files if not MIGRATION_PATTERN.match(name)]
    assert not bad, (
        f"Migration files must match V<NNN>__<lowercase_name>.sql, offenders: {bad}"
    )


def test_migration_policy_is_documented() -> None:
    readme = DB_ROOT / "migrations" / "README.md"
    assert readme.is_file(), "db/migrations/ must document migration policy"
    text = readme.read_text(encoding="utf-8")
    for phrase in (
        "V<NNN>__short_name.sql",
        "schema_migrations_tbl",
        "forward-only",
        "db/baseline/",
    ):
        assert phrase in text, f"migrations README missing: {phrase}"


def test_seed_policy_is_documented_and_opt_in() -> None:
    readme = DB_ROOT / "seeds" / "README.md"
    assert readme.is_file(), "db/seeds/ must document seed policy"
    text = readme.read_text(encoding="utf-8")
    assert "not" in text.lower() and "default bootstrap" in text
    assert "ON CONFLICT" in text

    bootstrap = (DB_ROOT / "bootstrap" / "init_database.sh").read_text(encoding="utf-8")
    assert "db/seeds" not in bootstrap
    assert 'apply_dir "seeds"' not in bootstrap


def test_db_docs_do_not_reference_stale_paths() -> None:
    stale = (
        "triggers/010_cds_drought_require_cell.sql",
        "functions/030_trg_cds_drought_require_cell.sql",
    )
    offenders: list[str] = []
    for path in DB_ROOT.rglob("*"):
        if path.suffix not in {".md", ".sql", ".sh", ".py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in stale:
            if needle in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    assert not offenders, f"Stale DB path references remain: {offenders}"


def test_safe_multistatement_units_are_transactional() -> None:
    """Views/triggers/routines should not leave partial deploy units behind."""
    checked_dirs = ("views", "triggers", "routines")
    offenders: list[str] = []
    for dirname in checked_dirs:
        for path in (DB_ROOT / dirname).glob("*.sql"):
            text = path.read_text(encoding="utf-8")
            if "BEGIN;" not in text or "COMMIT;" not in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"Multi-statement SQL deploy units must use BEGIN/COMMIT: {offenders}"
    )


def test_schema_only_migrations_were_folded_into_baseline() -> None:
    migrations_dir = DB_ROOT / "migrations"
    removed = {
        "V002__drop_redundant_audit_timestamps.sql",
        "V003__grid_row_col.sql",
        "V004__birdweather_processed_clip.sql",
        "V005__species_dashboard_cache.sql",
        "V006__gee_station_date_index.sql",
    }
    still_present = sorted(name for name in removed if (migrations_dir / name).exists())
    assert not still_present, (
        "Schema-only migrations should live in baseline/ once they become part "
        f"of the canonical schema, found: {still_present}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "infrastructure/docker/docker-compose.yml",
        "infrastructure/singularity/start.sh",
    ],
)
def test_bootstrap_consumers_use_new_path(path: str) -> None:
    file_path = REPO_ROOT / path
    if not file_path.exists():
        pytest.skip(f"{path} not present in this checkout")
    text = file_path.read_text(encoding="utf-8")
    assert (
        "db/bootstrap/init_database.sh" in text
        or "/init/db/bootstrap/init_database.sh" in text
    ), f"{path} must invoke db/bootstrap/init_database.sh"
    # The legacy direct mounts should be gone.
    assert "db/schemas/init_database.sh" not in text, (
        f"{path} still references the old db/schemas/init_database.sh path"
    )
    assert "/docker-entrypoint-initdb.d/002_create_tables.sql" not in text, (
        f"{path} still uses the per-file initdb mount pattern"
    )


def test_cds_drought_trigger_is_co_located() -> None:
    """Function + CREATE TRIGGER must live in one file (audit finding F6/M1)."""
    triggers = list((DB_ROOT / "triggers").glob("*.sql"))
    assert triggers, "db/triggers/ must contain at least the cds_drought guard"
    for path in triggers:
        text = path.read_text(encoding="utf-8")
        assert "CREATE OR REPLACE FUNCTION" in text or "CREATE FUNCTION" in text, (
            f"{path.name}: trigger function must live alongside its binding"
        )
        assert "CREATE TRIGGER" in text, (
            f"{path.name}: CREATE TRIGGER must be co-located with its function"
        )
