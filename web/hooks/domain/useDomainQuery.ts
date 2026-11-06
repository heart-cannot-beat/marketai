import { useCallback, useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import type { DomainOperationMap } from '@/lib/domain-client/generated/operation-map';
import { domainQueryKey } from '@/lib/domain-client/query-keys';
import {
  TRANSIENT_RETRY_INTERVAL_MS,
  TRANSIENT_RETRY_LIMIT_MS,
  isTransientProblem,
} from '@/lib/domain-client/transient-problems';
import type {
  DomainResultFor,
  ProblemDetails,
  SuccessDataFor,
} from '@/lib/domain-client/result-types';

/**
 * Every read (`GET`) operation in `DomainOperationMap` (spec §6.3,
 * `BFF_ROUTES` in `generated/paths.ts`) — the only operations
 * `useDomainQuery` may be pointed at. The mutating operations (`POST`/
 * `PUT`/`PATCH`) go through `useDomainCommand` instead, which threads
 * `X-Intent-Id`. Of the Issue #18 operations (credentials, spend
 * guardrails, usage, cost) only `list_credentials` is wired so far — the
 * Settings page's provider-credential section reads it; the rest still
 * wait for their own surface.
 */
export type ReadOperationId =
  | 'get_identity_probe'
  | 'get_operation'
  | 'list_experiments'
  | 'get_experiment'
  | 'get_experiment_timeline'
  | 'analyze_experiment_duplicates'
  | 'get_meta_launch_preview'
  | 'get_meta_launch_provisioning'
  | 'list_meta_connections'
  | 'get_meta_connection'
  | 'list_library_entry_types'
  | 'list_library_entries'
  | 'get_library_entry'
  | 'list_library_entry_versions'
  | 'list_library_entry_events'
  | 'get_library_version'
  | 'get_library_version_impact'
  | 'list_library_version_bindings'
  | 'get_library_command_outcome'
  | 'get_library_internal_entry'
  | 'get_library_internal_entry_context'
  | 'list_library_internal_entries'
  | 'get_library_reference_object'
  | 'get_product_intake'
  | 'list_products'
  | 'get_product_references'
  | 'get_product_brand_kit'
  | 'list_marketing_brand_kits'
  | 'list_test_plans'
  | 'get_test_plan'
  | 'get_generation_defaults'
  | 'get_results_import'
  | 'get_product_learnings'
  | 'get_results_template'
  | 'get_test_plan_results'
  | 'list_external_creatives'
  | 'get_external_creative'
  | 'get_library_data_source_credential_binding'
  | 'list_credentials'
  | 'get_generation_capabilities'
  | 'list_generation_drafts'
  | 'get_generation_draft'
  | 'get_generation_preview'
  | 'list_generation_runs'
  | 'get_generation_run'
  | 'list_generation_run_items'
  | 'get_generation_run_item'
  | 'list_generation_assets'
  | 'get_generation_asset_version'
  | 'get_generation_export_package'
  | 'get_generation_export_manifest'
  | 'get_localization_workspace'
  | 'list_localization_batches'
  | 'get_localization_batch'
  | 'get_localization_item'
  | 'get_localization_batch_outcome'
  | 'get_scale_enrollment'
  | 'get_scale_episode'
  | 'get_scale_enrollment_evidence'
  | 'get_scale_enrollment_breakdowns'
  | 'get_scale_command_outcome'
  | 'get_scale_target'
  | 'list_scale_targets'
  | 'list_scale_target_contexts'
  | 'list_scale_enrollment_candidates'
  | 'list_scale_available_modes';

type QueryParameterShape<T> = [T] extends [never]
  ? object
  : T extends object
    ? { [P in keyof T as [T[P]] extends [never] ? never : P]: T[P] }
    : object;

type QueryParams<K extends ReadOperationId> = QueryParameterShape<
  DomainOperationMap[K]['pathParams']
> &
  QueryParameterShape<DomainOperationMap[K]['queryParams']>;

interface UseDomainQueryOptions {
  /** How long a successful fetch stays "fresh" client-side before `isStale`
   * flips to `true`. This tracks the age of the browser's cached copy, not
   * whether the domain server-side data itself has changed underneath it —
   * a caller that needs to know the latter uses `meta.etag`/`refresh()`. */
  staleAfterMs?: number;
  /** When `false`, no request is made and the hook returns its empty state. */
  enabled?: boolean;
}

interface UseDomainQueryResult<K extends ReadOperationId> {
  data: SuccessDataFor<K> | undefined;
  problem: ProblemDetails | undefined;
  isLoading: boolean;
  isStale: boolean;
  lastSuccessfulAt: number | null;
  refresh: () => Promise<void>;
}

/**
 * Dispatches a `ReadOperationId` to its `DomainGateway` method. A small,
 * explicit switch rather than `(gateway as any)[operationId](...)` — the
 * generated `DomainOperationMap` keys are snake_case (`get_identity_probe`)
 * but `DomainGateway`'s methods are camelCase (`getIdentityProbe`), so no
 * mechanical string-casing bridges the two; each read operation is wired by
 * hand instead, keeping full type safety on the gateway call.
 */
function callReadOperation<K extends ReadOperationId>(
  gateway: DomainGateway,
  operationId: K,
  workspaceSlug: string,
  params: QueryParams<K> | undefined
): Promise<DomainResultFor<K>> {
  switch (operationId) {
    case 'get_identity_probe':
      return gateway.getIdentityProbe({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'get_operation': {
      const operationParams =
        params as DomainOperationMap['get_operation']['pathParams'];
      return gateway.pollOperation({
        workspaceSlug,
        operationId: operationParams.operation_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_experiments': {
      const p = params as DomainOperationMap['list_experiments']['queryParams'];
      return gateway.listExperiments({
        workspaceSlug,
        state: p.state,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_experiment': {
      const p = params as DomainOperationMap['get_experiment']['pathParams'];
      return gateway.getExperiment({
        workspaceSlug,
        experimentId: p.experiment_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_experiment_timeline': {
      const p =
        params as DomainOperationMap['get_experiment_timeline']['pathParams'] &
          DomainOperationMap['get_experiment_timeline']['queryParams'];
      return gateway.getExperimentTimeline({
        workspaceSlug,
        experimentId: p.experiment_id,
        cursor: p.cursor !== undefined ? Number(p.cursor) : undefined,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'analyze_experiment_duplicates': {
      const p =
        params as DomainOperationMap['analyze_experiment_duplicates']['pathParams'];
      return gateway.analyzeExperimentDuplicates({
        workspaceSlug,
        experimentId: p.experiment_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_meta_launch_preview': {
      const p =
        params as DomainOperationMap['get_meta_launch_preview']['pathParams'] &
          DomainOperationMap['get_meta_launch_preview']['queryParams'];
      return gateway.getMetaLaunchPreview({
        workspaceSlug,
        previewId: p.preview_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_meta_launch_provisioning': {
      const p =
        params as DomainOperationMap['get_meta_launch_provisioning']['pathParams'] &
          DomainOperationMap['get_meta_launch_provisioning']['queryParams'];
      return gateway.getMetaLaunchProvisioning({
        workspaceSlug,
        provisioningId: p.provisioning_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_meta_connections': {
      const p =
        params as DomainOperationMap['list_meta_connections']['queryParams'];
      return gateway.listMetaConnections({
        workspaceSlug,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_meta_connection': {
      const p =
        params as DomainOperationMap['get_meta_connection']['pathParams'] &
          DomainOperationMap['get_meta_connection']['queryParams'];
      return gateway.getMetaConnection({
        workspaceSlug,
        connectionId: p.connection_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_entry_types':
      return gateway.listLibraryEntryTypes({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'list_library_entries': {
      const p =
        params as DomainOperationMap['list_library_entries']['queryParams'];
      return gateway.listLibraryEntries({
        workspaceSlug,
        entryType: p.entry_type,
        includeArchived:
          p.include_archived !== undefined
            ? p.include_archived === 'true'
            : undefined,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_entry': {
      const p = params as DomainOperationMap['get_library_entry']['pathParams'];
      return gateway.getLibraryEntry({
        workspaceSlug,
        entryId: p.entry_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_entry_versions': {
      const p =
        params as DomainOperationMap['list_library_entry_versions']['pathParams'] &
          DomainOperationMap['list_library_entry_versions']['queryParams'];
      return gateway.listLibraryEntryVersions({
        workspaceSlug,
        entryId: p.entry_id,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_entry_events': {
      const p =
        params as DomainOperationMap['list_library_entry_events']['pathParams'] &
          DomainOperationMap['list_library_entry_events']['queryParams'];
      return gateway.listLibraryEntryEvents({
        workspaceSlug,
        entryId: p.entry_id,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_version': {
      const p =
        params as DomainOperationMap['get_library_version']['pathParams'];
      return gateway.getLibraryVersion({
        workspaceSlug,
        versionId: p.version_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_version_impact': {
      const p =
        params as DomainOperationMap['get_library_version_impact']['pathParams'];
      return gateway.getLibraryVersionImpact({
        workspaceSlug,
        versionId: p.version_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_version_bindings': {
      const p =
        params as DomainOperationMap['list_library_version_bindings']['pathParams'] &
          DomainOperationMap['list_library_version_bindings']['queryParams'];
      return gateway.listLibraryVersionBindings({
        workspaceSlug,
        versionId: p.version_id,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_command_outcome': {
      const p =
        params as DomainOperationMap['get_library_command_outcome']['pathParams'] &
          DomainOperationMap['get_library_command_outcome']['queryParams'];
      return gateway.getLibraryCommandOutcome({
        workspaceSlug,
        commandIdentity: p.command_identity,
        commandKind: p.command_kind,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_internal_entry': {
      const p =
        params as DomainOperationMap['get_library_internal_entry']['pathParams'];
      return gateway.getLibraryInternalEntry({
        workspaceSlug,
        entryType: p.entry_type,
        entryId: p.entry_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_internal_entry_context': {
      const p =
        params as DomainOperationMap['get_library_internal_entry_context']['pathParams'];
      return gateway.getLibraryInternalEntryContext({
        workspaceSlug,
        rootEntryId: p.root_entry_id,
        entryType: p.entry_type,
        entryId: p.entry_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_library_internal_entries': {
      const p =
        params as DomainOperationMap['list_library_internal_entries']['pathParams'] &
          DomainOperationMap['list_library_internal_entries']['queryParams'];
      return gateway.listLibraryInternalEntries({
        workspaceSlug,
        rootEntryId: p.root_entry_id,
        entryType: p.entry_type,
        parentId: p.parent_id,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_reference_object': {
      const p =
        params as DomainOperationMap['get_library_reference_object']['pathParams'];
      return gateway.getLibraryReferenceObject({
        workspaceSlug,
        objectId: p.object_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_product_intake': {
      const p =
        params as DomainOperationMap['get_product_intake']['pathParams'];
      return gateway.getProductIntake({
        workspaceSlug,
        intakeId: p.intake_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_product_references': {
      const p =
        params as DomainOperationMap['get_product_references']['pathParams'];
      return gateway.getProductReferences({
        workspaceSlug,
        productId: p.product_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_product_brand_kit': {
      const p =
        params as DomainOperationMap['get_product_brand_kit']['pathParams'];
      return gateway.getProductBrandKit({
        workspaceSlug,
        productId: p.product_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_marketing_brand_kits':
      return gateway.listMarketingBrandKits({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'list_products':
      return gateway.listProducts({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'list_test_plans': {
      const p = params as DomainOperationMap['list_test_plans']['queryParams'];
      return gateway.listTestPlans({
        workspaceSlug,
        productId: p.product_id,
        runId: p.run_id ?? undefined,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_test_plan': {
      const p = params as DomainOperationMap['get_test_plan']['pathParams'];
      return gateway.getTestPlan({
        workspaceSlug,
        planId: p.plan_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_defaults':
      return gateway.getGenerationDefaults({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'get_results_import': {
      const p =
        params as DomainOperationMap['get_results_import']['pathParams'];
      return gateway.getResultsImport({
        workspaceSlug,
        importId: p.import_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_product_learnings': {
      const p =
        params as DomainOperationMap['get_product_learnings']['pathParams'] &
          DomainOperationMap['get_product_learnings']['queryParams'];
      return gateway.getProductLearnings({
        workspaceSlug,
        productId: p.product_id,
        evidenceMode: p.evidence_mode ?? undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_results_template': {
      const p =
        params as DomainOperationMap['get_results_template']['pathParams'];
      return gateway.getResultsTemplate({
        workspaceSlug,
        productId: p.product_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_test_plan_results': {
      const p =
        params as DomainOperationMap['get_test_plan_results']['pathParams'] &
          DomainOperationMap['get_test_plan_results']['queryParams'];
      return gateway.getTestPlanResults({
        workspaceSlug,
        planId: p.plan_id,
        evidenceMode: p.evidence_mode ?? undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_external_creatives': {
      const p =
        params as DomainOperationMap['list_external_creatives']['queryParams'];
      return gateway.listExternalCreatives({
        workspaceSlug,
        productId: p.product_id ?? undefined,
        status: p.status ?? undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_external_creative': {
      const p =
        params as DomainOperationMap['get_external_creative']['pathParams'];
      return gateway.getExternalCreative({
        workspaceSlug,
        externalCreativeId: p.external_creative_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_library_data_source_credential_binding': {
      const p =
        params as DomainOperationMap['get_library_data_source_credential_binding']['pathParams'];
      return gateway.getLibraryDataSourceCredentialBinding({
        workspaceSlug,
        dataSourceEntryId: p.data_source_entry_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_credentials':
      return gateway.listCredentials({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'get_generation_capabilities':
      return gateway.getGenerationCapabilities({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'list_generation_drafts': {
      const p =
        params as DomainOperationMap['list_generation_drafts']['queryParams'];
      return gateway.listGenerationDrafts({
        workspaceSlug,
        productId: p.product_id,
        state: p.state,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_draft': {
      const p =
        params as DomainOperationMap['get_generation_draft']['pathParams'];
      return gateway.getGenerationDraft({
        workspaceSlug,
        draftId: p.draft_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_preview': {
      const p =
        params as DomainOperationMap['get_generation_preview']['pathParams'];
      return gateway.getGenerationPreview({
        workspaceSlug,
        previewId: p.preview_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_generation_runs': {
      const p =
        params as DomainOperationMap['list_generation_runs']['queryParams'];
      return gateway.listGenerationRuns({
        workspaceSlug,
        productId: p.product_id,
        status: p.status,
        executionMode: p.execution_mode,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_run': {
      const p =
        params as DomainOperationMap['get_generation_run']['pathParams'];
      return gateway.getGenerationRun({
        workspaceSlug,
        runId: p.run_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_generation_run_items': {
      const p =
        params as DomainOperationMap['list_generation_run_items']['pathParams'] &
          DomainOperationMap['list_generation_run_items']['queryParams'];
      return gateway.listGenerationRunItems({
        workspaceSlug,
        runId: p.run_id,
        state: p.state,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_run_item': {
      const p =
        params as DomainOperationMap['get_generation_run_item']['pathParams'];
      return gateway.getGenerationRunItem({
        workspaceSlug,
        runId: p.run_id,
        runItemId: p.run_item_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_generation_assets': {
      const p =
        params as DomainOperationMap['list_generation_assets']['queryParams'];
      return gateway.listGenerationAssets({
        workspaceSlug,
        productId: p.product_id,
        runId: p.run_id,
        provider: p.provider,
        executionMode: p.execution_mode,
        originKind: p.origin_kind,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_asset_version': {
      const p =
        params as DomainOperationMap['get_generation_asset_version']['pathParams'];
      return gateway.getGenerationAssetVersion({
        workspaceSlug,
        versionId: p.version_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_export_package': {
      const p =
        params as DomainOperationMap['get_generation_export_package']['pathParams'];
      return gateway.getGenerationExportPackage({
        workspaceSlug,
        exportPackageId: p.export_package_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_generation_export_manifest': {
      const p =
        params as DomainOperationMap['get_generation_export_manifest']['pathParams'];
      return gateway.getGenerationExportManifest({
        workspaceSlug,
        exportPackageId: p.export_package_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_localization_workspace':
      return gateway.getLocalizationWorkspace({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    case 'list_localization_batches': {
      const p =
        params as DomainOperationMap['list_localization_batches']['queryParams'];
      return gateway.listLocalizationBatches({
        workspaceSlug,
        phase: p.phase,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_localization_batch': {
      const p =
        params as DomainOperationMap['get_localization_batch']['pathParams'];
      return gateway.getLocalizationBatch({
        workspaceSlug,
        batchId: p.batch_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_localization_item': {
      const p =
        params as DomainOperationMap['get_localization_item']['pathParams'];
      return gateway.getLocalizationItem({
        workspaceSlug,
        batchId: p.batch_id,
        itemId: p.item_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_localization_batch_outcome': {
      const p =
        params as DomainOperationMap['get_localization_batch_outcome']['pathParams'];
      return gateway.getLocalizationBatchOutcome({
        workspaceSlug,
        batchId: p.batch_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_scale_enrollment': {
      const p =
        params as DomainOperationMap['get_scale_enrollment']['pathParams'] &
          DomainOperationMap['get_scale_enrollment']['queryParams'];
      return gateway.getScaleEnrollment({
        workspaceSlug,
        enrollmentId: p.enrollment_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_scale_episode': {
      const p =
        params as DomainOperationMap['get_scale_episode']['pathParams'] &
          DomainOperationMap['get_scale_episode']['queryParams'];
      return gateway.getScaleEpisode({
        workspaceSlug,
        enrollmentId: p.enrollment_id,
        episodeId: p.episode_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_scale_enrollment_evidence': {
      const p =
        params as DomainOperationMap['get_scale_enrollment_evidence']['pathParams'] &
          DomainOperationMap['get_scale_enrollment_evidence']['queryParams'];
      return gateway.getScaleEnrollmentEvidence({
        workspaceSlug,
        enrollmentId: p.enrollment_id,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_scale_enrollment_breakdowns': {
      const p =
        params as DomainOperationMap['get_scale_enrollment_breakdowns']['pathParams'] &
          DomainOperationMap['get_scale_enrollment_breakdowns']['queryParams'];
      return gateway.getScaleEnrollmentBreakdowns({
        workspaceSlug,
        enrollmentId: p.enrollment_id,
        metaMode: p.meta_mode,
        snapshotId: p.snapshot_id,
        breakdownType: p.breakdown_type,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_scale_command_outcome': {
      const p =
        params as DomainOperationMap['get_scale_command_outcome']['pathParams'];
      return gateway.getScaleCommandOutcome({
        workspaceSlug,
        metaMode: p.meta_mode,
        identity: p.identity,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'get_scale_target': {
      const p = params as DomainOperationMap['get_scale_target']['pathParams'];
      return gateway.getScaleTarget({
        workspaceSlug,
        metaMode: p.meta_mode,
        targetId: p.target_id,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_targets': {
      const p =
        params as DomainOperationMap['list_scale_targets']['pathParams'] &
          DomainOperationMap['list_scale_targets']['queryParams'];
      return gateway.listScaleTargets({
        workspaceSlug,
        metaMode: p.meta_mode,
        lifecycleState: p.lifecycle_state,
        productVersionId: p.product_version_id,
        offerVersionId: p.offer_version_id,
        marketCode: p.market_code,
        cursor: p.cursor,
        limit: p.limit !== undefined ? Number(p.limit) : undefined,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_target_contexts': {
      const p =
        params as DomainOperationMap['list_scale_target_contexts']['pathParams'];
      return gateway.listScaleTargetContexts({
        workspaceSlug,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_enrollment_candidates': {
      const p =
        params as DomainOperationMap['list_scale_enrollment_candidates']['pathParams'];
      return gateway.listScaleEnrollmentCandidates({
        workspaceSlug,
        metaMode: p.meta_mode,
      }) as Promise<DomainResultFor<K>>;
    }
    case 'list_scale_available_modes':
      return gateway.listScaleAvailableModes({ workspaceSlug }) as Promise<
        DomainResultFor<K>
      >;
    default: {
      const exhaustiveCheck: never = operationId;
      throw new Error(
        `Unsupported read operationId: ${String(exhaustiveCheck)}`
      );
    }
  }
}

const isTransientResult = (
  result: { kind: string; problem?: ProblemDetails } | undefined
) => result?.kind === 'problem' && isTransientProblem(result.problem);

/**
 * Typed, SWR-backed read hook wrapping `DomainGateway` (spec §6.6). Every
 * call is scoped to `useWorkspace().current.slug` — the BFF re-derives
 * authorization from verified claims server-side, so this scoping is about
 * cache-key correctness (never fusing two Workspaces' data under one SWR
 * key), not a client-side authorization boundary.
 */
export function useDomainQuery<K extends ReadOperationId>(
  operationId: K,
  params?: QueryParams<K>,
  options: UseDomainQueryOptions = {}
): UseDomainQueryResult<K> {
  const { staleAfterMs = 30_000, enabled = true } = options;
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const [isStale, setIsStale] = useState(false);
  const staleTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined
  );

  const paramsKey = JSON.stringify(params ?? {});
  const cacheKey = enabled
    ? domainQueryKey(
        current.slug,
        operationId,
        params as Record<string, string | number | boolean | undefined>
      )
    : null;

  const fetcher = useCallback(
    () => callReadOperation(gateway, operationId, current.slug, params),
    // `paramsKey` (not `params`) keeps this stable across renders that pass
    // a structurally-identical-but-newly-allocated params object, matching
    // the SWR cache key above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [gateway, operationId, current.slug, paramsKey]
  );

  // A problem is data here, so SWR never retries it as an error. A transient
  // one (#309) polls every 5 seconds instead, for up to ten minutes from the
  // first; nothing else polls. SWR reads the interval when its polling starts
  // and after each poll, so the function changes with `transientSince` to
  // start polling on the first transient result.
  const [transientSince, setTransientSince] = useState<number | null>(null);
  const refreshInterval = useCallback(
    (latest?: DomainResultFor<K>) =>
      transientSince !== null &&
      isTransientResult(latest) &&
      Date.now() - transientSince < TRANSIENT_RETRY_LIMIT_MS
        ? TRANSIENT_RETRY_INTERVAL_MS
        : 0,
    [transientSince]
  );

  const {
    data: rawResult,
    isLoading: swrIsLoading,
    mutate,
  } = useSWR<DomainResultFor<K>>(cacheKey, fetcher, {
    revalidateOnFocus: true,
    refreshInterval,
  });

  useEffect(() => {
    const transient = isTransientResult(rawResult);
    setTransientSince((since) => (transient ? (since ?? Date.now()) : null));
  }, [rawResult]);

  useEffect(() => {
    if (rawResult && rawResult.kind !== 'problem') {
      setLastSuccessfulAt(Date.now());
      setIsStale(false);

      if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
      staleTimerRef.current = setTimeout(() => setIsStale(true), staleAfterMs);
    }
    return () => {
      if (staleTimerRef.current) clearTimeout(staleTimerRef.current);
    };
  }, [rawResult, staleAfterMs]);

  // `rawResult.kind !== 'problem'` doesn't narrow `DomainResultFor<K>` for a
  // generic `K` the way it would for a concrete operationId, since TS can't
  // discriminate a union parameterized by an unresolved type argument — the
  // cast below is the same narrowing `SuccessDataFor<K>` already encodes.
  const data: SuccessDataFor<K> | undefined =
    rawResult && rawResult.kind !== 'problem'
      ? (rawResult as Exclude<DomainResultFor<K>, { kind: 'problem' }>).data
      : undefined;

  const problem: ProblemDetails | undefined =
    rawResult && rawResult.kind === 'problem'
      ? (rawResult as { kind: 'problem'; problem: ProblemDetails }).problem
      : undefined;

  const refresh = useCallback(async () => {
    // Refreshing a transient problem starts its ten minutes again.
    setTransientSince((since) => (since === null ? null : Date.now()));
    await mutate();
  }, [mutate]);

  return {
    data,
    problem,
    isLoading: enabled && swrIsLoading,
    isStale,
    lastSuccessfulAt,
    refresh,
  };
}
