/**
 * GENERATED FILE — do not edit by hand.
 * Generator: scripts/generate_paths.ts
 * Sources: contracts/openapi/marketai-domain-v1.json, contracts/openapi/marketai-domain-v1-policy.json
 * Regenerate with scripts/generate_all.sh — CI fails the build on drift.
 */
export type BffRoute = {
  readonly operationId: string;
  readonly method: string;
  readonly bffPattern: string;
  readonly pathParams: readonly string[];
  readonly requiresIdempotency: boolean;
};

export const BFF_ROUTES: readonly BffRoute[] = [
  {
    operationId: 'activate_scale_enrollment',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/activate',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'add_experiment_entry',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id/entries',
    pathParams: ['experiment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'add_product_reference',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/products/:product_id/references',
    pathParams: ['product_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'adopt_localization_isolated_output',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/items/:item_id/attempts/:attempt_id/adopt',
    pathParams: ['batch_id', 'item_id', 'attempt_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'analyze_experiment_duplicates',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/experiments/:experiment_id/duplicate-analysis',
    pathParams: ['experiment_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'apply_library_template',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/versions/:template_version_id/apply',
    pathParams: ['template_version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'approve_localization_copy',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/approve-copy',
    pathParams: ['batch_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'archive_competitor',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitors/:competitor_id/archive',
    pathParams: ['competitor_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'archive_library_entry',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/archive',
    pathParams: ['entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'authorize_localization_isolated_qc',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/items/:item_id/attempts/:attempt_id/authorize-isolated-qc',
    pathParams: ['batch_id', 'item_id', 'attempt_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'bind_credential',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/credentials/:credential_version_id/bindings/:scope',
    pathParams: ['credential_version_id', 'scope'],
    requiresIdempotency: false,
  },
  {
    operationId: 'bind_experiment_candidate',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/experiments/:experiment_id/candidates/:candidate_id/bind',
    pathParams: ['experiment_id', 'candidate_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'bind_library_data_source_credential',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:data_source_entry_id/credential-binding',
    pathParams: ['data_source_entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'bind_library_reference',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/bind',
    pathParams: ['entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'bind_marketing_delivery',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/bindings',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'cancel_generation_run',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/runs/:run_id/cancel',
    pathParams: ['run_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'cancel_localization_batch',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/localization/batches/:batch_id/cancel',
    pathParams: ['batch_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'check_library_data_source_connection',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:data_source_entry_id/connection-checks',
    pathParams: ['data_source_entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'clone_library_product',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/versions/:version_id/clone-product',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'close_marketing_round',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/close',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'close_scale_enrollment',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/close',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'commit_results_import',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/results/imports/:import_id/commit',
    pathParams: ['import_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_competitor',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitors/:competitor_id/confirm',
    pathParams: ['competitor_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_competitor_ad_tags',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/ads/:ad_id/tags',
    pathParams: ['scan_id', 'ad_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_experiment_proposal',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/experiments/:experiment_id/proposals/:proposal_id/confirm',
    pathParams: ['experiment_id', 'proposal_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_external_creative',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/external-creatives/:external_creative_id/confirm',
    pathParams: ['external_creative_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_localization_batch',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/localization/batches/:batch_id/confirm',
    pathParams: ['batch_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_marketing_ai_qc',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/ai-qc/confirm',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_marketing_metrics',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/metrics/confirm',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_meta_launch_preview',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/launch/previews/:preview_id/confirm',
    pathParams: ['preview_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'confirm_product_intake',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/products/intake/:intake_id/confirm',
    pathParams: ['intake_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'confirm_spend_guardrail',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/spend-guardrail/confirm',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'convert_scale_advice_occurrence',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/advice-occurrences/:occurrence_id/convert',
    pathParams: ['meta_mode', 'occurrence_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'create_competitor',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitors',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_competitor_scan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_credential',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/credentials',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'create_download_intent',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/download-intents',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_experiment',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/experiments',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_external_creatives',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/external-creatives',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_asset_review',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/asset-versions/:version_id/reviews',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_asset_version_download_intent',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/asset-versions/:version_id/download-intent',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_draft',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/drafts',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_export_download_intent',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/exports/:export_package_id/download-intent',
    pathParams: ['export_package_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_export_package',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/exports',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_preview',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/drafts/:draft_id/previews',
    pathParams: ['draft_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_regeneration_draft',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/asset-versions/:version_id/regeneration-drafts',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_review_summary_regeneration',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/regeneration-draft-batches',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_generation_run_recovery_preview',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/runs/:run_id/recovery-previews',
    pathParams: ['run_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_library_entry',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/entries',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_library_internal_entry',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:root_entry_id/internal',
    pathParams: ['root_entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_library_reference_download_intent',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/versions/:version_id/download-intent',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_library_reference_object',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/reference-objects',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_localization_batch',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/localization/batches',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_marketing_evidence_handoff',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/evidence-handoff',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_marketing_handoff',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/handoff',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_marketing_image_edit',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/image-edits',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_marketing_project',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/projects',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_meta_binding',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/meta/connections/:connection_id/bindings',
    pathParams: ['connection_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'create_meta_connection',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/meta/connections',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'create_meta_contract',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/meta/contracts',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'create_meta_launch_preview',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/launch/previews',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'create_product_intake',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/products/intake',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_product_intake_image_download_intent',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/products/intake/:intake_id/images/:image_id/download-intent',
    pathParams: ['intake_id', 'image_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_results_import',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/results/imports',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_activation_preview',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/activation-previews',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_advice_disposition',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/advice-threads/:thread_id/disposition',
    pathParams: ['meta_mode', 'thread_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_advice_snooze',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/advice-threads/:thread_id/snooze',
    pathParams: ['meta_mode', 'thread_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_enrollment',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments',
    pathParams: ['meta_mode'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_enrollment_candidate',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/enrollment-candidates',
    pathParams: ['meta_mode'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_export',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/exports',
    pathParams: ['meta_mode'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_export_download_intent',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/exports/:export_id/download-intents',
    pathParams: ['meta_mode', 'export_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_target',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/targets',
    pathParams: ['meta_mode'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_target_publication_preview',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/target-publication-previews',
    pathParams: ['meta_mode'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_scale_target_withdrawal_preview',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/targets/:target_id/withdrawal-previews',
    pathParams: ['meta_mode', 'target_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_test_plan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/test-plans',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_test_plan_run',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/test-plans/:plan_id/runs',
    pathParams: ['plan_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'create_upload_intent',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/upload-intents',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'decide_competitor_discovery',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/discoveries/:advertiser_page_id/decision',
    pathParams: ['scan_id', 'advertiser_page_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'delete_product_reference',
    method: 'DELETE',
    bffPattern:
      '/api/domain/v1/w/:slug/products/:product_id/references/:item_id',
    pathParams: ['product_id', 'item_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'download_marketing_handoff',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/download-intent',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'edit_marketing_project',
    method: 'PATCH',
    bffPattern: '/api/domain/v1/w/:slug/marketing/projects/:project_id',
    pathParams: ['project_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'evaluate_marketing_round',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/evaluate',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'execute_meta_launch_preview',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/launch/previews/:preview_id/execute',
    pathParams: ['preview_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'finalize_upload',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/upload-intents/:intent_id/finalize',
    pathParams: ['intent_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'flip_competitor_ad_relevance',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/ads/:ad_id/relevance',
    pathParams: ['scan_id', 'ad_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'fork_library_version',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/versions/:version_id/fork',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'freeze_experiment',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id/freeze',
    pathParams: ['experiment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'freeze_marketing_round',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/projects/:project_id/rounds',
    pathParams: ['project_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'get_competitor',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitors/:competitor_id',
    pathParams: ['competitor_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_competitor_resolution',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/competitors/:competitor_id/resolutions/:resolve_job_id',
    pathParams: ['competitor_id', 'resolve_job_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_competitor_scan',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_competitor_scan_snapshot',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/snapshots/:snapshot_id',
    pathParams: ['scan_id', 'snapshot_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_competitor_scan_summary',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/summary',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_cost_summary',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/cost-summary',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_experiment',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id',
    pathParams: ['experiment_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_experiment_timeline',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id/timeline',
    pathParams: ['experiment_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_external_creative',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/external-creatives/:external_creative_id',
    pathParams: ['external_creative_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_asset_version',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/asset-versions/:version_id',
    pathParams: ['version_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_capabilities',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/capabilities',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_defaults',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/defaults',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_draft',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/drafts/:draft_id',
    pathParams: ['draft_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_export_manifest',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/exports/:export_package_id/manifest',
    pathParams: ['export_package_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_export_package',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/exports/:export_package_id',
    pathParams: ['export_package_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_preview',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/previews/:preview_id',
    pathParams: ['preview_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_run',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/runs/:run_id',
    pathParams: ['run_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_generation_run_item',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/runs/:run_id/items/:run_item_id',
    pathParams: ['run_id', 'run_item_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_identity_probe',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/identity',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_command_outcome',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/library/command-outcomes/:command_identity',
    pathParams: ['command_identity'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_data_source_credential_binding',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:data_source_entry_id/credential-binding',
    pathParams: ['data_source_entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_entry',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id',
    pathParams: ['entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_internal_entry',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/internal/:entry_type/:entry_id',
    pathParams: ['entry_type', 'entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_internal_entry_context',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:root_entry_id/internal/:entry_type/:entry_id',
    pathParams: ['root_entry_id', 'entry_type', 'entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_reference_object',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/reference-objects/:object_id',
    pathParams: ['object_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_version',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/versions/:version_id',
    pathParams: ['version_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_library_version_impact',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/versions/:version_id/impact',
    pathParams: ['version_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_localization_batch',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/localization/batches/:batch_id',
    pathParams: ['batch_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_localization_batch_outcome',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/localization/batches/:batch_id/outcome',
    pathParams: ['batch_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_localization_item',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/items/:item_id',
    pathParams: ['batch_id', 'item_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_localization_workspace',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/localization/workspace',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_marketing_ai_qc_quote',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/ai-qc/quote',
    pathParams: ['round_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_marketing_capabilities',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/marketing/capabilities',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_marketing_evidence_handoff',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/evidence-handoff',
    pathParams: ['round_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_marketing_project',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/marketing/projects/:project_id',
    pathParams: ['project_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_marketing_round',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id',
    pathParams: ['round_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_marketing_workflow',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/projects/:project_id/workflow',
    pathParams: ['project_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_meta_connection',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/meta/connections/:connection_id',
    pathParams: ['connection_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_meta_launch_preview',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/launch/previews/:preview_id',
    pathParams: ['preview_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_meta_launch_provisioning',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/launch/operations/:provisioning_id',
    pathParams: ['provisioning_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_operation',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/operations/:operation_id',
    pathParams: ['operation_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_product_brand_kit',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/products/:product_id/brand-kit',
    pathParams: ['product_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_product_competitor_settings',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/products/:product_id/competitor-settings',
    pathParams: ['product_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_product_intake',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/products/intake/:intake_id',
    pathParams: ['intake_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_product_learnings',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/products/:product_id/learnings',
    pathParams: ['product_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_product_references',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/products/:product_id/references',
    pathParams: ['product_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_results_import',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/results/imports/:import_id',
    pathParams: ['import_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_results_template',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/products/:product_id/results/template',
    pathParams: ['product_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_scale_command_outcome',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/command-outcomes/:identity',
    pathParams: ['meta_mode', 'identity'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_scale_enrollment',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_scale_enrollment_breakdowns',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/breakdowns',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_scale_enrollment_evidence',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/evidence',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_scale_episode',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/episodes/:episode_id',
    pathParams: ['meta_mode', 'enrollment_id', 'episode_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_scale_target',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/targets/:target_id',
    pathParams: ['meta_mode', 'target_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_spend_guardrail',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/spend-guardrail',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_test_plan',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/test-plans/:plan_id',
    pathParams: ['plan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_test_plan_results',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/test-plans/:plan_id/results',
    pathParams: ['plan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'get_usage',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/usage',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'import_library_manual_facts',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:data_source_entry_id/manual-fact-imports',
    pathParams: ['data_source_entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'judge_experiment',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id/judgements',
    pathParams: ['experiment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'list_audit_events',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/audit',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitor_ad_assessments',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/ads/:ad_id/assessments',
    pathParams: ['scan_id', 'ad_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitor_ad_tags',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/ads/:ad_id/tags',
    pathParams: ['scan_id', 'ad_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitor_scan_ads',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/ads',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitor_scan_discoveries',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/discoveries',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitor_scan_snapshots',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/snapshots',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitor_scans',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_competitors',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitors',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_credentials',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/credentials',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_experiments',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/experiments',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_external_creatives',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/external-creatives',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_generation_assets',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/assets',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_generation_drafts',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/drafts',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_generation_run_items',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/runs/:run_id/items',
    pathParams: ['run_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_generation_runs',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/generation/runs',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_library_entries',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/entries',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_library_entry_events',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/events',
    pathParams: ['entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_library_entry_types',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/entry-types',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_library_entry_versions',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/versions',
    pathParams: ['entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_library_internal_entries',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:root_entry_id/internal',
    pathParams: ['root_entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_library_version_bindings',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/library/versions/:version_id/bindings',
    pathParams: ['version_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_localization_batches',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/localization/batches',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_marketing_brand_kits',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/marketing/brand-kits',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_marketing_projects',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/marketing/projects',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_meta_bindings',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/meta/connections/:connection_id/bindings',
    pathParams: ['connection_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_meta_connections',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/meta/connections',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_meta_contracts',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/meta/contracts',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_products',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/products',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_scale_advice_threads',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/advice-threads',
    pathParams: ['meta_mode'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_scale_available_modes',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/scale/available-meta-modes',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_scale_enrollment_candidates',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/enrollment-candidates',
    pathParams: ['meta_mode'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_scale_enrollments',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments',
    pathParams: ['meta_mode'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_scale_target_contexts',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/target-contexts/current',
    pathParams: ['meta_mode'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_scale_targets',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/scale/:meta_mode/targets',
    pathParams: ['meta_mode'],
    requiresIdempotency: false,
  },
  {
    operationId: 'list_test_plans',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/test-plans',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'pause_scale_enrollment',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/pause',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'prepare_marketing_feedback',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/feedback-drafts',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'prepare_marketing_generation',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/projects/:project_id/generation-drafts',
    pathParams: ['project_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'preview_competitor_scan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/preview',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'preview_library_archive_impact',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:entry_id/archive-impact',
    pathParams: ['entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'preview_library_publish_impact',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/entries/:entry_id/publish-impact',
    pathParams: ['entry_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'preview_library_template_reapply',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/versions/:template_version_id/reapply-preview',
    pathParams: ['template_version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'preview_library_withdraw_impact',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/versions/:version_id/withdraw-impact',
    pathParams: ['version_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'preview_marketing_metrics',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/metrics/preview',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'preview_spend_guardrail',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/spend-guardrail/preview',
    pathParams: [],
    requiresIdempotency: false,
  },
  {
    operationId: 'promote_library_reference_object',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/reference-objects/:object_id/promote',
    pathParams: ['object_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'publish_library_entry',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/publish',
    pathParams: ['entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'quote_competitor_resolve',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/competitors/:competitor_id/resolve-quote',
    pathParams: ['competitor_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'quote_competitor_scan_recluster',
    method: 'GET',
    bffPattern:
      '/api/domain/v1/w/:slug/competitor-scans/:scan_id/recluster-quote',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'quote_competitor_scan_resume',
    method: 'GET',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/resume-quote',
    pathParams: ['scan_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'recluster_competitor_scan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/recluster',
    pathParams: ['scan_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'reconcile_competitor_scan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/reconcile',
    pathParams: ['scan_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'reconcile_generation_run',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/runs/:run_id/reconcile',
    pathParams: ['run_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'reconcile_library_command_outcome',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/library/command-outcomes/:command_identity/reconcile',
    pathParams: ['command_identity'],
    requiresIdempotency: false,
  },
  {
    operationId: 'recover_meta_launch_operation',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/launch/operations/:operation_id/recovery',
    pathParams: ['operation_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'refreeze_competitor_scan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/snapshots',
    pathParams: ['scan_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'resolve_competitor',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitors/:competitor_id/resolve',
    pathParams: ['competitor_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'resolve_marketing_call',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/calls/:call_id/resolve',
    pathParams: ['call_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'resume_competitor_scan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/competitor-scans/:scan_id/resume',
    pathParams: ['scan_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'resume_generation_run_paused_items',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/runs/:run_id/resume-paused-items',
    pathParams: ['run_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'resume_localization_item',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/items/:item_id/resume',
    pathParams: ['batch_id', 'item_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'resume_scale_enrollment',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/enrollments/:enrollment_id/resume',
    pathParams: ['meta_mode', 'enrollment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'retire_meta_connection',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/meta/connections/:connection_id/retire',
    pathParams: ['connection_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'retry_generation_run_failed_items',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/generation/runs/:run_id/retry-failed-items',
    pathParams: ['run_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'retry_localization_item',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/items/:item_id/retry',
    pathParams: ['batch_id', 'item_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'retry_marketing_handoff',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/handoff/retry',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'reuse_marketing_creative',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/rounds/:round_id/creatives/reuse',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'review_marketing_creative',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/reviews',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'revise_test_plan',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/test-plans/:plan_id/revise',
    pathParams: ['plan_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'revoke_credential',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/credentials/:credential_version_id/revoke',
    pathParams: ['credential_version_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'rotate_credential',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/credentials/:credential_key/rotate',
    pathParams: ['credential_key'],
    requiresIdempotency: false,
  },
  {
    operationId: 'save_library_draft',
    method: 'PUT',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/draft',
    pathParams: ['entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'save_product_brand_kit',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/products/:product_id/brand-kit',
    pathParams: ['product_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'start_experiment',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id/start',
    pathParams: ['experiment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'start_localization_preparation',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/start-preparation',
    pathParams: ['batch_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'start_marketing_ai_qc',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/ai-qc',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'start_marketing_explanation',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/marketing/rounds/:round_id/explanation',
    pathParams: ['round_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'start_marketing_planning',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/marketing/projects/:project_id/planning',
    pathParams: ['project_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'submit_generation_run',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/generation/runs',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'submit_localization_claim_decisions',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/localization/batches/:batch_id/claim-decisions',
    pathParams: ['batch_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'supersede_meta_binding',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/meta/connections/:connection_id/bindings/:binding_id/supersede',
    pathParams: ['connection_id', 'binding_id'],
    requiresIdempotency: false,
  },
  {
    operationId: 'update_experiment_context',
    method: 'PUT',
    bffPattern: '/api/domain/v1/w/:slug/experiments/:experiment_id/context',
    pathParams: ['experiment_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'update_experiment_entry',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/experiments/:experiment_id/entries/:entry_id',
    pathParams: ['experiment_id', 'entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'update_generation_defaults',
    method: 'PUT',
    bffPattern: '/api/domain/v1/w/:slug/generation/defaults',
    pathParams: [],
    requiresIdempotency: true,
  },
  {
    operationId: 'update_generation_draft',
    method: 'PATCH',
    bffPattern: '/api/domain/v1/w/:slug/generation/drafts/:draft_id',
    pathParams: ['draft_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'update_product_competitor_settings',
    method: 'PUT',
    bffPattern:
      '/api/domain/v1/w/:slug/products/:product_id/competitor-settings',
    pathParams: ['product_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'update_product_reference',
    method: 'PATCH',
    bffPattern:
      '/api/domain/v1/w/:slug/products/:product_id/references/:item_id',
    pathParams: ['product_id', 'item_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'validate_library_draft',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/entries/:entry_id/validate',
    pathParams: ['entry_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'withdraw_library_version',
    method: 'POST',
    bffPattern: '/api/domain/v1/w/:slug/library/versions/:version_id/withdraw',
    pathParams: ['version_id'],
    requiresIdempotency: true,
  },
  {
    operationId: 'withdraw_scale_target',
    method: 'POST',
    bffPattern:
      '/api/domain/v1/w/:slug/scale/:meta_mode/targets/:target_id/withdraw',
    pathParams: ['meta_mode', 'target_id'],
    requiresIdempotency: true,
  },
] as const;

/** Single source of truth for the browser `DomainGateway` and the server route matcher (spec §6.3). */
export function buildBffPath(
  operationId: string,
  params: { slug: string } & Record<string, string>
): string {
  const route = BFF_ROUTES.find((r) => r.operationId === operationId);
  if (!route) {
    throw new Error(`Unknown operationId: ${operationId}`);
  }
  let builtPath = route.bffPattern.replace(
    ':slug',
    encodeURIComponent(params.slug)
  );
  for (const param of route.pathParams) {
    const value = params[param];
    if (value === undefined) {
      throw new Error(`Missing path param "${param}" for ${operationId}`);
    }
    builtPath = builtPath.replace(`:${param}`, encodeURIComponent(value));
  }
  return builtPath;
}
