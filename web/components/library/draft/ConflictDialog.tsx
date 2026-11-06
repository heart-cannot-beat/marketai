import { useTranslation } from 'next-i18next';
import { useEffect, useRef } from 'react';
import type { ConflictState } from '@/hooks/library/useDraftSession';

interface ConflictDialogProps {
  conflict: ConflictState;
  onReloadServer: () => void;
  onKeepLocal: () => void;
  onCancel: () => void;
}

export function ConflictDialog({
  conflict,
  onReloadServer,
  onKeepLocal,
  onCancel,
}: ConflictDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!dialog.open && typeof dialog.showModal === 'function') {
      try {
        dialog.showModal();
      } catch {
        // jsdom doesn't implement showModal
      }
    }
    cancelRef.current?.focus();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="conflict-title"
      aria-describedby="conflict-description"
      onClose={onCancel}
    >
      <div className="modal-box max-w-lg">
        <h3
          id="conflict-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('draft.conflict-title')}
        </h3>

        <p
          id="conflict-description"
          className="mt-2 text-sm text-gray-600 dark:text-gray-400"
        >
          {t('draft.conflict-description', {
            revision: String(conflict.serverRevision),
          })}
        </p>

        <div className="mt-4 space-y-2">
          <div className="rounded border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-800">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
              {t('draft.conflict-server-version')}
            </p>
            <p className="mt-1 text-sm text-gray-700 dark:text-gray-300">
              {t('draft.conflict-revision', {
                revision: String(conflict.serverRevision),
              })}
            </p>
          </div>
        </div>

        <div className="modal-action mt-6 flex flex-col gap-2 sm:flex-row sm:justify-end">
          <button
            ref={cancelRef}
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onCancel}
          >
            {t('draft.conflict-cancel')}
          </button>
          <button
            type="button"
            className="btn btn-outline btn-sm"
            onClick={onKeepLocal}
          >
            {t('draft.conflict-keep-local')}
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={onReloadServer}
          >
            {t('draft.conflict-reload-server')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('draft.conflict-cancel')}</button>
      </form>
    </dialog>
  );
}
