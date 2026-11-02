# Database Migrations

`db/migrations/` contains one-way SQL changes that must run once per database.
Use this folder for legacy catch-up, destructive DDL, and data backfills that
cannot safely live in the always-replayed `db/baseline/` files.

## When to Use Each Path

- Use `db/baseline/` for the canonical schema every fresh database should have.
  Baseline files must be idempotent and safe to run on every bootstrap.
- Use `db/migrations/` for ordered, ledger-backed changes that should not be
  replayed after they succeed.
- Keep Python repair scripts in `scripts/repair/` for operational workflows.
  If a repair changes database state permanently, add a corresponding SQL
  migration or document why the repair remains manual-only.

## Naming

Migration files must use:

```text
V<NNN>__short_name.sql
```

Examples:

```text
V001__backfill_grid_cell_ids.sql
V002__drop_legacy_station_columns.sql
```

Use lowercase letters, numbers, and underscores after the double underscore.
Numbers must be zero-padded so lexicographic order is the apply order.

## Apply Contract

`db/bootstrap/init_database.sh` creates `schema_migrations_tbl` and records the
basename of each applied migration. Each migration file and its ledger insert
run in one PostgreSQL transaction via `psql --single-transaction`, so a failed
migration is not marked as applied.

Prefer idempotent statements even here (`ADD COLUMN IF NOT EXISTS`,
`UPDATE ... WHERE ...`, `ON CONFLICT`) because they make failed deploy retries
and local testing safer.

## Rollback Posture

Migrations are forward-only. For production-like environments, take a database
backup before applying migrations that drop, rewrite, or irreversibly transform
data. If a bad migration ships, add a new forward migration that repairs the
state rather than editing an already-applied file.
