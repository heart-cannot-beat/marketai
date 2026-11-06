import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { useLibraryCommandImpact } from '@/hooks/library/useLibraryCommandImpact';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ArchiveDialogProps {
  entryId: string;
  open: boolean;
  onClose: () => void;
  onArchived: () => void;
}

export function ArchiveDialog({
  entryId,
  open,
  onClose,
  onArchived,
}: ArchiveDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [confirmed, setConfirmed] = useState(false);
  const archiveCmd = useLibraryCommand('archive');
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
  }, [open]);

  useEffect(() => {
    if (open && entryId) {
      impactHook.fetch('archive', entryId);
    }
    if (!open) {
      impactHook.clear();
      setConfirmed(false);
    }
  }, [open, entryId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null;

  const canArchive =
    confirmed &&
    impactHook.impact &&
    !impactHook.isStale &&
    impactHook.impact.blockingReasons.length === 0;

  const handleArchive = async () => {
    if (!impactHook.impact) return;

    const outcome = await archiveCmd.run({
      entry_id: entryId,
      ifMatch: impactHook.impact.targetRevision,
      body: {
        impact_id: impactHook.impact.impactId,
        impact_hash: impactHook.impact.impactHash,
      },
    });

    if (outcome.kind === 'success') {
      archiveCmd.reset();
      await invalidate({ resources: ['entry', 'entryList', 'events'] });
      onArchived();
    }
  };

  const handleClose = () => {
    setConfirmed(false);
    archiveCmd.reset();
    impactHook.clear();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="archive-title"
      aria-describedby="archive-description"
      onClose={handleClose}
    >
      <div className="modal-box max-w-lg">
        <h3
          id="archive-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('commands.archive-title')}
        </h3>

        <p
          id="archive-description"
          className="mt-2 text-sm text-gray-600 dark:text-gray-400"
        >
          {t('commands.archive-description')}
        </p>

        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-900/20">
          <p className="text-sm text-amber-800 dark:text-amber-300">
            {t('commands.archive-not-deletion')}
          </p>
        </div>

        {impactHook.isLoading && (
          <div className="mt-4" aria-busy="true">
            <p className="text-sm text-gray-500">
              {t('commands.loading-impact')}
            </p>
          </div>
        )}

        {impactHook.impact && impactHook.impact.blockingReasons.length > 0 && (
          <div
            className="mt-3 rounded border border-red-200 bg-red-50 p-3 dark:border-red-800 dark:bg-red-900/20"
            role="alert"
          >
            <ul className="list-disc pl-4 text-sm text-red-700 dark:text-red-400">
              {impactHook.impact.blockingReasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {impactHook.impact &&
          impactHook.impact.affectedSummaries.length > 0 && (
            <div className="mt-3">
              <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {t('commands.archive-affected', {
                  count: impactHook.impact.affectedSummaries.length,
                })}
              </p>
              <ul className="mt-1 space-y-1">
                {impactHook.impact.affectedSummaries.map((s) => (
                  <li
                    key={s.id}
                    className="text-sm text-gray-600 dark:text-gray-400"
                  >
                    {s.display_name} — {s.effect}
                  </li>
                ))}
              </ul>
            </div>
          )}

        <label className="mt-4 flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            className="checkbox checkbox-sm"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          <span className="text-sm text-gray-700 dark:text-gray-300">
            {t('commands.archive-confirm-checkbox')}
          </span>
        </label>

        {archiveCmd.problem && (
          <div
            className="mt-3 text-sm text-red-600 dark:text-red-400"
            role="alert"
          >
            {errorMessage(t, archiveCmd.problem)}
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
            disabled={!canArchive || archiveCmd.isPending}
            onClick={handleArchive}
          >
            {archiveCmd.isPending
              ? t('commands.archiving')
              : t('commands.confirm-archive')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('commands.cancel')}</button>
      </form>
    </dialog>
  );
}
