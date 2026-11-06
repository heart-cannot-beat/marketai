import { useTranslation } from 'next-i18next';
import { CostDisplay } from '@/components/generation/shared/CostDisplay';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type GenerationRecoveryPreview =
  components['schemas']['GenerationRecoveryPreview'];

interface RecoveryPreviewDisplayProps {
  preview: GenerationRecoveryPreview;
  onConfirm: () => void;
  onCancel: () => void;
  isSubmitting: boolean;
}

/** `GenerationRecoveryPreview.command` -> a translated, human label (spec:
 * `app/generation/response_models.py::RecoveryPreviewInput.command`, one of
 * `"retry_failed_items"` | `"resume_paused_items"`). */
const COMMAND_LABEL_KEYS: Record<string, string> = {
  retry_failed_items: 'generation-recovery-preview-command-retry',
  resume_paused_items: 'generation-recovery-preview-command-resume',
};

/**
 * Read-only display of a `GenerationRecoveryPreview` (`purpose:
 * 'run_recovery'`, `POST /runs/{id}/recovery-previews`) -- what a pending
 * `retry_failed_items`/`resume_paused_items` command would target and cost,
 * before the caller confirms it. Mirrors `preview/SubmitConfirmation.tsx`'s
 * expiry + `ActionEligibility` gating (`allowed_actions` action id
 * `"confirm"`, spec: `run_queries.recovery_preview_actions`) rather than
 * only the client-side `valid_until` countdown the task-11 brief sketched.
 */
export function RecoveryPreviewDisplay({
  preview,
  onConfirm,
  onCancel,
  isSubmitting,
}: RecoveryPreviewDisplayProps) {
  const { t } = useTranslation('common');

  const isExpired = Date.now() >= new Date(preview.valid_until).getTime();
  const confirmAction = preview.allowed_actions?.find(
    (a) => a.action === 'confirm'
  );
  const isBlockedByEligibility =
    preview.allowed_actions !== undefined && confirmAction?.allowed !== true;
  const targetCount = preview.targets?.length ?? 0;

  return (
    <div className="card bg-base-200 p-4 space-y-3">
      <h4 className="font-semibold">
        {t('generation-recovery-preview-heading')}
      </h4>
      <p className="text-sm">
        <strong>{t('generation-recovery-preview-action-label')}</strong>{' '}
        {t(COMMAND_LABEL_KEYS[preview.command] ?? preview.command)}
      </p>
      <p className="text-sm">
        <strong>{t('generation-recovery-preview-targets-label')}</strong>{' '}
        {t('generation-recovery-preview-targets-count', {
          count: targetCount,
        })}
      </p>
      <p className="text-sm">
        <strong>{t('generation-recovery-preview-cost-label')}</strong>{' '}
        <CostDisplay
          amount={preview.estimated_additional_cost.amount}
          currency={preview.estimated_additional_cost.currency}
        />
      </p>

      {isExpired && (
        <div className="alert alert-error text-sm" role="alert">
          {t('generation-recovery-preview-expired')}
        </div>
      )}
      {!isExpired && isBlockedByEligibility && (
        <div className="alert alert-warning text-sm" role="alert">
          {t('generation-recovery-preview-blocked', {
            reason:
              confirmAction?.reason_code ??
              t('generation-draft-reason-unknown'),
          })}
        </div>
      )}

      <div className="flex gap-2">
        <button type="button" className="btn btn-sm" onClick={onCancel}>
          {t('generation-recovery-preview-discard')}
        </button>
        <button
          type="button"
          className="btn btn-sm btn-primary"
          onClick={onConfirm}
          disabled={isExpired || isBlockedByEligibility || isSubmitting}
        >
          {isSubmitting
            ? t('generation-recovery-preview-confirming')
            : t('generation-recovery-preview-confirm')}
        </button>
      </div>
    </div>
  );
}
