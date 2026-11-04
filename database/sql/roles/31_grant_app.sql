-- NetPilot AI - Stage A6b-1 M3 (script 5 of 7): netpilot_app grants.
--
-- Purpose : SELECT everywhere the UI reads, plus the INSERT/UPDATE the existing
--           Streamlit features persist. NO DELETE and NO DDL for this role.
-- Run as  : netpilot_admin (table owner) or a superuser / neon_superuser.
-- Derived from the UI + services write paths:
--   SELECT-only (inventory the app displays but never mutates):
--     devices, interfaces, links, metric_samples, collection_runs, incidents,
--     metric_daily_rollups
--   append-only INSERTs: forecast_runs, ai_status_transitions, ai_evidence,
--     change_audit_events, digital_twin_snapshot_bindings, lab_experiments,
--     agentic_analysis_runs, connectivity_tests
--   INSERT + UPDATE: forecast_predictions (backfill_actuals), anomaly_events
--     (update_/resolve_anomaly_event), ai_runs (update_ai_run/set_human_review),
--     alert_rules (set_alert_rule_enabled), alert_events (record_alert_firing/
--     acknowledge/resolve), change_requests (update_change_request),
--     users (set_last_login/set_user_active)
-- Row deletion (the System Status "reset" and purge) is admin-only by design;
-- the app role therefore holds NO DELETE. See the runbook limitation L-M3-2.
-- Generated from database/pg_privileges.py; asserted by tests/test_pg_roles_m3.py.

-- ---- table privileges -----------------------------------------------------
GRANT SELECT, INSERT ON TABLE public.agentic_analysis_runs TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.ai_evidence TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.ai_runs TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.ai_status_transitions TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.alert_events TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.alert_rules TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.anomaly_events TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.change_audit_events TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.change_requests TO netpilot_app;
GRANT SELECT ON TABLE public.collection_runs TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.connectivity_tests TO netpilot_app;
GRANT SELECT ON TABLE public.devices TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.digital_twin_snapshot_bindings TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.forecast_predictions TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.forecast_runs TO netpilot_app;
GRANT SELECT ON TABLE public.incidents TO netpilot_app;
GRANT SELECT ON TABLE public.interfaces TO netpilot_app;
GRANT SELECT, INSERT ON TABLE public.lab_experiments TO netpilot_app;
GRANT SELECT ON TABLE public.links TO netpilot_app;
GRANT SELECT ON TABLE public.metric_daily_rollups TO netpilot_app;
GRANT SELECT ON TABLE public.metric_samples TO netpilot_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.users TO netpilot_app;

-- ---- sequence privileges (only for the tables the app may INSERT into) ----
GRANT USAGE ON SEQUENCE public.agentic_analysis_runs_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.ai_evidence_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.ai_runs_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.ai_status_transitions_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.alert_events_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.alert_rules_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.anomaly_events_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.change_audit_events_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.change_requests_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.connectivity_tests_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.digital_twin_snapshot_bindings_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.forecast_predictions_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.forecast_runs_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.lab_experiments_id_seq TO netpilot_app;
GRANT USAGE ON SEQUENCE public.users_id_seq TO netpilot_app;
