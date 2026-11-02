# Database Permissions

Single source of truth for the current PostgreSQL access model.

## Role Matrix

| Role | Source env var | Login | Scope | Notes |
|---|---|---|---|---|
| `root` | `POSTGRES_USER` | yes | full on `avian_biosurveillance` + `dagster_db` | Single local/dev login used by bootstrap, ingestion, frontend, maintenance scripts, and Dagster. |

## Hardening

Applied by `bootstrap/010_aux_databases_and_roles.sql` through the
`bootstrap/ensure_aux_roles.sh` connection wrapper:

- `REVOKE CREATE ON SCHEMA public FROM PUBLIC` on both databases.
- `REVOKE ALL ON DATABASE <db> FROM PUBLIC` so no anonymous/default access remains.
- Legacy roles (`superuser`, `admin`, `data_engineer`, `app_user`, `read_only`, `prefect_admin`) are reassigned to `root`, cleaned up with `DROP OWNED`, then dropped when present.

## Grants

- Tables, views, functions, sequences, and continuous aggregates are owned by `root` in the local/dev stack.
- `grants/010_role_grants.sql` is intentionally a no-op placeholder until a future multi-role deployment model is reintroduced.

## If You Reintroduce Multiple Roles

1. Add the new env vars to `.env`, the deployment env files, and the configuration table in `infrastructure/docker/README.md`.
2. Restore explicit role provisioning in `bootstrap/010_aux_databases_and_roles.sql`.
3. Restore object-specific grants in `grants/010_role_grants.sql`.
4. Update this file and add privilege-focused tests.
