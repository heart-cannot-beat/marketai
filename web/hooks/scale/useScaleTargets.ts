import { useCallback } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import {
  useDomainPaginatedQuery,
  type UseDomainPaginatedQueryResult,
} from '@/hooks/domain/useDomainPaginatedQuery';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { CommandOutcome } from '@/lib/domain-client/result-types';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

export type ScaleTarget = components['schemas']['TargetResponse'];
export type ScaleTargetPublicationPreview =
  components['schemas']['TargetPublicationPreviewResponse'];
export type ScaleTargetWithdrawalPreview =
  components['schemas']['TargetWithdrawalPreviewResponse'];

/** One context the publish flow offers, with the platform floor a Target
 * published into it is checked against (#134). */
export type ScaleTargetContext = components['schemas']['TargetContextResponse'];

/**
 * Target lifecycle (spec §10, §21, Issue #87 Tasks 2/6/7): server-driven
 * context discovery, paginated list, publication preview/confirm, and
 * withdrawal preview/confirm. Publish and withdraw both require a
 * two-phase preview -> confirm -- there is no direct one-step
 * create/withdraw anymore.
 */
export function useScaleTargets(
  metaMode: ScaleMetaMode,
  filters?: { lifecycleState?: string }
): UseDomainPaginatedQueryResult<ScaleTarget> {
  return useDomainPaginatedQuery<'list_scale_targets', ScaleTarget>(
    'list_scale_targets',
    {
      meta_mode: metaMode,
      lifecycle_state: filters?.lifecycleState,
    } as any
  );
}

export function useScaleTarget(
  targetId: string | undefined,
  metaMode: ScaleMetaMode
) {
  return useDomainQuery(
    'get_scale_target',
    targetId
      ? ({ target_id: targetId, meta_mode: metaMode } as any)
      : undefined,
    { enabled: !!targetId }
  );
}

/** Every context with an active/paused Enrollment and an observed currency
 * (spec §10) -- the publish flow's context selector reads this rather than
 * free-typing a product/offer/market/currency combination. */
export function useScaleTargetContexts(metaMode: ScaleMetaMode) {
  const query = useDomainQuery('list_scale_target_contexts', {
    meta_mode: metaMode,
  } as any);
  const contexts: ScaleTargetContext[] = query.data?.items ?? [];
  return { ...query, contexts };
}

export function useCreateScaleTargetPublicationPreview() {
  const command = useDomainCommand('create_scale_target_publication_preview');
  const run = useCallback(
    (params: {
      metaMode: ScaleMetaMode;
      productVersionId: string;
      offerVersionId: string;
      marketCode: string;
      currency: string;
      targetCpa?: string;
      breakevenRoas?: string;
      minimumEconomicSpend?: string;
    }): Promise<CommandOutcome<'create_scale_target_publication_preview'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: { meta_mode: params.metaMode } as any,
        body: {
          product_version_id: params.productVersionId,
          offer_version_id: params.offerVersionId,
          market_code: params.marketCode,
          currency: params.currency,
          target_cpa: params.targetCpa || null,
          breakeven_roas: params.breakevenRoas || null,
          minimum_economic_spend: params.minimumEconomicSpend || null,
        },
      }),
    [command]
  );
  return { ...command, run };
}

/** Confirms a publication preview -- Owner only (spec §10). */
export function usePublishScaleTarget() {
  const command = useDomainCommand('create_scale_target');
  const run = useCallback(
    (params: {
      metaMode: ScaleMetaMode;
      publicationPreviewId: string;
    }): Promise<CommandOutcome<'create_scale_target'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: { meta_mode: params.metaMode } as any,
        body: { publication_preview_id: params.publicationPreviewId },
      }),
    [command]
  );
  return { ...command, run };
}

export function useCreateScaleTargetWithdrawalPreview() {
  const command = useDomainCommand('create_scale_target_withdrawal_preview');
  const run = useCallback(
    (params: {
      targetId: string;
      metaMode: ScaleMetaMode;
      reason: string;
    }): Promise<CommandOutcome<'create_scale_target_withdrawal_preview'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          target_id: params.targetId,
          meta_mode: params.metaMode,
        } as any,
        body: { reason: params.reason },
      }),
    [command]
  );
  return { ...command, run };
}

/** Confirms a withdrawal preview -- Owner only, like publishing. */
export function useWithdrawScaleTarget() {
  const command = useDomainCommand('withdraw_scale_target');
  const run = useCallback(
    (params: {
      targetId: string;
      metaMode: ScaleMetaMode;
      withdrawalPreviewId: string;
    }): Promise<CommandOutcome<'withdraw_scale_target'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          target_id: params.targetId,
          meta_mode: params.metaMode,
        } as any,
        body: { withdrawal_preview_id: params.withdrawalPreviewId },
      }),
    [command]
  );
  return { ...command, run };
}
