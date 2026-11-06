import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import type { LibraryCategory } from '@/lib/library/navigation';
import { rootTypeForCategory } from '@/lib/library/navigation';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface CreateRootEntryDialogProps {
  category: LibraryCategory;
  open: boolean;
  onClose: () => void;
  onCreated: (entryId: string) => void;
}

export function CreateRootEntryDialog({
  category,
  open,
  onClose,
  onCreated,
}: CreateRootEntryDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [displayName, setDisplayName] = useState('');
  const createCmd = useLibraryCommand('create_entry');
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
      nameInputRef.current?.focus();
    }
  }, [open]);

  if (!open) return null;

  const entryType = rootTypeForCategory(category);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!displayName.trim()) return;

    const outcome = await createCmd.run({
      body: {
        entry_type: entryType,
        display_name: displayName.trim(),
      },
    });

    if (outcome.kind === 'success') {
      const created = outcome.data as Record<string, unknown>;
      const entryId = created.id as string;
      createCmd.reset();
      await invalidate({ resources: ['entryList', 'entryTypes'] });
      setDisplayName('');
      onCreated(entryId);
    }
  };

  const handleClose = () => {
    setDisplayName('');
    createCmd.reset();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="create-entry-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-md">
        <h3
          id="create-entry-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('commands.create-root-entry-title', {
            type: t(`entry-type.${entryType}`),
          })}
        </h3>

        <form onSubmit={handleSubmit} className="mt-4">
          <label className="form-control w-full">
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

          {createCmd.problem && (
            <div
              className="mt-3 text-sm text-red-600 dark:text-red-400"
              role="alert"
            >
              {errorMessage(t, createCmd.problem)}
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
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={!displayName.trim() || createCmd.isPending}
            >
              {createCmd.isPending
                ? t('commands.creating')
                : t('commands.create')}
            </button>
          </div>
        </form>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('commands.cancel')}</button>
      </form>
    </dialog>
  );
}
