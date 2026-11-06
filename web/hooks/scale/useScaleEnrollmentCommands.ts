import { useCallback } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { CommandOutcome } from '@/lib/domain-client/result-types';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

/** Creates a `draft` Enrollment (spec §4.1/§4.3 step 1) -- unlike the four
 * lifecycle transitions below, this is unconditional (no prior revision to
 * race against) and does not require `allowed_actions` gating: any actor
 * permitted to call the BFF route may draft an Enrollment.
 *
 * Issue #86 Task 9: the only field a caller supplies is the id of an
 * already-proven `ScaleEnrollmentCandidateRecord` (from
 * `useCreateScaleEnrollmentCandidate`/`useScaleEnrollmentCandidates`) --
 * every raw Winner/DeliveryTarget identifier a pre-#86 client sent directly
 * is rejected server-side as an unknown field. */
export function useCreateScaleEnrollment() {
  const command = useDomainCommand('create_scale_enrollment');
  const run = useCallback(
    (params: {
      metaMode: ScaleMetaMode;
      enrollmentCandidateId: string;
    }): Promise<CommandOutcome<'create_scale_enrollment'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: { meta_mode: params.metaMode } as any,
        body: { enrollment_candidate_id: params.enrollmentCandidateId },
      }),
    [command]
  );
  return { ...command, run };
}

/**
 * Pre-activation evidence snapshot + lineage-guard preview (spec §4.5,
 * Issue #86 Task 10) -- the required first step of both `activate` and
 * `resume` below. Its response `id` is the only field either transition
 * accepts; the preview itself carries the delivery-evidence snapshot and
 * lineage-guard binding the server computed, so the browser never
 * hand-assembles `delivery_config`/`episode_context` again.
 */
export function useCreateScaleActivationPreview() {
  const command = useDomainCommand('create_scale_activation_preview');
  const run = useCallback(
    (params: {
      enrollmentId: string;
      metaMode: ScaleMetaMode;
      expectedRevision: number;
    }): Promise<CommandOutcome<'create_scale_activation_preview'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          enrollment_id: params.enrollmentId,
          meta_mode: params.metaMode,
        } as any,
        body: { expected_revision: params.expectedRevision },
      }),
    [command]
  );
  return { ...command, run };
}

/**
 * Enrollment lifecycle commands (spec §4.3): `activate`/`pause`/`resume`/
 * `close`. All four are conditional writes -- the caller must pass the
 * Enrollment's current `revision` (from `EnrollmentResponse.revision`) as
 * `ifMatch`; the server 409s a stale write rather than silently overwriting
 * a lifecycle transition the browser hasn't seen yet.
 *
 * Whether a given transition is legal from the current lifecycle is decided
 * entirely by the server's `allowed_actions` on the Enrollment -- this hook
 * never reconstructs that eligibility client-side, it only executes what
 * the caller (gated by `allowed_actions`) already decided to attempt.
 *
 * Issue #86 Task 11: `activate`/`resume` accept only `activationPreviewId`
 * (from `useCreateScaleActivationPreview`) -- the pre-hardening
 * `deliveryConfig`/`episodeContext` shape no longer exists; a caller cannot
 * bypass the preview protocol even for `resume`, which will end up
 * continuing an already-open Episode untouched.
 */
export function useActivateScaleEnrollment() {
  const command = useDomainCommand('activate_scale_enrollment');
  const run = useCallback(
    (params: {
      enrollmentId: string;
      revision: number;
      metaMode: ScaleMetaMode;
      activationPreviewId: string;
    }): Promise<CommandOutcome<'activate_scale_enrollment'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          enrollment_id: params.enrollmentId,
          meta_mode: params.metaMode,
        } as any,
        ifMatch: params.revision,
        body: { activation_preview_id: params.activationPreviewId },
      }),
    [command]
  );
  return { ...command, run };
}

export function usePauseScaleEnrollment() {
  const command = useDomainCommand('pause_scale_enrollment');
  const run = useCallback(
    (params: {
      enrollmentId: string;
      revision: number;
      metaMode: ScaleMetaMode;
      reason?: string;
    }): Promise<CommandOutcome<'pause_scale_enrollment'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          enrollment_id: params.enrollmentId,
          meta_mode: params.metaMode,
        } as any,
        ifMatch: params.revision,
        body: { reason: params.reason ?? 'user_requested' },
      }),
    [command]
  );
  return { ...command, run };
}

export function useResumeScaleEnrollment() {
  const command = useDomainCommand('resume_scale_enrollment');
  const run = useCallback(
    (params: {
      enrollmentId: string;
      revision: number;
      metaMode: ScaleMetaMode;
      activationPreviewId: string;
    }): Promise<CommandOutcome<'resume_scale_enrollment'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          enrollment_id: params.enrollmentId,
          meta_mode: params.metaMode,
        } as any,
        ifMatch: params.revision,
        body: { activation_preview_id: params.activationPreviewId },
      }),
    [command]
  );
  return { ...command, run };
}

export function useCloseScaleEnrollment() {
  const command = useDomainCommand('close_scale_enrollment');
  const run = useCallback(
    (params: {
      enrollmentId: string;
      revision: number;
      metaMode: ScaleMetaMode;
      reason?: string;
    }): Promise<CommandOutcome<'close_scale_enrollment'>> =>
      command.execute({
        actionInstanceId: newActionId(),
        pathParams: {
          enrollment_id: params.enrollmentId,
          meta_mode: params.metaMode,
        } as any,
        ifMatch: params.revision,
        body: { reason: params.reason ?? 'user_requested' },
      }),
    [command]
  );
  return { ...command, run };
}
