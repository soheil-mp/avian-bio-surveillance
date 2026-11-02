-- =========================================================================
-- ROLE GRANTS (object-specific)
-- =========================================================================
-- The local/dev stack now uses a single PostgreSQL login role (``root``).
-- Root already owns all objects, so there are no additional object-level
-- grants to apply here. The file remains in place so the bootstrap load order
-- stays stable and future multi-role deployments have a dedicated home.
-- =========================================================================

DO $$
BEGIN
    NULL;
END
$$;
