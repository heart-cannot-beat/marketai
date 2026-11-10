-- MarketAI domain database: least-privilege runtime roles.
--
-- `deploy/postgres/control-plane-domain-roles.sql` establishes the boundary
-- *between* the control-plane and domain databases. This file divides the
-- domain database itself, so that no single role can both change the schema and
-- serve requests, and so that Row Level Security actually applies to the roles
-- that serve them.
--
--   marketai_domain_owner      owns the schema and runs Alembic. BYPASSRLS,
--                              because a migration has to see every Workspace.
--                              Never used to serve a request.
--   marketai_domain_api        FastAPI. DML only, RLS enforced.
--   marketai_domain_worker     Worker. DML only, RLS enforced.
--   marketai_domain_readonly   Reporting and support. SELECT only, RLS enforced.
--
-- The three runtime roles are explicitly NOBYPASSRLS and own nothing: a table
-- owner is exempt from its own policies unless the table also has FORCE ROW
-- LEVEL SECURITY, and relying on FORCE alone would make one forgotten table a
-- silent cross-tenant read. Not owning the tables is the durable half of that;
-- the migration sets FORCE as well.
--
-- This file must name only the domain database. It runs on every release
-- (`make db-release` applies it through `apply-domain-roles.sh`), and a
-- release that touches `marketai_control` is a release that changes the other
-- deployment unit's ACLs -- and that fails outright on a domain-only cluster,
-- where that database does not exist. The denial of control-plane access to
-- the domain runtime roles therefore lives in `cross-database-denials.sql`,
-- applied by the one-time bootstrap.
--
-- Normally applied by `deploy/postgres/apply-roles.sh` in the same psql
-- process as `control-plane-domain-roles.sql`. Run standalone as a cluster
-- superuser like this:
--
--   psql -v ON_ERROR_STOP=1 -d "$SUPERUSER_DATABASE_URL" \
--        -v api_password="$DOMAIN_API_PASSWORD" \
--        -v worker_password="$DOMAIN_WORKER_PASSWORD" \
--        -v readonly_password="$DOMAIN_READONLY_PASSWORD" \
--        -v release_nonce="$(openssl rand -hex 16)" \
--        -f deploy/postgres/domain-tenant-roles.sql
--
-- Passwords come from the caller's secret manager; none are stored here.

\set ON_ERROR_STOP on

-- The runtime roles live inside marketai_domain, so switch there rather than
-- asking the caller for a second connection string. `-reuse-previous=on`
-- carries every parameter of the connection already open -- `sslmode`
-- included -- which a reconstructed URL does not.
\connect -reuse-previous=on marketai_domain

-- Everything below runs in one transaction. The privilege section revokes
-- every runtime role's table and sequence privileges and grants the declared
-- matrix back, and a release applies this file while FastAPI and the Worker
-- keep serving: other sessions must see the previous matrix or the new one,
-- never the revoked state in between. (Golden Path PR Validation run
-- 36517751730 caught the Worker reading operation_records in that gap; the
-- read was refused and the process exited.) Every statement here is
-- transactional, and with ON_ERROR_STOP psql exits on the first error, which
-- rolls the whole convergence back.
BEGIN;

-- ---------------------------------------------------------------- roles ----

SELECT 'CREATE ROLE marketai_domain_api LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_domain_api')
\gexec
ALTER ROLE marketai_domain_api LOGIN NOBYPASSRLS PASSWORD :'api_password';

SELECT 'CREATE ROLE marketai_domain_worker LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_domain_worker')
\gexec
ALTER ROLE marketai_domain_worker LOGIN NOBYPASSRLS PASSWORD :'worker_password';

SELECT 'CREATE ROLE marketai_domain_readonly LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_domain_readonly')
\gexec
ALTER ROLE marketai_domain_readonly LOGIN NOBYPASSRLS PASSWORD :'readonly_password';

SELECT 'CREATE ROLE marketai_projection_writer NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_projection_writer')
\gexec
ALTER ROLE marketai_projection_writer NOLOGIN NOBYPASSRLS;
-- API and worker invoke narrowly-scoped SECURITY DEFINER functions when a
-- projection needs writer privileges.  Membership would also grant arbitrary
-- DELETE and SET ROLE, defeating that boundary.  REVOKE makes reruns converge
-- clusters that received the earlier, overly broad membership.
REVOKE marketai_projection_writer FROM marketai_domain_api, marketai_domain_worker;

SELECT 'CREATE ROLE marketai_erasure_principal NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_erasure_principal')
\gexec
ALTER ROLE marketai_erasure_principal NOLOGIN NOBYPASSRLS;

-- Only the migration role may change the schema, and it is the only one
-- allowed to see across Workspaces.
ALTER ROLE marketai_domain_owner BYPASSRLS;

-- No runtime role inherits the migration role's exemption.
REVOKE marketai_domain_owner FROM
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;

-- ------------------------------------------------- release identity ----
--
-- Stamp the database with the nonce this run generated. `release.sh` converges
-- roles over the superuser connection and migrates over MARKETAI_DATABASE_URL:
-- two independent strings, which may name two different clusters. Comparing
-- database name, role and bridge state cannot separate them -- two clusters
-- prepared the same way answer all three identically -- so the migration
-- target has to carry a mark that only *this* convergence could have left.
--
-- The verifier reads the database-level entry from `pg_db_role_setting`
-- directly. `current_setting` is not evidence of the stamp: a role default or
-- connection `options=-c...` value can override it for the session and make a
-- different database appear to carry this run's nonce. The catalog row is
-- scoped to exactly one database on exactly one cluster and cannot be shadowed
-- that way.
ALTER DATABASE marketai_domain SET marketai.release_nonce = :'release_nonce';

-- Alembic creates this owner-only table. The role file runs both before and
-- after migrations, so a fresh release skips it once and fills it on the
-- second pass. Runtime roles can call the verifier but cannot read the key.
SELECT (to_regclass('public.tenant_context_secrets') IS NOT NULL) AS context_key_table_exists
\gset
\if :context_key_table_exists
INSERT INTO public.tenant_context_secrets (key_id, secret)
VALUES (1, :'tenant_context_secret')
ON CONFLICT (key_id) DO UPDATE SET secret = EXCLUDED.secret;
REVOKE ALL ON TABLE public.tenant_context_secrets FROM PUBLIC;
REVOKE ALL ON TABLE public.tenant_context_secrets FROM
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
\endif

