/**
 * GENERATED FILE — do not edit by hand.
 * Generator: scripts/generate_operation_map.ts
 * Sources: contracts/openapi/marketai-domain-v1.json, contracts/openapi/marketai-domain-v1-policy.json
 * Regenerate with scripts/generate_all.sh — CI fails the build on drift.
 */
import type { components } from './domain-v1';

export type DomainOperationMap = {
  activate_scale_enrollment: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['EnrollmentActivateRequest'];
    successResponses: { 200: components['schemas']['EnrollmentResponse'] };
  };
  add_experiment_entry: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['AddExperimentEntryRequest'];
    successResponses: { 201: components['schemas']['ExperimentEntryMutation'] };
  };
  add_product_reference: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['AddProductReference'];
    successResponses: { 200: components['schemas']['ProductReferenceSet'] };
  };
  adopt_localization_isolated_output: {
    pathParams: { batch_id: string; item_id: string; attempt_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['AdoptIsolatedOutputResult'];
    };
  };
  analyze_experiment_duplicates: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['DuplicateAnalysisResponse'];
    };
  };
  apply_library_template: {
    pathParams: { template_version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ApplyTemplateInput'];
    successResponses: { 201: components['schemas']['LibraryEntryDetail'] };
  };
  approve_localization_copy: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ApproveCopyInput'];
    successResponses: { 200: components['schemas']['ApproveCopyResult'] };
  };
  archive_competitor: {
    pathParams: { competitor_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['Competitor'] };
  };
  archive_library_entry: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ArchiveInput'];
    successResponses: { 200: components['schemas']['LibraryEntryDetail'] };
  };
  authorize_localization_isolated_qc: {
    pathParams: { batch_id: string; item_id: string; attempt_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['AuthorizeIsolatedQCResult'];
    };
  };
  bind_credential: {
    pathParams: { credential_version_id: string; scope: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BindCredentialRequest'];
    successResponses: { 200: null };
  };
  bind_experiment_candidate: {
    pathParams: { experiment_id: string; candidate_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BindExperimentCandidateRequest'];
    successResponses: {
      200: components['schemas']['ExperimentBindingMutation'];
    };
  };
  bind_library_data_source_credential: {
    pathParams: { data_source_entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BindCredentialInput'];
    successResponses: {
      201: components['schemas']['CredentialBindingSummary'];
    };
  };
  bind_library_reference: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BindInput'];
    successResponses: { 201: components['schemas']['DraftSummary'] };
  };
  bind_marketing_delivery: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BindDelivery'];
    successResponses: { 201: components['schemas']['ResourceEnvelope'] };
  };
  cancel_generation_run: {
    pathParams: { run_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['RunCommandResult'] };
  };
  cancel_localization_batch: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CancelResult'] };
  };
  check_library_data_source_connection: {
    pathParams: { data_source_entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ConnectionCheckInput'];
    successResponses: { 202: components['schemas']['OperationAccepted'] };
  };
  clone_library_product: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CloneProductInput'];
    successResponses: { 201: components['schemas']['LibraryEntryDetail'] };
  };
  close_marketing_round: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CloseRound'];
    successResponses: { 200: components['schemas']['ResourceEnvelope'] };
  };
  close_scale_enrollment: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['EnrollmentCloseRequest'];
    successResponses: { 200: components['schemas']['EnrollmentResponse'] };
  };
  commit_results_import: {
    pathParams: { import_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ResultsImportView'] };
  };
  confirm_competitor: {
    pathParams: { competitor_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorConfirmRequest'];
    successResponses: { 200: components['schemas']['Competitor'] };
  };
  confirm_competitor_ad_tags: {
    pathParams: { scan_id: string; ad_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorAdTagsConfirm'];
    successResponses: {
      200: components['schemas']['CompetitorAdTagAssessment'];
    };
  };
  confirm_experiment_proposal: {
    pathParams: { experiment_id: string; proposal_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ConfirmExperimentProposalRequest'];
    successResponses: {
      200: components['schemas']['ExperimentConfirmationResult'];
    };
  };
  confirm_external_creative: {
    pathParams: { external_creative_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ExternalCreativeConfirmRequest'];
    successResponses: { 200: components['schemas']['ExternalCreativeDetail'] };
  };
  confirm_localization_batch: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['app__localization__routes__ConfirmInput'];
    successResponses: { 200: components['schemas']['ConfirmResult'] };
  };
  confirm_marketing_ai_qc: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ConfirmAIQC'];
    successResponses: { 200: components['schemas']['ResourceEnvelope'] };
  };
  confirm_marketing_metrics: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BatchCommand'];
    successResponses: { 200: components['schemas']['ResourceEnvelope'] };
  };
  confirm_meta_launch_preview: {
    pathParams: { preview_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['MultiCandidateConfirmInput'];
    successResponses: {
      200: components['schemas']['MultiCandidateConfirmationResponse'];
    };
  };
  confirm_product_intake: {
    pathParams: { intake_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ProductIntakeConfirmRequest'];
    successResponses: { 200: components['schemas']['ProductIntakeDetail'] };
  };
  confirm_spend_guardrail: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['GuardrailConfirmRequest'];
    successResponses: { 200: null };
  };
  convert_scale_advice_occurrence: {
    pathParams: { meta_mode: string; occurrence_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ConversionRequest'];
    successResponses: { 201: components['schemas']['ConversionResponse'] };
  };
  create_competitor: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorCreateRequest'];
    successResponses: { 201: components['schemas']['Competitor'] };
  };
  create_competitor_scan: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorScanRequest'];
    successResponses: { 202: components['schemas']['CompetitorScan'] };
  };
  create_credential: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateCredentialRequest'];
    successResponses: { 200: null };
  };
  create_download_intent: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateDownloadIntentRequest'];
    successResponses: {
      201: components['schemas']['app__main__DownloadIntentResponse'];
    };
  };
  create_experiment: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateExperimentRequest'];
    successResponses: { 201: components['schemas']['ExperimentDetail'] };
  };
  create_external_creatives: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ExternalCreativeCreateRequest'];
    successResponses: { 202: components['schemas']['ExternalCreativeList'] };
  };
  create_generation_asset_review: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateReviewInput'];
    successResponses: { 201: components['schemas']['AssetReview'] };
  };
  create_generation_asset_version_download_intent: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      201: components['schemas']['app__generation__response_models__DownloadIntentResponse'];
    };
  };
  create_generation_draft: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['app__generation__v1_router__CreateDraftInput'];
    successResponses: { 201: components['schemas']['GenerationDraftDetail'] };
  };
  create_generation_export_download_intent: {
    pathParams: { export_package_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      201: components['schemas']['app__generation__response_models__DownloadIntentResponse'];
    };
  };
  create_generation_export_package: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateExportInput'];
    successResponses: { 202: components['schemas']['ExportPackageQueued'] };
  };
  create_generation_preview: {
    pathParams: { draft_id: string };
    queryParams: { experiment_context?: string; skip_losers?: string };
    requestBody: never;
    successResponses: { 201: components['schemas']['GenerationPreviewDetail'] };
  };
  create_generation_regeneration_draft: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateRegenerationDraftInput'];
    successResponses: { 201: components['schemas']['GenerationDraftDetail'] };
  };
  create_generation_review_summary_regeneration: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['RegenerationDraftBatchInput'];
    successResponses: {
      201: components['schemas']['RegenerationDraftBatchResult'];
    };
  };
  create_generation_run_recovery_preview: {
    pathParams: { run_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['RecoveryPreviewInput'];
    successResponses: {
      201: components['schemas']['GenerationRecoveryPreview'];
    };
  };
  create_library_entry: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateEntryInput'];
    successResponses: { 201: components['schemas']['LibraryEntryDetail'] };
  };
  create_library_internal_entry: {
    pathParams: { root_entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateInternalEntryInput'];
    successResponses: { 201: components['schemas']['LibraryEntryDetail'] };
  };
  create_library_reference_download_intent: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 201: components['schemas']['ReferenceDownloadGrant'] };
  };
  create_library_reference_object: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateReferenceObjectInput'];
    successResponses: { 202: components['schemas']['OperationAccepted'] };
  };
  create_localization_batch: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['app__localization__routes__CreateDraftInput'];
    successResponses: { 201: components['schemas']['BatchDraftResult'] };
  };
  create_marketing_evidence_handoff: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateEvidenceHandoff'];
    successResponses: {
      201: components['schemas']['CreatedEvidenceHandoffEnvelope'];
    };
  };
  create_marketing_handoff: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateHandoff'];
    successResponses: { 202: components['schemas']['ResourceEnvelope'] };
  };
  create_marketing_image_edit: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateImageEdit'];
    successResponses: { 202: components['schemas']['ResourceEnvelope'] };
  };
  create_marketing_project: {
    pathParams: Record<string, never>;
    queryParams: {
      include_languages?: string;
      include_plan_review?: string;
      include_brand_style?: string;
      include_execution_plan?: string;
      include_competitor_review?: string;
    };
    requestBody: components['schemas']['CreateProject'];
    successResponses: { 201: components['schemas']['MarketingProjectDetail'] };
  };
  create_meta_binding: {
    pathParams: { connection_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateBindingRequest'];
    successResponses: { 201: null };
  };
  create_meta_connection: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateConnectionRequest'];
    successResponses: { 201: null };
  };
  create_meta_contract: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateContractRequest'];
    successResponses: { 201: null };
  };
  create_meta_launch_preview: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['MultiCandidatePreviewInput'];
    successResponses: {
      201: components['schemas']['MultiCandidatePreviewResponse'];
    };
  };
  create_product_intake: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ProductIntakeCreateRequest'];
    successResponses: { 202: components['schemas']['ProductIntakeAccepted'] };
  };
  create_product_intake_image_download_intent: {
    pathParams: { intake_id: string; image_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      201: components['schemas']['IntakeImageDownloadGrant'];
    };
  };
  create_results_import: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ResultsImportCreateRequest'];
    successResponses: { 201: components['schemas']['ResultsImportView'] };
  };
  create_scale_activation_preview: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ActivationPreviewCreateRequest'];
    successResponses: {
      201: components['schemas']['ActivationPreviewResponse'];
    };
  };
  create_scale_advice_disposition: {
    pathParams: { meta_mode: string; thread_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['AdviceDispositionRequest'];
    successResponses: {
      201: components['schemas']['AdviceDispositionResponse'];
    };
  };
  create_scale_advice_snooze: {
    pathParams: { meta_mode: string; thread_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['AdviceSnoozeRequest'];
    successResponses: { 201: components['schemas']['AdviceSnoozeResponse'] };
  };
  create_scale_delivery_target: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['DeliveryTargetCreateRequest'];
    successResponses: { 201: components['schemas']['DeliveryTargetResponse'] };
  };
  create_scale_enrollment: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['EnrollmentCreateRequest'];
    successResponses: { 201: components['schemas']['EnrollmentResponse'] };
  };
  create_scale_enrollment_candidate: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CandidateCreateRequest'];
    successResponses: { 201: components['schemas']['CandidateResponse'] };
  };
  create_scale_export: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ExportCreateRequest'];
    successResponses: { 202: components['schemas']['ExportResponse'] };
  };
  create_scale_export_download_intent: {
    pathParams: { meta_mode: string; export_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      201: components['schemas']['ExportDownloadIntentResponse'];
    };
  };
  create_scale_target: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TargetPublishRequest'];
    successResponses: { 201: components['schemas']['TargetResponse'] };
  };
  create_scale_target_publication_preview: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TargetPublicationPreviewRequest'];
    successResponses: {
      201: components['schemas']['TargetPublicationPreviewResponse'];
    };
  };
  create_scale_target_withdrawal_preview: {
    pathParams: { meta_mode: string; target_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TargetWithdrawalPreviewRequest'];
    successResponses: {
      201: components['schemas']['TargetWithdrawalPreviewResponse'];
    };
  };
  create_test_plan: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TestPlanCreateRequest'];
    successResponses: { 202: components['schemas']['TestPlanAccepted'] };
  };
  create_test_plan_run: {
    pathParams: { plan_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TestPlanRunRequest'];
    successResponses: { 202: components['schemas']['TestPlanRunResult'] };
  };
  create_upload_intent: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CreateUploadIntentRequest'];
    successResponses: { 201: components['schemas']['UploadIntentResponse'] };
  };
  decide_competitor_discovery: {
    pathParams: { scan_id: string; advertiser_page_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorDiscoveryDecisionRequest'];
    successResponses: {
      200: components['schemas']['CompetitorDiscoveryDecisionResult'];
    };
  };
  delete_product_reference: {
    pathParams: { product_id: string; item_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ProductReferenceSet'] };
  };
  download_marketing_handoff: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      201: components['schemas']['app__generation__response_models__DownloadIntentResponse'];
    };
  };
  edit_marketing_project: {
    pathParams: { project_id: string };
    queryParams: {
      include_languages?: string;
      include_plan_review?: string;
      include_brand_style?: string;
      include_execution_plan?: string;
      include_competitor_review?: string;
    };
    requestBody: components['schemas']['EditProject'];
    successResponses: { 200: components['schemas']['MarketingProjectDetail'] };
  };
  evaluate_marketing_round: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ResourceEnvelope'] };
  };
  execute_meta_launch_preview: {
    pathParams: { preview_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['MultiCandidateExecuteInput'];
    successResponses: {
      202: components['schemas']['LaunchProvisioningAccepted'];
    };
  };
  finalize_upload: {
    pathParams: { intent_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 202: components['schemas']['FinalizeUploadResponse'] };
  };
  flip_competitor_ad_relevance: {
    pathParams: { scan_id: string; ad_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorAdRelevanceFlip'];
    successResponses: { 200: components['schemas']['CompetitorAdAssessment'] };
  };
  fork_library_version: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ForkInput'];
    successResponses: { 201: components['schemas']['LibraryEntryDetail'] };
  };
  freeze_experiment: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['FreezeExperimentRequest'];
    successResponses: { 200: components['schemas']['ExperimentDetail'] };
  };
  freeze_marketing_round: {
    pathParams: { project_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['FreezeRound'];
    successResponses: { 201: components['schemas']['ResourceEnvelope'] };
  };
  get_competitor: {
    pathParams: { competitor_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['Competitor'] };
  };
  get_competitor_resolution: {
    pathParams: { competitor_id: string; resolve_job_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorResolution'] };
  };
  get_competitor_scan: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorScan'] };
  };
  get_competitor_scan_snapshot: {
    pathParams: { scan_id: string; snapshot_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorScanSnapshot'] };
  };
  get_competitor_scan_summary: {
    pathParams: { scan_id: string };
    queryParams: { snapshot_id?: string };
    requestBody: never;
    successResponses: {
      200: components['schemas']['CompetitorScanSnapshotSummary'];
    };
  };
  get_cost_summary: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  get_experiment: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ExperimentDetail'] };
  };
  get_experiment_timeline: {
    pathParams: { experiment_id: string };
    queryParams: { cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['ExperimentTimeline'] };
  };
  get_external_creative: {
    pathParams: { external_creative_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ExternalCreativeDetail'] };
  };
  get_generation_asset_version: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['AssetVersionDetail'] };
  };
  get_generation_capabilities: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['GenerationCapabilities'] };
  };
  get_generation_defaults: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['GenerationDefaultsView'] };
  };
  get_generation_draft: {
    pathParams: { draft_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['GenerationDraftDetail'] };
  };
  get_generation_export_manifest: {
    pathParams: { export_package_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ExportManifest'] };
  };
  get_generation_export_package: {
    pathParams: { export_package_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ExportPackageDetail'] };
  };
  get_generation_preview: {
    pathParams: { preview_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200:
        | components['schemas']['GenerationPreviewDetail']
        | components['schemas']['GenerationRecoveryPreview'];
    };
  };
  get_generation_run: {
    pathParams: { run_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['GenerationRunDetail'] };
  };
  get_generation_run_item: {
    pathParams: { run_id: string; run_item_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['GenerationRunItemDetail'] };
  };
  get_identity_probe: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['IdentityProbeResponse'] };
  };
  get_library_command_outcome: {
    pathParams: { command_identity: string };
    queryParams: { command_kind?: string };
    requestBody: never;
    successResponses: {
      200:
        | components['schemas']['LibraryCommandPending']
        | components['schemas']['LibraryCommandSucceeded']
        | components['schemas']['LibraryCommandProblem']
        | components['schemas']['LibraryCommandReconciliationRequired']
        | components['schemas']['LibraryCommandNotApplied'];
    };
  };
  get_library_data_source_connection_check: {
    pathParams: { data_source_entry_id: string; check_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['DataSourceConnectionCheck'];
    };
  };
  get_library_data_source_credential_binding: {
    pathParams: { data_source_entry_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['CredentialBindingSummary'];
    };
  };
  get_library_entry: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LibraryEntryDetail'] };
  };
  get_library_internal_entry: {
    pathParams: { entry_type: string; entry_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['InternalEntryMeta'] };
  };
  get_library_internal_entry_context: {
    pathParams: { root_entry_id: string; entry_type: string; entry_id: string };
    queryParams: { parent_type?: string; parent_id?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['InternalEntryContext'] };
  };
  get_library_reference_object: {
    pathParams: { object_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ReferenceObjectDetail'] };
  };
  get_library_version: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LibraryVersionDetail'] };
  };
  get_library_version_impact: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LibraryVersionImpact'] };
  };
  get_localization_batch: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LocalizationBatchDetail'] };
  };
  get_localization_batch_outcome: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['BatchOutcome'] };
  };
  get_localization_item: {
    pathParams: { batch_id: string; item_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LocalizationItemDetail'] };
  };
  get_localization_workspace: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LocalizationWorkspace'] };
  };
  get_marketing_ai_qc_quote: {
    pathParams: { round_id: string };
    queryParams: { kind?: string; strategy_language?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['AIQCQuote'] };
  };
  get_marketing_capabilities: {
    pathParams: Record<string, never>;
    queryParams: { include_reporting?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['PlannerCapabilities'] };
  };
  get_marketing_evidence_handoff: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['EvidenceHandoffEnvelope'] };
  };
  get_marketing_project: {
    pathParams: { project_id: string };
    queryParams: {
      include_recovery?: string;
      include_languages?: string;
      include_plan_review?: string;
      include_brand_style?: string;
      include_execution_plan?: string;
      include_competitor_review?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['MarketingProjectDetail'] };
  };
  get_marketing_round: {
    pathParams: { round_id: string };
    queryParams: {
      include_image_edits?: string;
      include_version_history?: string;
      include_languages?: string;
      include_plan_review?: string;
      include_board?: string;
      include_brand_style?: string;
      include_execution_plan?: string;
      include_qc_kinds?: string;
      include_board_thumbnails?: string;
      include_text_language?: string;
      include_qc_evidence?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['ResourceEnvelope'] };
  };
  get_marketing_workflow: {
    pathParams: { project_id: string };
    queryParams: {
      include_polling?: string;
      include_image_edits?: string;
      include_version_history?: string;
      include_languages?: string;
      include_plan_review?: string;
      include_board?: string;
      include_brand_style?: string;
      include_execution_plan?: string;
      include_qc_kinds?: string;
      include_board_thumbnails?: string;
      include_text_language?: string;
      include_qc_evidence?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['WorkflowEnvelope'] };
  };
  get_meta_connection: {
    pathParams: { connection_id: string };
    queryParams: { meta_mode: string };
    requestBody: never;
    successResponses: { 200: null };
  };
  get_meta_launch_preview: {
    pathParams: { preview_id: string };
    queryParams: { meta_mode: string };
    requestBody: never;
    successResponses: {
      200: components['schemas']['MultiCandidatePreviewResponse'];
    };
  };
  get_meta_launch_provisioning: {
    pathParams: { provisioning_id: string };
    queryParams: { meta_mode: string };
    requestBody: never;
    successResponses: {
      200: components['schemas']['LaunchProvisioningResponse'];
    };
  };
  get_operation: {
    pathParams: { operation_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['OperationDetailResponse'];
      304: null;
    };
  };
  get_product_brand_kit: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ProductBrandKit'] };
  };
  get_product_competitor_settings: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['ProductCompetitorSettings'];
    };
  };
  get_product_intake: {
    pathParams: { intake_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ProductIntakeDetail'] };
  };
  get_product_learnings: {
    pathParams: { product_id: string };
    queryParams: { evidence_mode?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['ProductLearnings'] };
  };
  get_product_references: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ProductReferenceSet'] };
  };
  get_results_import: {
    pathParams: { import_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ResultsImportView'] };
  };
  get_results_template: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ResultsTemplate'] };
  };
  get_scale_command_outcome: {
    pathParams: { meta_mode: string; identity: string };
    queryParams: { command_kind?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['CommandOutcomeResponse'] };
  };
  get_scale_enrollment: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['EnrollmentResponse'] };
  };
  get_scale_enrollment_breakdowns: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: {
      snapshot_id: string;
      breakdown_type: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['BreakdownResponse'] };
  };
  get_scale_enrollment_evidence: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['EnrollmentEvidenceResponse'];
    };
  };
  get_scale_episode: {
    pathParams: {
      meta_mode: string;
      enrollment_id: string;
      episode_id: string;
    };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['EpisodeResponse'] };
  };
  get_scale_target: {
    pathParams: { meta_mode: string; target_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['TargetResponse'] };
  };
  get_spend_guardrail: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  get_test_plan: {
    pathParams: { plan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['TestPlanDetail'] };
  };
  get_test_plan_results: {
    pathParams: { plan_id: string };
    queryParams: { evidence_mode?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['TestPlanResults'] };
  };
  get_usage: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  import_library_manual_facts: {
    pathParams: { data_source_entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ManualFactImportInput'];
    successResponses: { 202: components['schemas']['OperationAccepted'] };
  };
  judge_experiment: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['JudgeExperimentRequest'];
    successResponses: {
      200: components['schemas']['ExperimentJudgementResult'];
    };
  };
  list_audit_events: {
    pathParams: Record<string, never>;
    queryParams: {
      operation?: string;
      resource?: string;
      initiator?: string;
      executor?: string;
      event_type?: string;
      meta_mode?: string;
      since?: string;
      until?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: null };
  };
  list_competitor_ad_assessments: {
    pathParams: { scan_id: string; ad_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['CompetitorAdAssessmentList'];
    };
  };
  list_competitor_ad_tags: {
    pathParams: { scan_id: string; ad_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorAdTagList'] };
  };
  list_competitor_scan_ads: {
    pathParams: { scan_id: string };
    queryParams: {
      competitor_id?: string;
      role?: string;
      origin_kind?: string;
      min_days_running?: string;
      display_format?: string;
      relevance?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorAdPage'] };
  };
  list_competitor_scan_discoveries: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorDiscoveryList'] };
  };
  list_competitor_scan_snapshots: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['CompetitorScanSnapshotList'];
    };
  };
  list_competitor_scans: {
    pathParams: Record<string, never>;
    queryParams: { product_id?: string; status?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorScanList'] };
  };
  list_competitors: {
    pathParams: Record<string, never>;
    queryParams: { product_id?: string; status?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorList'] };
  };
  list_credentials: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  list_experiments: {
    pathParams: Record<string, never>;
    queryParams: { cursor?: string; limit?: string; state?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['ExperimentPage'] };
  };
  list_external_creatives: {
    pathParams: Record<string, never>;
    queryParams: { product_id?: string; status?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['ExternalCreativeList'] };
  };
  list_generation_assets: {
    pathParams: Record<string, never>;
    queryParams: {
      product_id?: string;
      run_id?: string;
      provider?: string;
      origin_kind?: string;
      execution_mode?: string;
      review_value?: string;
      aspect_ratio?: string;
      qc_state?: string;
      tag?: string;
      experiment_context?: string;
      conclusion?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['AssetGalleryPage'] };
  };
  list_generation_drafts: {
    pathParams: Record<string, never>;
    queryParams: {
      product_id?: string;
      state?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_generation_run_items: {
    pathParams: { run_id: string };
    queryParams: { state?: string; cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_generation_runs: {
    pathParams: Record<string, never>;
    queryParams: {
      product_id?: string;
      status?: string;
      execution_mode?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_library_entries: {
    pathParams: Record<string, never>;
    queryParams: {
      entry_type?: string;
      include_archived?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_library_entry_events: {
    pathParams: { entry_id: string };
    queryParams: { cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_library_entry_types: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LibraryEntryTypeList'] };
  };
  list_library_entry_versions: {
    pathParams: { entry_id: string };
    queryParams: { cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_library_internal_entries: {
    pathParams: { root_entry_id: string };
    queryParams: {
      entry_type: string;
      parent_type?: string;
      parent_id?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_library_version_bindings: {
    pathParams: { version_id: string };
    queryParams: { cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['Page'] };
  };
  list_localization_batches: {
    pathParams: Record<string, never>;
    queryParams: { phase?: string; cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['LocalizationBatchPage'] };
  };
  list_marketing_brand_kits: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['BrandKitList'] };
  };
  list_marketing_projects: {
    pathParams: Record<string, never>;
    queryParams: { limit?: string; cursor?: string; q?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['MarketingProjectPage'] };
  };
  list_meta_bindings: {
    pathParams: { connection_id: string };
    queryParams: { meta_mode: string; cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: null };
  };
  list_meta_connections: {
    pathParams: Record<string, never>;
    queryParams: { meta_mode: string; cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: null };
  };
  list_meta_contracts: {
    pathParams: Record<string, never>;
    queryParams: {
      meta_mode: string;
      connection_id?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: null };
  };
  list_products: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ProductList'] };
  };
  list_scale_advice_threads: {
    pathParams: { meta_mode: string };
    queryParams: {
      enrollment_id?: string;
      lifecycle?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: {
      200: components['schemas']['AdviceThreadListResponse'];
    };
  };
  list_scale_available_modes: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  list_scale_enrollment_candidates: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  list_scale_enrollments: {
    pathParams: { meta_mode: string };
    queryParams: { lifecycle?: string; cursor?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['EnrollmentListResponse'] };
  };
  list_scale_observed_ads: {
    pathParams: { meta_mode: string };
    queryParams: { meta_connection_id?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['ObservedAdListResponse'] };
  };
  list_scale_target_contexts: {
    pathParams: { meta_mode: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: null };
  };
  list_scale_targets: {
    pathParams: { meta_mode: string };
    queryParams: {
      lifecycle_state?: string;
      product_version_id?: string;
      offer_version_id?: string;
      market_code?: string;
      cursor?: string;
      limit?: string;
    };
    requestBody: never;
    successResponses: { 200: components['schemas']['TargetListResponse'] };
  };
  list_test_plans: {
    pathParams: Record<string, never>;
    queryParams: { product_id?: string; run_id?: string; limit?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['TestPlanList'] };
  };
  pause_scale_enrollment: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['EnrollmentPauseRequest'];
    successResponses: { 200: components['schemas']['EnrollmentResponse'] };
  };
  prepare_marketing_feedback: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['PrepareFeedback'];
    successResponses: { 201: components['schemas']['ResourceEnvelope'] };
  };
  prepare_marketing_generation: {
    pathParams: { project_id: string };
    queryParams: {
      include_languages?: string;
      include_plan_review?: string;
      include_brand_style?: string;
      include_execution_plan?: string;
      include_competitor_review?: string;
    };
    requestBody: components['schemas']['PrepareGeneration'];
    successResponses: { 201: components['schemas']['MarketingProjectDetail'] };
  };
  preview_competitor_scan: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorScanRequest'];
    successResponses: { 200: components['schemas']['CompetitorScanQuote'] };
  };
  preview_library_archive_impact: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 201: components['schemas']['CommandImpactProjection'] };
  };
  preview_library_publish_impact: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 201: components['schemas']['CommandImpactProjection'] };
  };
  preview_library_template_reapply: {
    pathParams: { template_version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ReapplyPreviewInput'];
    successResponses: {
      201: components['schemas']['TemplateDiffReportResponse'];
    };
  };
  preview_library_withdraw_impact: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 201: components['schemas']['CommandImpactProjection'] };
  };
  preview_marketing_metrics: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['PreviewMetrics'];
    successResponses: { 201: components['schemas']['ResourceEnvelope'] };
  };
  preview_spend_guardrail: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['GuardrailPreviewRequest'];
    successResponses: { 200: null };
  };
  promote_library_reference_object: {
    pathParams: { object_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['PromoteReferenceObjectInput'];
    successResponses: {
      201: components['schemas']['ReferencePromotionResult'];
    };
  };
  publish_library_entry: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['PublishInput'];
    successResponses: { 201: components['schemas']['LibraryVersionDetail'] };
  };
  quote_competitor_resolve: {
    pathParams: { competitor_id: string };
    queryParams: { execution_mode?: string; country?: string };
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorResolveQuote'] };
  };
  quote_competitor_scan_recluster: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: {
      200: components['schemas']['CompetitorScanReclusterQuote'];
    };
  };
  quote_competitor_scan_resume: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorScanQuote'] };
  };
  recluster_competitor_scan: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 202: components['schemas']['CompetitorScan'] };
  };
  reconcile_competitor_scan: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorScanReconcileRequest'];
    successResponses: { 200: components['schemas']['CompetitorScan'] };
  };
  reconcile_generation_run: {
    pathParams: { run_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ReconcileRunInput'];
    successResponses: { 200: components['schemas']['RunCommandResult'] };
  };
  reconcile_library_command_outcome: {
    pathParams: { command_identity: string };
    queryParams: { command_kind?: string };
    requestBody: never;
    successResponses: {
      200:
        | components['schemas']['LibraryCommandPending']
        | components['schemas']['LibraryCommandSucceeded']
        | components['schemas']['LibraryCommandProblem']
        | components['schemas']['LibraryCommandReconciliationRequired']
        | components['schemas']['LibraryCommandNotApplied'];
    };
  };
  recover_meta_launch_operation: {
    pathParams: { operation_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['MetaModeRequest'];
    successResponses: { 202: components['schemas']['LaunchRecoveryResponse'] };
  };
  refreeze_competitor_scan: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['CompetitorScanSnapshot'] };
  };
  resolve_competitor: {
    pathParams: { competitor_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['CompetitorResolveRequest'];
    successResponses: { 202: components['schemas']['CompetitorResolution'] };
  };
  resolve_marketing_call: {
    pathParams: { call_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ResolveCall'];
    successResponses: { 202: components['schemas']['ResolutionSummary'] };
  };
  resume_competitor_scan: {
    pathParams: { scan_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 202: components['schemas']['CompetitorScan'] };
  };
  resume_generation_run_paused_items: {
    pathParams: { run_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ResumePausedItemsInput'];
    successResponses: { 200: components['schemas']['RunCommandResult'] };
  };
  resume_localization_item: {
    pathParams: { batch_id: string; item_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['ResumeItemResult'] };
  };
  resume_scale_enrollment: {
    pathParams: { meta_mode: string; enrollment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['EnrollmentResumeRequest'];
    successResponses: { 200: components['schemas']['EnrollmentResponse'] };
  };
  retire_meta_connection: {
    pathParams: { connection_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['MetaModeRequest'];
    successResponses: { 200: null };
  };
  retry_generation_run_failed_items: {
    pathParams: { run_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['RetryFailedItemsInput'];
    successResponses: { 200: components['schemas']['RunCommandResult'] };
  };
  retry_localization_item: {
    pathParams: { batch_id: string; item_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['RetryItemResult'] };
  };
  retry_marketing_handoff: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 202: components['schemas']['ResourceEnvelope'] };
  };
  reuse_marketing_creative: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ReuseCreative'];
    successResponses: { 201: components['schemas']['ResourceEnvelope'] };
  };
  review_marketing_creative: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ReviewCreative'];
    successResponses: { 201: components['schemas']['ResourceEnvelope'] };
  };
  revise_test_plan: {
    pathParams: { plan_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TestPlanReviseRequest'];
    successResponses: { 200: components['schemas']['TestPlanDetail'] };
  };
  revoke_credential: {
    pathParams: { credential_version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['RevokeCredentialRequest'];
    successResponses: { 200: null };
  };
  rotate_credential: {
    pathParams: { credential_key: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['RotateCredentialRequest'];
    successResponses: { 200: null };
  };
  save_library_draft: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['SaveDraftInput'];
    successResponses: { 200: components['schemas']['LibraryEntryDetail'] };
  };
  save_product_brand_kit: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['BrandKitInput'];
    successResponses: { 200: components['schemas']['ProductBrandKit'] };
  };
  start_experiment: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['StartExperimentRequest'];
    successResponses: { 200: components['schemas']['ExperimentDetail'] };
  };
  start_localization_preparation: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['StartPreparationInput'];
    successResponses: { 200: components['schemas']['StartPreparationResult'] };
  };
  start_marketing_ai_qc: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['StartAIQC'];
    successResponses: { 202: components['schemas']['ResourceEnvelope'] };
  };
  start_marketing_explanation: {
    pathParams: { round_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['RequestExplanation'];
    successResponses: { 202: components['schemas']['ResourceEnvelope'] };
  };
  start_marketing_planning: {
    pathParams: { project_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['app__marketing__api__PlanRequest'];
    successResponses: { 202: components['schemas']['PlanningCallSummary'] };
  };
  submit_generation_run: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['SubmitRunInput'];
    successResponses: { 202: components['schemas']['OperationRef'] };
  };
  submit_localization_claim_decisions: {
    pathParams: { batch_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['SubmitClaimDecisionsInput'];
    successResponses: {
      200: components['schemas']['SubmitClaimDecisionsResult'];
    };
  };
  supersede_meta_binding: {
    pathParams: { connection_id: string; binding_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['SupersedeBindingRequest'];
    successResponses: { 201: null };
  };
  update_experiment_context: {
    pathParams: { experiment_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['UpdateExperimentContextRequest'];
    successResponses: {
      200: components['schemas']['ExperimentContextMutation'];
    };
  };
  update_experiment_entry: {
    pathParams: { experiment_id: string; entry_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['UpdateExperimentEntryRequest'];
    successResponses: { 200: components['schemas']['ExperimentEntryMutation'] };
  };
  update_generation_defaults: {
    pathParams: Record<string, never>;
    queryParams: Record<string, never>;
    requestBody: components['schemas']['GenerationDefaultsUpdate'];
    successResponses: { 200: components['schemas']['GenerationDefaultsView'] };
  };
  update_generation_draft: {
    pathParams: { draft_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['UpdateDraftInput'];
    successResponses: { 200: components['schemas']['GenerationDraftDetail'] };
  };
  update_product_competitor_settings: {
    pathParams: { product_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['ProductCompetitorSettingsUpdate'];
    successResponses: {
      200: components['schemas']['ProductCompetitorSettings'];
    };
  };
  update_product_reference: {
    pathParams: { product_id: string; item_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['UpdateProductReference'];
    successResponses: { 200: components['schemas']['ProductReferenceSet'] };
  };
  validate_library_draft: {
    pathParams: { entry_id: string };
    queryParams: Record<string, never>;
    requestBody: never;
    successResponses: { 200: components['schemas']['LibraryValidationResult'] };
  };
  withdraw_library_version: {
    pathParams: { version_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['WithdrawInput'];
    successResponses: { 200: components['schemas']['LibraryVersionImpact'] };
  };
  withdraw_scale_target: {
    pathParams: { meta_mode: string; target_id: string };
    queryParams: Record<string, never>;
    requestBody: components['schemas']['TargetWithdrawRequest'];
    successResponses: { 200: components['schemas']['TargetResponse'] };
  };
};
