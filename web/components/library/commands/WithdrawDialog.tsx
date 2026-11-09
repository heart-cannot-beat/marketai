import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import {
  useLibraryCommandImpact,
  type ImpactProjection,
} from '@/hooks/library/useLibraryCommandImpact';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { FindingText } from '@/components/library/FindingText';

interface WithdrawDialogProps {
  versionId: string;
  open: boolean;
  onClose: () => void;
  onWithdrawn: () => void;
}

export function WithdrawDialog({
  versionId,
  open,
  onClose,
  onWithdrawn,
}: WithdrawDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const [reason, setReason] = useState('');
  const withdrawCmd = useLibraryCommand('withdraw');
  const { invalidate } = useLibraryCache();
  const impactHook = useLibraryCommandImpact();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open && typeof dialog.showModal === 'function') {
      try {
        dialog.showModal();
      } catch {
        // jsdom
      }
    }
    if (open) {
      reasonRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (open && versionId) {
      impactHook.fetch('withdraw', versionId);
    }
    if (!open) {
      impactHook.clear();
    }
  }, [open, versionId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null;

  const canConfirm =
    reason.trim().length > 0 &&
    impactHook.impact &&
    !impactHook.isStale &&
    impactHook.impact.blocking.length === 0;

  const handleWithdraw = async () => {
    if (!impactHook.impact || !reason.trim()) return;

    const outcome = await withdrawCmd.run({
      version_id: versionId,
      body: {
        reason: reason.trim(),
        impact_id: impactHook.impact.impactId,
        impact_hash: impactHook.impact.impactHash,
      },
    });

    if (outcome.kind === 'success') {
      withdrawCmd.reset();
      await invalidate({
        resources: [
          'entry',
          'entryList',
          'versions',
          'events',
          'impact',
          'bindings',
        ],
      });
      setReason('');
      onWithdrawn();
    }
  };

  const handleClose = () => {
    setReason('');
    withdrawCmd.reset();
    impactHook.clear();
    onClose();
  };

  return (
    <>
      <dialog
        ref={dialogRef}
        className="modal modal-open"
        aria-labelledby="withdraw-title"
        onClose={handleClose}
      >
        <div className="modal-box max-w-lg">
          <h3
            id="withdraw-title"
            className="text-lg font-bold text-gray-900 dark:text-gray-100"
          >
            {t('commands.withdraw-title')}
          </h3>

          <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
            {t('commands.withdraw-description')}
          </p>

          {impactHook.isLoading && (
            <div className="mt-4" aria-busy="true">
              <p className="text-sm text-gray-500">
                {t('commands.loading-impact')}
              </p>
            </div>
          )}

          {impactHook.problem && (
            <div
              className="mt-4 text-sm text-red-600 dark:text-red-400"
              role="alert"
            >
              {errorMessage(t, impactHook.problem)}
            </div>
          )}

          {impactHook.impact && <WithdrawImpact impact={impactHook.impact} />}

          {impactHook.impact && impactHook.impact.blocking.length > 0 && (
            <div
              className="mt-3 rounded border border-red-200 bg-red-50 p-3 dark:border-red-800 dark:bg-red-900/20"
              role="alert"
            >
              <ul className="list-disc pl-4 text-sm text-red-700 dark:text-red-400">
                {impactHook.impact.blocking.map((finding, i) => (
                  <li key={`${finding.code}-${i}`}>
                    <FindingText finding={finding} />
                  </li>
                ))}
              </ul>
            </div>
          )}

          <label className="form-control mt-4 w-full">
            <div className="label">
              <span className="label-text">
                {t('commands.withdraw-reason-label')}
              </span>
            </div>
            <textarea
              ref={reasonRef}
              className="textarea textarea-bordered w-full"
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              required
            />
          </label>

          {withdrawCmd.problem && (
            <div
              className="mt-3 text-sm text-red-600 dark:text-red-400"
              role="alert"
            >
              {errorMessage(t, withdrawCmd.problem)}
            </div>
          )}

          <div className="modal-action mt-6">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={handleClose}
            >
              {t('commands.cancel')}
            </button>
            <button
              type="button"
              className="btn btn-warning btn-sm"
              disabled={!canConfirm || withdrawCmd.isPending}
              onClick={handleWithdraw}
            >
              {withdrawCmd.isPending
                ? t('commands.withdrawing')
                : t('commands.confirm-withdraw')}
            </button>
          </div>
        </div>
        <form method="dialog" className="modal-backdrop">
          <button type="submit">{t('commands.cancel')}</button>
        </form>
      </dialog>
    </>
  );
}

/**
 * What withdrawing this version does (`preview_withdraw_impact`): whether the
 * entry loses its effective version, and which published versions depend on
 * it and are newly blocked from new use (contract §14).
 */
function WithdrawImpact({ impact }: { impact: ImpactProjection }) {
  const { t } = useTranslation('library');
  const dependents = impact.affectedSummaries;
  const newlyBlocked = dependents.filter((d) => d.will_block).length;

  return (
    <section
      className="mt-4 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200"
      aria-labelledby="withdraw-impact-title"
      data-testid="withdraw-impact"
    >
      <h4 id="withdraw-impact-title" className="font-medium">
        {t('commands.withdraw-impact-title')}
      </h4>
      {impact.clearsCurrentVersion && (
        <p className="mt-1">{t('commands.withdraw-clears-current')}</p>
      )}
      {dependents.length === 0 ? (
        <p className="mt-1">{t('commands.withdraw-no-dependents')}</p>
      ) : (
        <>
          <p className="mt-1">
            {t('commands.withdraw-dependents', {
              count: dependents.length,
              blocked: newlyBlocked,
            })}
          </p>
          <ul className="mt-1 list-disc pl-4">
            {dependents.map((dependent) => (
              <li key={dependent.id}>
                {t(
                  dependent.will_block
                    ? 'commands.withdraw-dependent-blocks'
                    : 'commands.withdraw-dependent-blocked',
                  {
                    type: t(`entry-type.${dependent.entry_type}`, {
                      defaultValue: t('commands.withdraw-dependent-other'),
                    }),
                    no: String(dependent.version_no ?? ''),
                  }
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