-- --------------------------------------------- retired compatibility role ----
--
-- FastAPI and the Worker now connect as `marketai_domain_api` and
-- `marketai_domain_worker`; every domain unit of work receives a TenantContext.
-- The former shared role is retained only so an already-provisioned cluster is
-- converged safely during rollout.  NOBYPASSRLS is an explicit desired state,
-- not an omitted grant: deleting this statement would leave old clusters with
-- their persistent exemption.
--
-- `tests/test_domain_app_transition.py` keeps both halves executable. Its AST
-- gate requires zero DEFAULT_WORKSPACE_ID reads and zero untenantized session
-- calls before NOBYPASSRLS may be declared; its PostgreSQL drill proves that a
-- cluster which once held the bridge is actually revoked and fails closed.
ALTER ROLE marketai_domain_app NOBYPASSRLS;

-- Retiring the bypass without retiring the old ACLs would leave dormant
-- credentials able to read any future table whose migration forgot RLS.
-- Revoke both present and default privileges on every release. CONNECT remains
-- only so release diagnostics can identify a deployment accidentally pointed
-- at this role; it has no access to the domain schema.
REVOKE ALL ON SCHEMA public FROM marketai_domain_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM marketai_domain_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM marketai_domain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM marketai_domain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE USAGE, SELECT ON SEQUENCES FROM marketai_domain_app;

-- ------------------------------------------------------------ database ----

REVOKE ALL ON DATABASE marketai_domain FROM PUBLIC;

GRANT CONNECT, TEMPORARY ON DATABASE marketai_domain TO
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;

-- -------------------------------------------------------------- schema ----

GRANT USAGE ON SCHEMA public TO
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;

-- No CREATE: a runtime role must not be able to add a table that has no policy.
REVOKE CREATE ON SCHEMA public FROM
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;

-- New tables fail closed until the migration that introduces their runtime
-- statements grants them explicitly.  A blanket default DML grant would make
-- every future schema change an unreviewed privilege expansion.
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES
  FROM marketai_domain_api, marketai_domain_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE SELECT ON TABLES FROM marketai_domain_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE USAGE, SELECT ON SEQUENCES
  FROM marketai_domain_api, marketai_domain_worker;

-- API: request-side commands, queries and durable dispatch. Worker: execution
-- state only. There is no DELETE grant. The role script also runs before the
-- first migration on a fresh cluster, so grants are applied only to relations
-- that exist; the migration repeats the same matrix after creating the schema.
DO $grants$
DECLARE
  relation_name text;
