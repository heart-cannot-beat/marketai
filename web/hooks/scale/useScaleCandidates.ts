import { useCallback } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { CommandOutcome } from '@/lib/domain-client/result-types';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

/**
 * Server-computed Winner eligibility for new Enrollments (spec §4.4
 * candidate selection) -- confirmed Winner Decisions currently eligible for
 * enrollment that have no live Enrollment yet. Discovery only: a caller
 * still names the exact `decision_id`/`delivery_target_id` pair to
 * `useCreateScaleEnrollmentCandidate` below, which independently
 * re-validates the lineage before minting the candidate id
 * `useCreateScaleEnrollment` actually consumes.
 */
export function useScaleEnrollmentCandidates(metaMode: ScaleMetaMode) {
  return useDomainQuery('list_scale_enrollment_candidates', {
    meta_mode: metaMode,
  } as any);
}

/**
 * Proves a caller-named Winner Decision + DeliveryTarget pair is currently a
 * valid lineage and hands back a short-lived, narrow-scoped
 * `ScaleEnrollmentCandidateRecord` id (spec §4.4, Issue #86 Task 9) -- the
 * only field `useCreateScaleEnrollment` accepts.
 */
export function useCreateScaleEnrollmentCandidate() {
  const command = useDomainCommand('create_scale_enrollment_candidate');
  const run = useCallback(
    (params: {
      metaMode: ScaleMetaMode;
      winnerDecisionId: string;
      deliveryTargetId: string;
    }): Promise<CommandOutcome<'create_scale_enrollment_candidate'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: { meta_mode: params.metaMode } as any,
        body: {
          winner_decision_id: params.winnerDecisionId,
          delivery_target_id: params.deliveryTargetId,
        },
      }),
    [command]
  );
  return { ...command, run };
}
