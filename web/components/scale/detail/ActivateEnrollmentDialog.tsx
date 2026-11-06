import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import {
  useCreateScaleActivationPreview,
  useActivateScaleEnrollment,
} from '@/hooks/scale/useScaleEnrollmentCommands';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ActivateEnrollmentDialogProps {
  open: boolean;
  enrollmentId: string;
  revision: number;
  metaMode: ScaleMetaMode;
  onClose: () => void;
  onActivated: () => void;
}

/**
 * Two-step activation: create a server-side preview (spec §4.5, Issue #86
 * Task 10) that freezes lineage + delivery evidence, then confirm to
 * activate with only the `activation_preview_id` (Task 11). The 23-field
 * manual delivery-config/episode-context form this replaces is gone --
 * the server derives every field from its own evidence pipeline.
 */
export function ActivateEnrollmentDialog({
  open,
  enrollmentId,
  revision,
  metaMode,
  onClose,
  onActivated,
}: ActivateEnrollmentDialogProps) {
  const { t } = useTranslation('scale');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const previewCmd = useCreateScaleActivationPreview();
  const activateCmd = useActivateScaleEnrollment();

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
    if (!open) {
      setPreviewId(null);
      previewCmd.reset();
      activateCmd.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const isPending = previewCmd.isPending || activateCmd.isPending;
  const problem = activateCmd.problem ?? previewCmd.problem;

  const handlePreview = async () => {
    const outcome = await previewCmd.run({
      enrollmentId,
      metaMode,
      expectedRevision: revision,
    });
    if (outcome.kind === 'success') {
      setPreviewId(outcome.data.id);
    }
  };

  const handleConfirm = async () => {
    if (!previewId) return;
    const outcome = await activateCmd.run({
      enrollmentId,
      revision,
      metaMode,
      activationPreviewId: previewId,
    });
    if (outcome.kind === 'success') {
      onActivated();
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="activate-dialog-title"
      onClose={onClose}
    >
      <div className="modal-box max-w-lg">
        <h3 id="activate-dialog-title" className="text-lg font-bold">
          {t('activate-dialog.title')}
        </h3>

        {!previewId ? (
          <>
            <p className="mt-2 text-sm text-base-content/70">
              {t('activate-dialog.preview-description')}
            </p>
            {problem && (
              <div className="mt-3 text-sm text-error" role="alert">
                {errorMessage(t, problem)}
              </div>
            )}
            <div className="modal-action mt-6">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={onClose}
              >
                {t('activate-dialog.cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={isPending}
                onClick={handlePreview}
              >
                {previewCmd.isPending
                  ? t('activate-dialog.previewing')
                  : t('activate-dialog.preview')}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="mt-2 text-sm text-base-content/70">
              {t('activate-dialog.confirm-description')}
            </p>
            <div className="mt-3 rounded-lg bg-base-200 p-3 text-sm">
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                <dt className="text-base-content/60">
                  {t('activate-dialog.preview-id')}
                </dt>
                <dd className="font-mono text-xs break-all">{previewId}</dd>
              </dl>
            </div>
            {problem && (
              <div className="mt-3 text-sm text-error" role="alert">
                {errorMessage(t, problem)}
              </div>
            )}
            {activateCmd.reconciliation && (
              <div className="mt-3 text-sm text-warning" role="alert">
                {t('activate-dialog.reconciliation-required')}
              </div>
            )}
            <div className="modal-action mt-6">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={onClose}
              >
                {t('activate-dialog.cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={isPending}
                onClick={handleConfirm}
              >
                {activateCmd.isPending
                  ? t('activate-dialog.activating')
                  : t('activate-dialog.confirm')}
              </button>
            </div>
          </>
        )}
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('activate-dialog.cancel')}</button>
      </form>
    </dialog>
  );
}
