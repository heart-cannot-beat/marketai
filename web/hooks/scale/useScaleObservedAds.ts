import { useCallback } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { CommandOutcome } from '@/lib/domain-client/result-types';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

/**
 * The ads the Workspace's active connections in this Meta mode have observed
 * (#129): a sandbox connection's scenario ads, or the ads of an
 * internal-validation connection's published delivery read. A connection that
 * has observed nothing comes back in `unavailable` with its reason.
 */
export function useScaleObservedAds(metaMode: ScaleMetaMode, enabled = true) {
  return useDomainQuery(
    'list_scale_observed_ads',
    { meta_mode: metaMode },
    { enabled }
  );
}

/**
 * Makes a Winner's DeliveryTarget from one observed ad (#129). The server
 * resolves the creative, Offer, Landing Page, market, placement and
 * attribution from recorded evidence and refuses with a typed reason when
 * the ad does not match the Winner; the browser names only the ad.
 */
export function useCreateScaleDeliveryTarget() {
  const command = useDomainCommand('create_scale_delivery_target');
  const run = useCallback(
    (params: {
      metaMode: ScaleMetaMode;
      winnerDecisionId: string;
      metaConnectionId: string;
      adRemoteId: string;
    }): Promise<CommandOutcome<'create_scale_delivery_target'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: { meta_mode: params.metaMode },
        body: {
          winner_decision_id: params.winnerDecisionId,
          meta_connection_id: params.metaConnectionId,
          ad_remote_id: params.adRemoteId,
        },
      }),
    [command]
  );
  return { ...command, run };
}
