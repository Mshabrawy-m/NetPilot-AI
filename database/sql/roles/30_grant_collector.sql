-- NetPilot AI - Stage A6b-1 M3 (script 4 of 7): netpilot_collector grants.
--
-- Purpose : the exact privileges the standalone collector runner needs.
-- Run as  : netpilot_admin (table owner) or a superuser / neon_superuser.
-- Derived from: tools/run_collector.py -> network.metrics_collector.collect_once
--   upsert_device / upsert_interface / upsert_link / set_device_status
--       -> devices, interfaces, links : SELECT + INSERT + UPDATE
--   add_metric_sample                 -> metric_samples : SELECT + INSERT
--   start_/finish_collection_run      -> collection_runs : SELECT + INSERT + UPDATE
--   open_incident / resolve_open_incidents -> incidents : SELECT + INSERT + UPDATE
-- The collector MERGES existing rows (SELECT before INSERT/UPDATE) and NEVER
-- deletes, so it gets no DELETE. SELECT on metric_samples is used only to
-- resolve prior state; it never removes samples.
-- These statements are generated from database/pg_privileges.py and are asserted
-- for exact equality by tests/test_pg_roles_m3.py - edit the module, not this file.

-- ---- table privileges -----------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON TABLE public.collection_runs TO netpilot_collector;
GRANT SELECT, INSERT, UPDATE ON TABLE public.devices TO netpilot_collector;
GRANT SELECT, INSERT, UPDATE ON TABLE public.incidents TO netpilot_collector;
GRANT SELECT, INSERT, UPDATE ON TABLE public.interfaces TO netpilot_collector;
GRANT SELECT, INSERT, UPDATE ON TABLE public.links TO netpilot_collector;
GRANT SELECT, INSERT ON TABLE public.metric_samples TO netpilot_collector;

-- ---- sequence privileges (required for the SERIAL id on every INSERT) -----
GRANT USAGE ON SEQUENCE public.collection_runs_id_seq TO netpilot_collector;
GRANT USAGE ON SEQUENCE public.devices_id_seq TO netpilot_collector;
GRANT USAGE ON SEQUENCE public.incidents_id_seq TO netpilot_collector;
GRANT USAGE ON SEQUENCE public.interfaces_id_seq TO netpilot_collector;
GRANT USAGE ON SEQUENCE public.links_id_seq TO netpilot_collector;
GRANT USAGE ON SEQUENCE public.metric_samples_id_seq TO netpilot_collector;
