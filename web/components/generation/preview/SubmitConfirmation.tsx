import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { errorMessage } from '@/lib/i18n/domain-errors';

type ActionEligibility = components['schemas']['ActionEligibility'];

interface SubmitConfirmationProps {
  previewId: string;
  previewHash: string;
  /** Revision frozen into the Preview and required as If-Match when the
   * atomic submission consumes it. */
  draftRevision: number;
  /** `GenerationPreviewDetail.valid_until` -- pass `''`/nullish when the
   * preview carries no expiry (mirrors `PreviewValidity`'s "no `valid_until`
   * means not expired" handling); `new Date('').getTime()` is `NaN`, and any
   * comparison against `NaN` is `false`, so an empty string never disables
   * the button through this check alone. */
  validUntil: string;
  /** `GenerationPreviewDetail.allowed_actions` (`run_queries.preview_actions`,
   * action id `"submit"`) -- when supplied, gates the button alongside the
   * client-side `validUntil` countdown. `ActionEligibility` is the
   * authoritative source (spec: "ActionEligibility drives UI eligibility");
   * `validUntil` only covers the one failure mode (TTL expiry) the client
   * can evaluate between refetches. Optional so this component stays usable
   * wherever a caller hasn't fetched `allowed_actions` (e.g. a test, or a
   * recovery-preview caller with no such field). */
  allowedActions?: ActionEligibility[];
  onSubmitted: () => void;
}

/**
 * Confirms and submits a frozen, still-valid `GenerationPreviewDetail` as a
 * GenerationRun (`submit_generation_run` -- `SubmitRunInput: { preview_id,
 * preview_hash }`, spec: `app/generation/v1_router.py::submit_run`). On
 * success the response is an `OperationRef { operation_id,
 * generation_run_id }` (Atomic Submission is itself async); this component
 * doesn't track that Operation, it hands the caller off to
 * `RunProgressView` via `navigateToRun`, which polls both the Run and its
 * latest command's Operation together (Task 8's `useRunProgress`).
 */
export function SubmitConfirmation({
  previewId,
  previewHash,
  draftRevision,
  validUntil,
  allowedActions,
  onSubmitted,
}: SubmitConfirmationProps) {
  const { t } = useTranslation('common');
  const { execute, isPending } = useDomainCommand('submit_generation_run');
  const { navigateToRun } = useGenerationNav();
  const [submitError, setSubmitError] = useState<string | null>(null);

  const isExpired = Date.now() >= new Date(validUntil).getTime();
  const submitAction = allowedActions?.find((a) => a.action === 'submit');
  const isBlockedByEligibility =
    allowedActions !== undefined && submitAction?.allowed !== true;
  const isDisabled = isExpired || isPending || isBlockedByEligibility;

  async function handleSubmit() {
    setSubmitError(null);
    const outcome = await execute({
      actionInstanceId: newActionId(),
      body: { preview_id: previewId, preview_hash: previewHash },
      ifMatch: draftRevision,
    });

    if (outcome.kind === 'success') {
      onSubmitted();
      navigateToRun(outcome.data.generation_run_id);
    } else if (outcome.kind === 'problem') {
      setSubmitError(errorMessage(t, outcome.problem));
    }
  }

  return (
    <div className="mt-4 border-t pt-4">
      {submitError && (
        <div className="alert alert-error mb-2 text-sm" role="alert">
          <span>{submitError}</span>
        </div>
      )}

      <button
        type="button"
        className="btn btn-primary w-full"
        onClick={handleSubmit}
        disabled={isDisabled}
      >
        {isPending
          ? t('generation-submit-run-submitting')
          : t('generation-submit-run-submit')}
      </button>

      {isExpired && (
        <p className="mt-1 text-xs text-error">
          {t('generation-submit-run-expired')}
        </p>
      )}

      {!isExpired && isBlockedByEligibility && (
        <p className="mt-1 text-xs text-error">
          {t('generation-submit-run-blocked', {
            reason:
              submitAction?.reason_code ?? t('generation-draft-reason-unknown'),
          })}
        </p>
      )}
    </div>
  );
}
