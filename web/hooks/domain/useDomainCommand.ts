import { useCallback, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import type { DomainOperationMap } from '@/lib/domain-client/generated/operation-map';
import type {
  BodyParam,
  CommandOutcome,
  DomainResultFor,
  ProblemDetails,
  SuccessDataFor,
} from '@/lib/domain-client/result-types';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';

/**
 * Every non-safe (`POST`/`PUT`/`PATCH`) operation in `DomainOperationMap`
 * (spec §6.3, `BFF_ROUTES` in `generated/paths.ts`) — the only operations
 * `useDomainCommand` may be pointed at. The read operations
 * (`get_identity_probe`, `get_operation`, and the rest) go through
 * `useDomainQuery` instead, which is uncached and never threads
 * `X-Intent-Id`. The Issue #18 operations (credentials, spend guardrails)
 * are wired: the Settings page's provider-credential section drives the four
 * credential commands, and its spend-guardrail section (#126) the guardrail's
 * preview and confirm.
 */
export type CommandOperationId =
  | 'create_upload_intent'
  | 'create_download_intent'
  | 'finalize_upload'
  | 'create_experiment'
  | 'add_experiment_entry'
  | 'update_experiment_context'
  | 'freeze_experiment'
  | 'bind_experiment_candidate'
  | 'start_experiment'
  | 'judge_experiment'
  | 'confirm_experiment_proposal'
  | 'create_meta_launch_preview'
  | 'confirm_meta_launch_preview'
  | 'execute_meta_launch_preview'
  | 'recover_meta_launch_operation'
  | 'create_meta_connection'
  | 'create_library_entry'
  | 'create_library_internal_entry'
  | 'save_library_draft'
  | 'validate_library_draft'
  | 'publish_library_entry'
  | 'archive_library_entry'
  | 'bind_library_reference'
  | 'withdraw_library_version'
  | 'fork_library_version'
  | 'clone_library_product'
  | 'apply_library_template'
  | 'create_library_reference_object'
  | 'create_library_reference_download_intent'
  | 'promote_library_reference_object'
  | 'create_product_intake'
  | 'confirm_product_intake'
  | 'create_product_intake_image_download_intent'
  | 'create_test_plan'
  | 'revise_test_plan'
  | 'create_test_plan_run'
  | 'update_generation_defaults'
  | 'create_results_import'
  | 'commit_results_import'
  | 'create_external_creatives'
  | 'confirm_external_creative'
  | 'reconcile_library_command_outcome'
  | 'preview_library_publish_impact'
  | 'preview_library_archive_impact'
  | 'preview_library_withdraw_impact'
  | 'preview_library_template_reapply'
  | 'bind_library_data_source_credential'
  | 'check_library_data_source_connection'
  | 'import_library_manual_facts'
  | 'create_generation_draft'
  | 'update_generation_draft'
  | 'create_generation_preview'
  | 'submit_generation_run'
  | 'create_generation_run_recovery_preview'
  | 'cancel_generation_run'
  | 'retry_generation_run_failed_items'
  | 'resume_generation_run_paused_items'
  | 'reconcile_generation_run'
  | 'create_generation_asset_review'
  | 'create_generation_regeneration_draft'
  | 'create_generation_asset_version_download_intent'
  | 'create_generation_export_package'
  | 'create_generation_export_download_intent'
  | 'create_localization_batch'
  | 'start_localization_preparation'
  | 'submit_localization_claim_decisions'
  | 'approve_localization_copy'
  | 'confirm_localization_batch'
  | 'retry_localization_item'
  | 'resume_localization_item'
  | 'authorize_localization_isolated_qc'
  | 'adopt_localization_isolated_output'
  | 'cancel_localization_batch'
  | 'create_scale_enrollment'
  | 'create_scale_enrollment_candidate'
  | 'create_scale_activation_preview'
  | 'activate_scale_enrollment'
  | 'pause_scale_enrollment'
  | 'resume_scale_enrollment'
  | 'close_scale_enrollment'
  | 'create_scale_advice_disposition'
  | 'create_scale_advice_snooze'
  | 'convert_scale_advice_occurrence'
  | 'create_scale_target_publication_preview'
  | 'create_scale_target'
  | 'create_scale_target_withdrawal_preview'
  | 'withdraw_scale_target'
  | 'create_scale_export'
  | 'create_scale_export_download_intent'
  | 'create_credential'
  | 'rotate_credential'
  | 'bind_credential'
  | 'revoke_credential'
  | 'preview_spend_guardrail'
  | 'confirm_spend_guardrail';

type CommandParams<K extends CommandOperationId> = {
  pathParams?: DomainOperationMap[K]['pathParams'];
  queryParams?: DomainOperationMap[K]['queryParams'];
  actionInstanceId: string;
  ifMatch?: string | number;
};

export type ExecuteParams<K extends CommandOperationId> = CommandParams<K> &
  BodyParam<K>;

export interface UseDomainCommandResult<K extends CommandOperationId> {
  execute: (params: ExecuteParams<K>) => Promise<CommandOutcome<K>>;
  /** Re-sends the last `execute()` call's params verbatim — same
   * `actionInstanceId`, so the `DomainGateway`/`IntentTracker` replay this
   * as the same idempotent intent rather than minting a new one. */
  retry: () => Promise<CommandOutcome<K>>;
  /** Clears local display state (`result`/`problem`/`reconciliation`) and
   * forgets the last params, so a stale `retry()` can no longer fire. This
   * never touches `IntentTracker`'s persisted state — an unresolved intent
   * (e.g. a retained, ambiguous outcome) is only cleared by the gateway
   * itself or `IntentTracker.dismissReconciliation()`. */
  reset: () => void;
  result: SuccessDataFor<K> | undefined;
  problem: ProblemDetails | undefined;
  reconciliation: ReconciliationRequired | undefined;
  isPending: boolean;
}

/**
 * Dispatches a `CommandOperationId` to its `DomainGateway` method. A small,
 * explicit switch rather than `(gateway as any)[operationId](...)` — the
 * generated `DomainOperationMap` keys are snake_case
 * (`create_upload_intent`) but `DomainGateway`'s methods are camelCase
 * (`createUploadIntent`), so no mechanical string-casing bridges the two
 * (mirrors `callReadOperation` in `useDomainQuery.ts`). `finalize_upload`
 * additionally takes its intent id as a path param
 * (`{ intent_id: string }`) rather than a request body, which
 * `DomainGateway.finalizeUpload` surfaces as `uploadIntentId`.
 */
function callCommandOperation<K extends CommandOperationId>(
  gateway: DomainGateway,
  operationId: K,
  workspaceSlug: string,
  actionInstanceId: string,
  body: DomainOperationMap[K]['requestBody'],
  pathParams: DomainOperationMap[K]['pathParams'] | undefined,
  transport: { ifMatch?: string | number }
): Promise<DomainResultFor<K> | ReconciliationRequired> {
  switch (operationId) {
    case 'create_upload_intent':
      return gateway.createUploadIntent({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_upload_intent']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_download_intent':
      return gateway.createDownloadIntent({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_download_intent']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'finalize_upload': {
      const finalizePathParams =
        pathParams as DomainOperationMap['finalize_upload']['pathParams'];
      return gateway.finalizeUpload({
        workspaceSlug,
        actionInstanceId,
        uploadIntentId: finalizePathParams.intent_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_experiment':
      return gateway.createExperiment({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_experiment']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'add_experiment_entry': {
      const p =
        pathParams as DomainOperationMap['add_experiment_entry']['pathParams'];
      return gateway.addExperimentEntry({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        body: body as DomainOperationMap['add_experiment_entry']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'update_experiment_context': {
      const p =
        pathParams as DomainOperationMap['update_experiment_context']['pathParams'];
      return gateway.updateExperimentContext({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        body: body as DomainOperationMap['update_experiment_context']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'freeze_experiment': {
      const p =
        pathParams as DomainOperationMap['freeze_experiment']['pathParams'];
      return gateway.freezeExperiment({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        body: body as DomainOperationMap['freeze_experiment']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'bind_experiment_candidate': {
      const p =
        pathParams as DomainOperationMap['bind_experiment_candidate']['pathParams'];
      return gateway.bindExperimentCandidate({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        candidateId: p.candidate_id,
        body: body as DomainOperationMap['bind_experiment_candidate']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'start_experiment': {
      const p =
        pathParams as DomainOperationMap['start_experiment']['pathParams'];
      return gateway.startExperiment({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        body: body as DomainOperationMap['start_experiment']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'judge_experiment': {
      const p =
        pathParams as DomainOperationMap['judge_experiment']['pathParams'];
      return gateway.judgeExperiment({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        body: body as DomainOperationMap['judge_experiment']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'confirm_experiment_proposal': {
      const p =
        pathParams as DomainOperationMap['confirm_experiment_proposal']['pathParams'];
      return gateway.confirmExperimentProposal({
        workspaceSlug,
        actionInstanceId,
        experimentId: p.experiment_id,
        proposalId: p.proposal_id,
        body: body as DomainOperationMap['confirm_experiment_proposal']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_meta_launch_preview':
      return gateway.createMetaLaunchPreview({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_meta_launch_preview']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_meta_connection':
      return gateway.createMetaConnection({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_meta_connection']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'confirm_meta_launch_preview': {
      const p =
        pathParams as DomainOperationMap['confirm_meta_launch_preview']['pathParams'];
      return gateway.confirmMetaLaunchPreview({
        workspaceSlug,
        actionInstanceId,
        previewId: p.preview_id,
        body: body as DomainOperationMap['confirm_meta_launch_preview']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'execute_meta_launch_preview': {
      const p =
        pathParams as DomainOperationMap['execute_meta_launch_preview']['pathParams'];
      return gateway.executeMetaLaunchPreview({
        workspaceSlug,
        actionInstanceId,
        previewId: p.preview_id,
        body: body as DomainOperationMap['execute_meta_launch_preview']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'recover_meta_launch_operation': {
      const p =
        pathParams as DomainOperationMap['recover_meta_launch_operation']['pathParams'];
      return gateway.recoverMetaLaunchOperation({
        workspaceSlug,
        actionInstanceId,
        operationId: p.operation_id,
        body: body as DomainOperationMap['recover_meta_launch_operation']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_library_entry':
      return gateway.createLibraryEntry({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_library_entry']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_library_internal_entry': {
      const p =
        pathParams as DomainOperationMap['create_library_internal_entry']['pathParams'];
      return gateway.createLibraryInternalEntry({
        workspaceSlug,
        actionInstanceId,
        rootEntryId: p.root_entry_id,
        body: body as DomainOperationMap['create_library_internal_entry']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'save_library_draft': {
      const p =
        pathParams as DomainOperationMap['save_library_draft']['pathParams'];
      return gateway.saveLibraryDraft({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
        body: body as DomainOperationMap['save_library_draft']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'validate_library_draft': {
      const p =
        pathParams as DomainOperationMap['validate_library_draft']['pathParams'];
      return gateway.validateLibraryDraft({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'publish_library_entry': {
      const p =
        pathParams as DomainOperationMap['publish_library_entry']['pathParams'];
      return gateway.publishLibraryEntry({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
        body: body as DomainOperationMap['publish_library_entry']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'archive_library_entry': {
      const p =
        pathParams as DomainOperationMap['archive_library_entry']['pathParams'];
      return gateway.archiveLibraryEntry({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
        body: body as DomainOperationMap['archive_library_entry']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'bind_library_reference': {
      const p =
        pathParams as DomainOperationMap['bind_library_reference']['pathParams'];
      return gateway.bindLibraryReference({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
        body: body as DomainOperationMap['bind_library_reference']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'withdraw_library_version': {
      const p =
        pathParams as DomainOperationMap['withdraw_library_version']['pathParams'];
      return gateway.withdrawLibraryVersion({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
        body: body as DomainOperationMap['withdraw_library_version']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'fork_library_version': {
      const p =
        pathParams as DomainOperationMap['fork_library_version']['pathParams'];
      return gateway.forkLibraryVersion({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
        body: body as DomainOperationMap['fork_library_version']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'clone_library_product': {
      const p =
        pathParams as DomainOperationMap['clone_library_product']['pathParams'];
      return gateway.cloneLibraryProduct({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
        body: body as DomainOperationMap['clone_library_product']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'apply_library_template': {
      const p =
        pathParams as DomainOperationMap['apply_library_template']['pathParams'];
      return gateway.applyLibraryTemplate({
        workspaceSlug,
        actionInstanceId,
        templateVersionId: p.template_version_id,
        body: body as DomainOperationMap['apply_library_template']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_library_reference_object':
      return gateway.createLibraryReferenceObject({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_library_reference_object']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_library_reference_download_intent': {
      const p =
        pathParams as DomainOperationMap['create_library_reference_download_intent']['pathParams'];
      return gateway.createLibraryReferenceDownloadIntent({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'promote_library_reference_object': {
      const p =
        pathParams as DomainOperationMap['promote_library_reference_object']['pathParams'];
      return gateway.promoteLibraryReferenceObject({
        workspaceSlug,
        actionInstanceId,
        objectId: p.object_id,
        body: body as DomainOperationMap['promote_library_reference_object']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_product_intake':
      return gateway.createProductIntake({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_product_intake']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'confirm_product_intake': {
      const p =
        pathParams as DomainOperationMap['confirm_product_intake']['pathParams'];
      return gateway.confirmProductIntake({
        workspaceSlug,
        actionInstanceId,
        intakeId: p.intake_id,
        body: body as DomainOperationMap['confirm_product_intake']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_product_intake_image_download_intent': {
      const p =
        pathParams as DomainOperationMap['create_product_intake_image_download_intent']['pathParams'];
      return gateway.createProductIntakeImageDownloadIntent({
        workspaceSlug,
        actionInstanceId,
        intakeId: p.intake_id,
        imageId: p.image_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_test_plan':
      return gateway.createTestPlan({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_test_plan']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'revise_test_plan': {
      const p =
        pathParams as DomainOperationMap['revise_test_plan']['pathParams'];
      return gateway.reviseTestPlan({
        workspaceSlug,
        actionInstanceId,
        planId: p.plan_id,
        body: body as DomainOperationMap['revise_test_plan']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_test_plan_run': {
      const p =
        pathParams as DomainOperationMap['create_test_plan_run']['pathParams'];
      return gateway.createTestPlanRun({
        workspaceSlug,
        actionInstanceId,
        planId: p.plan_id,
        body: body as DomainOperationMap['create_test_plan_run']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'update_generation_defaults':
      return gateway.updateGenerationDefaults({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['update_generation_defaults']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_results_import':
      return gateway.createResultsImport({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_results_import']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'commit_results_import': {
      const p =
        pathParams as DomainOperationMap['commit_results_import']['pathParams'];
      return gateway.commitResultsImport({
        workspaceSlug,
        actionInstanceId,
        importId: p.import_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_external_creatives':
      return gateway.createExternalCreatives({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_external_creatives']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'confirm_external_creative': {
      const p =
        pathParams as DomainOperationMap['confirm_external_creative']['pathParams'];
      return gateway.confirmExternalCreative({
        workspaceSlug,
        actionInstanceId,
        externalCreativeId: p.external_creative_id,
        body: body as DomainOperationMap['confirm_external_creative']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'reconcile_library_command_outcome': {
      const p =
        pathParams as DomainOperationMap['reconcile_library_command_outcome']['pathParams'];
      const q =
        pathParams as unknown as DomainOperationMap['reconcile_library_command_outcome']['queryParams'];
      return gateway.reconcileLibraryCommandOutcome({
        workspaceSlug,
        actionInstanceId,
        commandIdentity: p.command_identity,
        commandKind: q.command_kind,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'preview_library_publish_impact': {
      const p =
        pathParams as DomainOperationMap['preview_library_publish_impact']['pathParams'];
      return gateway.previewLibraryPublishImpact({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'preview_library_archive_impact': {
      const p =
        pathParams as DomainOperationMap['preview_library_archive_impact']['pathParams'];
      return gateway.previewLibraryArchiveImpact({
        workspaceSlug,
        actionInstanceId,
        entryId: p.entry_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'preview_library_withdraw_impact': {
      const p =
        pathParams as DomainOperationMap['preview_library_withdraw_impact']['pathParams'];
      return gateway.previewLibraryWithdrawImpact({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'preview_library_template_reapply': {
      const p =
        pathParams as DomainOperationMap['preview_library_template_reapply']['pathParams'];
      return gateway.previewLibraryTemplateReapply({
        workspaceSlug,
        actionInstanceId,
        templateVersionId: p.template_version_id,
        body: body as DomainOperationMap['preview_library_template_reapply']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'bind_library_data_source_credential': {
      const p =
        pathParams as DomainOperationMap['bind_library_data_source_credential']['pathParams'];
      return gateway.bindLibraryDataSourceCredential({
        workspaceSlug,
        actionInstanceId,
        dataSourceEntryId: p.data_source_entry_id,
        body: body as DomainOperationMap['bind_library_data_source_credential']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'check_library_data_source_connection': {
      const p =
        pathParams as DomainOperationMap['check_library_data_source_connection']['pathParams'];
      return gateway.checkLibraryDataSourceConnection({
        workspaceSlug,
        actionInstanceId,
        dataSourceEntryId: p.data_source_entry_id,
        body: body as DomainOperationMap['check_library_data_source_connection']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'import_library_manual_facts': {
      const p =
        pathParams as DomainOperationMap['import_library_manual_facts']['pathParams'];
      return gateway.importLibraryManualFacts({
        workspaceSlug,
        actionInstanceId,
        dataSourceEntryId: p.data_source_entry_id,
        body: body as DomainOperationMap['import_library_manual_facts']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_generation_draft':
      return gateway.createGenerationDraft({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_generation_draft']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'update_generation_draft': {
      const p =
        pathParams as DomainOperationMap['update_generation_draft']['pathParams'];
      return gateway.updateGenerationDraft({
        workspaceSlug,
        actionInstanceId,
        draftId: p.draft_id,
        body: body as DomainOperationMap['update_generation_draft']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_generation_preview': {
      const p =
        pathParams as DomainOperationMap['create_generation_preview']['pathParams'];
      return gateway.createGenerationPreview({
        workspaceSlug,
        actionInstanceId,
        draftId: p.draft_id,
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'submit_generation_run':
      return gateway.submitGenerationRun({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['submit_generation_run']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_generation_run_recovery_preview': {
      const p =
        pathParams as DomainOperationMap['create_generation_run_recovery_preview']['pathParams'];
      return gateway.createGenerationRunRecoveryPreview({
        workspaceSlug,
        actionInstanceId,
        runId: p.run_id,
        body: body as DomainOperationMap['create_generation_run_recovery_preview']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'cancel_generation_run': {
      const p =
        pathParams as DomainOperationMap['cancel_generation_run']['pathParams'];
      return gateway.cancelGenerationRun({
        workspaceSlug,
        actionInstanceId,
        runId: p.run_id,
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'retry_generation_run_failed_items': {
      const p =
        pathParams as DomainOperationMap['retry_generation_run_failed_items']['pathParams'];
      return gateway.retryGenerationRunFailedItems({
        workspaceSlug,
        actionInstanceId,
        runId: p.run_id,
        body: body as DomainOperationMap['retry_generation_run_failed_items']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'resume_generation_run_paused_items': {
      const p =
        pathParams as DomainOperationMap['resume_generation_run_paused_items']['pathParams'];
      return gateway.resumeGenerationRunPausedItems({
        workspaceSlug,
        actionInstanceId,
        runId: p.run_id,
        body: body as DomainOperationMap['resume_generation_run_paused_items']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'reconcile_generation_run': {
      const p =
        pathParams as DomainOperationMap['reconcile_generation_run']['pathParams'];
      return gateway.reconcileGenerationRun({
        workspaceSlug,
        actionInstanceId,
        runId: p.run_id,
        body: body as DomainOperationMap['reconcile_generation_run']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_generation_asset_review': {
      const p =
        pathParams as DomainOperationMap['create_generation_asset_review']['pathParams'];
      return gateway.createGenerationAssetReview({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
        body: body as DomainOperationMap['create_generation_asset_review']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_generation_regeneration_draft': {
      const p =
        pathParams as DomainOperationMap['create_generation_regeneration_draft']['pathParams'];
      return gateway.createGenerationRegenerationDraft({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
        body: body as DomainOperationMap['create_generation_regeneration_draft']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_generation_asset_version_download_intent': {
      const p =
        pathParams as DomainOperationMap['create_generation_asset_version_download_intent']['pathParams'];
      return gateway.createGenerationAssetVersionDownloadIntent({
        workspaceSlug,
        actionInstanceId,
        versionId: p.version_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_generation_export_package':
      return gateway.createGenerationExportPackage({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_generation_export_package']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'create_generation_export_download_intent': {
      const p =
        pathParams as DomainOperationMap['create_generation_export_download_intent']['pathParams'];
      return gateway.createGenerationExportDownloadIntent({
        workspaceSlug,
        actionInstanceId,
        exportPackageId: p.export_package_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_localization_batch':
      return gateway.createLocalizationBatch({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_localization_batch']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'start_localization_preparation': {
      const p =
        pathParams as DomainOperationMap['start_localization_preparation']['pathParams'];
      return gateway.startLocalizationPreparation({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        body: body as DomainOperationMap['start_localization_preparation']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'submit_localization_claim_decisions': {
      const p =
        pathParams as DomainOperationMap['submit_localization_claim_decisions']['pathParams'];
      return gateway.submitLocalizationClaimDecisions({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        body: body as DomainOperationMap['submit_localization_claim_decisions']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'approve_localization_copy': {
      const p =
        pathParams as DomainOperationMap['approve_localization_copy']['pathParams'];
      return gateway.approveLocalizationCopy({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        body: body as DomainOperationMap['approve_localization_copy']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'confirm_localization_batch': {
      const p =
        pathParams as DomainOperationMap['confirm_localization_batch']['pathParams'];
      return gateway.confirmLocalizationBatch({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        body: body as DomainOperationMap['confirm_localization_batch']['requestBody'],
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'retry_localization_item': {
      const p =
        pathParams as DomainOperationMap['retry_localization_item']['pathParams'];
      return gateway.retryLocalizationItem({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        itemId: p.item_id,
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'resume_localization_item': {
      const p =
        pathParams as DomainOperationMap['resume_localization_item']['pathParams'];
      return gateway.resumeLocalizationItem({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        itemId: p.item_id,
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'authorize_localization_isolated_qc': {
      const p =
        pathParams as DomainOperationMap['authorize_localization_isolated_qc']['pathParams'];
      return gateway.authorizeLocalizationIsolatedQc({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        itemId: p.item_id,
        attemptId: p.attempt_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'adopt_localization_isolated_output': {
      const p =
        pathParams as DomainOperationMap['adopt_localization_isolated_output']['pathParams'];
      return gateway.adoptLocalizationIsolatedOutput({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        itemId: p.item_id,
        attemptId: p.attempt_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'cancel_localization_batch': {
      const p =
        pathParams as DomainOperationMap['cancel_localization_batch']['pathParams'];
      return gateway.cancelLocalizationBatch({
        workspaceSlug,
        actionInstanceId,
        batchId: p.batch_id,
        ...transport,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_enrollment': {
      const p =
        pathParams as DomainOperationMap['create_scale_enrollment']['pathParams'];
      return gateway.createScaleEnrollment({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        body: body as DomainOperationMap['create_scale_enrollment']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_enrollment_candidate': {
      const p =
        pathParams as DomainOperationMap['create_scale_enrollment_candidate']['pathParams'];
      return gateway.createScaleEnrollmentCandidate({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        body: body as DomainOperationMap['create_scale_enrollment_candidate']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_activation_preview': {
      const p =
        pathParams as DomainOperationMap['create_scale_activation_preview']['pathParams'];
      return gateway.createScaleActivationPreview({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        enrollmentId: p.enrollment_id,
        body: body as DomainOperationMap['create_scale_activation_preview']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'activate_scale_enrollment': {
      const p =
        pathParams as DomainOperationMap['activate_scale_enrollment']['pathParams'];
      return gateway.activateScaleEnrollment({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        enrollmentId: p.enrollment_id,
        body: body as DomainOperationMap['activate_scale_enrollment']['requestBody'],
        ifMatch: transport.ifMatch,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'pause_scale_enrollment': {
      const p =
        pathParams as DomainOperationMap['pause_scale_enrollment']['pathParams'];
      return gateway.pauseScaleEnrollment({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        enrollmentId: p.enrollment_id,
        body: body as DomainOperationMap['pause_scale_enrollment']['requestBody'],
        ifMatch: transport.ifMatch,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'resume_scale_enrollment': {
      const p =
        pathParams as DomainOperationMap['resume_scale_enrollment']['pathParams'];
      return gateway.resumeScaleEnrollment({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        enrollmentId: p.enrollment_id,
        body: body as DomainOperationMap['resume_scale_enrollment']['requestBody'],
        ifMatch: transport.ifMatch,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'close_scale_enrollment': {
      const p =
        pathParams as DomainOperationMap['close_scale_enrollment']['pathParams'];
      return gateway.closeScaleEnrollment({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        enrollmentId: p.enrollment_id,
        body: body as DomainOperationMap['close_scale_enrollment']['requestBody'],
        ifMatch: transport.ifMatch,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_advice_disposition': {
      const p =
        pathParams as DomainOperationMap['create_scale_advice_disposition']['pathParams'];
      return gateway.createScaleAdviceDisposition({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        threadId: p.thread_id,
        body: body as DomainOperationMap['create_scale_advice_disposition']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_advice_snooze': {
      const p =
        pathParams as DomainOperationMap['create_scale_advice_snooze']['pathParams'];
      return gateway.createScaleAdviceSnooze({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        threadId: p.thread_id,
        body: body as DomainOperationMap['create_scale_advice_snooze']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'convert_scale_advice_occurrence': {
      const p =
        pathParams as DomainOperationMap['convert_scale_advice_occurrence']['pathParams'];
      return gateway.convertScaleAdviceOccurrence({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        occurrenceId: p.occurrence_id,
        body: body as DomainOperationMap['convert_scale_advice_occurrence']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_target_publication_preview': {
      const p =
        pathParams as DomainOperationMap['create_scale_target_publication_preview']['pathParams'];
      return gateway.createScaleTargetPublicationPreview({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        body: body as DomainOperationMap['create_scale_target_publication_preview']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_target': {
      const p =
        pathParams as DomainOperationMap['create_scale_target']['pathParams'];
      return gateway.createScaleTarget({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        body: body as DomainOperationMap['create_scale_target']['requestBody'],
        ifMatch: transport.ifMatch,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_target_withdrawal_preview': {
      const p =
        pathParams as DomainOperationMap['create_scale_target_withdrawal_preview']['pathParams'];
      return gateway.createScaleTargetWithdrawalPreview({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        targetId: p.target_id,
        body: body as DomainOperationMap['create_scale_target_withdrawal_preview']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'withdraw_scale_target': {
      const p =
        pathParams as DomainOperationMap['withdraw_scale_target']['pathParams'];
      return gateway.withdrawScaleTarget({
        workspaceSlug,
        actionInstanceId,
        targetId: p.target_id,
        metaMode: p.meta_mode,
        body: body as DomainOperationMap['withdraw_scale_target']['requestBody'],
        ifMatch: transport.ifMatch,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_export': {
      const p =
        pathParams as DomainOperationMap['create_scale_export']['pathParams'];
      return gateway.createScaleExport({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        body: body as DomainOperationMap['create_scale_export']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_scale_export_download_intent': {
      const p =
        pathParams as DomainOperationMap['create_scale_export_download_intent']['pathParams'];
      return gateway.createScaleExportDownloadIntent({
        workspaceSlug,
        actionInstanceId,
        metaMode: p.meta_mode,
        exportId: p.export_id,
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'create_credential': {
      return gateway.createCredential({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['create_credential']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'rotate_credential': {
      const p =
        pathParams as DomainOperationMap['rotate_credential']['pathParams'];
      return gateway.rotateCredential({
        workspaceSlug,
        actionInstanceId,
        credentialKey: p.credential_key,
        body: body as DomainOperationMap['rotate_credential']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'bind_credential': {
      const p =
        pathParams as DomainOperationMap['bind_credential']['pathParams'];
      return gateway.bindCredential({
        workspaceSlug,
        actionInstanceId,
        credentialVersionId: p.credential_version_id,
        scope: p.scope,
        body: body as DomainOperationMap['bind_credential']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'revoke_credential': {
      const p =
        pathParams as DomainOperationMap['revoke_credential']['pathParams'];
      return gateway.revokeCredential({
        workspaceSlug,
        actionInstanceId,
        credentialVersionId: p.credential_version_id,
        body: body as DomainOperationMap['revoke_credential']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    }
    case 'preview_spend_guardrail':
      return gateway.previewSpendGuardrail({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['preview_spend_guardrail']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    case 'confirm_spend_guardrail':
      return gateway.confirmSpendGuardrail({
        workspaceSlug,
        actionInstanceId,
        body: body as DomainOperationMap['confirm_spend_guardrail']['requestBody'],
      }) as Promise<DomainResultFor<K> | ReconciliationRequired>;
    default: {
      const exhaustiveCheck: never = operationId;
      throw new Error(
        `Unsupported command operationId: ${String(exhaustiveCheck)}`
      );
    }
  }
}

/**
 * Typed mutation hook wrapping `DomainGateway`'s three non-safe operations
 * (spec §6.10). Every call is scoped to `useWorkspace().current.slug` — the
 * BFF re-derives authorization from verified claims server-side, so this
 * scoping is about routing the request to the right Workspace, not a
 * client-side authorization boundary.
 *
 * `IntentTracker.acquire()`/`resolve()`/`retain()` all live inside
 * `DomainGateway` itself (spec §6.10) — this hook only threads the caller's
 * `actionInstanceId` through, and surfaces the three possible outcomes
 * (`result`, `problem`, `reconciliation`) as separate, mutually exclusive
 * pieces of state. A thrown error (network failure with no `Response` at
 * all) is left unhandled here and propagates to the caller — the gateway
 * has already `retain()`-ed the intent as ambiguous before rethrowing, so
 * this hook's job is just to not swallow that signal.
 */
export function useDomainCommand<K extends CommandOperationId>(
  operationId: K
): UseDomainCommandResult<K> {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();

  const [result, setResult] = useState<SuccessDataFor<K>>();
  const [problem, setProblem] = useState<ProblemDetails>();
  const [reconciliation, setReconciliation] =
    useState<ReconciliationRequired>();
  const [isPending, setIsPending] = useState(false);
  const lastParamsRef = useRef<ExecuteParams<K> | null>(null);

  const execute = useCallback(
    async (params: ExecuteParams<K>): Promise<CommandOutcome<K>> => {
      lastParamsRef.current = params;
      setResult(undefined);
      setProblem(undefined);
      setReconciliation(undefined);
      setIsPending(true);

      try {
        const response = await callCommandOperation(
          gateway,
          operationId,
          current.slug,
          params.actionInstanceId,
          (params as BodyParam<K>).body as DomainOperationMap[K]['requestBody'],
          params.pathParams,
          { ifMatch: params.ifMatch }
        );

        const meta =
          'meta' in response
            ? (
                response as {
                  meta: {
                    etag: string | null;
                    retryAt: number | null;
                    location: string | null;
                  };
                }
              ).meta
            : { etag: null, retryAt: null, location: null };

        if (response.kind === 'reconciliation_required') {
          const recon = response as ReconciliationRequired;
          setReconciliation(recon);
          return {
            kind: 'reconciliation',
            actionInstanceId: recon.actionInstanceId,
            staleSince: recon.staleSince,
          };
        }

        if (response.kind === 'problem') {
          const p = (response as { kind: 'problem'; problem: ProblemDetails })
            .problem;
          setProblem(p);
          return { kind: 'problem', problem: p, meta };
        }

        const data = (
          response as Exclude<DomainResultFor<K>, { kind: 'problem' }>
        ).data;
        setResult(data);
        return { kind: 'success', data, meta };
      } finally {
        setIsPending(false);
      }
    },
    [gateway, operationId, current.slug]
  );

  const retry = useCallback(async (): Promise<CommandOutcome<K>> => {
    if (!lastParamsRef.current) {
      return {
        kind: 'problem',
        problem: {
          type: 'client_error',
          title: 'No previous command',
          status: 0,
          detail: 'retry() called before execute()',
          instance: '',
          code: 'no_previous_command',
          trace_id: '',
          retryable: false,
          field_errors: [],
        },
        meta: { etag: null, retryAt: null, location: null },
      };
    }
    return execute(lastParamsRef.current);
  }, [execute]);

  const reset = useCallback(() => {
    setResult(undefined);
    setProblem(undefined);
    setReconciliation(undefined);
    lastParamsRef.current = null;
  }, []);

  return { execute, retry, reset, result, problem, reconciliation, isPending };
}
