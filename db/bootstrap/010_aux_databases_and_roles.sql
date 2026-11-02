-- =========================================================================
-- AUXILIARY DATABASES + LEGACY ROLE CLEANUP
-- =========================================================================
-- psql script invoked by ``bootstrap/ensure_aux_roles.sh``.
--
-- It is intentionally kept under ``bootstrap/`` rather than ``grants/``:
-- database creation, public-schema hardening, and ownership cleanup must run
-- before baseline DDL and must be repeated against every relevant database.
-- =========================================================================

\set ON_ERROR_STOP on

SELECT set_config('ab.bootstrap_main_db', :'main_db', false);
SELECT set_config('ab.bootstrap_drop_roles', :'drop_legacy_roles', false);

\echo === aux roles: preparing current database ===

REVOKE CREATE ON SCHEMA public FROM public;

SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database())
\gexec

-- The Dagster instance keeps orchestration state outside the application
-- schema. Create it only from the main application database connection.
SELECT format('CREATE DATABASE %I', 'dagster_db')
WHERE current_database() = current_setting('ab.bootstrap_main_db')
  AND NOT EXISTS (
      SELECT 1
      FROM pg_database
      WHERE datname = 'dagster_db'
  )
\gexec

DO $$
DECLARE
    legacy_roles CONSTANT TEXT[] := ARRAY[
        'superuser',
        'admin',
        'data_engineer',
        'app_user',
        'read_only',
        'prefect_admin'
    ];
    legacy_role TEXT;
    target_role TEXT := current_user;
    should_drop_roles BOOLEAN :=
        COALESCE(current_setting('ab.bootstrap_drop_roles', true), 'false')::BOOLEAN;
BEGIN
    FOREACH legacy_role IN ARRAY legacy_roles LOOP
        IF legacy_role = target_role
           OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = legacy_role) THEN
            CONTINUE;
        END IF;

        EXECUTE format('REASSIGN OWNED BY %I TO %I', legacy_role, target_role);
        EXECUTE format('DROP OWNED BY %I', legacy_role);
        RAISE NOTICE
            'Reassigned and dropped owned objects for legacy role % in database %',
            legacy_role,
            current_database();

        IF should_drop_roles THEN
            EXECUTE format('DROP ROLE %I', legacy_role);
            RAISE NOTICE 'Dropped legacy role %', legacy_role;
        END IF;
    END LOOP;
END
$$;
