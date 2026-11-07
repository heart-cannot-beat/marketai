/**
 * `DomainGateway` — the browser-only client for the BFF domain proxy
 * (spec §6.6). No access to `req`/`res`, JWT issuers or `DOMAIN_API_BASE_URL`
 * — only a same-origin `fetch` to a path `buildBffPath()` resolves from the
 * generated allowlist manifest.
 */
import { buildBffPath } from './generated/paths';
import type {
  DomainResultFor,
  ProblemDetails,
  ResponseMeta,
} from './result-types';
import type { components } from './generated/domain-v1';
import type { DomainOperationMap } from './generated/operation-map';
import { CsrfTokenManager } from './csrf';
import { IntentTracker, type ReconciliationRequired } from './intent';

type IdentityProbeResponse = components['schemas']['IdentityProbeResponse'];
type OperationDetailResponse = components['schemas']['OperationDetailResponse'];
type CreateUploadIntentRequest =
  components['schemas']['CreateUploadIntentRequest'];
type UploadIntentResponse = components['schemas']['UploadIntentResponse'];
type FinalizeUploadResponse = components['schemas']['FinalizeUploadResponse'];
type CreateDownloadIntentRequest =
  components['schemas']['CreateDownloadIntentRequest'];
type DownloadIntentResponse =
  components['schemas']['app__main__DownloadIntentResponse'];

type CommandTransport = {
  /** Draft aggregate revision, forwarded by the BFF as `If-Match`. */
  ifMatch?: string | number;
};

type ConditionalCommandTransport = Pick<CommandTransport, 'ifMatch'>;

