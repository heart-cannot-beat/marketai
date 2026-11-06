import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ForkDialogProps {
  versionId: string;
  sourceDisplayName: string;
  open: boolean;
  onClose: () => void;
  onForked: (entryId: string, location: string | null) => void;
}

export function ForkDialog({
  versionId,
  sourceDisplayName,
  open,
  onClose,
  onForked,
}: ForkDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [displayName, setDisplayName] = useState('');
  const forkCmd = useLibraryCommand('fork');
  const { invalidate } = useLibraryCache();

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
      setDisplayName(`${sourceDisplayName} (fork)`);
      nameInputRef.current?.focus();
    }
  }, [open, sourceDisplayName]);

  if (!open) return null;

  const handleFork = async () => {
    if (!displayName.trim()) return;

    const outcome = await forkCmd.run({
      version_id: versionId,
      body: {
        display_name: displayName.trim(),
      },
    });

    if (outcome.kind === 'success') {
      const result = outcome.data as Record<string, unknown>;
      const entryId = result.id as string;
      const location = (outcome.meta?.location as string) ?? null;
      forkCmd.reset();
      await invalidate({
        resources: ['entryList', 'versions', 'events', 'entryTypes'],
      });
      onForked(entryId, location);
    }
  };

  const handleClose = () => {
    setDisplayName('');
    forkCmd.reset();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="fork-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-md">
        <h3
          id="fork-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('commands.fork-title')}
        </h3>

        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          {t('commands.fork-description', { name: sourceDisplayName })}
        </p>

        <label className="form-control mt-4 w-full">
          <div className="label">
            <span className="label-text">
              {t('commands.display-name-label')}
            </span>
          </div>
          <input
            ref={nameInputRef}
            type="text"
            className="input input-bordered w-full"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            required
            autoComplete="off"
          />
        </label>

        {forkCmd.problem && (
          <div
            className="mt-3 text-sm text-red-600 dark:text-red-400"
            role="alert"
          >
            {errorMessage(t, forkCmd.problem)}
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
            className="btn btn-primary btn-sm"
            disabled={!displayName.trim() || forkCmd.isPending}
            onClick={handleFork}
          >
            {forkCmd.isPending
              ? t('commands.forking')
              : t('commands.confirm-fork')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('commands.cancel')}</button>
      </form>
    </dialog>
  );
}
