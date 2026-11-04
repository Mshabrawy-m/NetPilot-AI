-- NetPilot AI - Stage A6b-1 M3 (script 1 of 7): create the roles.
--
-- Purpose : define the five least-privilege database roles used by the live
--           PostgreSQL deployment.

-- Run as  : a role permitted to create roles - a superuser, or Neon's
--           `neon_superuser` (Neon does not expose a plain superuser to customers).
-- Idempotent : guarded by pg_roles existence checks; safe to re-run.
-- Credentials : NONE. Roles are created WITHOUT a password. Authentication is
--           attached out-of-band by the operator from the secret manager / Neon
--           console (see docs/NetPilot_M3_PostgreSQL_Roles_Runbook.md). No
--           secret value ever appears in this repository.
--
-- If your deployment prefers a membership model instead of logging in as these
-- roles directly, drop `LOGIN` here and `GRANT <role> TO <your_login_user>`;
-- the privilege grants in scripts 30-40 are unchanged either way.

-- Schema owner / migration role. Holds DDL via object ownership (script 20),
-- never used by the running application.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netpilot_admin') THEN
    CREATE ROLE netpilot_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END $$;

-- Standalone collector runner (network/metrics_collector.collect_once).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netpilot_collector') THEN
    CREATE ROLE netpilot_collector LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END $$;

-- Hosted Streamlit application: reads broadly, writes analytics/audit rows.
-- Deliberately has NO DELETE and NO DDL (see scripts 31).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netpilot_app') THEN
    CREATE ROLE netpilot_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END $$;

-- Retention runner (tools/run_retention.py): SELECT/DELETE on metric_samples.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netpilot_retention') THEN
    CREATE ROLE netpilot_retention LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END $$;

-- Daily-rollup runner (tools/run_rollup.py).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netpilot_rollup') THEN
    CREATE ROLE netpilot_rollup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END $$;

-- No role here is a superuser or holds CREATEROLE/CREATEDB; each gets only the
-- object privileges granted in the scripts that follow.
