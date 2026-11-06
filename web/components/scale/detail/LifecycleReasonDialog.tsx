import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface LifecycleReasonDialogProps {
  open: boolean;
  titleKey: string;
  confirmKey: string;
  problem?: { detail: string } | undefined;
  isPending: boolean;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

/** Shared reason-capture dialog for `pause`/`close` (spec §4.3) -- both
 * commands' request bodies are identical in shape (`{ meta_mode, reason }`),
 * differing only in title/confirm copy and which command executes. */
export function LifecycleReasonDialog({
  open,
  titleKey,
  confirmKey,
  problem,
  isPending,
  onConfirm,
  onCancel,
}: LifecycleReasonDialogProps) {
  const { t } = useTranslation('scale');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState('user_requested');

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
    if (!open) setReason('user_requested');
  }, [open]);

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="lifecycle-reason-title"
      onClose={onCancel}
    >
      <div className="modal-box max-w-md">
        <h3 id="lifecycle-reason-title" className="text-lg font-bold">
          {t(titleKey)}
        </h3>

        <label className="form-control mt-4">
          <span className="label-text text-xs">
            {t('lifecycle-dialog.reason-label')}
          </span>
          <input
            type="text"
            className="input input-bordered input-sm"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>

        {problem && (
          <div className="mt-3 text-sm text-error" role="alert">
            {errorMessage(t, problem)}
          </div>
        )}

        <div className="modal-action mt-6">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onCancel}
          >
            {t('lifecycle-dialog.cancel')}
          </button>
          <button
            type="button"
            className="btn btn-warning btn-sm"
            disabled={isPending}
            onClick={() => onConfirm(reason)}
          >
            {isPending ? t('lifecycle-dialog.pending') : t(confirmKey)}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('lifecycle-dialog.cancel')}</button>
      </form>
    </dialog>
  );
}
