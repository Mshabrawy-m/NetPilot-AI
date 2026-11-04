-- NetPilot AI - Stage A6b-1 M3 (script 3 of 7): schema privileges for runtime roles.
--
-- Purpose : give the four runtime roles the minimum schema access they need -
--           USAGE (the right to *see* objects in `public`), and explicitly NOT
--           CREATE (no runtime role may create tables/functions = no DDL).
-- Run as  : netpilot_admin (the schema owner) or a superuser / neon_superuser.
-- Idempotent : GRANT is idempotent.
--
-- USAGE on the schema is separate from and additional to the per-table /
-- per-sequence GRANTs in scripts 30-33: without it a role could not resolve any
-- object name, and with it but without CREATE it still cannot issue DDL.

GRANT USAGE ON SCHEMA public TO netpilot_collector;
GRANT USAGE ON SCHEMA public TO netpilot_app;
GRANT USAGE ON SCHEMA public TO netpilot_retention;
GRANT USAGE ON SCHEMA public TO netpilot_rollup;

-- Intentionally NO `GRANT CREATE ON SCHEMA public` for any runtime role. DDL is
-- confined to netpilot_admin (which owns the schema; see script 20).