BEGIN
  -- ---- SELECT: API + Readonly (shared) ----
  -- Tables that both API and Readonly may read. Credential base tables are
  -- excluded here (ciphertext exposure); narrowly-scoped metadata columns
  -- needed by request-side readiness checks are granted separately below.
  -- entitlement_snapshot_inbox and entitlement_projections are handled
  -- separately because they have different API/Readonly needs.
  FOREACH relation_name IN ARRAY ARRAY[
    'workspaces', 'products', 'dimension_schema_versions', 'dimensions',
    'dimension_values', 'dimension_concept_keys', 'plans', 'plan_entries',
    'generation_runs',
    'generation_drafts', 'resolution_previews',
    'resolution_preview_confirmations', 'generation_recipe_snapshots',
    'run_items', 'provider_attempt_events', 'provider_attempt_projections',
    'run_item_asset_version_bindings', 'asset_qc_attempts',
    'asset_qc_check_results', 'generation_run_commands', 'export_packages',
    'assets',
    'asset_versions', 'quality_reviews', 'verdict_decisions', 'jobs', 'cost_ledger',
    'meta_accounts', 'meta_sync_runs', 'meta_ingestions', 'meta_ingestion_pages',
    'ad_daily_metrics', 'meta_entity_status_events', 'meta_ad_bindings',
    'launch_previews', 'launch_confirmations', 'launch_operations', 'launch_steps',
    'meta_sync_schedules', 'experiments', 'experiment_entries',
    'experiment_ad_bindings', 'judgements',
    'experiment_decisions', 'operation_records', 'idempotency_records',
    'upload_intent_records', 'download_intent_records',
    'library_entries', 'library_drafts', 'library_versions',
    'library_dependencies', 'reference_objects', 'reference_bindings',
    'library_events', 'library_idempotency', 'library_command_impacts',
    'library_entry_contexts', 'library_stepup_assertions',
    'product_intakes',
    'test_plans', 'test_plan_directions', 'test_plan_runs', 'generation_defaults',
    'result_imports', 'result_observations', 'learning_snapshots',
    'external_creatives', 'external_creative_tag_confirmations',
    'entitlement_projections',
    'spend_guardrails',
    'customer_notification_outbox',
    'guardrail_notification_states',
    'experiment_versions', 'experiment_version_candidates',
    'experiment_version_candidate_concepts', 'experiment_version_concept_resolutions',
    'decision_proposals', 'decision_proposal_state_events', 'decision_confirmations',
    'experiment_eligibility_events', 'experiment_eligibility_projections',
    'contextual_verdict_projections',
    'experiment_timeline_heads', 'experiment_timeline_events',
    'experiment_notification_outbox', 'experiment_next_round_records',
    'experiment_pattern_candidates', 'onboarding_fact_outbox',
    'market_policies', 'market_policy_versions', 'market_policy_version_events',
    'truth_packages', 'truth_package_markets', 'truth_source_attempts',
    'truth_facts', 'truth_derived_facts', 'truth_derived_fact_inputs',
    'fact_library_dependencies', 'fact_market_policy_dependencies',
    'localization_batches', 'localization_batch_sources',
    'localization_batch_commands', 'localization_batch_market_targets',
    'source_claim_inventories', 'source_claim_confirmations',
    'localization_claim_decision_sets', 'localization_claim_decisions',
    'localized_copy_versions', 'localized_copy_approvals',
    'localization_items', 'localization_attempts',
    'localization_attempt_outputs', 'localization_output_publications',
    'localization_qc_records', 'localization_semantic_claims',
    'localization_item_provider_calls',
    'localization_preparation_provider_calls',
    'market_asset_bindings', 'market_asset_binding_events',
    'localized_asset_lineages',
    'scale_delivery_targets', 'scale_command_journal',
    'scale_lineage_guards', 'scale_enrollment_candidates',
    'scale_delivery_evidence_snapshots', 'scale_activation_previews'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_api, marketai_domain_readonly', relation_name);
    END IF;
  END LOOP;

  -- ---- SELECT: API only (additional tables) ----
  -- spend_reservations and capacity_reservations are request-side state;
  -- the readonly role does not need them. usage_events backs
  -- `GET /api/v1/usage` (#347); without it every call was a 500.
  FOREACH relation_name IN ARRAY ARRAY[
    'spend_reservations', 'capacity_reservations',
    'provider_call_attempts', 'provider_cost_ledger',
    'usage_events'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;

  -- ---- SELECT: Readonly (additional tables) ----
  -- usage_events and provider_cost_ledger are reporting tables. The API reads
  -- both too (the group above); the Worker reads neither.
  FOREACH relation_name IN ARRAY ARRAY[
    'usage_events', 'provider_cost_ledger'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_readonly', relation_name);
    END IF;
  END LOOP;

  -- credential_metadata view: non-secret columns from credential_versions.
  -- The API serves the credential lifecycle (create / rotate / revoke / bind
  -- / list, `app.entitlements.credential_admin`) and the capabilities and
  -- Generation admission readiness checks from the two credential base
  -- tables. Keep those as column grants of the non-secret columns only:
  -- table-level SELECT would expose encrypted_secret, encrypted_data_key and
  -- nonce, which only the Worker (the decrypting side) may read.
  IF to_regclass('public.credential_metadata') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT ON public.credential_metadata TO marketai_domain_api, marketai_domain_readonly';
  END IF;
  IF to_regclass('public.credential_versions') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT (id, workspace_id, credential_key, version_no, provider, '
         || 'display_label, state, encryption_key_id, encryption_algorithm, '
         || 'aad_schema_version, created_by, created_at, rotated_at, rotated_by, '
         || 'revoked_at, revoked_by, revoke_reason) '
         || 'ON TABLE public.credential_versions TO marketai_domain_api';
  END IF;
  IF to_regclass('public.credential_bindings') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT (id, workspace_id, binding_scope, provider, credential_version_id, '
         || 'revision, bound_by, created_at, updated_at) '
         || 'ON TABLE public.credential_bindings TO marketai_domain_api';
  END IF;

  -- Operational history is queried through the read-only identity, never by
  -- the request-serving API role. Keep those capabilities distinct even
  -- though both roles are constrained by the same tenant RLS policies.
  FOREACH relation_name IN ARRAY ARRAY[
    'audit_events', 'launch_audit_log', 'experiment_state_events',
    'operation_state_events', 'library_audit'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_readonly', relation_name);
    END IF;
  END LOOP;

  -- The one exception: the API serves the Workspace audit trail itself
  -- (`GET /api/v1/audit`, spec §8.8, `app/main.py::list_audit_events`) and
  -- has no second, read-only connection to route that through. Reads stay
  -- inside the tenant RLS policy; the append-only trigger keeps the rows
  -- immutable; UPDATE/DELETE are never granted.
  IF to_regclass('public.audit_events') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT ON TABLE public.audit_events TO marketai_domain_api';
  END IF;

  -- ---- UPDATE: API ----
  -- No UPDATE on entitlement_projections (projection function only),
  -- provider_call_attempts (Worker-only CAS UPDATE), or
  -- spend_reservations/capacity_reservations (API creates; Worker CAS updates).
  FOREACH relation_name IN ARRAY ARRAY[
    'products', 'generation_runs', 'generation_drafts', 'resolution_previews',
    'run_items', 'asset_versions', 'jobs',
    'meta_sync_schedules', 'experiments',
    -- Draft entries are edited in place and removed by setting removed_at
    -- (#284); a frozen experiment's entries never change.
    'experiment_entries',
    'launch_confirmations', 'operation_records', 'idempotency_records',
    'upload_intent_records', 'download_intent_records',
    'library_entries', 'library_drafts', 'library_versions', 'reference_objects',
    'library_idempotency', 'library_command_impacts',
    'library_entry_contexts', 'library_stepup_assertions',
    'product_intakes',
    'test_plans', 'test_plan_directions', 'test_plan_runs', 'generation_defaults',
    -- Results imports move preview -> committed (#166); observations and
    -- learning snapshots are append-only and never get UPDATE.
    'result_imports',
    -- External creatives (#168): confirm advances the row; tag
    -- confirmations are append-only and never get UPDATE.
    'external_creatives',
    'credential_versions', 'credential_bindings',
    'spend_guardrails',
    'customer_notification_outbox',
    'guardrail_notification_states',
    'experiment_eligibility_projections',
    'contextual_verdict_projections',
    'experiment_timeline_heads',
    'localization_batches', 'localization_items',
    'localized_copy_versions',
    'localization_semantic_claims',
    'localization_preparation_provider_calls',
    'market_asset_bindings'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT UPDATE ON TABLE public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;

  -- ---- INSERT: API ----
  -- No direct INSERT on entitlement_snapshot_inbox or entitlement_projections
  -- (written only through marketai_apply_workspace_projection_v2).
  -- No INSERT on provider_call_attempts (Worker-only).
  FOREACH relation_name IN ARRAY ARRAY[
    'products', 'generation_runs', 'generation_drafts',
    'resolution_previews', 'resolution_preview_confirmations',
    'generation_recipe_snapshots', 'run_items', 'generation_run_commands',
    'export_packages', 'assets',
    -- Adopting an isolated Localization output publishes its child
    -- AssetVersion in the API transaction, like finalize does in the Worker
    -- (#46, spec §3H).
    'asset_versions', 'jobs',
    'cost_ledger', 'meta_accounts',
    'meta_sync_runs', 'meta_ingestions', 'meta_sync_schedules', 'experiments',
    'launch_previews', 'launch_confirmations', 'launch_operations',
    'dimension_schema_versions', 'dimensions', 'dimension_values',
    'dimension_concept_keys', 'plans', 'plan_entries', 'quality_reviews',
    'verdict_decisions', 'experiment_entries',
    'experiment_ad_bindings', 'experiment_state_events', 'judgements',
    'experiment_decisions', 'launch_audit_log', 'operation_records',
    'operation_state_events', 'idempotency_records',
    'upload_intent_records', 'download_intent_records',
    'library_entries', 'library_drafts', 'library_versions',
    'library_dependencies', 'reference_objects', 'reference_bindings',
    'library_events', 'library_audit', 'library_idempotency',
    'library_command_impacts', 'library_entry_contexts', 'library_stepup_assertions',
    'product_intakes',
    'test_plans', 'test_plan_directions', 'test_plan_runs', 'generation_defaults',
    'result_imports', 'result_observations', 'learning_snapshots',
    'external_creatives', 'external_creative_tag_confirmations',
    'credential_versions', 'credential_bindings',
    'spend_guardrails', 'spend_guardrail_events',
    'spend_reservations', 'capacity_reservations',
    'provider_cost_ledger',
    'usage_events', 'customer_notification_outbox',
    'guardrail_notification_states',
    'experiment_versions', 'experiment_version_candidates',
    'experiment_version_candidate_concepts', 'experiment_version_concept_resolutions',
    'decision_proposals', 'decision_proposal_state_events', 'decision_confirmations',
    'experiment_eligibility_events', 'experiment_eligibility_projections',
    'contextual_verdict_projections',
    'experiment_timeline_heads', 'experiment_timeline_events',
    'experiment_notification_outbox', 'experiment_next_round_records',
    'experiment_pattern_candidates', 'onboarding_fact_outbox',
    'market_policy_versions', 'market_policy_version_events',
    'truth_packages', 'truth_package_markets', 'truth_source_attempts',
    'truth_facts', 'truth_derived_facts', 'truth_derived_fact_inputs',
    'fact_library_dependencies', 'fact_market_policy_dependencies',
    'localization_batches', 'localization_batch_sources',
    'localization_batch_commands', 'localization_batch_market_targets',
    'source_claim_inventories', 'source_claim_confirmations',
    'localization_claim_decision_sets', 'localization_claim_decisions',
    'localized_copy_versions', 'localized_copy_approvals',
    'localization_items', 'localization_attempts',
    'localization_attempt_outputs', 'localization_output_publications',
    'localization_qc_records', 'localization_semantic_claims',
    'localization_item_provider_calls',
    'localization_preparation_provider_calls',
    'market_asset_bindings', 'market_asset_binding_events',
    'localized_asset_lineages',
    'scale_delivery_targets', 'scale_command_journal',
    'scale_lineage_guards', 'scale_enrollment_candidates',
    'scale_delivery_evidence_snapshots', 'scale_activation_previews'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT INSERT ON TABLE public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;
  IF to_regclass('public.audit_events') IS NOT NULL THEN
    EXECUTE 'GRANT INSERT ON TABLE public.audit_events TO marketai_domain_api';
  END IF;

  -- ---- SELECT: Worker ----
  -- credential_versions and credential_bindings for envelope access and
  -- binding lookup at execution time.
  -- No spend_guardrails, provider_cost_ledger, usage_events,
  -- customer_notification_outbox, or guardrail_notification_states.
  FOREACH relation_name IN ARRAY ARRAY[
    'workspaces', 'products', 'generation_runs', 'generation_drafts',
    'resolution_previews', 'resolution_preview_confirmations',
    'generation_recipe_snapshots', 'run_items', 'provider_attempt_events',
    'provider_attempt_projections', 'run_item_asset_version_bindings',
    'asset_qc_attempts', 'asset_qc_check_results', 'generation_run_commands',
    'export_packages', 'assets', 'asset_versions', 'jobs', 'cost_ledger',
    'meta_accounts', 'meta_sync_runs', 'meta_ingestions', 'meta_ingestion_pages',
    'ad_daily_metrics', 'meta_entity_status_events', 'meta_ad_bindings',
    'meta_sync_schedules', 'launch_previews', 'launch_operations', 'launch_steps', 'launch_audit_log',
    'experiments', 'experiment_entries', 'experiment_ad_bindings',
    -- Localization jobs re-validate their source Winner, which reads the
    -- Judgement behind the Decision (winner_eligibility.evaluate_winner_identity).
    'experiment_decisions', 'judgements', 'quality_reviews', 'operation_records',
    'idempotency_records', 'upload_intent_records', 'download_intent_records',
    'library_entries', 'library_drafts', 'library_versions',
    'library_dependencies', 'reference_objects', 'reference_bindings',
    'library_events', 'library_command_impacts', 'library_entry_contexts',
    'library_stepup_assertions', 'product_intakes',
    -- The test-plan drafting Job reads its plan and writes the directions (#163).
    'test_plans', 'test_plan_directions',
    -- The external-creative Job gates, stores and tags its upload (#168).
    'external_creatives', 'external_creative_tag_confirmations',
    'credential_versions', 'credential_bindings',
    'entitlement_projections',
    'spend_reservations', 'capacity_reservations',
    'provider_call_attempts', 'provider_attempt_events',
    'provider_attempt_projections',
    'experiment_versions', 'experiment_version_candidates',
    'experiment_version_candidate_concepts', 'experiment_version_concept_resolutions',
    'decision_proposals', 'decision_proposal_state_events', 'decision_confirmations',
    'experiment_eligibility_events', 'experiment_eligibility_projections',
    'contextual_verdict_projections',
    'experiment_timeline_heads', 'experiment_timeline_events',
    'experiment_notification_outbox', 'experiment_next_round_records',
    'experiment_pattern_candidates', 'onboarding_fact_outbox',
    'market_policies', 'market_policy_versions', 'market_policy_version_events',
    'truth_packages', 'truth_package_markets', 'truth_source_attempts',
    'truth_facts', 'truth_derived_facts', 'truth_derived_fact_inputs',
    'fact_library_dependencies', 'fact_market_policy_dependencies',
    'localization_batches', 'localization_batch_sources',
    'localization_batch_commands', 'localization_batch_market_targets',
    'source_claim_inventories', 'source_claim_confirmations',
    'localization_claim_decision_sets', 'localization_claim_decisions',
    'localized_copy_versions', 'localized_copy_approvals',
    'localization_items', 'localization_attempts',
    'localization_attempt_outputs', 'localization_output_publications',
    'localization_qc_records', 'localization_semantic_claims',
    'localization_item_provider_calls',
    'localization_preparation_provider_calls',
    'market_asset_bindings', 'market_asset_binding_events',
    'localized_asset_lineages',
    'scale_delivery_targets', 'scale_command_journal',
    'scale_lineage_guards', 'scale_enrollment_candidates',
    'scale_delivery_evidence_snapshots', 'scale_activation_previews'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_worker', relation_name);
    END IF;
  END LOOP;
  IF to_regclass('public.audit_events') IS NOT NULL THEN
    EXECUTE 'GRANT INSERT ON TABLE public.audit_events TO marketai_domain_worker';
  END IF;

  -- ---- INSERT: Worker ----
  FOREACH relation_name IN ARRAY ARRAY[
    'assets', 'asset_versions', 'cost_ledger', 'spend_reservations',
    'meta_ingestion_pages', 'ad_daily_metrics',
    'meta_entity_status_events', 'meta_ad_bindings', 'launch_steps',
    'launch_audit_log', 'experiment_ad_bindings', 'meta_sync_runs',
    'meta_ingestions', 'jobs', 'operation_state_events',
    -- A scheduled Meta observation sync has no API command to admit it:
    -- its tick Job opens one Operation per sync run (zze4f5a6b7c8, #107).
    'operation_records',
    'library_entries', 'library_drafts', 'library_events', 'library_audit',
    -- The reference gate (#158) and product intake (#161) run in the Worker:
    -- intake quarantines the images it fetched as reference objects.
    'reference_objects',
    'test_plan_directions',
    'provider_cost_ledger', 'usage_events', 'provider_call_attempts',
    'provider_attempt_events', 'provider_attempt_projections',
    'run_item_asset_version_bindings', 'asset_qc_attempts',
    'asset_qc_check_results',
    -- The VLM quality check (#169) projects a failed image's quality as an
    -- append-only `machine_qc` review row (Needs Changes / Rejected).
    'quality_reviews',
    'experiment_versions', 'experiment_version_candidates',
    'experiment_version_candidate_concepts', 'experiment_version_concept_resolutions',
    'decision_proposals', 'decision_proposal_state_events', 'decision_confirmations',
    'experiment_eligibility_events', 'experiment_eligibility_projections',
    'contextual_verdict_projections',
    'experiment_timeline_heads', 'experiment_timeline_events',
    'experiment_notification_outbox', 'experiment_next_round_records',
    'experiment_pattern_candidates', 'onboarding_fact_outbox',
    'truth_packages', 'truth_package_markets', 'truth_source_attempts',
    'truth_facts', 'truth_derived_facts', 'truth_derived_fact_inputs',
    'fact_library_dependencies', 'fact_market_policy_dependencies',
    'localization_batches', 'localization_batch_sources',
    'localization_batch_commands', 'localization_batch_market_targets',
    'source_claim_inventories', 'source_claim_confirmations',
    'localization_claim_decision_sets', 'localization_claim_decisions',
    'localized_copy_versions', 'localized_copy_approvals',
    'localization_items', 'localization_attempts',
    'localization_attempt_outputs', 'localization_output_publications',
    'localization_qc_records', 'localization_semantic_claims',
    'localization_item_provider_calls',
    'localization_preparation_provider_calls',
    'market_asset_bindings', 'market_asset_binding_events',
    'localized_asset_lineages',
    'scale_delivery_targets', 'scale_command_journal',
    'scale_lineage_guards', 'scale_enrollment_candidates',
    'scale_delivery_evidence_snapshots', 'scale_activation_previews'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT INSERT ON TABLE public.%I TO marketai_domain_worker', relation_name);
    END IF;
  END LOOP;

  -- ---- UPDATE: Worker ----
  -- CAS state transitions only; no customer_notification_outbox.
  FOREACH relation_name IN ARRAY ARRAY[
    'generation_runs', 'run_items', 'asset_versions', 'asset_qc_attempts',
    'export_packages',
    'assets', 'jobs', 'meta_accounts', 'meta_sync_runs',
    'meta_ingestions', 'ad_daily_metrics', 'meta_sync_schedules',
    'launch_operations', 'launch_steps', 'operation_records', 'idempotency_records',
    'upload_intent_records', 'download_intent_records',
    'library_versions',
    -- The Worker reference gate records its verdict on the object (#158),
    -- and the product-intake Job advances its intake (#161).
    'reference_objects', 'product_intakes',
    -- The test-plan drafting Job advances its plan (#163).
    'test_plans',
    -- The external-creative Job advances its row (#168).
    'external_creatives',
    'spend_reservations', 'capacity_reservations',
    'provider_call_attempts', 'provider_attempt_projections',
    'onboarding_fact_outbox',
    'experiment_eligibility_projections',
    'contextual_verdict_projections',
    'localization_batches', 'localization_items',
    'localization_attempts',
    'localization_semantic_claims',
    'localization_preparation_provider_calls',
    'market_asset_bindings'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT UPDATE ON TABLE public.%I TO marketai_domain_worker', relation_name);
    END IF;
  END LOOP;

  -- ---- Meta domain + multi-candidate Launch (m23b, a79b) ----
  -- These tables are scoped by (workspace_id, meta_mode) through
  -- marketai_meta_context_is_current(). Their migrations grant the same
  -- matrix, but this file revokes everything on every release and only the
  -- grants repeated here survive the "4/4 runtime grants" pass: without them
  -- every Launch preview failed with "permission denied for table
  -- meta_connection_records" on a hosted cluster.
  FOREACH relation_name IN ARRAY ARRAY[
    'meta_connection_records', 'meta_data_source_binding_records',
    'scenario_version_records', 'fake_meta_object_records',
    'meta_call_intent_records', 'meta_call_outcome_records',
    'meta_call_reconciliation_records', 'meta_call_intent_binding_records',
    'observation_contract_generation_records', 'meta_sync_run_records',
    'meta_evidence_page_records', 'meta_page_fetch_attempt_records',
    'evidence_publication_records', 'evidence_attestation_records',
    'meta_status_history_records', 'observation_coverage_ledger_records',
    'meta_sync_schedule_records', 'scale_verdict_taint_records',
    'verdict_supersession_records',
    'launch_preview_records', 'launch_candidate_records',
    'launch_confirmation_records', 'launch_provisioning_records',
    'launch_provisioning_status_history_records',
    'launch_saga_step_attempt_records', 'launch_parity_check_records',
    'launch_reconciliation_records'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO marketai_domain_api, marketai_domain_worker', relation_name);
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_readonly', relation_name);
    END IF;
  END LOOP;

  -- ---- Library prompt baselines and data-source credential bindings ----
  -- Mirrors l1a2b3c4d5e6: request-side writes, execution-side reads.
  FOREACH relation_name IN ARRAY ARRAY[
    'prompt_baselines', 'datasource_credential_bindings'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO marketai_domain_api', relation_name);
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_worker, marketai_domain_readonly', relation_name);
    END IF;
  END LOOP;

  -- ---- Scale evidence, evaluation and advice (f5eeeab4e9b9, ad2) ----
  -- Mirrors f5eeeab4e9b9's GRANT_MATRIX row for row, plus the Worker's
  -- INSERT on scale_episodes (zzb1e2f3a4b5, #92): an evaluation that sees
  -- the delivery configuration change closes the Episode and opens the next
  -- one in the Worker (Scale contract §1 item 4, §16.2). Every runtime role
  -- reads every Scale table, INSERT/UPDATE go to exactly the roles listed
  -- (table, API, Worker), and no role gets DELETE. The migrations grant the
  -- same matrix, but this file revokes everything on every release and only
  -- the grants repeated here survive the "4/4 runtime grants" pass: without
  -- them a released cluster denies every Scale enrollment, evidence and
  -- advice statement ("permission denied for table scale_enrollments").
  DECLARE
    scale_grant text[];
  BEGIN
    FOREACH scale_grant SLICE 1 IN ARRAY ARRAY[
      ARRAY['scale_policy_versions', 'INSERT, UPDATE', ''],
      ARRAY['scale_enrollments', 'INSERT, UPDATE', 'UPDATE'],
      ARRAY['scale_enrollment_events', 'INSERT', 'INSERT'],
      ARRAY['scale_episodes', 'INSERT, UPDATE', 'INSERT, UPDATE'],
      ARRAY['scale_episode_events', 'INSERT', 'INSERT'],
      ARRAY['scale_target_versions', 'INSERT, UPDATE', ''],
      ARRAY['scale_target_lifecycle_events', 'INSERT', ''],
      ARRAY['scale_metric_semantic_contract_sets', 'INSERT', ''],
      ARRAY['scale_metric_semantic_contract_set_members', 'INSERT', ''],
      ARRAY['scale_daily_metric_sources', '', 'INSERT'],
      ARRAY['scale_exact_window_sources', '', 'INSERT'],
      ARRAY['scale_breakdown_sources', '', 'INSERT'],
      ARRAY['scale_breakdown_daily_children', '', 'INSERT'],
      ARRAY['scale_breakdown_privacy_records', '', 'INSERT'],
      ARRAY['scale_delivery_config_sources', '', 'INSERT'],
      ARRAY['scale_evidence_source_manifests', '', 'INSERT'],
      ARRAY['scale_manifest_items', '', 'INSERT'],
      ARRAY['scale_source_quality_issues', '', 'INSERT'],
      ARRAY['scale_evidence_readiness_projections', '', 'INSERT, UPDATE'],
      ARRAY['scale_evidence_snapshots', '', 'INSERT, UPDATE'],
      ARRAY['scale_evidence_source_links', '', 'INSERT'],
      ARRAY['scale_snapshot_quality_issues', '', 'INSERT'],
      ARRAY['scale_snapshot_breakdown_segments', '', 'INSERT'],
      ARRAY['scale_snapshot_breakdown_daily', '', 'INSERT'],
      ARRAY['scale_evaluation_runs', '', 'INSERT, UPDATE'],
      ARRAY['scale_evaluations', '', 'INSERT'],
      ARRAY['scale_evaluation_signals', '', 'INSERT'],
      ARRAY['scale_evaluation_advice_candidates', '', 'INSERT'],
      ARRAY['scale_evaluation_streak_counters', '', 'INSERT'],
      ARRAY['scale_evaluation_streak_resets', '', 'INSERT'],
      ARRAY['scale_episode_projections', '', 'INSERT, UPDATE'],
      ARRAY['scale_advice_threads', 'UPDATE', 'INSERT, UPDATE'],
      ARRAY['scale_advice_occurrences', '', 'INSERT'],
      ARRAY['scale_advice_thread_events', '', 'INSERT'],
      ARRAY['scale_advice_dispositions', 'INSERT', ''],
      ARRAY['scale_advice_snoozes', 'INSERT, UPDATE', 'UPDATE'],
      ARRAY['scale_advice_notification_decisions', '', 'INSERT'],
      ARRAY['scale_advice_conversions', 'INSERT', ''],
      ARRAY['scale_test_plan_drafts', 'INSERT, UPDATE', ''],
      ARRAY['scale_advice_outcomes', 'INSERT', ''],
      ARRAY['scale_unobserved_factors', 'INSERT', ''],
      ARRAY['scale_exports', 'INSERT', 'UPDATE']
    ] LOOP
      IF to_regclass(format('public.%I', scale_grant[1])) IS NOT NULL THEN
        EXECUTE format(
          'GRANT SELECT ON TABLE public.%I TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly',
          scale_grant[1]
        );
        IF scale_grant[2] <> '' THEN
          EXECUTE format('GRANT %s ON TABLE public.%I TO marketai_domain_api', scale_grant[2], scale_grant[1]);
        END IF;
        IF scale_grant[3] <> '' THEN
          EXECUTE format('GRANT %s ON TABLE public.%I TO marketai_domain_worker', scale_grant[3], scale_grant[1]);
        END IF;
      END IF;
    END LOOP;
  END;

  -- Target publication/withdrawal previews (ad2) are API-only commands: the
  -- API consumes a preview through its consumption columns, never the row.
  FOREACH relation_name IN ARRAY ARRAY[
    'scale_target_publication_previews', 'scale_target_withdrawal_previews'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly', relation_name);
      EXECUTE format('GRANT INSERT ON TABLE public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;
  IF to_regclass('public.scale_target_publication_previews') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE (consumed_at, result_target_version_id) ON TABLE public.scale_target_publication_previews TO marketai_domain_api';
  END IF;
  IF to_regclass('public.scale_target_withdrawal_previews') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE (consumed_at, withdrawn_at) ON TABLE public.scale_target_withdrawal_previews TO marketai_domain_api';
  END IF;

  -- Enrollment and activation are API commands (ab1/ac1): an Enrollment
  -- Candidate and an activation preview are each consumed exactly once
  -- through a consumed_at CAS, and activation records the Episode's
  -- baseline delivery configuration as source revision 1
  -- (app/scale/enrollment.py). f5eeeab4e9b9's matrix predates that path.
  IF to_regclass('public.scale_enrollment_candidates') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE (consumed_at) ON TABLE public.scale_enrollment_candidates TO marketai_domain_api';
  END IF;
  IF to_regclass('public.scale_activation_previews') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE (consumed_at) ON TABLE public.scale_activation_previews TO marketai_domain_api';
  END IF;
  IF to_regclass('public.scale_delivery_config_sources') IS NOT NULL THEN
    EXECUTE 'GRANT INSERT ON TABLE public.scale_delivery_config_sources TO marketai_domain_api';
  END IF;

  -- ---- Row locks without write access ----
  -- PostgreSQL requires UPDATE on at least one column for SELECT ... FOR
  -- UPDATE/SHARE. Freeze, review and Launch serialize on append-only
  -- evidence rows, and Launch re-validates eligibility under those locks
  -- both when the API enqueues and when the Worker runs each saga call.
  -- A grant on the identity column is enough to lock: the append-only
  -- triggers still reject every UPDATE, and has_table_privilege(...,
  -- 'UPDATE') stays false, so the immutability matrix is unchanged.
  FOREACH relation_name IN ARRAY ARRAY[
    'experiment_versions', 'experiment_version_candidates',
    'quality_reviews', 'truth_packages'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT UPDATE (id) ON TABLE public.%I TO marketai_domain_api, marketai_domain_worker', relation_name);
    END IF;
  END LOOP;
  -- The Worker locks the frozen Experiment for the same re-validation but
  -- never rewrites it; the API keeps its command-side UPDATE above.
  IF to_regclass('public.experiments') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE (id) ON TABLE public.experiments TO marketai_domain_worker';
  END IF;
  -- Scale API commands lock the Winner/DeliveryTarget lineage guard
  -- (app/scale/lineage_guard.py) and the Advice occurrence they act on
  -- (app/scale/advice.py). Guard revisions move only through the SECURITY
  -- DEFINER marketai_bump_scale_lineage_guards() trigger, and occurrences
  -- are append-only (scale_reject_update_delete), so the API gets the lock
  -- and no write.
  FOREACH relation_name IN ARRAY ARRAY[
    'scale_lineage_guards', 'scale_advice_occurrences'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT UPDATE (id) ON TABLE public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;

  -- Question-led planning: immutable revision history and non-secret
  -- provider readiness survive repeatable role application.
  FOREACH relation_name IN ARRAY ARRAY[
    'marketing_projects', 'marketing_revisions',
    'marketing_planning_calls', 'marketing_generation_links',
    'marketing_rounds', 'marketing_qc_reviews', 'marketing_handoffs',
    'marketing_ad_bindings', 'marketing_metric_batches', 'marketing_evaluations', 'marketing_ai_qc_calls', 'marketing_creative_reuses', 'marketing_feedback_drafts'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly', relation_name);
      EXECUTE format('GRANT INSERT ON TABLE public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;
  FOREACH relation_name IN ARRAY ARRAY['marketing_projects', 'marketing_planning_calls'] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT UPDATE ON TABLE public.%I TO marketai_domain_api, marketai_domain_worker', relation_name);
    END IF;
  END LOOP;
  FOREACH relation_name IN ARRAY ARRAY['marketing_rounds', 'marketing_metric_batches', 'marketing_handoffs'] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT UPDATE ON public.%I TO marketai_domain_api', relation_name);
    END IF;
  END LOOP;
  IF to_regclass('public.marketing_ai_qc_calls') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE ON public.marketing_ai_qc_calls TO marketai_domain_worker';
  END IF;
  IF to_regclass('public.marketing_handoffs') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE ON public.marketing_handoffs TO marketai_domain_worker';
  END IF;
  IF to_regclass('public.marketing_revisions') IS NOT NULL THEN
    EXECUTE 'GRANT INSERT ON public.marketing_revisions TO marketai_domain_worker';
  END IF;
  IF to_regclass('public.workspace_provider_bindings') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT ON public.workspace_provider_bindings TO marketai_domain_api';
  END IF;

  -- Projection function: API only.
  IF to_regclass('public.entitlement_projections') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_apply_workspace_projection_v2 FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_apply_workspace_projection_v2 TO marketai_domain_api';
  END IF;

  -- Tenant-neutral claim; settlement remains tenant-scoped and RLS-enforced.
  IF to_regprocedure('public.marketai_claim_next_onboarding_fact(text,integer)') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_claim_next_onboarding_fact(text, integer) FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_claim_next_onboarding_fact(text, integer) TO marketai_domain_worker';
  END IF;

  -- Tenant-neutral reconciliation candidates (ids only); every fold that
  -- follows runs in a signed TenantContext under RLS (#22).
  IF to_regprocedure('public.marketai_legacy_operation_candidates_v1(text[],integer)') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_legacy_operation_candidates_v1(text[], integer) FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_legacy_operation_candidates_v1(text[], integer) TO marketai_domain_worker';
  END IF;
  IF to_regprocedure('public.marketai_admitted_operation_candidates_v1(integer)') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_admitted_operation_candidates_v1(integer) FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_admitted_operation_candidates_v1(integer) TO marketai_domain_worker';
  END IF;

  -- Meta observation dispatch (zze4f5a6b7c8, #107): due schedules become
  -- meta_observation_tick Jobs and their job.enqueued audit events, nothing
  -- else; the tick then runs in a signed TenantContext under RLS.
  IF to_regprocedure('public.marketai_dispatch_meta_observation_ticks_v1(integer)') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_dispatch_meta_observation_ticks_v1(integer) FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_dispatch_meta_observation_ticks_v1(integer) TO marketai_domain_worker';
  END IF;

  -- Platform Meta API (zzc2f3a4b5c6, #91 and #94). The API keeps SELECT only
  -- on workspaces and only the non-secret columns of credential_versions:
  -- bootstrap creates the platform Workspace (is_platform_workspace = true,
  -- never an existing row) through the first function, and credential
  -- revocation learns which Workspace owns a version (the id, nothing else)
  -- through the second, then revokes in a signed TenantContext under RLS.
  IF to_regprocedure('public.marketai_create_platform_workspace_v1(text,text)') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_create_platform_workspace_v1(text, text) FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_create_platform_workspace_v1(text, text) TO marketai_domain_api';
  END IF;
  IF to_regprocedure('public.marketai_credential_version_workspace_v1(text)') IS NOT NULL THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.marketai_credential_version_workspace_v1(text) FROM PUBLIC';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.marketai_credential_version_workspace_v1(text) TO marketai_domain_api';
  END IF;

  -- ---- projection_writer: mutable projection tables ----
  FOREACH relation_name IN ARRAY ARRAY[
    'experiment_eligibility_projections',
    'experiment_versions', 'experiment_version_candidates',
    'experiment_eligibility_events',
    'experiment_timeline_heads', 'experiment_timeline_events'
  ] LOOP
    IF to_regclass(format('public.%I', relation_name)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO marketai_projection_writer', relation_name);
    END IF;
  END LOOP;

  IF to_regclass('public.experiment_eligibility_projections') IS NOT NULL THEN
    EXECUTE 'GRANT UPDATE ON TABLE public.experiment_eligibility_projections TO marketai_projection_writer';
  END IF;

  IF to_regclass('public.contextual_verdict_projections') IS NOT NULL THEN
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.contextual_verdict_projections TO marketai_projection_writer';
  END IF;
END
$grants$;

-- Tenant-authenticated budget totals, without raw ledger SELECT for the API.
DO $budget$
BEGIN
  IF to_regprocedure('public.marketai_settled_cost_total(text,timestamptz)') IS NOT NULL THEN
    REVOKE ALL ON FUNCTION public.marketai_settled_cost_total(text,timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.marketai_settled_cost_total(text,timestamptz) TO marketai_domain_api, marketai_domain_worker;
  END IF;
END $budget$;

-- Local image operations keep frozen inputs API-owned and completion Worker-owned.
DO $image_edit_grants$
BEGIN
  IF to_regclass('public.marketing_image_edits') IS NOT NULL THEN
    GRANT SELECT ON marketing_image_edits TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON marketing_image_edits TO marketai_domain_api;
    GRANT UPDATE (state, error_code, result_asset_version_id) ON marketing_image_edits TO marketai_domain_worker;
  END IF;
END $image_edit_grants$;

-- Brand baseline versions are frozen by plans and recipes: API-appended, never updated.
DO $brand_kit_grants$
BEGIN
  IF to_regclass('public.product_brand_kits') IS NOT NULL THEN
    GRANT SELECT ON product_brand_kits TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON product_brand_kits TO marketai_domain_api;
  END IF;
END $brand_kit_grants$;

-- Competitor list (#269): the API adds and archives competitors and admits
-- resolves; the Worker advances a resolve and appends an automatic match.
-- Confirmations are append-only for both: no UPDATE, no DELETE.
DO $competitor_grants$
BEGIN
  IF to_regclass('public.competitors') IS NOT NULL THEN
    GRANT SELECT ON competitors TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT, UPDATE ON competitors TO marketai_domain_api;
  END IF;
  IF to_regclass('public.competitor_resolutions') IS NOT NULL THEN
    GRANT SELECT ON competitor_resolutions TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_resolutions TO marketai_domain_api;
    GRANT UPDATE ON competitor_resolutions TO marketai_domain_worker;
  END IF;
  IF to_regclass('public.competitor_identity_confirmations') IS NOT NULL THEN
    GRANT SELECT ON competitor_identity_confirmations TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_identity_confirmations TO marketai_domain_api, marketai_domain_worker;
  END IF;
END $competitor_grants$;

-- Competitor scans (#270): the API admits a scan and re-admits it for a
-- resume or an Owner's reconciliation; the Worker advances it and appends
-- observations and their origins. Observations and origins are append-only
-- for both: no UPDATE, no DELETE.
DO $competitor_scan_grants$
BEGIN
  IF to_regclass('public.competitor_scans') IS NOT NULL THEN
    GRANT SELECT ON competitor_scans TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT, UPDATE ON competitor_scans TO marketai_domain_api;
    GRANT UPDATE ON competitor_scans TO marketai_domain_worker;
  END IF;
  IF to_regclass('public.competitor_ads') IS NOT NULL THEN
    GRANT SELECT ON competitor_ads TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_ads TO marketai_domain_worker;
  END IF;
  IF to_regclass('public.competitor_ad_origins') IS NOT NULL THEN
    GRANT SELECT ON competitor_ad_origins TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_ad_origins TO marketai_domain_worker;
  END IF;
END $competitor_scan_grants$;

-- Competitor judgments (#271): the Worker appends a scan's AI and rule
-- relevance judgments, the API appends a member's flip. Append-only for
-- both: no UPDATE, no DELETE.
DO $competitor_assessment_grants$
BEGIN
  IF to_regclass('public.competitor_ad_assessments') IS NOT NULL THEN
    GRANT SELECT ON competitor_ad_assessments TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_ad_assessments TO marketai_domain_api, marketai_domain_worker;
  END IF;
END $competitor_assessment_grants$;

-- Competitor clusters and snapshots (#272): the Worker appends a scan's
-- clusters revisions (its cluster step and a paid recluster's Job) and
-- freezes snapshots; the API freezes on a member's refreeze. Tag revisions
-- live in competitor_ad_assessments (granted above). Append-only for both:
-- no UPDATE, no DELETE. products.competitor_snapshot_in_drafts is covered
-- by the API's table-level UPDATE on products.
DO $competitor_snapshot_grants$
BEGIN
  IF to_regclass('public.competitor_scan_assessments') IS NOT NULL THEN
    GRANT SELECT ON competitor_scan_assessments TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_scan_assessments TO marketai_domain_worker;
  END IF;
  IF to_regclass('public.competitor_scan_snapshots') IS NOT NULL THEN
    GRANT SELECT ON competitor_scan_snapshots TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_scan_snapshots TO marketai_domain_api, marketai_domain_worker;
  END IF;
END $competitor_snapshot_grants$;

-- Competitor discovery (#276): the API appends a member's accept or reject of
-- a recommended advertiser (an accept also creates the competitor and its
-- confirmation, granted above); the Worker reads the decisions to leave
-- rejected advertisers out of later scans. Append-only: no UPDATE, no DELETE.
DO $competitor_discovery_grants$
BEGIN
  IF to_regclass('public.competitor_discovery_decisions') IS NOT NULL THEN
    GRANT SELECT ON competitor_discovery_decisions TO marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
    GRANT INSERT ON competitor_discovery_decisions TO marketai_domain_api;
  END IF;
END $competitor_discovery_grants$;

-- Spend guardrail previews (#386): the API inserts an Owner's previewed
-- change and consumes it once on confirm, through consumed_at and
-- result_revision only. The Worker gets nothing, as for spend_guardrails.
-- No DELETE.
DO $spend_guardrail_preview_grants$
BEGIN
  IF to_regclass('public.spend_guardrail_previews') IS NOT NULL THEN
    GRANT SELECT ON TABLE public.spend_guardrail_previews TO marketai_domain_api, marketai_domain_readonly;
    GRANT INSERT ON TABLE public.spend_guardrail_previews TO marketai_domain_api;
    GRANT UPDATE (consumed_at, result_revision) ON TABLE public.spend_guardrail_previews TO marketai_domain_api;
  END IF;
END $spend_guardrail_preview_grants$;

COMMIT;
