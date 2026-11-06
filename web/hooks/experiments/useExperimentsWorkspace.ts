import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';

export type ExperimentSummary = components['schemas']['ExperimentSummary'];
export type ExperimentDetail = components['schemas']['ExperimentDetail'];
export type ExperimentProposal = components['schemas']['ExperimentProposal'];

/**
 * Generated-contract-backed query/command bundle for the Experiments page.
 * The selected id comes from the URL, so remounting or refreshing rehydrates
 * the same persisted detail, proposal and confirmation evidence.
 */
export function useExperimentsWorkspace(experimentId: string | null) {
  const list = useDomainPaginatedQuery<'list_experiments', ExperimentSummary>(
    'list_experiments',
    undefined,
    { pageSize: 50 }
  );

  const detail = useDomainQuery(
    'get_experiment',
    experimentId ? { experiment_id: experimentId } : undefined,
    { enabled: experimentId !== null, staleAfterMs: 15_000 }
  );

  return {
    list,
    detail,
    commands: {
      create: useDomainCommand('create_experiment'),
      addEntry: useDomainCommand('add_experiment_entry'),
      updateContext: useDomainCommand('update_experiment_context'),
      freeze: useDomainCommand('freeze_experiment'),
      bindCandidate: useDomainCommand('bind_experiment_candidate'),
      start: useDomainCommand('start_experiment'),
      judge: useDomainCommand('judge_experiment'),
      confirmProposal: useDomainCommand('confirm_experiment_proposal'),
      createLaunchPreview: useDomainCommand('create_meta_launch_preview'),
      confirmLaunchPreview: useDomainCommand('confirm_meta_launch_preview'),
      executeLaunchPreview: useDomainCommand('execute_meta_launch_preview'),
      recoverLaunchOperation: useDomainCommand('recover_meta_launch_operation'),
    },
  };
}

export function useAnalyzeDuplicates(experimentId: string | null) {
  return useDomainQuery(
    'analyze_experiment_duplicates',
    experimentId ? { experiment_id: experimentId } : undefined,
    { enabled: experimentId !== null, staleAfterMs: 10_000 }
  );
}

export function useExperimentEvidence(experimentId: string | null) {
  return useDomainQuery(
    'get_experiment_timeline',
    experimentId ? { experiment_id: experimentId } : undefined,
    { enabled: experimentId !== null, staleAfterMs: 10_000 }
  );
}

export function useMetaLaunchPreview(
  previewId: string | null,
  metaMode: string
) {
  return useDomainQuery(
    'get_meta_launch_preview',
    previewId ? { preview_id: previewId, meta_mode: metaMode } : undefined,
    { enabled: previewId !== null, staleAfterMs: 5_000 }
  );
}

export function useMetaLaunchProvisioning(
  provisioningId: string | null,
  metaMode: string
) {
  return useDomainQuery(
    'get_meta_launch_provisioning',
    provisioningId
      ? { provisioning_id: provisioningId, meta_mode: metaMode }
      : undefined,
    { enabled: provisioningId !== null, staleAfterMs: 3_000 }
  );
}
