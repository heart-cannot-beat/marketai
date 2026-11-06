import { useState } from 'react';
import { useTranslation } from 'next-i18next';

interface ReconciliationGuidanceProps {
  /** Approximate count of RunItems in the `uncertain` state (spec:
   * `run_queries.run_actions`'s `has_uncertain` check) -- `undefined` when
   * the caller couldn't derive it from the `GenerationRunDetail` it has on
   * hand, in which case the guidance text stays generic. */
  uncertainCount?: number;
  /** `ActionEligibility` for the `"reconcile"` action (spec: "ActionEligibility
   * drives UI eligibility") -- when `false`, the note field and submit button
   * stay disabled and `reasonCode` explains why. */
  isAllowed: boolean;
  reasonCode?: string | null;
  onReconcile: (operatorNote: string) => void;
  isSubmitting: boolean;
}

/**
 * Collects the operator note `reconcile_generation_run` requires
 * (`ReconcileRunInput.operator_note`, spec: `app/generation/v1_router.py`)
 * for RunItems left in the `uncertain` state after a worker interruption --
 * a crash-ambiguous provider call that could not be resolved automatically.
 */
export function ReconciliationGuidance({
  uncertainCount,
  isAllowed,
  reasonCode,
  onReconcile,
  isSubmitting,
}: ReconciliationGuidanceProps) {
  const { t } = useTranslation('common');
  const [note, setNote] = useState('');

  return (
    <div className="card bg-warning/10 p-4 space-y-3">
      <h4 className="font-semibold text-warning">
        {t('generation-recovery-reconcile-heading')}
      </h4>
      <p className="text-sm">
        {uncertainCount !== undefined
          ? t('generation-recovery-reconcile-description-count', {
              count: uncertainCount,
            })
          : t('generation-recovery-reconcile-description')}
      </p>
      {!isAllowed && (
        <p className="text-xs text-warning">
          {t('generation-recovery-reconcile-blocked', {
            reason: reasonCode ?? t('generation-draft-reason-unknown'),
          })}
        </p>
      )}
      <textarea
        className="textarea textarea-bordered w-full"
        placeholder={t('generation-recovery-reconcile-note-placeholder')}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={3}
        disabled={!isAllowed || isSubmitting}
      />
      <button
        type="button"
        className="btn btn-sm btn-warning"
        onClick={() => onReconcile(note)}
        disabled={!isAllowed || !note.trim() || isSubmitting}
      >
        {isSubmitting
          ? t('generation-recovery-reconcile-submitting')
          : t('generation-recovery-reconcile-submit')}
      </button>
    </div>
  );
}
