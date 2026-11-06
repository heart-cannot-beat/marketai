import { useTranslation } from 'next-i18next';

interface CancelConfirmationProps {
  /** Whether the Run may still have RunItems in flight when cancelled --
   * `cancel_generation_run` (spec: `app/generation/run_commands.py`) routes
   * those to `uncertain` rather than reporting them as cancelled, so the
   * caller sees a warning before confirming. */
  hasInFlightItems: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  isSubmitting: boolean;
}

/** Interstitial confirmation before `cancel_generation_run` fires -- a
 * destructive action once a Run is active, so this never calls the command
 * directly, only surfaces the caller's `onConfirm`. */
export function CancelConfirmation({
  hasInFlightItems,
  onConfirm,
  onCancel,
  isSubmitting,
}: CancelConfirmationProps) {
  const { t } = useTranslation('common');

  return (
    <div className="card bg-base-200 p-4 space-y-3">
      <h4 className="font-semibold">
        {t('generation-recovery-cancel-confirm-heading')}
      </h4>
      {hasInFlightItems && (
        <div className="alert alert-warning text-sm" role="alert">
          {t('generation-recovery-cancel-confirm-warning')}
        </div>
      )}
      <div className="flex gap-2">
        <button type="button" className="btn btn-sm" onClick={onCancel}>
          {t('generation-recovery-cancel-confirm-keep-running')}
        </button>
        <button
          type="button"
          className="btn btn-sm btn-error"
          onClick={onConfirm}
          disabled={isSubmitting}
        >
          {isSubmitting
            ? t('generation-recovery-cancel-confirm-cancelling')
            : t('generation-recovery-cancel-confirm-submit')}
        </button>
      </div>
    </div>
  );
}
