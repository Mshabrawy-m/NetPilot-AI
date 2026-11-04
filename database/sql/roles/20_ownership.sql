-- NetPilot AI - Stage A6b-1 M3 (script 2 of 7): object ownership -> admin.
--
-- Purpose : make `netpilot_admin` the OWNER of every table and sequence in the
--           `public` schema, so DDL/migrations are the admin's exclusive job and
--           runtime roles obtain access only through the explicit GRANTs in
--           scripts 30-40 (never through ownership).
-- Run as  : a superuser, or Neon's `neon_superuser` (transferring ownership to
--           another role requires being a superuser or the current owner).
-- Ordering: run this AFTER the schema exists - i.e. after `init_db`/migrations
--           have created the tables (see the runbook: either bootstrap the
--           schema AS netpilot_admin, or run init_db once and then this script).
-- Idempotent : `ALTER ... OWNER TO` re-applied to the same owner is a no-op; the
--           loop simply re-asserts ownership on every run.
--
-- Foreign keys are NOT altered here. FK enforcement (interfaces.device_id,
-- forecast_predictions.run_id, ai_*.run_id, alert_events.rule_id,
-- change_audit_events.request_id) is executed by PostgreSQL as the *table
-- owner*, so moving ownership to admin does not change runtime behaviour and
-- the INSERT roles still need no REFERENCES grant.

-- Revoke the default PUBLIC pseudo-role's rights on the schema (defence in
-- depth; PostgreSQL 15+ already limits CREATE on public, this is belt-and-
-- suspenders for older/self-hosted servers).
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- Full schema rights for the owner role, including GRANT OPTION so admin can
-- delegate USAGE to runtime roles (script 21). Without GRANT OPTION, admin
-- cannot propagate the privilege and the runtime roles remain invisible.
GRANT ALL ON SCHEMA public TO netpilot_admin WITH GRANT OPTION;

-- Transfer ownership of every existing table and sequence to netpilot_admin.
-- New objects created by admin later are owned by admin automatically; see
-- script 40 for the (deliberately minimal) default-privilege policy.
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.relname AS name, c.relkind AS kind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'S')          -- ordinary tables + sequences
  LOOP
    IF r.kind = 'S' THEN
      EXECUTE format('ALTER SEQUENCE public.%I OWNER TO netpilot_admin', r.name);
    ELSE
      EXECUTE format('ALTER TABLE public.%I OWNER TO netpilot_admin', r.name);
    END IF;
  END LOOP;
END $$;
