-- NetPilot AI - Stage A6b-1 M3 (script 7 of 7): default privileges.
--
-- Purpose : define what happens to objects created in the FUTURE by the owner
--           role, so additive migrations stay workable without silently
--           widening privileges.
-- Run as  : netpilot_admin (default privileges apply to objects that role creates).
-- Idempotent : ALTER DEFAULT PRIVILEGES replaces the grantor's default list for
--           the object type; re-applying the same GRANT is a no-op.
--
-- POLICY (deliberately narrow, least-privilege):
--   The schema is ADDITIVE-ONLY (init_db uses CREATE TABLE IF NOT EXISTS; no
--   destructive migration). To keep the hosted *reader* working across a new
--   additive table without re-running this repo, netpilot_app is granted SELECT
--   on future tables. This is the ONLY blanket default privilege.
--   There is intentionally NO default INSERT/UPDATE/DELETE for any runtime role
--   and NO default sequence privilege. A brand-new writable table must be
--   granted to its writer explicitly (extend database/pg_privileges.py and
--   scripts 30-32). That keeps "new table" from ever silently becoming
--   deletable or writable by the wrong role.
--   If a future table ever holds data the app must NOT read, drop this default
--   and grant SELECT per table instead (one line to remove below).

ALTER DEFAULT PRIVILEGES FOR ROLE netpilot_admin IN SCHEMA public
    GRANT SELECT ON TABLES TO netpilot_app;

-- Explicitly none of the following (documented for reviewers):
--   ALTER DEFAULT PRIVILEGES ... GRANT INSERT/UPDATE/DELETE ...  -- not granted
--   ALTER DEFAULT PRIVILEGES ... GRANT USAGE ON SEQUENCES ...    -- not granted
