# Database Seeds

`db/seeds/` is the reserved home for deterministic SQL seed data: small lookup
sets, reference rows, demo fixtures, or reproducible development data that
should be versioned with the schema.

Seeds are intentionally **not** part of the default bootstrap path. The default
database init must create schema only; ingestion and backfill scripts remain
responsible for production-like observational data.

## What Belongs Here

- Small reference rows required for local development or repeatable demos.
- Static lookup data that is safer as SQL than as a Python repair script.
- Test fixtures that need stable primary keys and can be loaded explicitly.

## What Does Not Belong Here

- Historical source data from BirdWeather, KNMI, Movebank, eBird, Earth Engine,
  or CDS drought.
- Large generated fixtures.
- One-way repair/backfill logic. Use `db/migrations/` for ledger-backed SQL
  changes or `scripts/repair/` for manual operational workflows.

## File Convention

Use ordered, idempotent SQL files:

```text
010_reference_name.sql
020_demo_rows.sql
```

Seed statements should use stable keys plus `ON CONFLICT` so explicit re-runs
are safe.
