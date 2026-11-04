-- NetPilot AI - Stage A6b-1 M3 (script 6 of 7): netpilot_retention + netpilot_rollup.
--
-- Purpose : the two maintenance-runner roles, kept minimal and disjoint.
-- Run as  : netpilot_admin (table owner) or a superuser / neon_superuser.
--
-- netpilot_retention : tools/run_retention.py -> repository.delete_expired_live_samples.
--   The function SELECTs candidate ids then DELETEs expired *live* rows. It
--   touches metric_samples ONLY. It gets no INSERT, hence no sequence. It gets
--   no access to any other table.
-- netpilot_rollup    : tools/run_rollup.py -> repository.upsert_daily_rollups.
--   SELECT metric_samples (aggregation) ; SELECT + INSERT + UPDATE
--   metric_daily_rollups (ON CONFLICT DO UPDATE upsert). The INSERT needs USAGE
--   on the SERIAL sequence metric_daily_rollups_id_seq. No DELETE.
-- Generated from database/pg_privileges.py; asserted by tests/test_pg_roles_m3.py.

-- ---- netpilot_retention: SELECT + DELETE on metric_samples ONLY ------------
GRANT SELECT, DELETE ON TABLE public.metric_samples TO netpilot_retention;

-- ---- netpilot_rollup: rollup table + source samples ------------------------
GRANT SELECT, INSERT, UPDATE ON TABLE public.metric_daily_rollups TO netpilot_rollup;
GRANT SELECT ON TABLE public.metric_samples TO netpilot_rollup;

-- ---- netpilot_rollup: sequence for the upsert INSERT -----------------------
GRANT USAGE ON SEQUENCE public.metric_daily_rollups_id_seq TO netpilot_rollup;