export interface DomainGateway {
  resolveMarketingCall(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    callId: string;
    body: DomainOperationMap['resolve_marketing_call']['requestBody'];
  }): Promise<
    DomainResultFor<'resolve_marketing_call'> | ReconciliationRequired
  >;

  getIdentityProbe(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_identity_probe'>>;

  pollOperation(params: {
    workspaceSlug: string;
    operationId: string;
    /** A previously observed `ResponseMeta.etag` — sent as `If-None-Match`
     * so an unchanged Operation replies `304` instead of re-sending the
     * full body (spec §5.6). */
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_operation'>>;

  /**
   * Phase 1 of the file lifecycle (spec §7.2): validates purpose/MIME/size
   * with FastAPI and returns an `UploadGrant` for the browser to POST/PUT
   * directly to object storage (`BrowserBlobTransferClient`, Phase 2). Never
   * absorbs binary data itself.
   *
   * `actionInstanceId` is a stable identifier for the logical user action
   * (spec §6.10) — passed to `IntentTracker.acquire()` to derive the
   * `X-Intent-Id` this non-safe, idempotent operation requires. A retained,
   * ambiguous-outcome intent past its safe-retry window surfaces as
   * `ReconciliationRequired` instead of sending the request.
   */
  createUploadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: CreateUploadIntentRequest;
  }): Promise<DomainResultFor<'create_upload_intent'> | ReconciliationRequired>;

  /**
   * Phase 3 of the file lifecycle (spec §7.2, §7.8): always asynchronous —
   * 202 + Operation. `uploadIntentId` is the `intent_id` returned by
   * `createUploadIntent`, not the `IntentTracker` action identity.
   */
  finalizeUpload(params: {
    workspaceSlug: string;
    uploadIntentId: string;
    actionInstanceId: string;
  }): Promise<DomainResultFor<'finalize_upload'> | ReconciliationRequired>;

  /** Creates an auditable `DownloadIntentRecord` and returns a signed `DownloadGrant` (spec §7.12) — POST, not GET, because it is a non-safe, audited write. */
  createDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: CreateDownloadIntentRequest;
  }): Promise<
    DomainResultFor<'create_download_intent'> | ReconciliationRequired
  >;

  // ---- Library reads (spec: docs/product/creative-library-ownership-and-reuse-contract.md) ----

  listLibraryEntryTypes(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_library_entry_types'>>;

  listLibraryEntries(params: {
    workspaceSlug: string;
    entryType?: string;
    includeArchived?: boolean;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_entries'>>;

  getLibraryEntry(params: {
    workspaceSlug: string;
    entryId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_library_entry'>>;

  listLibraryEntryVersions(params: {
    workspaceSlug: string;
    entryId: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_entry_versions'>>;

  listLibraryEntryEvents(params: {
    workspaceSlug: string;
    entryId: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_entry_events'>>;

  getLibraryVersion(params: {
    workspaceSlug: string;
    versionId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_library_version'>>;

  getLibraryVersionImpact(params: {
    workspaceSlug: string;
    versionId: string;
  }): Promise<DomainResultFor<'get_library_version_impact'>>;

  listLibraryVersionBindings(params: {
    workspaceSlug: string;
    versionId: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_version_bindings'>>;

  // ---- Library commands ----

  createLibraryEntry(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_library_entry']['requestBody'];
  }): Promise<DomainResultFor<'create_library_entry'> | ReconciliationRequired>;

  saveLibraryDraft(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['save_library_draft']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'save_library_draft'> | ReconciliationRequired>;

  validateLibraryDraft(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
    } & CommandTransport
  ): Promise<
    DomainResultFor<'validate_library_draft'> | ReconciliationRequired
  >;

  publishLibraryEntry(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['publish_library_entry']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'publish_library_entry'> | ReconciliationRequired>;

  archiveLibraryEntry(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['archive_library_entry']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'archive_library_entry'> | ReconciliationRequired>;

  bindLibraryReference(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['bind_library_reference']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'bind_library_reference'> | ReconciliationRequired
  >;

  withdrawLibraryVersion(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      versionId: string;
      body: DomainOperationMap['withdraw_library_version']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'withdraw_library_version'> | ReconciliationRequired
  >;

  forkLibraryVersion(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['fork_library_version']['requestBody'];
  }): Promise<DomainResultFor<'fork_library_version'> | ReconciliationRequired>;

  cloneLibraryProduct(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['clone_library_product']['requestBody'];
  }): Promise<
    DomainResultFor<'clone_library_product'> | ReconciliationRequired
  >;

  applyLibraryTemplate(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    templateVersionId: string;
    body: DomainOperationMap['apply_library_template']['requestBody'];
  }): Promise<
    DomainResultFor<'apply_library_template'> | ReconciliationRequired
  >;

  createLibraryReferenceObject(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_library_reference_object']['requestBody'];
  }): Promise<
    DomainResultFor<'create_library_reference_object'> | ReconciliationRequired
  >;

  createLibraryReferenceDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
  }): Promise<
    | DomainResultFor<'create_library_reference_download_intent'>
    | ReconciliationRequired
  >;

  /** Gate evidence for one reference object: state, per-check results and
   * the stable `failure_code` the UI keys its copy on (Issue #158). */
  getLibraryReferenceObject(params: {
    workspaceSlug: string;
    objectId: string;
  }): Promise<DomainResultFor<'get_library_reference_object'>>;

  /** Publishes a released reference object as a ReferenceAssetVersion and
   * optionally binds it to a product/person Draft -- the server builds the
   * version content from gate evidence, so no raw JSON crosses this seam. */
  promoteLibraryReferenceObject(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    objectId: string;
    body: DomainOperationMap['promote_library_reference_object']['requestBody'];
  }): Promise<
    DomainResultFor<'promote_library_reference_object'> | ReconciliationRequired
  >;

  // ---- Products (Issue #162) ----

  /** The Workspace's products with display name, main image and latest
   * Run -- product home, product names and the New Draft picker. */
  listProducts(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_products'>>;

  getProductReferences(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_product_references'>>;
  addProductReference(params: {
    workspaceSlug: string;
    productId: string;
    actionInstanceId: string;
    ifMatch: number;
    body: DomainOperationMap['add_product_reference']['requestBody'];
  }): Promise<
    DomainResultFor<'add_product_reference'> | ReconciliationRequired
  >;
  updateProductReference(params: {
    workspaceSlug: string;
    productId: string;
    itemId: string;
    actionInstanceId: string;
    ifMatch: number;
    body: DomainOperationMap['update_product_reference']['requestBody'];
  }): Promise<
    DomainResultFor<'update_product_reference'> | ReconciliationRequired
  >;
  deleteProductReference(params: {
    workspaceSlug: string;
    productId: string;
    itemId: string;
    actionInstanceId: string;
    ifMatch: number;
  }): Promise<
    DomainResultFor<'delete_product_reference'> | ReconciliationRequired
  >;

  // ---- Product intake (Issue #161) ----

  /** Starts recognizing a product from a link, finalized image uploads
   * (purpose `product_intake_image`) and/or text; 202 with the intake and
   * its Operation. The same input within 24 h returns the same intake. */
  createProductIntake(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_product_intake']['requestBody'];
  }): Promise<
    DomainResultFor<'create_product_intake'> | ReconciliationRequired
  >;

  /** Status, per-step evidence and the AI product-card draft. */
  getProductIntake(params: {
    workspaceSlug: string;
    intakeId: string;
  }): Promise<DomainResultFor<'get_product_intake'>>;

  /** Publishes the (edited) card as a ProductVersion and returns `prod_`. */
  confirmProductIntake(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    intakeId: string;
    body: DomainOperationMap['confirm_product_intake']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_product_intake'> | ReconciliationRequired
  >;

  /** A short-lived signed link to a released candidate image. */
  createProductIntakeImageDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    intakeId: string;
    imageId: string;
  }): Promise<
    | DomainResultFor<'create_product_intake_image_download_intent'>
    | ReconciliationRequired
  >;

  // ---- Test plans and generation defaults (Issue #163) ----

  /** Admits a test plan for a product; 202 with the plan and the
   * Operation of its drafting Job (poll `getTestPlan` until `ready`). */
  createTestPlan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_test_plan']['requestBody'];
  }): Promise<DomainResultFor<'create_test_plan'> | ReconciliationRequired>;

  /** Newest plans first, optionally for one product, or the plan behind
   * one generation Run (`runId`). */
  listTestPlans(params: {
    workspaceSlug: string;
    productId?: string;
    runId?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_test_plans'>>;

  /** Plan, direction cards, per-scale counts and max cost, linked Runs. */
  getTestPlan(params: {
    workspaceSlug: string;
    planId: string;
  }): Promise<DomainResultFor<'get_test_plan'>>;

  /** Removes / restores / edits / adds directions; requires `ifMatch`
   * (the plan's `revision`). */
  reviseTestPlan(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      planId: string;
      body: DomainOperationMap['revise_test_plan']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<DomainResultFor<'revise_test_plan'> | ReconciliationRequired>;

  /** Materialises `sample` or `full` into a submitted Run (idempotent per
   * plan + stage). */
  createTestPlanRun(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    planId: string;
    body: DomainOperationMap['create_test_plan_run']['requestBody'];
  }): Promise<DomainResultFor<'create_test_plan_run'> | ReconciliationRequired>;

  /** The Workspace's effective「生成默认值」. */
  getGenerationDefaults(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_generation_defaults'>>;

  /** Owner-only; `ifMatch` is the current `revision` (`0` before the
   * first save). */
  updateGenerationDefaults(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      body: DomainOperationMap['update_generation_defaults']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'update_generation_defaults'> | ReconciliationRequired
  >;

  // ---- Results imports and learnings (Issue #166) ----

  /** Parses, validates and matches result rows into a preview (201);
   * nothing is counted until `commitResultsImport`. */
  createResultsImport(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_results_import']['requestBody'];
  }): Promise<
    DomainResultFor<'create_results_import'> | ReconciliationRequired
  >;

  /** An import with its match preview. */
  getResultsImport(params: {
    workspaceSlug: string;
    importId: string;
  }): Promise<DomainResultFor<'get_results_import'>>;

  /** Counts the accepted rows and recomputes the learnings (409
   * `preview_stale` when the evidence changed since the preview). */
  commitResultsImport(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    importId: string;
  }): Promise<
    DomainResultFor<'commit_results_import'> | ReconciliationRequired
  >;

  /** 「这个产品我们已经学到了什么」: the latest learnings snapshot. */
  getProductLearnings(params: {
    workspaceSlug: string;
    productId: string;
    evidenceMode?: string;
  }): Promise<DomainResultFor<'get_product_learnings'>>;

  /** Rows of the downloadable results template (one per image). */
  getResultsTemplate(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_results_template'>>;

  /** One plan's directions and factor values with metrics. */
  getTestPlanResults(params: {
    workspaceSlug: string;
    planId: string;
    evidenceMode?: string;
  }): Promise<DomainResultFor<'get_test_plan_results'>>;

  // ---- External creatives (Issue #168) ----

  /** Admits finalized `external_creative` uploads for one product: each
   * is gated, stored as an external AssetVersion and tagged (202). The
   * same image bytes for the same product replay the existing creative. */
  createExternalCreatives(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_external_creatives']['requestBody'];
  }): Promise<
    DomainResultFor<'create_external_creatives'> | ReconciliationRequired
  >;

  /** Newest external creatives first, optionally for one product / status. */
  listExternalCreatives(params: {
    workspaceSlug: string;
    productId?: string;
    status?: string;
  }): Promise<DomainResultFor<'list_external_creatives'>>;

  /** Status, vocabulary, the AI tag suggestion and the confirmed tags. */
  getExternalCreative(params: {
    workspaceSlug: string;
    externalCreativeId: string;
  }): Promise<DomainResultFor<'get_external_creative'>>;

  /** Confirms (or corrects) the tags; the image gets the concept identity a
   * generated image with the same values carries. */
  confirmExternalCreative(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    externalCreativeId: string;
    body: DomainOperationMap['confirm_external_creative']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_external_creative'> | ReconciliationRequired
  >;

  // ---- Library internal resources, command outcomes, impacts, data sources ----

  getLibraryCommandOutcome(params: {
    workspaceSlug: string;
    commandIdentity: string;
    commandKind?: string;
  }): Promise<DomainResultFor<'get_library_command_outcome'>>;

  reconcileLibraryCommandOutcome(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    commandIdentity: string;
    commandKind?: string;
  }): Promise<
    | DomainResultFor<'reconcile_library_command_outcome'>
    | ReconciliationRequired
  >;

  listLibraryInternalEntries(params: {
    workspaceSlug: string;
    rootEntryId: string;
    entryType?: string;
    parentId?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_internal_entries'>>;

  getLibraryInternalEntry(params: {
    workspaceSlug: string;
    entryType: string;
    entryId: string;
  }): Promise<DomainResultFor<'get_library_internal_entry'>>;

  getLibraryInternalEntryContext(params: {
    workspaceSlug: string;
    rootEntryId: string;
    entryType: string;
    entryId: string;
  }): Promise<DomainResultFor<'get_library_internal_entry_context'>>;

  createLibraryInternalEntry(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    rootEntryId: string;
    body: DomainOperationMap['create_library_internal_entry']['requestBody'];
  }): Promise<
    DomainResultFor<'create_library_internal_entry'> | ReconciliationRequired
  >;

  previewLibraryPublishImpact(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    entryId: string;
  }): Promise<
    DomainResultFor<'preview_library_publish_impact'> | ReconciliationRequired
  >;

  previewLibraryArchiveImpact(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    entryId: string;
  }): Promise<
    DomainResultFor<'preview_library_archive_impact'> | ReconciliationRequired
  >;

  previewLibraryWithdrawImpact(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
  }): Promise<
    DomainResultFor<'preview_library_withdraw_impact'> | ReconciliationRequired
  >;

  previewLibraryTemplateReapply(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    templateVersionId: string;
    body: DomainOperationMap['preview_library_template_reapply']['requestBody'];
  }): Promise<
    DomainResultFor<'preview_library_template_reapply'> | ReconciliationRequired
  >;

  getLibraryDataSourceCredentialBinding(params: {
    workspaceSlug: string;
    dataSourceEntryId: string;
  }): Promise<DomainResultFor<'get_library_data_source_credential_binding'>>;

  bindLibraryDataSourceCredential(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      dataSourceEntryId: string;
      body: DomainOperationMap['bind_library_data_source_credential']['requestBody'];
    } & CommandTransport
  ): Promise<
    | DomainResultFor<'bind_library_data_source_credential'>
    | ReconciliationRequired
  >;

  checkLibraryDataSourceConnection(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    dataSourceEntryId: string;
    body: DomainOperationMap['check_library_data_source_connection']['requestBody'];
  }): Promise<
    | DomainResultFor<'check_library_data_source_connection'>
    | ReconciliationRequired
  >;

  /** One finished connection check: `checkId` is the `resource_id` of its
   * Operation's result (#383). */
  getLibraryDataSourceConnectionCheck(params: {
    workspaceSlug: string;
    dataSourceEntryId: string;
    checkId: string;
  }): Promise<DomainResultFor<'get_library_data_source_connection_check'>>;

  importLibraryManualFacts(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    dataSourceEntryId: string;
    body: DomainOperationMap['import_library_manual_facts']['requestBody'];
  }): Promise<
    DomainResultFor<'import_library_manual_facts'> | ReconciliationRequired
  >;

  // ---- Experiments reads and commands ----

  listExperiments(params: {
    workspaceSlug: string;
    state?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_experiments'>>;

  getExperiment(params: {
    workspaceSlug: string;
    experimentId: string;
  }): Promise<DomainResultFor<'get_experiment'>>;

  getExperimentTimeline(params: {
    workspaceSlug: string;
    experimentId: string;
    cursor?: number;
    limit?: number;
  }): Promise<DomainResultFor<'get_experiment_timeline'>>;

  analyzeExperimentDuplicates(params: {
    workspaceSlug: string;
    experimentId: string;
  }): Promise<DomainResultFor<'analyze_experiment_duplicates'>>;

  createExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_experiment']['requestBody'];
  }): Promise<DomainResultFor<'create_experiment'> | ReconciliationRequired>;

  addExperimentEntry(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['add_experiment_entry']['requestBody'];
  }): Promise<DomainResultFor<'add_experiment_entry'> | ReconciliationRequired>;

  updateExperimentContext(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['update_experiment_context']['requestBody'];
  }): Promise<
    DomainResultFor<'update_experiment_context'> | ReconciliationRequired
  >;

  freezeExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['freeze_experiment']['requestBody'];
  }): Promise<DomainResultFor<'freeze_experiment'> | ReconciliationRequired>;

  bindExperimentCandidate(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    candidateId: string;
    body: DomainOperationMap['bind_experiment_candidate']['requestBody'];
  }): Promise<
    DomainResultFor<'bind_experiment_candidate'> | ReconciliationRequired
  >;

  startExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['start_experiment']['requestBody'];
  }): Promise<DomainResultFor<'start_experiment'> | ReconciliationRequired>;

  judgeExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['judge_experiment']['requestBody'];
  }): Promise<DomainResultFor<'judge_experiment'> | ReconciliationRequired>;

  confirmExperimentProposal(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    proposalId: string;
    body: DomainOperationMap['confirm_experiment_proposal']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_experiment_proposal'> | ReconciliationRequired
  >;

  createMetaLaunchPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_meta_launch_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'create_meta_launch_preview'> | ReconciliationRequired
  >;

  createMetaConnection(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_meta_connection']['requestBody'];
  }): Promise<
    DomainResultFor<'create_meta_connection'> | ReconciliationRequired
  >;

  listMetaConnections(params: {
    workspaceSlug: string;
    metaMode: string;
  }): Promise<DomainResultFor<'list_meta_connections'>>;

  getMetaConnection(params: {
    workspaceSlug: string;
    connectionId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_meta_connection'>>;

  confirmMetaLaunchPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    previewId: string;
    body: DomainOperationMap['confirm_meta_launch_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_meta_launch_preview'> | ReconciliationRequired
  >;

  getMetaLaunchPreview(params: {
    workspaceSlug: string;
    previewId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_meta_launch_preview'>>;

  executeMetaLaunchPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    previewId: string;
    body: DomainOperationMap['execute_meta_launch_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'execute_meta_launch_preview'> | ReconciliationRequired
  >;

  getMetaLaunchProvisioning(params: {
    workspaceSlug: string;
    provisioningId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_meta_launch_provisioning'>>;

  recoverMetaLaunchOperation(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    operationId: string;
    body: DomainOperationMap['recover_meta_launch_operation']['requestBody'];
  }): Promise<
    DomainResultFor<'recover_meta_launch_operation'> | ReconciliationRequired
  >;

  // ---- Generation reads (spec: docs/product/v9-functional-alignment-acceptance.md) ----

  getMarketingWorkflow(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    projectId: string;
    includePolling?: boolean;
    includeImageEdits?: boolean;
    includeVersionHistory?: boolean;
    includeBoard?: boolean;
    includeExecutionPlan?: boolean;
    includeQcKinds?: boolean;
    includeBoardThumbnails?: boolean;
    /** #255: the handoff's language and display names, and preflight issue params. */
    includeTextLanguage?: boolean;
    /** #294/#295: the single-image transcription and copy diff, and why a QC output was refused. */
    includeQcEvidence?: boolean;
    /** #367: the frozen plan's review and style, as the project read returns the project's plan. */
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
  }): Promise<DomainResultFor<'get_marketing_workflow'>>;
  freezeMarketingRound(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    projectId: string;
    expectedRevision?: number;
    body: DomainOperationMap['freeze_marketing_round']['requestBody'];
  }): Promise<
    DomainResultFor<'freeze_marketing_round'> | ReconciliationRequired
  >;
  reuseMarketingCreative(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['reuse_marketing_creative']['requestBody'];
  }): Promise<
    DomainResultFor<'reuse_marketing_creative'> | ReconciliationRequired
  >;
  getMarketingEvidenceHandoff(params: {
    workspaceSlug: string;
    roundId: string;
  }): Promise<DomainResultFor<'get_marketing_evidence_handoff'>>;
  createMarketingEvidenceHandoff(params: {
    workspaceSlug: string;
    roundId: string;
    expectedRevision: number;
    actionInstanceId: string;
    body: DomainOperationMap['create_marketing_evidence_handoff']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_marketing_evidence_handoff'>
    | ReconciliationRequired
  >;
  createMarketingImageEdit(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['create_marketing_image_edit']['requestBody'];
  }): Promise<
    DomainResultFor<'create_marketing_image_edit'> | ReconciliationRequired
  >;
  prepareMarketingFeedback(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['prepare_marketing_feedback']['requestBody'];
  }): Promise<
    DomainResultFor<'prepare_marketing_feedback'> | ReconciliationRequired
  >;
  startMarketingExplanation(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['start_marketing_explanation']['requestBody'];
  }): Promise<
    DomainResultFor<'start_marketing_explanation'> | ReconciliationRequired
  >;
  reviewMarketingCreative(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['review_marketing_creative']['requestBody'];
  }): Promise<
    DomainResultFor<'review_marketing_creative'> | ReconciliationRequired
  >;
  createMarketingHandoff(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['create_marketing_handoff']['requestBody'];
  }): Promise<
    DomainResultFor<'create_marketing_handoff'> | ReconciliationRequired
  >;
  bindMarketingDelivery(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['bind_marketing_delivery']['requestBody'];
  }): Promise<
    DomainResultFor<'bind_marketing_delivery'> | ReconciliationRequired
  >;
  previewMarketingMetrics(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['preview_marketing_metrics']['requestBody'];
  }): Promise<
    DomainResultFor<'preview_marketing_metrics'> | ReconciliationRequired
  >;
  confirmMarketingMetrics(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['confirm_marketing_metrics']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_marketing_metrics'> | ReconciliationRequired
  >;
  evaluateMarketingRound(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
  }): Promise<
    DomainResultFor<'evaluate_marketing_round'> | ReconciliationRequired
  >;
  closeMarketingRound(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['close_marketing_round']['requestBody'];
  }): Promise<
    DomainResultFor<'close_marketing_round'> | ReconciliationRequired
  >;
  downloadMarketingHandoff(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
  }): Promise<
    DomainResultFor<'download_marketing_handoff'> | ReconciliationRequired
  >;

  retryMarketingHandoff(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    expectedRevision?: number;
    roundId: string;
  }): Promise<
    DomainResultFor<'retry_marketing_handoff'> | ReconciliationRequired
  >;
  getMarketingRound(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    roundId: string;
    includeImageEdits?: boolean;
    includeVersionHistory?: boolean;
    includeBoard?: boolean;
    includeExecutionPlan?: boolean;
    includeQcKinds?: boolean;
    includeBoardThumbnails?: boolean;
    /** #255: the handoff's language and display names, and preflight issue params. */
    includeTextLanguage?: boolean;
    /** #294/#295: the single-image transcription and copy diff, and why a QC output was refused. */
    includeQcEvidence?: boolean;
  }): Promise<DomainResultFor<'get_marketing_round'>>;
  getMarketingAIQCQuote(params: {
    workspaceSlug: string;
    roundId: string;
    kind?: 'single' | 'paired';
    /** The interface's strategy language; used only when the round's plan has none (J-43). */
    strategyLanguage?: 'zh-CN' | 'en-US';
  }): Promise<DomainResultFor<'get_marketing_ai_qc_quote'>>;
  startMarketingAIQC(params: {
    workspaceSlug: string;
    roundId: string;
    expectedRevision?: number;
    actionInstanceId: string;
    body: DomainOperationMap['start_marketing_ai_qc']['requestBody'];
  }): Promise<
    DomainResultFor<'start_marketing_ai_qc'> | ReconciliationRequired
  >;
  confirmMarketingAIQC(params: {
    workspaceSlug: string;
    roundId: string;
    expectedRevision?: number;
    actionInstanceId: string;
    body: DomainOperationMap['confirm_marketing_ai_qc']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_marketing_ai_qc'> | ReconciliationRequired
  >;
  getMarketingCapabilities(params: {
    workspaceSlug: string;
    includeReporting?: boolean;
  }): Promise<DomainResultFor<'get_marketing_capabilities'>>;
  listMarketingProjects(params: {
    workspaceSlug: string;
    limit?: number;
    cursor?: string;
    q?: string;
  }): Promise<DomainResultFor<'list_marketing_projects'>>;
  getMarketingProject(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    includeRecovery?: boolean;
    projectId: string;
  }): Promise<DomainResultFor<'get_marketing_project'>>;
  createMarketingProject(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    actionInstanceId: string;
    expectedRevision?: number;
    body: DomainOperationMap['create_marketing_project']['requestBody'];
  }): Promise<
    DomainResultFor<'create_marketing_project'> | ReconciliationRequired
  >;
  editMarketingProject(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    actionInstanceId: string;
    expectedRevision?: number;
    projectId: string;
    body: DomainOperationMap['edit_marketing_project']['requestBody'];
  }): Promise<
    DomainResultFor<'edit_marketing_project'> | ReconciliationRequired
  >;
  startMarketingPlanning(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    expectedRevision?: number;
    projectId: string;
    body: DomainOperationMap['start_marketing_planning']['requestBody'];
  }): Promise<
    DomainResultFor<'start_marketing_planning'> | ReconciliationRequired
  >;
  prepareMarketingGeneration(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    actionInstanceId: string;
    expectedRevision?: number;
    projectId: string;
    body: DomainOperationMap['prepare_marketing_generation']['requestBody'];
  }): Promise<
    DomainResultFor<'prepare_marketing_generation'> | ReconciliationRequired
  >;
  listMarketingBrandKits(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_marketing_brand_kits'>>;
  getProductBrandKit(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_product_brand_kit'>>;
  /** Owner-only; `ifMatch` is the current `revision` (`0` before the
   * first version). Each change appends an immutable version. */
  saveProductBrandKit(params: {
    workspaceSlug: string;
    productId: string;
    actionInstanceId: string;
    ifMatch: number;
    body: DomainOperationMap['save_product_brand_kit']['requestBody'];
  }): Promise<
    DomainResultFor<'save_product_brand_kit'> | ReconciliationRequired
  >;

  getGenerationCapabilities(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_generation_capabilities'>>;

  listGenerationDrafts(params: {
    workspaceSlug: string;
    productId?: string;
    state?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_drafts'>>;

  getGenerationDraft(params: {
    workspaceSlug: string;
    draftId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_generation_draft'>>;

  getGenerationPreview(params: {
    workspaceSlug: string;
    previewId: string;
  }): Promise<DomainResultFor<'get_generation_preview'>>;

  listGenerationRuns(params: {
    workspaceSlug: string;
    productId?: string;
    status?: string;
    executionMode?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_runs'>>;

  getGenerationRun(params: {
    workspaceSlug: string;
    runId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_generation_run'>>;

  listGenerationRunItems(params: {
    workspaceSlug: string;
    runId: string;
    state?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_run_items'>>;

  getGenerationRunItem(params: {
    workspaceSlug: string;
    runId: string;
    runItemId: string;
  }): Promise<DomainResultFor<'get_generation_run_item'>>;

  listGenerationAssets(params: {
    workspaceSlug: string;
    productId?: string;
    runId?: string;
    provider?: string;
    executionMode?: string;
    originKind?: string;
    reviewValue?: string;
    aspectRatio?: string;
    qcState?: string;
    tag?: string;
    experimentContext?: string;
    conclusion?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_assets'>>;

  getGenerationAssetVersion(params: {
    workspaceSlug: string;
    versionId: string;
  }): Promise<DomainResultFor<'get_generation_asset_version'>>;

  getGenerationExportPackage(params: {
    workspaceSlug: string;
    exportPackageId: string;
  }): Promise<DomainResultFor<'get_generation_export_package'>>;

  getGenerationExportManifest(params: {
    workspaceSlug: string;
    exportPackageId: string;
  }): Promise<DomainResultFor<'get_generation_export_manifest'>>;

  // ---- Generation commands ----

  createGenerationDraft(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_generation_draft']['requestBody'];
  }): Promise<
    DomainResultFor<'create_generation_draft'> | ReconciliationRequired
  >;

  updateGenerationDraft(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      draftId: string;
      body: DomainOperationMap['update_generation_draft']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'update_generation_draft'> | ReconciliationRequired
  >;

  createGenerationPreview(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      draftId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'create_generation_preview'> | ReconciliationRequired
  >;

  submitGenerationRun(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      body: DomainOperationMap['submit_generation_run']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<DomainResultFor<'submit_generation_run'> | ReconciliationRequired>;

  createGenerationRunRecoveryPreview(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['create_generation_run_recovery_preview']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'create_generation_run_recovery_preview'>
    | ReconciliationRequired
  >;

  cancelGenerationRun(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
    } & ConditionalCommandTransport
  ): Promise<DomainResultFor<'cancel_generation_run'> | ReconciliationRequired>;

  retryGenerationRunFailedItems(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['retry_generation_run_failed_items']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'retry_generation_run_failed_items'>
    | ReconciliationRequired
  >;

  resumeGenerationRunPausedItems(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['resume_generation_run_paused_items']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'resume_generation_run_paused_items'>
    | ReconciliationRequired
  >;

  reconcileGenerationRun(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['reconcile_generation_run']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'reconcile_generation_run'> | ReconciliationRequired
  >;

  createGenerationAssetReview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['create_generation_asset_review']['requestBody'];
  }): Promise<
    DomainResultFor<'create_generation_asset_review'> | ReconciliationRequired
  >;

  createGenerationRegenerationDraft(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['create_generation_regeneration_draft']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_generation_regeneration_draft'>
    | ReconciliationRequired
  >;

  createGenerationAssetVersionDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
  }): Promise<
    | DomainResultFor<'create_generation_asset_version_download_intent'>
    | ReconciliationRequired
  >;

  createGenerationExportPackage(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_generation_export_package']['requestBody'];
  }): Promise<
    DomainResultFor<'create_generation_export_package'> | ReconciliationRequired
  >;

  createGenerationExportDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    exportPackageId: string;
  }): Promise<
    | DomainResultFor<'create_generation_export_download_intent'>
    | ReconciliationRequired
  >;

  // ---- Localization reads and commands ----

  getLocalizationWorkspace(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_localization_workspace'>>;

  listLocalizationBatches(params: {
    workspaceSlug: string;
    phase?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_localization_batches'>>;

  getLocalizationBatch(params: {
    workspaceSlug: string;
    batchId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_localization_batch'>>;

  getLocalizationItem(params: {
    workspaceSlug: string;
    batchId: string;
    itemId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_localization_item'>>;

  getLocalizationBatchOutcome(params: {
    workspaceSlug: string;
    batchId: string;
  }): Promise<DomainResultFor<'get_localization_batch_outcome'>>;

  createLocalizationBatch(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_localization_batch']['requestBody'];
  }): Promise<
    DomainResultFor<'create_localization_batch'> | ReconciliationRequired
  >;

  startLocalizationPreparation(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    batchId: string;
    body: DomainOperationMap['start_localization_preparation']['requestBody'];
  }): Promise<
    DomainResultFor<'start_localization_preparation'> | ReconciliationRequired
  >;

  submitLocalizationClaimDecisions(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      body: DomainOperationMap['submit_localization_claim_decisions']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'submit_localization_claim_decisions'>
    | ReconciliationRequired
  >;

  approveLocalizationCopy(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      body: DomainOperationMap['approve_localization_copy']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'approve_localization_copy'> | ReconciliationRequired
  >;

  confirmLocalizationBatch(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      body: DomainOperationMap['confirm_localization_batch']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'confirm_localization_batch'> | ReconciliationRequired
  >;

  retryLocalizationItem(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      itemId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'retry_localization_item'> | ReconciliationRequired
  >;

  resumeLocalizationItem(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      itemId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'resume_localization_item'> | ReconciliationRequired
  >;

  /**
   * Admit only the frozen external QC steps a preserved isolated output
   * still needs. No `ifMatch`: this command admits work rather than
   * transitioning the Item, so there is no aggregate revision for the
   * caller to have raced.
   */
  authorizeLocalizationIsolatedQc(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    batchId: string;
    itemId: string;
    attemptId: string;
  }): Promise<
    | DomainResultFor<'authorize_localization_isolated_qc'>
    | ReconciliationRequired
  >;

  /** Recheck and publish an isolated output whose frozen QC plan passed. */
  adoptLocalizationIsolatedOutput(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    batchId: string;
    itemId: string;
    attemptId: string;
  }): Promise<
    | DomainResultFor<'adopt_localization_isolated_output'>
    | ReconciliationRequired
  >;

  cancelLocalizationBatch(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'cancel_localization_batch'> | ReconciliationRequired
  >;
  // ---- Scale reads (spec: docs/product/scale-advice-semantics-contract.md) ----

  /** The Meta modes this request's identity may select (spec §3.11) — no
   * `metaMode` param, since the whole point is discovering which modes are
   * permitted before choosing one. Root-level, not mounted under `{meta_mode}`. */
  listScaleAvailableModes(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_scale_available_modes'>>;

  listScaleEnrollments(params: {
    workspaceSlug: string;
    metaMode: string;
    lifecycle?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_scale_enrollments'>>;

  getScaleEnrollment(params: {
    workspaceSlug: string;
    enrollmentId: string;
    metaMode: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_scale_enrollment'>>;

  getScaleEpisode(params: {
    workspaceSlug: string;
    enrollmentId: string;
    episodeId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_scale_episode'>>;

  getScaleEnrollmentEvidence(params: {
    workspaceSlug: string;
    enrollmentId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_scale_enrollment_evidence'>>;

  /** Snapshot-pinned, paginated breakdown projection (spec §4.2, Issue #87
   * Task 4) — `snapshotId`/`breakdownType` are required query params (the
   * server has no default snapshot or breakdown type to fall back to). */
  getScaleEnrollmentBreakdowns(params: {
    workspaceSlug: string;
    enrollmentId: string;
    metaMode: string;
    snapshotId: string;
    breakdownType: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'get_scale_enrollment_breakdowns'>>;

  listScaleAdviceThreads(params: {
    workspaceSlug: string;
    metaMode: string;
    enrollmentId?: string;
    lifecycle?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_scale_advice_threads'>>;

  listScaleTargets(params: {
    workspaceSlug: string;
    metaMode: string;
    lifecycleState?: string;
    productVersionId?: string;
    offerVersionId?: string;
    marketCode?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_scale_targets'>>;

  getScaleTarget(params: {
    workspaceSlug: string;
    targetId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_scale_target'>>;

  /** Every distinct (ProductVersion, OfferVersion, market) context with a
   * currently active/paused Enrollment, its observed currency, and whether
   * an active Target already exists for it (spec §10, §21, Issue #87 Task
   * 2) — feeds the publish-flow context selector so the browser never
   * free-types a product/offer/market/currency combination the server has
   * no Episode evidence for. */
  listScaleTargetContexts(params: {
    workspaceSlug: string;
    metaMode: string;
  }): Promise<DomainResultFor<'list_scale_target_contexts'>>;

  getScaleCommandOutcome(params: {
    workspaceSlug: string;
    metaMode: string;
    identity: string;
  }): Promise<DomainResultFor<'get_scale_command_outcome'>>;

  listScaleEnrollmentCandidates(params: {
    workspaceSlug: string;
    metaMode: string;
  }): Promise<DomainResultFor<'list_scale_enrollment_candidates'>>;

  // ---- Scale commands ----

  createScaleEnrollment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_enrollment']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_enrollment'> | ReconciliationRequired
  >;

  /** Server-computed Winner-eligibility candidate (spec §4.4) — proves a
   * caller-named Winner Decision + DeliveryTarget pair is currently a valid
   * lineage and hands back the opaque, narrow-scoped candidate id every
   * other Scale write (`createScaleEnrollment`) actually consumes. */
  createScaleEnrollmentCandidate(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_enrollment_candidate']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_scale_enrollment_candidate'>
    | ReconciliationRequired
  >;

  /** Pre-activation evidence snapshot + lineage-guard preview (spec §4.5,
   * Issue #86 Task 10) — its `id` is the only field `activateScaleEnrollment`/
   * `resumeScaleEnrollment` accept, replacing the old raw `delivery_config`/
   * `episode_context` shape. */
  createScaleActivationPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    enrollmentId: string;
    body: DomainOperationMap['create_scale_activation_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_activation_preview'> | ReconciliationRequired
  >;

  activateScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['activate_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'activate_scale_enrollment'> | ReconciliationRequired
  >;

  pauseScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['pause_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'pause_scale_enrollment'> | ReconciliationRequired
  >;

  resumeScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['resume_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'resume_scale_enrollment'> | ReconciliationRequired
  >;

  closeScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['close_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'close_scale_enrollment'> | ReconciliationRequired
  >;

  createScaleAdviceDisposition(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    threadId: string;
    body: DomainOperationMap['create_scale_advice_disposition']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_advice_disposition'> | ReconciliationRequired
  >;

  createScaleAdviceSnooze(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    threadId: string;
    body: DomainOperationMap['create_scale_advice_snooze']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_advice_snooze'> | ReconciliationRequired
  >;

  convertScaleAdviceOccurrence(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    occurrenceId: string;
    body: DomainOperationMap['convert_scale_advice_occurrence']['requestBody'];
  }): Promise<
    DomainResultFor<'convert_scale_advice_occurrence'> | ReconciliationRequired
  >;

  /** Pre-publication impact preview (spec §10, §21, Issue #87 Task 2) — its
   * `id` is the only field `createScaleTarget` accepts, replacing the old
   * raw product/offer/market/currency create shape. Expires 15 minutes
   * after creation; consuming it (or letting it expire) requires a fresh
   * preview. */
  createScaleTargetPublicationPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_target_publication_preview']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_scale_target_publication_preview'>
    | ReconciliationRequired
  >;

  /** Confirms a publication preview — Owner only. `ifMatch` is threaded
   * exactly like `activateScaleEnrollment`'s. */
  createScaleTarget(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      body: DomainOperationMap['create_scale_target']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'create_scale_target'> | ReconciliationRequired>;

  /** Pre-withdrawal impact preview (spec §10, §21, Issue #87 Task 2) — its
   * `id` is the only field `withdrawScaleTarget` accepts. */
  createScaleTargetWithdrawalPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    targetId: string;
    body: DomainOperationMap['create_scale_target_withdrawal_preview']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_scale_target_withdrawal_preview'>
    | ReconciliationRequired
  >;

  /** Owner only, like `createScaleTarget`. Now `POST .../withdraw` with a
   * `{ withdrawal_preview_id }` body (Issue #87 Task 2), not the old
   * bodyless `DELETE`. */
  withdrawScaleTarget(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      targetId: string;
      metaMode: string;
      body: DomainOperationMap['withdraw_scale_target']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'withdraw_scale_target'> | ReconciliationRequired>;

  /** Creates a durable export Operation (spec §16, §22, Issue #87 Task 5) —
   * 202, not 201: the export itself completes asynchronously via the Scale
   * Worker, tracked through `pollOperation`/`useOperation` on the returned
   * `operation_id`. */
  createScaleExport(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_export']['requestBody'];
  }): Promise<DomainResultFor<'create_scale_export'> | ReconciliationRequired>;

  /** Creates an auditable download intent for a completed export (spec
   * §7.12, §16) — mirrors `createGenerationExportDownloadIntent`. */
  createScaleExportDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    exportId: string;
  }): Promise<
    | DomainResultFor<'create_scale_export_download_intent'>
    | ReconciliationRequired
  >;

  // ---- Owner credentials (Settings → Provider credentials; app/entitlements/api.py) ----

  /** Non-secret metadata of the Workspace's active credential versions —
   * Owner only. The secret itself never leaves the Domain: the API role
   * cannot even read the ciphertext columns. */
  listCredentials(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_credentials'>>;

  /** Stores a provider key (`plaintext_b64`) as version 1 of
   * `credential_key`. Owner only. */
  createCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_credential']['requestBody'];
  }): Promise<DomainResultFor<'create_credential'> | ReconciliationRequired>;

  /** Replaces the active version of `credential_key` with a new one (the
   * old version becomes `rotated`). Owner only. Existing bindings keep
   * pointing at the rotated version until `bindCredential` moves them. */
  rotateCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    credentialKey: string;
    body: DomainOperationMap['rotate_credential']['requestBody'];
  }): Promise<DomainResultFor<'rotate_credential'> | ReconciliationRequired>;

  /** Points the Workspace's `scope` binding for `body.provider` (e.g.
   * `generation` / `openai`, what `get_generation_capabilities` reports as
   * `credential_readiness`) at `credentialVersionId`. Owner only. */
  bindCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    credentialVersionId: string;
    scope: string;
    body: DomainOperationMap['bind_credential']['requestBody'];
  }): Promise<DomainResultFor<'bind_credential'> | ReconciliationRequired>;

  /** Revokes the active version behind `credentialVersionId`'s key.
   * Owner only. */
  revokeCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    credentialVersionId: string;
    body: DomainOperationMap['revoke_credential']['requestBody'];
  }): Promise<DomainResultFor<'revoke_credential'> | ReconciliationRequired>;

  // ---- Competitors (docs/product/competitor-scan-spec.md §4, §5) ----

  /** A product's competitors (or the Workspace's), newest first. */
  listCompetitors(params: {
    workspaceSlug: string;
    productId?: string;
    status?: 'active' | 'archived' | 'all';
  }): Promise<DomainResultFor<'list_competitors'>>;

  getCompetitor(params: {
    workspaceSlug: string;
    competitorId: string;
  }): Promise<DomainResultFor<'get_competitor'>>;

  /** Adds one §4.1 input; a link carrying a page id confirms it at once. */
  createCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_competitor']['requestBody'];
  }): Promise<DomainResultFor<'create_competitor'> | ReconciliationRequired>;

  /** The maximum a resolve can cost, and the modes it can run in. */
  quoteCompetitorResolve(params: {
    workspaceSlug: string;
    competitorId: string;
    executionMode?: 'byok' | 'mock';
    country?: string;
  }): Promise<DomainResultFor<'quote_competitor_resolve'>>;

  /** Reserves the quoted maximum and queues the paid `competitor.resolve` Job. */
  resolveCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    competitorId: string;
    body: DomainOperationMap['resolve_competitor']['requestBody'];
  }): Promise<DomainResultFor<'resolve_competitor'> | ReconciliationRequired>;

  getCompetitorResolution(params: {
    workspaceSlug: string;
    competitorId: string;
    resolveJobId: string;
  }): Promise<DomainResultFor<'get_competitor_resolution'>>;

  /** Appends a confirmation revision binding the competitor to one page. */
  confirmCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    competitorId: string;
    body: DomainOperationMap['confirm_competitor']['requestBody'];
  }): Promise<DomainResultFor<'confirm_competitor'> | ReconciliationRequired>;

  /** Owner only. */
  archiveCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    competitorId: string;
  }): Promise<DomainResultFor<'archive_competitor'> | ReconciliationRequired>;

  /** Both quote lines of a scan and the keys it needs; reserves nothing. */
  previewCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['preview_competitor_scan']['requestBody'];
  }): Promise<
    DomainResultFor<'preview_competitor_scan'> | ReconciliationRequired
  >;

  /** Reserves both lines under one Operation and queues the scan. */
  createCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_competitor_scan']['requestBody'];
  }): Promise<
    DomainResultFor<'create_competitor_scan'> | ReconciliationRequired
  >;

  listCompetitorScans(params: {
    workspaceSlug: string;
    productId?: string;
    status?: string;
  }): Promise<DomainResultFor<'list_competitor_scans'>>;

  getCompetitorScan(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'get_competitor_scan'>>;

  /** What resuming would reserve: the remaining pages and LLM steps only. */
  quoteCompetitorScanResume(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'quote_competitor_scan_resume'>>;

  resumeCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
  }): Promise<
    DomainResultFor<'resume_competitor_scan'> | ReconciliationRequired
  >;

  /** Owner only: whether the provider charged one pending page. */
  reconcileCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    body: DomainOperationMap['reconcile_competitor_scan']['requestBody'];
  }): Promise<
    DomainResultFor<'reconcile_competitor_scan'> | ReconciliationRequired
  >;

  listCompetitorScanAds(params: {
    workspaceSlug: string;
    scanId: string;
    competitorId?: string;
    role?: 'competitor' | 'self';
    originKind?: 'page' | 'keyword';
    minDaysRunning?: number;
    displayFormat?: string;
    relevance?: 'relevant' | 'adjacent' | 'unrelated' | 'unassessed';
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_competitor_scan_ads'>>;

  // ---- Competitor judgments and snapshots (spec §7.1, §9) ----

  /** Appends a member's relevance revision (flipping back is one more). */
  flipCompetitorAdRelevance(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    adId: string;
    body: DomainOperationMap['flip_competitor_ad_relevance']['requestBody'];
  }): Promise<
    DomainResultFor<'flip_competitor_ad_relevance'> | ReconciliationRequired
  >;

  listCompetitorAdAssessments(params: {
    workspaceSlug: string;
    scanId: string;
    adId: string;
  }): Promise<DomainResultFor<'list_competitor_ad_assessments'>>;

  /** Appends a member's tags revision against the scan's frozen vocabulary. */
  confirmCompetitorAdTags(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    adId: string;
    body: DomainOperationMap['confirm_competitor_ad_tags']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_competitor_ad_tags'> | ReconciliationRequired
  >;

  listCompetitorAdTags(params: {
    workspaceSlug: string;
    scanId: string;
    adId: string;
  }): Promise<DomainResultFor<'list_competitor_ad_tags'>>;

  /** Free and pure: freezes the revisions in effect now. */
  refreezeCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
  }): Promise<
    DomainResultFor<'refreeze_competitor_scan'> | ReconciliationRequired
  >;

  getCompetitorScanSummary(params: {
    workspaceSlug: string;
    scanId: string;
    snapshotId?: string;
  }): Promise<DomainResultFor<'get_competitor_scan_summary'>>;

  /** What a recluster can cost at most; reserves nothing. */
  quoteCompetitorScanRecluster(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'quote_competitor_scan_recluster'>>;

  /** Reserves `cluster:1` and queues the paid recluster Job. */
  reclusterCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
  }): Promise<
    DomainResultFor<'recluster_competitor_scan'> | ReconciliationRequired
  >;

  getProductCompetitorSettings(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_product_competitor_settings'>>;

  updateProductCompetitorSettings(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    productId: string;
    body: DomainOperationMap['update_product_competitor_settings']['requestBody'];
  }): Promise<
    | DomainResultFor<'update_product_competitor_settings'>
    | ReconciliationRequired
  >;

  // ---- Competitor discovery (spec §8) ----

  /** The advertisers a scan's keywords found and its discover step recommends (#276). */
  listCompetitorScanDiscoveries(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'list_competitor_scan_discoveries'>>;

  /** Accept (a competitor confirmed by the ads' page ID, no resolve) or reject one recommendation. */
  decideCompetitorDiscovery(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    advertiserPageId: string;
    body: DomainOperationMap['decide_competitor_discovery']['requestBody'];
  }): Promise<
    DomainResultFor<'decide_competitor_discovery'> | ReconciliationRequired
  >;
}

function readResponseMeta(response: Response): ResponseMeta {
  return {
    etag: response.headers.get('etag'),
    retryAt: response.headers.has('retry-after')
      ? Number(response.headers.get('retry-after'))
      : null,
    location: response.headers.get('location'),
  };
}

/** UUID when available, a short random fallback otherwise (spec §6.6 step 4). */
function newTraceId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `trace_${Math.random().toString(36).slice(2)}`;
}

/**
 * `fetch` is injectable so a caller (or a future test) can drive this
 * class without a real browser `fetch` global — production code never
 * passes it and gets the same-origin `window.fetch`.
 */
type FetchFn = typeof fetch;

export class DefaultDomainGateway implements DomainGateway {
  private readonly fetchFn: FetchFn;
  private readonly csrf: CsrfTokenManager;
  private readonly intents: IntentTracker;

  constructor(config: {
    csrfTokenManager: CsrfTokenManager;
    intentTracker: IntentTracker;
    fetchFn?: FetchFn;
  }) {
    // Browser fetch is receiver-sensitive. Storing it unbound and later
    // invoking it as `this.fetchFn(...)` makes Window.fetch observe the
    // gateway instance as `this` and throw "Illegal invocation".
    this.fetchFn = config.fetchFn ?? globalThis.fetch.bind(globalThis);
    this.csrf = config.csrfTokenManager;
    this.intents = config.intentTracker;
  }

  async getIdentityProbe(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_identity_probe'>> {
    const path = buildBffPath('get_identity_probe', {
      slug: params.workspaceSlug,
    });
    // GET is a safe method — the BFF gate skips CSRF verification for it
    // (spec §4.1), so no X-CSRF-Token is attached here.
    const response = await this.fetchFn(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-Trace-Id': newTraceId(),
      },
    });

    const meta = readResponseMeta(response);

    if (response.status === 200) {
      const data = (await response.json()) as IdentityProbeResponse;
      return { kind: 'ok', status: 200, data, meta };
    }

    const problem = (await response.json()) as ProblemDetails;
    return { kind: 'problem', status: response.status, problem, meta };
  }

  async pollOperation(params: {
    workspaceSlug: string;
    operationId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_operation'>> {
    const path = buildBffPath('get_operation', {
      slug: params.workspaceSlug,
      operation_id: params.operationId,
    });
    // GET is a safe method — no X-CSRF-Token (spec §4.1, §6.6 step 2).
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'X-Trace-Id': newTraceId(),
    };
    if (params.ifNoneMatch) {
      headers['If-None-Match'] = params.ifNoneMatch;
    }

    const response = await this.fetchFn(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers,
    });

    const meta = readResponseMeta(response);

    if (response.status === 200) {
      const data = (await response.json()) as OperationDetailResponse;
      return { kind: 'ok', status: 200, data, meta };
    }

    // 304 carries no body (spec §5.6) — the caller keeps its cached copy
    // and relies on `meta.etag`/`meta.retryAt` for the next poll.
    if (response.status === 304) {
      return { kind: 'not_modified', status: 304, data: null, meta };
    }

    const problem = (await response.json()) as ProblemDetails;
    return { kind: 'problem', status: response.status, problem, meta };
  }

  async createUploadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: CreateUploadIntentRequest;
  }): Promise<
    DomainResultFor<'create_upload_intent'> | ReconciliationRequired
  > {
    // Coalesces a concurrent double-click for the same actionInstanceId
    // into a single in-flight promise (spec §6.10) — acquire() itself only
    // runs once per coalesced batch, so a second caller never mints (or
    // reads) a second X-Intent-Id for the same click.
    return this.intents.deduplicateInflight(
      params.actionInstanceId,
      async () => {
        const acquired = this.intents.acquire(
          params.actionInstanceId,
          'create_upload_intent'
        );
        if ('kind' in acquired) {
          return acquired;
        }

        const path = buildBffPath('create_upload_intent', {
          slug: params.workspaceSlug,
        });
        const { response, consumedProblem } = await this.sendMutating({
          path,
          intent: acquired,
          body: JSON.stringify(params.body),
        });

        return this.finishMutating<
          'create_upload_intent',
          UploadIntentResponse
        >(acquired, response, consumedProblem, 201, 'created');
      }
    );
  }

  async finalizeUpload(params: {
    workspaceSlug: string;
    uploadIntentId: string;
    actionInstanceId: string;
  }): Promise<DomainResultFor<'finalize_upload'> | ReconciliationRequired> {
    return this.intents.deduplicateInflight(
      params.actionInstanceId,
      async () => {
        const acquired = this.intents.acquire(
          params.actionInstanceId,
          'finalize_upload'
        );
        if ('kind' in acquired) {
          return acquired;
        }

        const path = buildBffPath('finalize_upload', {
          slug: params.workspaceSlug,
          intent_id: params.uploadIntentId,
        });
        // finalize_upload carries no request body (spec §7.8) — Content-Type
        // is omitted along with it.
        const { response, consumedProblem } = await this.sendMutating({
          path,
          intent: acquired,
        });

        return this.finishMutating<'finalize_upload', FinalizeUploadResponse>(
          acquired,
          response,
          consumedProblem,
          202,
          'accepted'
        );
      }
    );
  }

  async createDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: CreateDownloadIntentRequest;
  }): Promise<
    DomainResultFor<'create_download_intent'> | ReconciliationRequired
  > {
    return this.intents.deduplicateInflight(
      params.actionInstanceId,
      async () => {
        const acquired = this.intents.acquire(
          params.actionInstanceId,
          'create_download_intent'
        );
        if ('kind' in acquired) {
          return acquired;
        }

        const path = buildBffPath('create_download_intent', {
          slug: params.workspaceSlug,
        });
        const { response, consumedProblem } = await this.sendMutating({
          path,
          intent: acquired,
          body: JSON.stringify(params.body),
        });

        return this.finishMutating<
          'create_download_intent',
          DownloadIntentResponse
        >(acquired, response, consumedProblem, 201, 'created');
      }
    );
  }

  /**
   * POSTs a non-safe, idempotent request: attaches `X-CSRF-Token` (spec
   * §4.1) and `X-Intent-Id` (spec §6.10), and refreshes the CSRF token once
   * on a `csrf_validation_failed` 403 before surfacing a second failure
   * (spec §6.9). `consumedProblem` carries the parsed body when this helper
   * already read it while checking for that retry, so the caller never
   * double-reads the `Response` stream.
   *
   * A thrown `fetchFn` error (network failure, no `Response` at all) is an
   * *ambiguous* outcome — the request may or may not have reached the
   * server — so the intent is `retain()`-ed before rethrowing rather than
   * left `"inflight"`. That way a same-page retry past `safeRetryUntil`
   * surfaces `ReconciliationRequired` instead of resending indefinitely
   * (spec §6.10).
   */
  private async sendMutating(params: {
    path: string;
    /** Defaults to `POST` — `save_library_draft` (PUT),
     * `update_generation_draft` (PATCH), `activate_scale_enrollment`/
     * `pause_scale_enrollment`/`resume_scale_enrollment`/
     * `close_scale_enrollment` (PUT), and `withdraw_scale_target` (DELETE)
     * are the non-safe operations that use a different verb (spec §6.3,
     * `BFF_ROUTES` in `generated/paths.ts`). */
    method?: 'POST' | 'PUT' | 'PATCH' | 'DELETE';
    intent: { intentId: string; actionInstanceId: string };
    body?: string;
    ifMatch?: string | number;
  }): Promise<{ response: Response; consumedProblem: ProblemDetails | null }> {
    const buildHeaders = (csrfToken: string): Record<string, string> => {
      const headers: Record<string, string> = {
        Accept: 'application/json',
        'X-Trace-Id': newTraceId(),
        'X-CSRF-Token': csrfToken,
        'X-Intent-Id': params.intent.intentId,
      };
      if (params.body !== undefined) {
        headers['Content-Type'] = 'application/json';
      }
      if (params.ifMatch !== undefined) {
        headers['If-Match'] = String(params.ifMatch);
      }
      return headers;
    };

    const doFetch = async (): Promise<Response> => {
      let csrfToken: string;
      try {
        csrfToken = await this.csrf.getToken();
      } catch (error) {
        // No command was dispatched. Do not turn a rejected preflight into
        // an ambiguous mutation that eventually requires reconciliation.
        this.intents.resolve(params.intent);
        throw error;
      }
      try {
        return await this.fetchFn(params.path, {
          method: params.method ?? 'POST',
          credentials: 'same-origin',
          headers: buildHeaders(csrfToken),
          body: params.body,
        });
      } catch (error) {
        this.intents.retain(params.intent);
        throw error;
      }
    };

    let response = await doFetch();

    if (response.status === 403) {
      const problem = (await response.json()) as ProblemDetails;
      if (problem.code === 'csrf_validation_failed') {
        this.csrf.clear();
        response = await doFetch();
        return { response, consumedProblem: null };
      }
      return { response, consumedProblem: problem };
    }

    return { response, consumedProblem: null };
  }

  /**
   * Shared response handling for every mutating method (spec §6.10).
   * Resolves the `IntentTracker` entry on any definitive server answer
   * (success or problem) so a resubmission acquires a fresh intent rather
   * than idempotently replaying a stale one; a thrown network error (no
   * `Response` at all) never reaches here — `sendMutating` already
   * `retain()`-ed the intent and rethrew before this is called.
   *
   * `TData` defaults to `unknown` for the generic `runCommand()` callers
   * below — the parsed body is re-cast to `DomainResultFor<K>` regardless,
   * so a precise `TData` only matters for the three original call sites
   * that still specify it explicitly.
   */
  private async finishMutating<
    K extends keyof DomainOperationMap,
    TData = unknown,
  >(
    intent: { intentId: string; actionInstanceId: string },
    response: Response,
    consumedProblem: ProblemDetails | null,
    successStatus: number,
    successKind: 'ok' | 'created' | 'accepted'
  ): Promise<DomainResultFor<K>> {
    const meta = readResponseMeta(response);

    if (consumedProblem !== null) {
      this.intents.resolve(intent);
      return {
        kind: 'problem',
        status: response.status,
        problem: consumedProblem,
        meta,
      } as DomainResultFor<K>;
    }

    if (response.status === successStatus) {
      const data = (await response.json()) as TData;
      // `create_upload_intent`/`create_download_intent` echo the intent's
      // own `expires_at` (spec §7.11) — a tighter bound than the browser's
      // default idempotency-retention window, since a replay past this
      // point returns 410 `*_intent_expired` regardless of
      // `safeRetryUntil`. `finalize_upload`'s response carries no
      // `expires_at`, so this is a no-op for it. Narrowing runs before
      // `resolve()` deletes the record on this terminal-success path today;
      // it stays correct if a future change keeps a resolved-but-replayable
      // intent around instead of deleting it outright.
      const expiresAt = (data as { expires_at?: unknown }).expires_at;
      if (typeof expiresAt === 'string') {
        const expiresAtMs = Date.parse(expiresAt);
        if (!Number.isNaN(expiresAtMs)) {
          this.intents.refineTimeBounds(intent.actionInstanceId, {
            safeRetryUntil: expiresAtMs,
            localRetentionUntil: expiresAtMs,
          });
        }
      }
      this.intents.resolve(intent);
      return {
        kind: successKind,
        status: successStatus,
        data,
        meta,
      } as unknown as DomainResultFor<K>;
    }

    const problem = (await response.json()) as ProblemDetails;
    this.intents.resolve(intent);
    return {
      kind: 'problem',
      status: response.status,
      problem,
      meta,
    } as DomainResultFor<K>;
  }

  /**
   * Shared `GET` handling for every read operation added in this file
   * (spec §6.6 step 2) — every one of them declares a single `200` success
   * response (`operation-map.ts`), unlike `get_operation`'s `200`/`304`
   * pair, so a single status check covers them all. GET is a safe method —
   * no `X-CSRF-Token`/`X-Intent-Id`.
   */
  private async sendSafe<K extends keyof DomainOperationMap>(
    path: string,
    ifNoneMatch?: string
  ): Promise<DomainResultFor<K>> {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'X-Trace-Id': newTraceId(),
    };
    if (ifNoneMatch !== undefined) {
      headers['If-None-Match'] = ifNoneMatch;
    }

    const response = await this.fetchFn(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers,
    });

    const meta = readResponseMeta(response);

    if (response.status === 304) {
      return {
        kind: 'not_modified',
        status: 304,
        data: null,
        meta,
      } as DomainResultFor<K>;
    }

    if (response.status === 200) {
      const data = await response.json();
      return { kind: 'ok', status: 200, data, meta } as DomainResultFor<K>;
    }

    const problem = (await response.json()) as ProblemDetails;
    return { kind: 'problem', status: response.status, problem, meta };
  }

  /**
   * Shared idempotent-command handling for every mutating operation added
   * in this file — mirrors `createUploadIntent`/`finalizeUpload`/
   * `createDownloadIntent` above (`acquire()` → `sendMutating()` →
   * `finishMutating()`, coalesced through `deduplicateInflight()`) without
   * repeating that plumbing 26 times. `body` is `undefined` for the
   * operations whose `DomainOperationMap[K]['requestBody']` is `never`
   * (e.g. `archive_library_entry`, `cancel_generation_run`).
   */
  private async runCommand<K extends keyof DomainOperationMap>(
    operationId: K,
    actionInstanceId: string,
    path: string,
    method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
    body: unknown,
    successStatus: number,
    successKind: 'ok' | 'created' | 'accepted',
    transport?: CommandTransport
  ): Promise<DomainResultFor<K> | ReconciliationRequired> {
    return this.intents.deduplicateInflight(actionInstanceId, async () => {
      const acquired = this.intents.acquire(actionInstanceId, operationId);
      if ('kind' in acquired) {
        return acquired;
      }

      const { response, consumedProblem } = await this.sendMutating({
        path,
        method,
        intent: acquired,
        body: body === undefined ? undefined : JSON.stringify(body),
        ifMatch: transport?.ifMatch,
      });

      return this.finishMutating<K>(
        acquired,
        response,
        consumedProblem,
        successStatus,
        successKind
      );
    });
  }

  /** Appends only the defined entries of `params` as a `?`-prefixed query string — omitted (`undefined`) filters are never sent, matching the optional-query-param contract in `operation-map.ts`. */
  private buildQuery(
    params: Record<string, string | number | boolean | undefined>
  ): string {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) {
        qs.set(key, String(value));
      }
    }
    const serialized = qs.toString();
    return serialized ? `?${serialized}` : '';
  }

  // ==== Library reads ====

  async listLibraryEntryTypes(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_library_entry_types'>> {
    const path = buildBffPath('list_library_entry_types', {
      slug: params.workspaceSlug,
    });
    return this.sendSafe<'list_library_entry_types'>(path);
  }

  async listLibraryEntries(params: {
    workspaceSlug: string;
    entryType?: string;
    includeArchived?: boolean;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_entries'>> {
    const path =
      buildBffPath('list_library_entries', { slug: params.workspaceSlug }) +
      this.buildQuery({
        entry_type: params.entryType,
        include_archived: params.includeArchived,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_library_entries'>(path);
  }

  async getLibraryEntry(params: {
    workspaceSlug: string;
    entryId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_library_entry'>> {
    const path = buildBffPath('get_library_entry', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.sendSafe<'get_library_entry'>(path, params.ifNoneMatch);
  }

  async listLibraryEntryVersions(params: {
    workspaceSlug: string;
    entryId: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_entry_versions'>> {
    const path =
      buildBffPath('list_library_entry_versions', {
        slug: params.workspaceSlug,
        entry_id: params.entryId,
      }) + this.buildQuery({ cursor: params.cursor, limit: params.limit });
    return this.sendSafe<'list_library_entry_versions'>(path);
  }

  async listLibraryEntryEvents(params: {
    workspaceSlug: string;
    entryId: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_entry_events'>> {
    const path =
      buildBffPath('list_library_entry_events', {
        slug: params.workspaceSlug,
        entry_id: params.entryId,
      }) + this.buildQuery({ cursor: params.cursor, limit: params.limit });
    return this.sendSafe<'list_library_entry_events'>(path);
  }

  async getLibraryVersion(params: {
    workspaceSlug: string;
    versionId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_library_version'>> {
    const path = buildBffPath('get_library_version', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.sendSafe<'get_library_version'>(path, params.ifNoneMatch);
  }

  async getLibraryVersionImpact(params: {
    workspaceSlug: string;
    versionId: string;
  }): Promise<DomainResultFor<'get_library_version_impact'>> {
    const path = buildBffPath('get_library_version_impact', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.sendSafe<'get_library_version_impact'>(path);
  }

  async listLibraryVersionBindings(params: {
    workspaceSlug: string;
    versionId: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_version_bindings'>> {
    const path =
      buildBffPath('list_library_version_bindings', {
        slug: params.workspaceSlug,
        version_id: params.versionId,
      }) + this.buildQuery({ cursor: params.cursor, limit: params.limit });
    return this.sendSafe<'list_library_version_bindings'>(path);
  }

  // ==== Library commands ====

  async createLibraryEntry(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_library_entry']['requestBody'];
  }): Promise<
    DomainResultFor<'create_library_entry'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_library_entry', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_library_entry',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async saveLibraryDraft(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['save_library_draft']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'save_library_draft'> | ReconciliationRequired> {
    const path = buildBffPath('save_library_draft', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'save_library_draft',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok',
      params
    );
  }

  async validateLibraryDraft(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
    } & CommandTransport
  ): Promise<
    DomainResultFor<'validate_library_draft'> | ReconciliationRequired
  > {
    const path = buildBffPath('validate_library_draft', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'validate_library_draft',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      200,
      'ok',
      params
    );
  }

  async publishLibraryEntry(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['publish_library_entry']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'publish_library_entry'> | ReconciliationRequired
  > {
    const path = buildBffPath('publish_library_entry', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'publish_library_entry',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created',
      params
    );
  }

  async archiveLibraryEntry(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['archive_library_entry']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'archive_library_entry'> | ReconciliationRequired
  > {
    const path = buildBffPath('archive_library_entry', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'archive_library_entry',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      params
    );
  }

  async bindLibraryReference(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      entryId: string;
      body: DomainOperationMap['bind_library_reference']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'bind_library_reference'> | ReconciliationRequired
  > {
    const path = buildBffPath('bind_library_reference', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'bind_library_reference',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created',
      params
    );
  }

  async withdrawLibraryVersion(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      versionId: string;
      body: DomainOperationMap['withdraw_library_version']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'withdraw_library_version'> | ReconciliationRequired
  > {
    const path = buildBffPath('withdraw_library_version', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'withdraw_library_version',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      params
    );
  }

  async forkLibraryVersion(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['fork_library_version']['requestBody'];
  }): Promise<
    DomainResultFor<'fork_library_version'> | ReconciliationRequired
  > {
    const path = buildBffPath('fork_library_version', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'fork_library_version',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async cloneLibraryProduct(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['clone_library_product']['requestBody'];
  }): Promise<
    DomainResultFor<'clone_library_product'> | ReconciliationRequired
  > {
    const path = buildBffPath('clone_library_product', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'clone_library_product',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async applyLibraryTemplate(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    templateVersionId: string;
    body: DomainOperationMap['apply_library_template']['requestBody'];
  }): Promise<
    DomainResultFor<'apply_library_template'> | ReconciliationRequired
  > {
    const path = buildBffPath('apply_library_template', {
      slug: params.workspaceSlug,
      template_version_id: params.templateVersionId,
    });
    return this.runCommand(
      'apply_library_template',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createLibraryReferenceObject(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_library_reference_object']['requestBody'];
  }): Promise<
    DomainResultFor<'create_library_reference_object'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_library_reference_object', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_library_reference_object',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async createLibraryReferenceDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
  }): Promise<
    | DomainResultFor<'create_library_reference_download_intent'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_library_reference_download_intent', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'create_library_reference_download_intent',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  async getLibraryReferenceObject(params: {
    workspaceSlug: string;
    objectId: string;
  }): Promise<DomainResultFor<'get_library_reference_object'>> {
    const path = buildBffPath('get_library_reference_object', {
      slug: params.workspaceSlug,
      object_id: params.objectId,
    });
    return this.sendSafe<'get_library_reference_object'>(path);
  }

  async promoteLibraryReferenceObject(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    objectId: string;
    body: DomainOperationMap['promote_library_reference_object']['requestBody'];
  }): Promise<
    DomainResultFor<'promote_library_reference_object'> | ReconciliationRequired
  > {
    const path = buildBffPath('promote_library_reference_object', {
      slug: params.workspaceSlug,
      object_id: params.objectId,
    });
    return this.runCommand(
      'promote_library_reference_object',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  // ==== Products (Issue #162) ====

  async listProducts(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_products'>> {
    const path = buildBffPath('list_products', {
      slug: params.workspaceSlug,
    });
    return this.sendSafe<'list_products'>(path);
  }

  async getProductReferences(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_product_references'>> {
    return this.sendSafe<'get_product_references'>(
      buildBffPath('get_product_references', {
        slug: params.workspaceSlug,
        product_id: params.productId,
      })
    );
  }

  async addProductReference(params: {
    workspaceSlug: string;
    productId: string;
    actionInstanceId: string;
    ifMatch: number;
    body: DomainOperationMap['add_product_reference']['requestBody'];
  }): Promise<
    DomainResultFor<'add_product_reference'> | ReconciliationRequired
  > {
    return this.runCommand(
      'add_product_reference',
      params.actionInstanceId,
      buildBffPath('add_product_reference', {
        slug: params.workspaceSlug,
        product_id: params.productId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async updateProductReference(params: {
    workspaceSlug: string;
    productId: string;
    itemId: string;
    actionInstanceId: string;
    ifMatch: number;
    body: DomainOperationMap['update_product_reference']['requestBody'];
  }): Promise<
    DomainResultFor<'update_product_reference'> | ReconciliationRequired
  > {
    return this.runCommand(
      'update_product_reference',
      params.actionInstanceId,
      buildBffPath('update_product_reference', {
        slug: params.workspaceSlug,
        product_id: params.productId,
        item_id: params.itemId,
      }),
      'PATCH',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async deleteProductReference(params: {
    workspaceSlug: string;
    productId: string;
    itemId: string;
    actionInstanceId: string;
    ifMatch: number;
  }): Promise<
    DomainResultFor<'delete_product_reference'> | ReconciliationRequired
  > {
    return this.runCommand(
      'delete_product_reference',
      params.actionInstanceId,
      buildBffPath('delete_product_reference', {
        slug: params.workspaceSlug,
        product_id: params.productId,
        item_id: params.itemId,
      }),
      'DELETE',
      undefined,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  // ==== Product intake (Issue #161) ====

  async createProductIntake(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_product_intake']['requestBody'];
  }): Promise<
    DomainResultFor<'create_product_intake'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_product_intake', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_product_intake',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async getProductIntake(params: {
    workspaceSlug: string;
    intakeId: string;
  }): Promise<DomainResultFor<'get_product_intake'>> {
    const path = buildBffPath('get_product_intake', {
      slug: params.workspaceSlug,
      intake_id: params.intakeId,
    });
    return this.sendSafe<'get_product_intake'>(path);
  }

  async confirmProductIntake(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    intakeId: string;
    body: DomainOperationMap['confirm_product_intake']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_product_intake'> | ReconciliationRequired
  > {
    const path = buildBffPath('confirm_product_intake', {
      slug: params.workspaceSlug,
      intake_id: params.intakeId,
    });
    return this.runCommand(
      'confirm_product_intake',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async createProductIntakeImageDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    intakeId: string;
    imageId: string;
  }): Promise<
    | DomainResultFor<'create_product_intake_image_download_intent'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_product_intake_image_download_intent', {
      slug: params.workspaceSlug,
      intake_id: params.intakeId,
      image_id: params.imageId,
    });
    return this.runCommand(
      'create_product_intake_image_download_intent',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  // ==== Test plans and generation defaults (Issue #163) ====

  async createTestPlan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_test_plan']['requestBody'];
  }): Promise<DomainResultFor<'create_test_plan'> | ReconciliationRequired> {
    const path = buildBffPath('create_test_plan', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_test_plan',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async listTestPlans(params: {
    workspaceSlug: string;
    productId?: string;
    runId?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_test_plans'>> {
    const path =
      buildBffPath('list_test_plans', { slug: params.workspaceSlug }) +
      this.buildQuery({
        product_id: params.productId,
        run_id: params.runId,
        limit: params.limit,
      });
    return this.sendSafe<'list_test_plans'>(path);
  }

  async getTestPlan(params: {
    workspaceSlug: string;
    planId: string;
  }): Promise<DomainResultFor<'get_test_plan'>> {
    const path = buildBffPath('get_test_plan', {
      slug: params.workspaceSlug,
      plan_id: params.planId,
    });
    return this.sendSafe<'get_test_plan'>(path);
  }

  async reviseTestPlan(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      planId: string;
      body: DomainOperationMap['revise_test_plan']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<DomainResultFor<'revise_test_plan'> | ReconciliationRequired> {
    const path = buildBffPath('revise_test_plan', {
      slug: params.workspaceSlug,
      plan_id: params.planId,
    });
    return this.runCommand(
      'revise_test_plan',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async createTestPlanRun(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    planId: string;
    body: DomainOperationMap['create_test_plan_run']['requestBody'];
  }): Promise<
    DomainResultFor<'create_test_plan_run'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_test_plan_run', {
      slug: params.workspaceSlug,
      plan_id: params.planId,
    });
    return this.runCommand(
      'create_test_plan_run',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async getGenerationDefaults(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_generation_defaults'>> {
    const path = buildBffPath('get_generation_defaults', {
      slug: params.workspaceSlug,
    });
    return this.sendSafe<'get_generation_defaults'>(path);
  }

  async updateGenerationDefaults(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      body: DomainOperationMap['update_generation_defaults']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'update_generation_defaults'> | ReconciliationRequired
  > {
    const path = buildBffPath('update_generation_defaults', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'update_generation_defaults',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  // ==== Results imports and learnings (Issue #166) ====

  async createResultsImport(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_results_import']['requestBody'];
  }): Promise<
    DomainResultFor<'create_results_import'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_results_import', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_results_import',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async getResultsImport(params: {
    workspaceSlug: string;
    importId: string;
  }): Promise<DomainResultFor<'get_results_import'>> {
    const path = buildBffPath('get_results_import', {
      slug: params.workspaceSlug,
      import_id: params.importId,
    });
    return this.sendSafe<'get_results_import'>(path);
  }

  async commitResultsImport(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    importId: string;
  }): Promise<
    DomainResultFor<'commit_results_import'> | ReconciliationRequired
  > {
    const path = buildBffPath('commit_results_import', {
      slug: params.workspaceSlug,
      import_id: params.importId,
    });
    return this.runCommand(
      'commit_results_import',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      200,
      'ok'
    );
  }

  async getProductLearnings(params: {
    workspaceSlug: string;
    productId: string;
    evidenceMode?: string;
  }): Promise<DomainResultFor<'get_product_learnings'>> {
    const path =
      buildBffPath('get_product_learnings', {
        slug: params.workspaceSlug,
        product_id: params.productId,
      }) + this.buildQuery({ evidence_mode: params.evidenceMode });
    return this.sendSafe<'get_product_learnings'>(path);
  }

  async getResultsTemplate(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_results_template'>> {
    const path = buildBffPath('get_results_template', {
      slug: params.workspaceSlug,
      product_id: params.productId,
    });
    return this.sendSafe<'get_results_template'>(path);
  }

  async getTestPlanResults(params: {
    workspaceSlug: string;
    planId: string;
    evidenceMode?: string;
  }): Promise<DomainResultFor<'get_test_plan_results'>> {
    const path =
      buildBffPath('get_test_plan_results', {
        slug: params.workspaceSlug,
        plan_id: params.planId,
      }) + this.buildQuery({ evidence_mode: params.evidenceMode });
    return this.sendSafe<'get_test_plan_results'>(path);
  }

  // ==== External creatives (Issue #168) ====

  async createExternalCreatives(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_external_creatives']['requestBody'];
  }): Promise<
    DomainResultFor<'create_external_creatives'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_external_creatives', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_external_creatives',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async listExternalCreatives(params: {
    workspaceSlug: string;
    productId?: string;
    status?: string;
  }): Promise<DomainResultFor<'list_external_creatives'>> {
    const path =
      buildBffPath('list_external_creatives', { slug: params.workspaceSlug }) +
      this.buildQuery({ product_id: params.productId, status: params.status });
    return this.sendSafe<'list_external_creatives'>(path);
  }

  async getExternalCreative(params: {
    workspaceSlug: string;
    externalCreativeId: string;
  }): Promise<DomainResultFor<'get_external_creative'>> {
    const path = buildBffPath('get_external_creative', {
      slug: params.workspaceSlug,
      external_creative_id: params.externalCreativeId,
    });
    return this.sendSafe<'get_external_creative'>(path);
  }

  async confirmExternalCreative(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    externalCreativeId: string;
    body: DomainOperationMap['confirm_external_creative']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_external_creative'> | ReconciliationRequired
  > {
    const path = buildBffPath('confirm_external_creative', {
      slug: params.workspaceSlug,
      external_creative_id: params.externalCreativeId,
    });
    return this.runCommand(
      'confirm_external_creative',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  // ==== Library internal resources, command outcomes, impacts, data sources ====

  async getLibraryCommandOutcome(params: {
    workspaceSlug: string;
    commandIdentity: string;
    commandKind?: string;
  }): Promise<DomainResultFor<'get_library_command_outcome'>> {
    const path =
      buildBffPath('get_library_command_outcome', {
        slug: params.workspaceSlug,
        command_identity: params.commandIdentity,
      }) + this.buildQuery({ command_kind: params.commandKind });
    return this.sendSafe<'get_library_command_outcome'>(path);
  }

  async reconcileLibraryCommandOutcome(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    commandIdentity: string;
    commandKind?: string;
  }): Promise<
    | DomainResultFor<'reconcile_library_command_outcome'>
    | ReconciliationRequired
  > {
    const path =
      buildBffPath('reconcile_library_command_outcome', {
        slug: params.workspaceSlug,
        command_identity: params.commandIdentity,
      }) + this.buildQuery({ command_kind: params.commandKind });
    return this.runCommand(
      'reconcile_library_command_outcome',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      200,
      'ok'
    );
  }

  async listLibraryInternalEntries(params: {
    workspaceSlug: string;
    rootEntryId: string;
    entryType?: string;
    parentId?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_library_internal_entries'>> {
    const path =
      buildBffPath('list_library_internal_entries', {
        slug: params.workspaceSlug,
        root_entry_id: params.rootEntryId,
      }) +
      this.buildQuery({
        entry_type: params.entryType,
        parent_id: params.parentId,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_library_internal_entries'>(path);
  }

  async getLibraryInternalEntry(params: {
    workspaceSlug: string;
    entryType: string;
    entryId: string;
  }): Promise<DomainResultFor<'get_library_internal_entry'>> {
    const path = buildBffPath('get_library_internal_entry', {
      slug: params.workspaceSlug,
      entry_type: params.entryType,
      entry_id: params.entryId,
    });
    return this.sendSafe<'get_library_internal_entry'>(path);
  }

  async getLibraryInternalEntryContext(params: {
    workspaceSlug: string;
    rootEntryId: string;
    entryType: string;
    entryId: string;
  }): Promise<DomainResultFor<'get_library_internal_entry_context'>> {
    const path = buildBffPath('get_library_internal_entry_context', {
      slug: params.workspaceSlug,
      root_entry_id: params.rootEntryId,
      entry_type: params.entryType,
      entry_id: params.entryId,
    });
    return this.sendSafe<'get_library_internal_entry_context'>(path);
  }

  async createLibraryInternalEntry(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    rootEntryId: string;
    body: DomainOperationMap['create_library_internal_entry']['requestBody'];
  }): Promise<
    DomainResultFor<'create_library_internal_entry'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_library_internal_entry', {
      slug: params.workspaceSlug,
      root_entry_id: params.rootEntryId,
    });
    return this.runCommand(
      'create_library_internal_entry',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async previewLibraryPublishImpact(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    entryId: string;
  }): Promise<
    DomainResultFor<'preview_library_publish_impact'> | ReconciliationRequired
  > {
    const path = buildBffPath('preview_library_publish_impact', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'preview_library_publish_impact',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  async previewLibraryArchiveImpact(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    entryId: string;
  }): Promise<
    DomainResultFor<'preview_library_archive_impact'> | ReconciliationRequired
  > {
    const path = buildBffPath('preview_library_archive_impact', {
      slug: params.workspaceSlug,
      entry_id: params.entryId,
    });
    return this.runCommand(
      'preview_library_archive_impact',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  async previewLibraryWithdrawImpact(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
  }): Promise<
    DomainResultFor<'preview_library_withdraw_impact'> | ReconciliationRequired
  > {
    const path = buildBffPath('preview_library_withdraw_impact', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'preview_library_withdraw_impact',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  async previewLibraryTemplateReapply(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    templateVersionId: string;
    body: DomainOperationMap['preview_library_template_reapply']['requestBody'];
  }): Promise<
    DomainResultFor<'preview_library_template_reapply'> | ReconciliationRequired
  > {
    const path = buildBffPath('preview_library_template_reapply', {
      slug: params.workspaceSlug,
      template_version_id: params.templateVersionId,
    });
    return this.runCommand(
      'preview_library_template_reapply',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async getLibraryDataSourceCredentialBinding(params: {
    workspaceSlug: string;
    dataSourceEntryId: string;
  }): Promise<DomainResultFor<'get_library_data_source_credential_binding'>> {
    const path = buildBffPath('get_library_data_source_credential_binding', {
      slug: params.workspaceSlug,
      data_source_entry_id: params.dataSourceEntryId,
    });
    return this.sendSafe<'get_library_data_source_credential_binding'>(path);
  }

  async bindLibraryDataSourceCredential(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      dataSourceEntryId: string;
      body: DomainOperationMap['bind_library_data_source_credential']['requestBody'];
    } & CommandTransport
  ): Promise<
    | DomainResultFor<'bind_library_data_source_credential'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('bind_library_data_source_credential', {
      slug: params.workspaceSlug,
      data_source_entry_id: params.dataSourceEntryId,
    });
    return this.runCommand(
      'bind_library_data_source_credential',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created',
      params
    );
  }

  async checkLibraryDataSourceConnection(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    dataSourceEntryId: string;
    body: DomainOperationMap['check_library_data_source_connection']['requestBody'];
  }): Promise<
    | DomainResultFor<'check_library_data_source_connection'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('check_library_data_source_connection', {
      slug: params.workspaceSlug,
      data_source_entry_id: params.dataSourceEntryId,
    });
    return this.runCommand(
      'check_library_data_source_connection',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async getLibraryDataSourceConnectionCheck(params: {
    workspaceSlug: string;
    dataSourceEntryId: string;
    checkId: string;
  }): Promise<DomainResultFor<'get_library_data_source_connection_check'>> {
    const path = buildBffPath('get_library_data_source_connection_check', {
      slug: params.workspaceSlug,
      data_source_entry_id: params.dataSourceEntryId,
      check_id: params.checkId,
    });
    return this.sendSafe<'get_library_data_source_connection_check'>(path);
  }

  async importLibraryManualFacts(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    dataSourceEntryId: string;
    body: DomainOperationMap['import_library_manual_facts']['requestBody'];
  }): Promise<
    DomainResultFor<'import_library_manual_facts'> | ReconciliationRequired
  > {
    const path = buildBffPath('import_library_manual_facts', {
      slug: params.workspaceSlug,
      data_source_entry_id: params.dataSourceEntryId,
    });
    return this.runCommand(
      'import_library_manual_facts',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  // ==== Experiments ====

  async listExperiments(params: {
    workspaceSlug: string;
    state?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_experiments'>> {
    const path =
      buildBffPath('list_experiments', { slug: params.workspaceSlug }) +
      this.buildQuery({
        state: params.state,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_experiments'>(path);
  }

  async getExperiment(params: {
    workspaceSlug: string;
    experimentId: string;
  }): Promise<DomainResultFor<'get_experiment'>> {
    const path = buildBffPath('get_experiment', {
      slug: params.workspaceSlug,
      experiment_id: params.experimentId,
    });
    return this.sendSafe<'get_experiment'>(path);
  }

  async getExperimentTimeline(params: {
    workspaceSlug: string;
    experimentId: string;
    cursor?: number;
    limit?: number;
  }): Promise<DomainResultFor<'get_experiment_timeline'>> {
    const path =
      buildBffPath('get_experiment_timeline', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
      }) + this.buildQuery({ cursor: params.cursor, limit: params.limit });
    return this.sendSafe<'get_experiment_timeline'>(path);
  }

  async analyzeExperimentDuplicates(params: {
    workspaceSlug: string;
    experimentId: string;
  }): Promise<DomainResultFor<'analyze_experiment_duplicates'>> {
    const path = buildBffPath('analyze_experiment_duplicates', {
      slug: params.workspaceSlug,
      experiment_id: params.experimentId,
    });
    return this.sendSafe<'analyze_experiment_duplicates'>(path);
  }

  async createExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_experiment']['requestBody'];
  }): Promise<DomainResultFor<'create_experiment'> | ReconciliationRequired> {
    return this.runCommand(
      'create_experiment',
      params.actionInstanceId,
      buildBffPath('create_experiment', { slug: params.workspaceSlug }),
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async addExperimentEntry(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['add_experiment_entry']['requestBody'];
  }): Promise<
    DomainResultFor<'add_experiment_entry'> | ReconciliationRequired
  > {
    return this.runCommand(
      'add_experiment_entry',
      params.actionInstanceId,
      buildBffPath('add_experiment_entry', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
      }),
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async updateExperimentContext(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['update_experiment_context']['requestBody'];
  }): Promise<
    DomainResultFor<'update_experiment_context'> | ReconciliationRequired
  > {
    return this.runCommand(
      'update_experiment_context',
      params.actionInstanceId,
      buildBffPath('update_experiment_context', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
      }),
      'PUT',
      params.body,
      200,
      'ok'
    );
  }

  async freezeExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['freeze_experiment']['requestBody'];
  }): Promise<DomainResultFor<'freeze_experiment'> | ReconciliationRequired> {
    return this.runCommand(
      'freeze_experiment',
      params.actionInstanceId,
      buildBffPath('freeze_experiment', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async bindExperimentCandidate(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    candidateId: string;
    body: DomainOperationMap['bind_experiment_candidate']['requestBody'];
  }): Promise<
    DomainResultFor<'bind_experiment_candidate'> | ReconciliationRequired
  > {
    return this.runCommand(
      'bind_experiment_candidate',
      params.actionInstanceId,
      buildBffPath('bind_experiment_candidate', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
        candidate_id: params.candidateId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async startExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['start_experiment']['requestBody'];
  }): Promise<DomainResultFor<'start_experiment'> | ReconciliationRequired> {
    return this.runCommand(
      'start_experiment',
      params.actionInstanceId,
      buildBffPath('start_experiment', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async judgeExperiment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    body: DomainOperationMap['judge_experiment']['requestBody'];
  }): Promise<DomainResultFor<'judge_experiment'> | ReconciliationRequired> {
    return this.runCommand(
      'judge_experiment',
      params.actionInstanceId,
      buildBffPath('judge_experiment', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async confirmExperimentProposal(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    experimentId: string;
    proposalId: string;
    body: DomainOperationMap['confirm_experiment_proposal']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_experiment_proposal'> | ReconciliationRequired
  > {
    return this.runCommand(
      'confirm_experiment_proposal',
      params.actionInstanceId,
      buildBffPath('confirm_experiment_proposal', {
        slug: params.workspaceSlug,
        experiment_id: params.experimentId,
        proposal_id: params.proposalId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async createMetaLaunchPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_meta_launch_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'create_meta_launch_preview'> | ReconciliationRequired
  > {
    return this.runCommand(
      'create_meta_launch_preview',
      params.actionInstanceId,
      buildBffPath('create_meta_launch_preview', {
        slug: params.workspaceSlug,
      }),
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createMetaConnection(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_meta_connection']['requestBody'];
  }): Promise<
    DomainResultFor<'create_meta_connection'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_meta_connection', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_meta_connection',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async listMetaConnections(params: {
    workspaceSlug: string;
    metaMode: string;
  }): Promise<DomainResultFor<'list_meta_connections'>> {
    const path =
      buildBffPath('list_meta_connections', {
        slug: params.workspaceSlug,
      }) + this.buildQuery({ meta_mode: params.metaMode });
    return this.sendSafe<'list_meta_connections'>(path);
  }

  async getMetaConnection(params: {
    workspaceSlug: string;
    connectionId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_meta_connection'>> {
    const path =
      buildBffPath('get_meta_connection', {
        slug: params.workspaceSlug,
        connection_id: params.connectionId,
      }) + this.buildQuery({ meta_mode: params.metaMode });
    return this.sendSafe<'get_meta_connection'>(path);
  }

  async confirmMetaLaunchPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    previewId: string;
    body: DomainOperationMap['confirm_meta_launch_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_meta_launch_preview'> | ReconciliationRequired
  > {
    return this.runCommand(
      'confirm_meta_launch_preview',
      params.actionInstanceId,
      buildBffPath('confirm_meta_launch_preview', {
        slug: params.workspaceSlug,
        preview_id: params.previewId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async getMetaLaunchPreview(params: {
    workspaceSlug: string;
    previewId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_meta_launch_preview'>> {
    const path =
      buildBffPath('get_meta_launch_preview', {
        slug: params.workspaceSlug,
        preview_id: params.previewId,
      }) + this.buildQuery({ meta_mode: params.metaMode });
    return this.sendSafe<'get_meta_launch_preview'>(path);
  }

  async executeMetaLaunchPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    previewId: string;
    body: DomainOperationMap['execute_meta_launch_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'execute_meta_launch_preview'> | ReconciliationRequired
  > {
    return this.runCommand(
      'execute_meta_launch_preview',
      params.actionInstanceId,
      buildBffPath('execute_meta_launch_preview', {
        slug: params.workspaceSlug,
        preview_id: params.previewId,
      }),
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async getMetaLaunchProvisioning(params: {
    workspaceSlug: string;
    provisioningId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_meta_launch_provisioning'>> {
    const path =
      buildBffPath('get_meta_launch_provisioning', {
        slug: params.workspaceSlug,
        provisioning_id: params.provisioningId,
      }) + this.buildQuery({ meta_mode: params.metaMode });
    return this.sendSafe<'get_meta_launch_provisioning'>(path);
  }

  async recoverMetaLaunchOperation(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    operationId: string;
    body: DomainOperationMap['recover_meta_launch_operation']['requestBody'];
  }): Promise<
    DomainResultFor<'recover_meta_launch_operation'> | ReconciliationRequired
  > {
    return this.runCommand(
      'recover_meta_launch_operation',
      params.actionInstanceId,
      buildBffPath('recover_meta_launch_operation', {
        slug: params.workspaceSlug,
        operation_id: params.operationId,
      }),
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  // ==== Generation reads ====

  async resolveMarketingCall(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    callId: string;
    body: DomainOperationMap['resolve_marketing_call']['requestBody'];
  }): Promise<
    DomainResultFor<'resolve_marketing_call'> | ReconciliationRequired
  > {
    return this.runCommand(
      'resolve_marketing_call',
      params.actionInstanceId,
      buildBffPath('resolve_marketing_call', {
        slug: params.workspaceSlug,
        call_id: params.callId,
      }),
      'POST',
      params.body,
      202,
      'accepted'
    );
  }
  async getMarketingWorkflow(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    projectId: string;
    includePolling?: boolean;
    includeImageEdits?: boolean;
    includeVersionHistory?: boolean;
    includeBoard?: boolean;
    includeExecutionPlan?: boolean;
    includeQcKinds?: boolean;
    includeBoardThumbnails?: boolean;
    /** #255: the handoff's language and display names, and preflight issue params. */
    includeTextLanguage?: boolean;
    /** #294/#295: the single-image transcription and copy diff, and why a QC output was refused. */
    includeQcEvidence?: boolean;
    /** #367: the frozen plan's review and style, as the project read returns the project's plan. */
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
  }): Promise<DomainResultFor<'get_marketing_workflow'>> {
    return this.sendSafe<'get_marketing_workflow'>(
      buildBffPath('get_marketing_workflow', {
        slug: params.workspaceSlug,
        project_id: params.projectId,
      }) +
        this.buildQuery({
          include_polling: params.includePolling,
          include_image_edits: params.includeImageEdits || undefined,
          include_version_history: params.includeVersionHistory || undefined,
          include_languages: params.includeLanguages || undefined,
          include_board: params.includeBoard || undefined,
          include_execution_plan: params.includeExecutionPlan || undefined,
          include_qc_kinds: params.includeQcKinds || undefined,
          include_board_thumbnails: params.includeBoardThumbnails || undefined,
          include_text_language: params.includeTextLanguage || undefined,
          include_qc_evidence: params.includeQcEvidence || undefined,
          // The workflow route has no competitor review: a round never shows it (#273).
          include_plan_review: params.includePlanReview || undefined,
          include_brand_style: params.includeBrandStyle || undefined,
        })
    );
  }

  async freezeMarketingRound(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    projectId: string;
    expectedRevision?: number;
    body: DomainOperationMap['freeze_marketing_round']['requestBody'];
  }): Promise<
    DomainResultFor<'freeze_marketing_round'> | ReconciliationRequired
  > {
    return this.runCommand(
      'freeze_marketing_round',
      params.actionInstanceId,
      buildBffPath('freeze_marketing_round', {
        slug: params.workspaceSlug,
        project_id: params.projectId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async reuseMarketingCreative(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['reuse_marketing_creative']['requestBody'];
  }): Promise<
    DomainResultFor<'reuse_marketing_creative'> | ReconciliationRequired
  > {
    return this.runCommand(
      'reuse_marketing_creative',
      params.actionInstanceId,
      buildBffPath('reuse_marketing_creative', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async getMarketingEvidenceHandoff(params: {
    workspaceSlug: string;
    roundId: string;
  }): Promise<DomainResultFor<'get_marketing_evidence_handoff'>> {
    return this.sendSafe<'get_marketing_evidence_handoff'>(
      buildBffPath('get_marketing_evidence_handoff', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      })
    );
  }
  async createMarketingEvidenceHandoff(params: {
    workspaceSlug: string;
    roundId: string;
    expectedRevision: number;
    actionInstanceId: string;
    body: DomainOperationMap['create_marketing_evidence_handoff']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_marketing_evidence_handoff'>
    | ReconciliationRequired
  > {
    return this.runCommand(
      'create_marketing_evidence_handoff',
      params.actionInstanceId,
      buildBffPath('create_marketing_evidence_handoff', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async createMarketingImageEdit(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['create_marketing_image_edit']['requestBody'];
  }): Promise<
    DomainResultFor<'create_marketing_image_edit'> | ReconciliationRequired
  > {
    return this.runCommand(
      'create_marketing_image_edit',
      params.actionInstanceId,
      buildBffPath('create_marketing_image_edit', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      202,
      'accepted',
      { ifMatch: params.expectedRevision }
    );
  }

  async prepareMarketingFeedback(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['prepare_marketing_feedback']['requestBody'];
  }): Promise<
    DomainResultFor<'prepare_marketing_feedback'> | ReconciliationRequired
  > {
    return this.runCommand(
      'prepare_marketing_feedback',
      params.actionInstanceId,
      buildBffPath('prepare_marketing_feedback', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async startMarketingExplanation(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['start_marketing_explanation']['requestBody'];
  }): Promise<
    DomainResultFor<'start_marketing_explanation'> | ReconciliationRequired
  > {
    return this.runCommand(
      'start_marketing_explanation',
      params.actionInstanceId,
      buildBffPath('start_marketing_explanation', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      202,
      'accepted',
      { ifMatch: params.expectedRevision }
    );
  }

  async reviewMarketingCreative(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['review_marketing_creative']['requestBody'];
  }): Promise<
    DomainResultFor<'review_marketing_creative'> | ReconciliationRequired
  > {
    return this.runCommand(
      'review_marketing_creative',
      params.actionInstanceId,
      buildBffPath('review_marketing_creative', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async createMarketingHandoff(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['create_marketing_handoff']['requestBody'];
  }): Promise<
    DomainResultFor<'create_marketing_handoff'> | ReconciliationRequired
  > {
    return this.runCommand(
      'create_marketing_handoff',
      params.actionInstanceId,
      buildBffPath('create_marketing_handoff', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      202,
      'accepted',
      { ifMatch: params.expectedRevision }
    );
  }

  async bindMarketingDelivery(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['bind_marketing_delivery']['requestBody'];
  }): Promise<
    DomainResultFor<'bind_marketing_delivery'> | ReconciliationRequired
  > {
    return this.runCommand(
      'bind_marketing_delivery',
      params.actionInstanceId,
      buildBffPath('bind_marketing_delivery', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async previewMarketingMetrics(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['preview_marketing_metrics']['requestBody'];
  }): Promise<
    DomainResultFor<'preview_marketing_metrics'> | ReconciliationRequired
  > {
    return this.runCommand(
      'preview_marketing_metrics',
      params.actionInstanceId,
      buildBffPath('preview_marketing_metrics', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async confirmMarketingMetrics(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['confirm_marketing_metrics']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_marketing_metrics'> | ReconciliationRequired
  > {
    return this.runCommand(
      'confirm_marketing_metrics',
      params.actionInstanceId,
      buildBffPath('confirm_marketing_metrics', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.expectedRevision }
    );
  }

  async evaluateMarketingRound(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
  }): Promise<
    DomainResultFor<'evaluate_marketing_round'> | ReconciliationRequired
  > {
    return this.runCommand(
      'evaluate_marketing_round',
      params.actionInstanceId,
      buildBffPath('evaluate_marketing_round', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      undefined,
      200,
      'ok',
      { ifMatch: params.expectedRevision }
    );
  }

  async closeMarketingRound(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
    expectedRevision?: number;
    body: DomainOperationMap['close_marketing_round']['requestBody'];
  }): Promise<
    DomainResultFor<'close_marketing_round'> | ReconciliationRequired
  > {
    return this.runCommand(
      'close_marketing_round',
      params.actionInstanceId,
      buildBffPath('close_marketing_round', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.expectedRevision }
    );
  }

  async downloadMarketingHandoff(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    roundId: string;
  }): Promise<
    DomainResultFor<'download_marketing_handoff'> | ReconciliationRequired
  > {
    return this.runCommand(
      'download_marketing_handoff',
      params.actionInstanceId,
      buildBffPath('download_marketing_handoff', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      undefined,
      201,
      'created'
    );
  }

  async retryMarketingHandoff(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    expectedRevision?: number;
    roundId: string;
  }): Promise<
    DomainResultFor<'retry_marketing_handoff'> | ReconciliationRequired
  > {
    return this.runCommand(
      'retry_marketing_handoff',
      params.actionInstanceId,
      buildBffPath('retry_marketing_handoff', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      undefined,
      202,
      'accepted',
      { ifMatch: params.expectedRevision }
    );
  }

  async getMarketingRound(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    roundId: string;
    includeImageEdits?: boolean;
    includeVersionHistory?: boolean;
    includeBoard?: boolean;
    includeExecutionPlan?: boolean;
    includeQcKinds?: boolean;
    includeBoardThumbnails?: boolean;
    /** #255: the handoff's language and display names, and preflight issue params. */
    includeTextLanguage?: boolean;
    /** #294/#295: the single-image transcription and copy diff, and why a QC output was refused. */
    includeQcEvidence?: boolean;
  }): Promise<DomainResultFor<'get_marketing_round'>> {
    return this.sendSafe<'get_marketing_round'>(
      buildBffPath('get_marketing_round', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }) +
        this.buildQuery({
          include_image_edits: params.includeImageEdits || undefined,
          include_version_history: params.includeVersionHistory || undefined,
          include_languages: params.includeLanguages || undefined,
          include_board: params.includeBoard || undefined,
          include_execution_plan: params.includeExecutionPlan || undefined,
          include_qc_kinds: params.includeQcKinds || undefined,
          include_board_thumbnails: params.includeBoardThumbnails || undefined,
          include_text_language: params.includeTextLanguage || undefined,
          include_qc_evidence: params.includeQcEvidence || undefined,
        })
    );
  }

  async getMarketingAIQCQuote(params: {
    workspaceSlug: string;
    roundId: string;
    kind?: 'single' | 'paired';
    strategyLanguage?: 'zh-CN' | 'en-US';
  }): Promise<DomainResultFor<'get_marketing_ai_qc_quote'>> {
    return this.sendSafe<'get_marketing_ai_qc_quote'>(
      buildBffPath('get_marketing_ai_qc_quote', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }) +
        this.buildQuery({
          kind: params.kind,
          strategy_language: params.strategyLanguage,
        })
    );
  }

  async startMarketingAIQC(params: {
    workspaceSlug: string;
    roundId: string;
    expectedRevision?: number;
    actionInstanceId: string;
    body: DomainOperationMap['start_marketing_ai_qc']['requestBody'];
  }): Promise<
    DomainResultFor<'start_marketing_ai_qc'> | ReconciliationRequired
  > {
    return this.runCommand(
      'start_marketing_ai_qc',
      params.actionInstanceId,
      buildBffPath('start_marketing_ai_qc', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      202,
      'accepted',
      { ifMatch: params.expectedRevision }
    );
  }

  async confirmMarketingAIQC(params: {
    workspaceSlug: string;
    roundId: string;
    expectedRevision?: number;
    actionInstanceId: string;
    body: DomainOperationMap['confirm_marketing_ai_qc']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_marketing_ai_qc'> | ReconciliationRequired
  > {
    return this.runCommand(
      'confirm_marketing_ai_qc',
      params.actionInstanceId,
      buildBffPath('confirm_marketing_ai_qc', {
        slug: params.workspaceSlug,
        round_id: params.roundId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.expectedRevision }
    );
  }

  async getMarketingCapabilities(params: {
    workspaceSlug: string;
    includeReporting?: boolean;
  }): Promise<DomainResultFor<'get_marketing_capabilities'>> {
    return this.sendSafe<'get_marketing_capabilities'>(
      buildBffPath('get_marketing_capabilities', {
        slug: params.workspaceSlug,
      }) + this.buildQuery({ include_reporting: params.includeReporting })
    );
  }

  async listMarketingProjects(params: {
    workspaceSlug: string;
    limit?: number;
    cursor?: string;
    q?: string;
  }): Promise<DomainResultFor<'list_marketing_projects'>> {
    return this.sendSafe<'list_marketing_projects'>(
      buildBffPath('list_marketing_projects', { slug: params.workspaceSlug }) +
        this.buildQuery({
          limit: params.limit,
          cursor: params.cursor,
          q: params.q,
        })
    );
  }

  async getMarketingProject(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    includeRecovery?: boolean;
    projectId: string;
  }): Promise<DomainResultFor<'get_marketing_project'>> {
    return this.sendSafe<'get_marketing_project'>(
      buildBffPath('get_marketing_project', {
        slug: params.workspaceSlug,
        project_id: params.projectId,
      }) +
        this.buildQuery({
          include_recovery: params.includeRecovery,
          include_languages: params.includeLanguages || undefined,
          include_plan_review: params.includePlanReview || undefined,
          include_brand_style: params.includeBrandStyle || undefined,
          include_execution_plan: params.includeExecutionPlan || undefined,
          // #273: competitor review rides on plan review; a build that reads one reads both.
          include_competitor_review: params.includePlanReview || undefined,
        })
    );
  }

  async createMarketingProject(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    actionInstanceId: string;
    expectedRevision?: number;
    body: DomainOperationMap['create_marketing_project']['requestBody'];
  }): Promise<
    DomainResultFor<'create_marketing_project'> | ReconciliationRequired
  > {
    return this.runCommand(
      'create_marketing_project',
      params.actionInstanceId,
      buildBffPath('create_marketing_project', { slug: params.workspaceSlug }) +
        this.buildQuery({
          include_languages: params.includeLanguages || undefined,
          include_plan_review: params.includePlanReview || undefined,
          include_brand_style: params.includeBrandStyle || undefined,
          include_execution_plan: params.includeExecutionPlan || undefined,
          // #273: competitor review rides on plan review; a build that reads one reads both.
          include_competitor_review: params.includePlanReview || undefined,
        }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async editMarketingProject(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    actionInstanceId: string;
    expectedRevision?: number;
    projectId: string;
    body: DomainOperationMap['edit_marketing_project']['requestBody'];
  }): Promise<
    DomainResultFor<'edit_marketing_project'> | ReconciliationRequired
  > {
    return this.runCommand(
      'edit_marketing_project',
      params.actionInstanceId,
      buildBffPath('edit_marketing_project', {
        slug: params.workspaceSlug,
        project_id: params.projectId,
      }) +
        this.buildQuery({
          include_languages: params.includeLanguages || undefined,
          include_plan_review: params.includePlanReview || undefined,
          include_brand_style: params.includeBrandStyle || undefined,
          include_execution_plan: params.includeExecutionPlan || undefined,
          // #273: competitor review rides on plan review; a build that reads one reads both.
          include_competitor_review: params.includePlanReview || undefined,
        }),
      'PATCH',
      params.body,
      200,
      'ok',
      { ifMatch: params.expectedRevision }
    );
  }

  async startMarketingPlanning(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    expectedRevision?: number;
    projectId: string;
    body: DomainOperationMap['start_marketing_planning']['requestBody'];
  }): Promise<
    DomainResultFor<'start_marketing_planning'> | ReconciliationRequired
  > {
    return this.runCommand(
      'start_marketing_planning',
      params.actionInstanceId,
      buildBffPath('start_marketing_planning', {
        slug: params.workspaceSlug,
        project_id: params.projectId,
      }),
      'POST',
      params.body,
      202,
      'accepted',
      { ifMatch: params.expectedRevision }
    );
  }

  async prepareMarketingGeneration(params: {
    workspaceSlug: string;
    includeLanguages?: boolean;
    includePlanReview?: boolean;
    includeBrandStyle?: boolean;
    includeExecutionPlan?: boolean;
    actionInstanceId: string;
    expectedRevision?: number;
    projectId: string;
    body: DomainOperationMap['prepare_marketing_generation']['requestBody'];
  }): Promise<
    DomainResultFor<'prepare_marketing_generation'> | ReconciliationRequired
  > {
    return this.runCommand(
      'prepare_marketing_generation',
      params.actionInstanceId,
      buildBffPath('prepare_marketing_generation', {
        slug: params.workspaceSlug,
        project_id: params.projectId,
      }) +
        this.buildQuery({
          include_languages: params.includeLanguages || undefined,
          include_plan_review: params.includePlanReview || undefined,
          include_brand_style: params.includeBrandStyle || undefined,
          include_execution_plan: params.includeExecutionPlan || undefined,
          // #273: competitor review rides on plan review; a build that reads one reads both.
          include_competitor_review: params.includePlanReview || undefined,
        }),
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.expectedRevision }
    );
  }

  async listMarketingBrandKits(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_marketing_brand_kits'>> {
    return this.sendSafe<'list_marketing_brand_kits'>(
      buildBffPath('list_marketing_brand_kits', { slug: params.workspaceSlug })
    );
  }

  async getProductBrandKit(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_product_brand_kit'>> {
    return this.sendSafe<'get_product_brand_kit'>(
      buildBffPath('get_product_brand_kit', {
        slug: params.workspaceSlug,
        product_id: params.productId,
      })
    );
  }

  async saveProductBrandKit(params: {
    workspaceSlug: string;
    productId: string;
    actionInstanceId: string;
    ifMatch: number;
    body: DomainOperationMap['save_product_brand_kit']['requestBody'];
  }): Promise<
    DomainResultFor<'save_product_brand_kit'> | ReconciliationRequired
  > {
    return this.runCommand(
      'save_product_brand_kit',
      params.actionInstanceId,
      buildBffPath('save_product_brand_kit', {
        slug: params.workspaceSlug,
        product_id: params.productId,
      }),
      'PUT',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async getGenerationCapabilities(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_generation_capabilities'>> {
    const path = buildBffPath('get_generation_capabilities', {
      slug: params.workspaceSlug,
    });
    return this.sendSafe<'get_generation_capabilities'>(path);
  }

  async listGenerationDrafts(params: {
    workspaceSlug: string;
    productId?: string;
    state?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_drafts'>> {
    const path =
      buildBffPath('list_generation_drafts', { slug: params.workspaceSlug }) +
      this.buildQuery({
        product_id: params.productId,
        state: params.state,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_generation_drafts'>(path);
  }

  async getGenerationDraft(params: {
    workspaceSlug: string;
    draftId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_generation_draft'>> {
    const path = buildBffPath('get_generation_draft', {
      slug: params.workspaceSlug,
      draft_id: params.draftId,
    });
    return this.sendSafe<'get_generation_draft'>(path, params.ifNoneMatch);
  }

  async getGenerationPreview(params: {
    workspaceSlug: string;
    previewId: string;
  }): Promise<DomainResultFor<'get_generation_preview'>> {
    const path = buildBffPath('get_generation_preview', {
      slug: params.workspaceSlug,
      preview_id: params.previewId,
    });
    return this.sendSafe<'get_generation_preview'>(path);
  }

  async listGenerationRuns(params: {
    workspaceSlug: string;
    productId?: string;
    status?: string;
    executionMode?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_runs'>> {
    const path =
      buildBffPath('list_generation_runs', { slug: params.workspaceSlug }) +
      this.buildQuery({
        product_id: params.productId,
        status: params.status,
        execution_mode: params.executionMode,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_generation_runs'>(path);
  }

  async getGenerationRun(params: {
    workspaceSlug: string;
    runId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_generation_run'>> {
    const path = buildBffPath('get_generation_run', {
      slug: params.workspaceSlug,
      run_id: params.runId,
    });
    return this.sendSafe<'get_generation_run'>(path, params.ifNoneMatch);
  }

  async listGenerationRunItems(params: {
    workspaceSlug: string;
    runId: string;
    state?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_run_items'>> {
    const path =
      buildBffPath('list_generation_run_items', {
        slug: params.workspaceSlug,
        run_id: params.runId,
      }) +
      this.buildQuery({
        state: params.state,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_generation_run_items'>(path);
  }

  async getGenerationRunItem(params: {
    workspaceSlug: string;
    runId: string;
    runItemId: string;
  }): Promise<DomainResultFor<'get_generation_run_item'>> {
    const path = buildBffPath('get_generation_run_item', {
      slug: params.workspaceSlug,
      run_id: params.runId,
      run_item_id: params.runItemId,
    });
    return this.sendSafe<'get_generation_run_item'>(path);
  }

  async listGenerationAssets(params: {
    workspaceSlug: string;
    productId?: string;
    runId?: string;
    provider?: string;
    executionMode?: string;
    originKind?: string;
    reviewValue?: string;
    aspectRatio?: string;
    qcState?: string;
    tag?: string;
    experimentContext?: string;
    conclusion?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_generation_assets'>> {
    const path =
      buildBffPath('list_generation_assets', { slug: params.workspaceSlug }) +
      this.buildQuery({
        product_id: params.productId,
        run_id: params.runId,
        provider: params.provider,
        execution_mode: params.executionMode,
        origin_kind: params.originKind,
        review_value: params.reviewValue,
        aspect_ratio: params.aspectRatio,
        qc_state: params.qcState,
        tag: params.tag,
        experiment_context: params.experimentContext,
        conclusion: params.conclusion,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_generation_assets'>(path);
  }

  async getGenerationAssetVersion(params: {
    workspaceSlug: string;
    versionId: string;
  }): Promise<DomainResultFor<'get_generation_asset_version'>> {
    const path = buildBffPath('get_generation_asset_version', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.sendSafe<'get_generation_asset_version'>(path);
  }

  async getGenerationExportPackage(params: {
    workspaceSlug: string;
    exportPackageId: string;
  }): Promise<DomainResultFor<'get_generation_export_package'>> {
    const path = buildBffPath('get_generation_export_package', {
      slug: params.workspaceSlug,
      export_package_id: params.exportPackageId,
    });
    return this.sendSafe<'get_generation_export_package'>(path);
  }

  async getGenerationExportManifest(params: {
    workspaceSlug: string;
    exportPackageId: string;
  }): Promise<DomainResultFor<'get_generation_export_manifest'>> {
    const path = buildBffPath('get_generation_export_manifest', {
      slug: params.workspaceSlug,
      export_package_id: params.exportPackageId,
    });
    return this.sendSafe<'get_generation_export_manifest'>(path);
  }

  // ==== Generation commands ====

  async createGenerationDraft(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_generation_draft']['requestBody'];
  }): Promise<
    DomainResultFor<'create_generation_draft'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_draft', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_generation_draft',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async updateGenerationDraft(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      draftId: string;
      body: DomainOperationMap['update_generation_draft']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'update_generation_draft'> | ReconciliationRequired
  > {
    const path = buildBffPath('update_generation_draft', {
      slug: params.workspaceSlug,
      draft_id: params.draftId,
    });
    return this.runCommand(
      'update_generation_draft',
      params.actionInstanceId,
      path,
      'PATCH',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async createGenerationPreview(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      draftId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'create_generation_preview'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_preview', {
      slug: params.workspaceSlug,
      draft_id: params.draftId,
    });
    return this.runCommand(
      'create_generation_preview',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created',
      { ifMatch: params.ifMatch }
    );
  }

  async submitGenerationRun(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      body: DomainOperationMap['submit_generation_run']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'submit_generation_run'> | ReconciliationRequired
  > {
    const path = buildBffPath('submit_generation_run', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'submit_generation_run',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted',
      { ifMatch: params.ifMatch }
    );
  }

  async createGenerationRunRecoveryPreview(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['create_generation_run_recovery_preview']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'create_generation_run_recovery_preview'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_run_recovery_preview', {
      slug: params.workspaceSlug,
      run_id: params.runId,
    });
    return this.runCommand(
      'create_generation_run_recovery_preview',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.ifMatch }
    );
  }

  async cancelGenerationRun(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'cancel_generation_run'> | ReconciliationRequired
  > {
    const path = buildBffPath('cancel_generation_run', {
      slug: params.workspaceSlug,
      run_id: params.runId,
    });
    return this.runCommand(
      'cancel_generation_run',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async retryGenerationRunFailedItems(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['retry_generation_run_failed_items']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'retry_generation_run_failed_items'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('retry_generation_run_failed_items', {
      slug: params.workspaceSlug,
      run_id: params.runId,
    });
    return this.runCommand(
      'retry_generation_run_failed_items',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async resumeGenerationRunPausedItems(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['resume_generation_run_paused_items']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'resume_generation_run_paused_items'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('resume_generation_run_paused_items', {
      slug: params.workspaceSlug,
      run_id: params.runId,
    });
    return this.runCommand(
      'resume_generation_run_paused_items',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async reconcileGenerationRun(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      runId: string;
      body: DomainOperationMap['reconcile_generation_run']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'reconcile_generation_run'> | ReconciliationRequired
  > {
    const path = buildBffPath('reconcile_generation_run', {
      slug: params.workspaceSlug,
      run_id: params.runId,
    });
    return this.runCommand(
      'reconcile_generation_run',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async createGenerationAssetReview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['create_generation_asset_review']['requestBody'];
  }): Promise<
    DomainResultFor<'create_generation_asset_review'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_asset_review', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'create_generation_asset_review',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createGenerationRegenerationDraft(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
    body: DomainOperationMap['create_generation_regeneration_draft']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_generation_regeneration_draft'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_regeneration_draft', {
      slug: params.workspaceSlug,
      version_id: params.versionId,
    });
    return this.runCommand(
      'create_generation_regeneration_draft',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createGenerationAssetVersionDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    versionId: string;
  }): Promise<
    | DomainResultFor<'create_generation_asset_version_download_intent'>
    | ReconciliationRequired
  > {
    const path = buildBffPath(
      'create_generation_asset_version_download_intent',
      { slug: params.workspaceSlug, version_id: params.versionId }
    );
    return this.runCommand(
      'create_generation_asset_version_download_intent',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  async createGenerationExportPackage(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_generation_export_package']['requestBody'];
  }): Promise<
    DomainResultFor<'create_generation_export_package'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_export_package', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_generation_export_package',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async createGenerationExportDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    exportPackageId: string;
  }): Promise<
    | DomainResultFor<'create_generation_export_download_intent'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_generation_export_download_intent', {
      slug: params.workspaceSlug,
      export_package_id: params.exportPackageId,
    });
    return this.runCommand(
      'create_generation_export_download_intent',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  // ==== Localization ====

  async getLocalizationWorkspace(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'get_localization_workspace'>> {
    return this.sendSafe<'get_localization_workspace'>(
      buildBffPath('get_localization_workspace', { slug: params.workspaceSlug })
    );
  }

  async listLocalizationBatches(params: {
    workspaceSlug: string;
    phase?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_localization_batches'>> {
    const path =
      buildBffPath('list_localization_batches', {
        slug: params.workspaceSlug,
      }) +
      this.buildQuery({
        phase: params.phase,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_localization_batches'>(path);
  }

  async getLocalizationBatch(params: {
    workspaceSlug: string;
    batchId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_localization_batch'>> {
    const path = buildBffPath('get_localization_batch', {
      slug: params.workspaceSlug,
      batch_id: params.batchId,
    });
    return this.sendSafe<'get_localization_batch'>(path, params.ifNoneMatch);
  }

  async getLocalizationItem(params: {
    workspaceSlug: string;
    batchId: string;
    itemId: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_localization_item'>> {
    const path = buildBffPath('get_localization_item', {
      slug: params.workspaceSlug,
      batch_id: params.batchId,
      item_id: params.itemId,
    });
    return this.sendSafe<'get_localization_item'>(path, params.ifNoneMatch);
  }

  async getLocalizationBatchOutcome(params: {
    workspaceSlug: string;
    batchId: string;
  }): Promise<DomainResultFor<'get_localization_batch_outcome'>> {
    return this.sendSafe<'get_localization_batch_outcome'>(
      buildBffPath('get_localization_batch_outcome', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
      })
    );
  }

  async createLocalizationBatch(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_localization_batch']['requestBody'];
  }): Promise<
    DomainResultFor<'create_localization_batch'> | ReconciliationRequired
  > {
    return this.runCommand(
      'create_localization_batch',
      params.actionInstanceId,
      buildBffPath('create_localization_batch', { slug: params.workspaceSlug }),
      'POST',
      params.body,
      201,
      'created'
    );
  }

  // ==== Scale reads ====

  async listScaleAvailableModes(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_scale_available_modes'>> {
    const path = buildBffPath('list_scale_available_modes', {
      slug: params.workspaceSlug,
    });
    return this.sendSafe<'list_scale_available_modes'>(path);
  }

  async listScaleEnrollments(params: {
    workspaceSlug: string;
    metaMode: string;
    lifecycle?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_scale_enrollments'>> {
    const path =
      buildBffPath('list_scale_enrollments', {
        slug: params.workspaceSlug,
        meta_mode: params.metaMode,
      }) +
      this.buildQuery({
        lifecycle: params.lifecycle,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_scale_enrollments'>(path);
  }

  async getScaleEnrollment(params: {
    workspaceSlug: string;
    enrollmentId: string;
    metaMode: string;
    ifNoneMatch?: string;
  }): Promise<DomainResultFor<'get_scale_enrollment'>> {
    const path = buildBffPath('get_scale_enrollment', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.sendSafe<'get_scale_enrollment'>(path, params.ifNoneMatch);
  }

  async getScaleEpisode(params: {
    workspaceSlug: string;
    enrollmentId: string;
    episodeId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_scale_episode'>> {
    const path = buildBffPath('get_scale_episode', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
      episode_id: params.episodeId,
    });
    return this.sendSafe<'get_scale_episode'>(path);
  }

  async getScaleEnrollmentEvidence(params: {
    workspaceSlug: string;
    enrollmentId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_scale_enrollment_evidence'>> {
    const path = buildBffPath('get_scale_enrollment_evidence', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.sendSafe<'get_scale_enrollment_evidence'>(path);
  }

  async getScaleEnrollmentBreakdowns(params: {
    workspaceSlug: string;
    enrollmentId: string;
    metaMode: string;
    snapshotId: string;
    breakdownType: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'get_scale_enrollment_breakdowns'>> {
    const path =
      buildBffPath('get_scale_enrollment_breakdowns', {
        slug: params.workspaceSlug,
        meta_mode: params.metaMode,
        enrollment_id: params.enrollmentId,
      }) +
      this.buildQuery({
        snapshot_id: params.snapshotId,
        breakdown_type: params.breakdownType,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'get_scale_enrollment_breakdowns'>(path);
  }

  async listScaleAdviceThreads(params: {
    workspaceSlug: string;
    metaMode: string;
    enrollmentId?: string;
    lifecycle?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_scale_advice_threads'>> {
    const path =
      buildBffPath('list_scale_advice_threads', {
        slug: params.workspaceSlug,
        meta_mode: params.metaMode,
      }) +
      this.buildQuery({
        enrollment_id: params.enrollmentId,
        lifecycle: params.lifecycle,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_scale_advice_threads'>(path);
  }

  async listScaleTargets(params: {
    workspaceSlug: string;
    metaMode: string;
    lifecycleState?: string;
    productVersionId?: string;
    offerVersionId?: string;
    marketCode?: string;
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_scale_targets'>> {
    const path =
      buildBffPath('list_scale_targets', {
        slug: params.workspaceSlug,
        meta_mode: params.metaMode,
      }) +
      this.buildQuery({
        lifecycle_state: params.lifecycleState,
        product_version_id: params.productVersionId,
        offer_version_id: params.offerVersionId,
        market_code: params.marketCode,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_scale_targets'>(path);
  }

  async getScaleTarget(params: {
    workspaceSlug: string;
    targetId: string;
    metaMode: string;
  }): Promise<DomainResultFor<'get_scale_target'>> {
    const path = buildBffPath('get_scale_target', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      target_id: params.targetId,
    });
    return this.sendSafe<'get_scale_target'>(path);
  }

  async listScaleTargetContexts(params: {
    workspaceSlug: string;
    metaMode: string;
  }): Promise<DomainResultFor<'list_scale_target_contexts'>> {
    const path = buildBffPath('list_scale_target_contexts', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.sendSafe<'list_scale_target_contexts'>(path);
  }

  async getScaleCommandOutcome(params: {
    workspaceSlug: string;
    metaMode: string;
    identity: string;
  }): Promise<DomainResultFor<'get_scale_command_outcome'>> {
    const path = buildBffPath('get_scale_command_outcome', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      identity: params.identity,
    });
    return this.sendSafe<'get_scale_command_outcome'>(path);
  }

  async listScaleEnrollmentCandidates(params: {
    workspaceSlug: string;
    metaMode: string;
  }): Promise<DomainResultFor<'list_scale_enrollment_candidates'>> {
    const path = buildBffPath('list_scale_enrollment_candidates', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.sendSafe<'list_scale_enrollment_candidates'>(path);
  }

  // ==== Scale commands ====

  async createScaleEnrollment(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_enrollment']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_enrollment'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_enrollment', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.runCommand(
      'create_scale_enrollment',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createScaleEnrollmentCandidate(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_enrollment_candidate']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_scale_enrollment_candidate'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_enrollment_candidate', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.runCommand(
      'create_scale_enrollment_candidate',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createScaleActivationPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    enrollmentId: string;
    body: DomainOperationMap['create_scale_activation_preview']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_activation_preview'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_activation_preview', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.runCommand(
      'create_scale_activation_preview',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async startLocalizationPreparation(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    batchId: string;
    body: DomainOperationMap['start_localization_preparation']['requestBody'];
  }): Promise<
    DomainResultFor<'start_localization_preparation'> | ReconciliationRequired
  > {
    return this.runCommand(
      'start_localization_preparation',
      params.actionInstanceId,
      buildBffPath('start_localization_preparation', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
      }),
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async activateScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['activate_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'activate_scale_enrollment'> | ReconciliationRequired
  > {
    const path = buildBffPath('activate_scale_enrollment', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.runCommand(
      'activate_scale_enrollment',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async pauseScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['pause_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'pause_scale_enrollment'> | ReconciliationRequired
  > {
    const path = buildBffPath('pause_scale_enrollment', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.runCommand(
      'pause_scale_enrollment',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async resumeScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['resume_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'resume_scale_enrollment'> | ReconciliationRequired
  > {
    const path = buildBffPath('resume_scale_enrollment', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.runCommand(
      'resume_scale_enrollment',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async closeScaleEnrollment(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      enrollmentId: string;
      body: DomainOperationMap['close_scale_enrollment']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'close_scale_enrollment'> | ReconciliationRequired
  > {
    const path = buildBffPath('close_scale_enrollment', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      enrollment_id: params.enrollmentId,
    });
    return this.runCommand(
      'close_scale_enrollment',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async createScaleAdviceDisposition(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    threadId: string;
    body: DomainOperationMap['create_scale_advice_disposition']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_advice_disposition'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_advice_disposition', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      thread_id: params.threadId,
    });
    return this.runCommand(
      'create_scale_advice_disposition',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createScaleAdviceSnooze(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    threadId: string;
    body: DomainOperationMap['create_scale_advice_snooze']['requestBody'];
  }): Promise<
    DomainResultFor<'create_scale_advice_snooze'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_advice_snooze', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      thread_id: params.threadId,
    });
    return this.runCommand(
      'create_scale_advice_snooze',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async convertScaleAdviceOccurrence(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    occurrenceId: string;
    body: DomainOperationMap['convert_scale_advice_occurrence']['requestBody'];
  }): Promise<
    DomainResultFor<'convert_scale_advice_occurrence'> | ReconciliationRequired
  > {
    const path = buildBffPath('convert_scale_advice_occurrence', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      occurrence_id: params.occurrenceId,
    });
    return this.runCommand(
      'convert_scale_advice_occurrence',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createScaleTargetPublicationPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_target_publication_preview']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_scale_target_publication_preview'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_target_publication_preview', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.runCommand(
      'create_scale_target_publication_preview',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async createScaleTarget(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      metaMode: string;
      body: DomainOperationMap['create_scale_target']['requestBody'];
    } & CommandTransport
  ): Promise<DomainResultFor<'create_scale_target'> | ReconciliationRequired> {
    const path = buildBffPath('create_scale_target', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.runCommand(
      'create_scale_target',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created',
      { ifMatch: params.ifMatch }
    );
  }

  async createScaleTargetWithdrawalPreview(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    targetId: string;
    body: DomainOperationMap['create_scale_target_withdrawal_preview']['requestBody'];
  }): Promise<
    | DomainResultFor<'create_scale_target_withdrawal_preview'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_target_withdrawal_preview', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      target_id: params.targetId,
    });
    return this.runCommand(
      'create_scale_target_withdrawal_preview',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async withdrawScaleTarget(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      targetId: string;
      metaMode: string;
      body: DomainOperationMap['withdraw_scale_target']['requestBody'];
    } & CommandTransport
  ): Promise<
    DomainResultFor<'withdraw_scale_target'> | ReconciliationRequired
  > {
    const path = buildBffPath('withdraw_scale_target', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      target_id: params.targetId,
    });
    return this.runCommand(
      'withdraw_scale_target',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async submitLocalizationClaimDecisions(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      body: DomainOperationMap['submit_localization_claim_decisions']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    | DomainResultFor<'submit_localization_claim_decisions'>
    | ReconciliationRequired
  > {
    return this.runCommand(
      'submit_localization_claim_decisions',
      params.actionInstanceId,
      buildBffPath('submit_localization_claim_decisions', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async approveLocalizationCopy(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      body: DomainOperationMap['approve_localization_copy']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'approve_localization_copy'> | ReconciliationRequired
  > {
    return this.runCommand(
      'approve_localization_copy',
      params.actionInstanceId,
      buildBffPath('approve_localization_copy', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async confirmLocalizationBatch(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      body: DomainOperationMap['confirm_localization_batch']['requestBody'];
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'confirm_localization_batch'> | ReconciliationRequired
  > {
    return this.runCommand(
      'confirm_localization_batch',
      params.actionInstanceId,
      buildBffPath('confirm_localization_batch', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
      }),
      'POST',
      params.body,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async retryLocalizationItem(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      itemId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'retry_localization_item'> | ReconciliationRequired
  > {
    return this.runCommand(
      'retry_localization_item',
      params.actionInstanceId,
      buildBffPath('retry_localization_item', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
        item_id: params.itemId,
      }),
      'POST',
      undefined,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async resumeLocalizationItem(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
      itemId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'resume_localization_item'> | ReconciliationRequired
  > {
    return this.runCommand(
      'resume_localization_item',
      params.actionInstanceId,
      buildBffPath('resume_localization_item', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
        item_id: params.itemId,
      }),
      'POST',
      undefined,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async authorizeLocalizationIsolatedQc(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    batchId: string;
    itemId: string;
    attemptId: string;
  }): Promise<
    | DomainResultFor<'authorize_localization_isolated_qc'>
    | ReconciliationRequired
  > {
    return this.runCommand(
      'authorize_localization_isolated_qc',
      params.actionInstanceId,
      buildBffPath('authorize_localization_isolated_qc', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
        item_id: params.itemId,
        attempt_id: params.attemptId,
      }),
      'POST',
      undefined,
      200,
      'ok'
    );
  }

  async adoptLocalizationIsolatedOutput(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    batchId: string;
    itemId: string;
    attemptId: string;
  }): Promise<
    | DomainResultFor<'adopt_localization_isolated_output'>
    | ReconciliationRequired
  > {
    return this.runCommand(
      'adopt_localization_isolated_output',
      params.actionInstanceId,
      buildBffPath('adopt_localization_isolated_output', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
        item_id: params.itemId,
        attempt_id: params.attemptId,
      }),
      'POST',
      undefined,
      200,
      'ok'
    );
  }

  async cancelLocalizationBatch(
    params: {
      workspaceSlug: string;
      actionInstanceId: string;
      batchId: string;
    } & ConditionalCommandTransport
  ): Promise<
    DomainResultFor<'cancel_localization_batch'> | ReconciliationRequired
  > {
    return this.runCommand(
      'cancel_localization_batch',
      params.actionInstanceId,
      buildBffPath('cancel_localization_batch', {
        slug: params.workspaceSlug,
        batch_id: params.batchId,
      }),
      'POST',
      undefined,
      200,
      'ok',
      { ifMatch: params.ifMatch }
    );
  }

  async createScaleExport(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    body: DomainOperationMap['create_scale_export']['requestBody'];
  }): Promise<DomainResultFor<'create_scale_export'> | ReconciliationRequired> {
    const path = buildBffPath('create_scale_export', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
    });
    return this.runCommand(
      'create_scale_export',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async createScaleExportDownloadIntent(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    metaMode: string;
    exportId: string;
  }): Promise<
    | DomainResultFor<'create_scale_export_download_intent'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('create_scale_export_download_intent', {
      slug: params.workspaceSlug,
      meta_mode: params.metaMode,
      export_id: params.exportId,
    });
    return this.runCommand(
      'create_scale_export_download_intent',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      201,
      'created'
    );
  }

  // ==== Owner credentials ====

  async listCredentials(params: {
    workspaceSlug: string;
  }): Promise<DomainResultFor<'list_credentials'>> {
    const path = buildBffPath('list_credentials', {
      slug: params.workspaceSlug,
    });
    return this.sendSafe<'list_credentials'>(path);
  }

  async createCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_credential']['requestBody'];
  }): Promise<DomainResultFor<'create_credential'> | ReconciliationRequired> {
    const path = buildBffPath('create_credential', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_credential',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async rotateCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    credentialKey: string;
    body: DomainOperationMap['rotate_credential']['requestBody'];
  }): Promise<DomainResultFor<'rotate_credential'> | ReconciliationRequired> {
    const path = buildBffPath('rotate_credential', {
      slug: params.workspaceSlug,
      credential_key: params.credentialKey,
    });
    return this.runCommand(
      'rotate_credential',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async bindCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    credentialVersionId: string;
    scope: string;
    body: DomainOperationMap['bind_credential']['requestBody'];
  }): Promise<DomainResultFor<'bind_credential'> | ReconciliationRequired> {
    const path = buildBffPath('bind_credential', {
      slug: params.workspaceSlug,
      credential_version_id: params.credentialVersionId,
      scope: params.scope,
    });
    return this.runCommand(
      'bind_credential',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok'
    );
  }

  async revokeCredential(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    credentialVersionId: string;
    body: DomainOperationMap['revoke_credential']['requestBody'];
  }): Promise<DomainResultFor<'revoke_credential'> | ReconciliationRequired> {
    const path = buildBffPath('revoke_credential', {
      slug: params.workspaceSlug,
      credential_version_id: params.credentialVersionId,
    });
    return this.runCommand(
      'revoke_credential',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  // ==== Competitors ====

  async listCompetitors(params: {
    workspaceSlug: string;
    productId?: string;
    status?: 'active' | 'archived' | 'all';
  }): Promise<DomainResultFor<'list_competitors'>> {
    const path =
      buildBffPath('list_competitors', { slug: params.workspaceSlug }) +
      this.buildQuery({ product_id: params.productId, status: params.status });
    return this.sendSafe<'list_competitors'>(path);
  }

  async getCompetitor(params: {
    workspaceSlug: string;
    competitorId: string;
  }): Promise<DomainResultFor<'get_competitor'>> {
    const path = buildBffPath('get_competitor', {
      slug: params.workspaceSlug,
      competitor_id: params.competitorId,
    });
    return this.sendSafe<'get_competitor'>(path);
  }

  async createCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_competitor']['requestBody'];
  }): Promise<DomainResultFor<'create_competitor'> | ReconciliationRequired> {
    const path = buildBffPath('create_competitor', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_competitor',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      201,
      'created'
    );
  }

  async quoteCompetitorResolve(params: {
    workspaceSlug: string;
    competitorId: string;
    executionMode?: 'byok' | 'mock';
    country?: string;
  }): Promise<DomainResultFor<'quote_competitor_resolve'>> {
    const path =
      buildBffPath('quote_competitor_resolve', {
        slug: params.workspaceSlug,
        competitor_id: params.competitorId,
      }) +
      this.buildQuery({
        execution_mode: params.executionMode,
        country: params.country,
      });
    return this.sendSafe<'quote_competitor_resolve'>(path);
  }

  async resolveCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    competitorId: string;
    body: DomainOperationMap['resolve_competitor']['requestBody'];
  }): Promise<DomainResultFor<'resolve_competitor'> | ReconciliationRequired> {
    const path = buildBffPath('resolve_competitor', {
      slug: params.workspaceSlug,
      competitor_id: params.competitorId,
    });
    return this.runCommand(
      'resolve_competitor',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async getCompetitorResolution(params: {
    workspaceSlug: string;
    competitorId: string;
    resolveJobId: string;
  }): Promise<DomainResultFor<'get_competitor_resolution'>> {
    const path = buildBffPath('get_competitor_resolution', {
      slug: params.workspaceSlug,
      competitor_id: params.competitorId,
      resolve_job_id: params.resolveJobId,
    });
    return this.sendSafe<'get_competitor_resolution'>(path);
  }

  async confirmCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    competitorId: string;
    body: DomainOperationMap['confirm_competitor']['requestBody'];
  }): Promise<DomainResultFor<'confirm_competitor'> | ReconciliationRequired> {
    const path = buildBffPath('confirm_competitor', {
      slug: params.workspaceSlug,
      competitor_id: params.competitorId,
    });
    return this.runCommand(
      'confirm_competitor',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async archiveCompetitor(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    competitorId: string;
  }): Promise<DomainResultFor<'archive_competitor'> | ReconciliationRequired> {
    const path = buildBffPath('archive_competitor', {
      slug: params.workspaceSlug,
      competitor_id: params.competitorId,
    });
    return this.runCommand(
      'archive_competitor',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      200,
      'ok'
    );
  }

  async previewCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['preview_competitor_scan']['requestBody'];
  }): Promise<
    DomainResultFor<'preview_competitor_scan'> | ReconciliationRequired
  > {
    const path = buildBffPath('preview_competitor_scan', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'preview_competitor_scan',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async createCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    body: DomainOperationMap['create_competitor_scan']['requestBody'];
  }): Promise<
    DomainResultFor<'create_competitor_scan'> | ReconciliationRequired
  > {
    const path = buildBffPath('create_competitor_scan', {
      slug: params.workspaceSlug,
    });
    return this.runCommand(
      'create_competitor_scan',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      202,
      'accepted'
    );
  }

  async listCompetitorScans(params: {
    workspaceSlug: string;
    productId?: string;
    status?: string;
  }): Promise<DomainResultFor<'list_competitor_scans'>> {
    const path =
      buildBffPath('list_competitor_scans', { slug: params.workspaceSlug }) +
      this.buildQuery({ product_id: params.productId, status: params.status });
    return this.sendSafe<'list_competitor_scans'>(path);
  }

  async getCompetitorScan(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'get_competitor_scan'>> {
    const path = buildBffPath('get_competitor_scan', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.sendSafe<'get_competitor_scan'>(path);
  }

  async quoteCompetitorScanResume(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'quote_competitor_scan_resume'>> {
    const path = buildBffPath('quote_competitor_scan_resume', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.sendSafe<'quote_competitor_scan_resume'>(path);
  }

  async resumeCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
  }): Promise<
    DomainResultFor<'resume_competitor_scan'> | ReconciliationRequired
  > {
    const path = buildBffPath('resume_competitor_scan', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.runCommand(
      'resume_competitor_scan',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      202,
      'accepted'
    );
  }

  async reconcileCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    body: DomainOperationMap['reconcile_competitor_scan']['requestBody'];
  }): Promise<
    DomainResultFor<'reconcile_competitor_scan'> | ReconciliationRequired
  > {
    const path = buildBffPath('reconcile_competitor_scan', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.runCommand(
      'reconcile_competitor_scan',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async listCompetitorScanAds(params: {
    workspaceSlug: string;
    scanId: string;
    competitorId?: string;
    role?: 'competitor' | 'self';
    originKind?: 'page' | 'keyword';
    minDaysRunning?: number;
    displayFormat?: string;
    relevance?: 'relevant' | 'adjacent' | 'unrelated' | 'unassessed';
    cursor?: string;
    limit?: number;
  }): Promise<DomainResultFor<'list_competitor_scan_ads'>> {
    const path =
      buildBffPath('list_competitor_scan_ads', {
        slug: params.workspaceSlug,
        scan_id: params.scanId,
      }) +
      this.buildQuery({
        competitor_id: params.competitorId,
        role: params.role,
        origin_kind: params.originKind,
        min_days_running: params.minDaysRunning,
        display_format: params.displayFormat,
        relevance: params.relevance,
        cursor: params.cursor,
        limit: params.limit,
      });
    return this.sendSafe<'list_competitor_scan_ads'>(path);
  }

  async flipCompetitorAdRelevance(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    adId: string;
    body: DomainOperationMap['flip_competitor_ad_relevance']['requestBody'];
  }): Promise<
    DomainResultFor<'flip_competitor_ad_relevance'> | ReconciliationRequired
  > {
    const path = buildBffPath('flip_competitor_ad_relevance', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
      ad_id: params.adId,
    });
    return this.runCommand(
      'flip_competitor_ad_relevance',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async listCompetitorAdAssessments(params: {
    workspaceSlug: string;
    scanId: string;
    adId: string;
  }): Promise<DomainResultFor<'list_competitor_ad_assessments'>> {
    const path = buildBffPath('list_competitor_ad_assessments', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
      ad_id: params.adId,
    });
    return this.sendSafe<'list_competitor_ad_assessments'>(path);
  }

  async confirmCompetitorAdTags(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    adId: string;
    body: DomainOperationMap['confirm_competitor_ad_tags']['requestBody'];
  }): Promise<
    DomainResultFor<'confirm_competitor_ad_tags'> | ReconciliationRequired
  > {
    const path = buildBffPath('confirm_competitor_ad_tags', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
      ad_id: params.adId,
    });
    return this.runCommand(
      'confirm_competitor_ad_tags',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }

  async listCompetitorAdTags(params: {
    workspaceSlug: string;
    scanId: string;
    adId: string;
  }): Promise<DomainResultFor<'list_competitor_ad_tags'>> {
    const path = buildBffPath('list_competitor_ad_tags', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
      ad_id: params.adId,
    });
    return this.sendSafe<'list_competitor_ad_tags'>(path);
  }

  async refreezeCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
  }): Promise<
    DomainResultFor<'refreeze_competitor_scan'> | ReconciliationRequired
  > {
    const path = buildBffPath('refreeze_competitor_scan', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.runCommand(
      'refreeze_competitor_scan',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      200,
      'ok'
    );
  }

  async getCompetitorScanSummary(params: {
    workspaceSlug: string;
    scanId: string;
    snapshotId?: string;
  }): Promise<DomainResultFor<'get_competitor_scan_summary'>> {
    const path =
      buildBffPath('get_competitor_scan_summary', {
        slug: params.workspaceSlug,
        scan_id: params.scanId,
      }) + this.buildQuery({ snapshot_id: params.snapshotId });
    return this.sendSafe<'get_competitor_scan_summary'>(path);
  }

  async quoteCompetitorScanRecluster(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'quote_competitor_scan_recluster'>> {
    const path = buildBffPath('quote_competitor_scan_recluster', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.sendSafe<'quote_competitor_scan_recluster'>(path);
  }

  async reclusterCompetitorScan(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
  }): Promise<
    DomainResultFor<'recluster_competitor_scan'> | ReconciliationRequired
  > {
    const path = buildBffPath('recluster_competitor_scan', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.runCommand(
      'recluster_competitor_scan',
      params.actionInstanceId,
      path,
      'POST',
      undefined,
      202,
      'accepted'
    );
  }

  async getProductCompetitorSettings(params: {
    workspaceSlug: string;
    productId: string;
  }): Promise<DomainResultFor<'get_product_competitor_settings'>> {
    const path = buildBffPath('get_product_competitor_settings', {
      slug: params.workspaceSlug,
      product_id: params.productId,
    });
    return this.sendSafe<'get_product_competitor_settings'>(path);
  }

  async updateProductCompetitorSettings(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    productId: string;
    body: DomainOperationMap['update_product_competitor_settings']['requestBody'];
  }): Promise<
    | DomainResultFor<'update_product_competitor_settings'>
    | ReconciliationRequired
  > {
    const path = buildBffPath('update_product_competitor_settings', {
      slug: params.workspaceSlug,
      product_id: params.productId,
    });
    return this.runCommand(
      'update_product_competitor_settings',
      params.actionInstanceId,
      path,
      'PUT',
      params.body,
      200,
      'ok'
    );
  }

  async listCompetitorScanDiscoveries(params: {
    workspaceSlug: string;
    scanId: string;
  }): Promise<DomainResultFor<'list_competitor_scan_discoveries'>> {
    const path = buildBffPath('list_competitor_scan_discoveries', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
    });
    return this.sendSafe<'list_competitor_scan_discoveries'>(path);
  }

  async decideCompetitorDiscovery(params: {
    workspaceSlug: string;
    actionInstanceId: string;
    scanId: string;
    advertiserPageId: string;
    body: DomainOperationMap['decide_competitor_discovery']['requestBody'];
  }): Promise<
    DomainResultFor<'decide_competitor_discovery'> | ReconciliationRequired
  > {
    const path = buildBffPath('decide_competitor_discovery', {
      slug: params.workspaceSlug,
      scan_id: params.scanId,
      advertiser_page_id: params.advertiserPageId,
    });
    return this.runCommand(
      'decide_competitor_discovery',
      params.actionInstanceId,
      path,
      'POST',
      params.body,
      200,
      'ok'
    );
  }
}
