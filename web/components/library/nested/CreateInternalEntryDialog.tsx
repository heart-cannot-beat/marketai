import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import type {
  InternalEntryType,
  LibraryEntryType,
} from '@/lib/library/navigation';
import { isInternal, getPlacement } from '@/lib/library/navigation';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface CreateInternalEntryDialogProps {
  entryType: InternalEntryType;
  parentEntryId: string;
  parentEntryType: LibraryEntryType;
  rootEntryId: string;
  open: boolean;
  onClose: () => void;
  onCreated: (entryId: string) => void;
}

export function CreateInternalEntryDialog({
  entryType,
  parentEntryId,
  parentEntryType,
  rootEntryId,
  open,
  onClose,
  onCreated,
}: CreateInternalEntryDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [displayName, setDisplayName] = useState('');
  const createCmd = useLibraryCommand('create_internal_entry');
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

  const placement = getPlacement(entryType);
  const isValidContext =
    isInternal(entryType) &&
    placement.kind === 'internal' &&
    placement.parentKinds.includes(parentEntryType);

  if (!isValidContext) {
    return (
      <dialog
        ref={dialogRef}
        className="modal modal-open"
        aria-labelledby="create-internal-title"
        onClose={onClose}
      >
        <div className="modal-box max-w-md">
          <p className="text-sm text-red-600 dark:text-red-400" role="alert">
            {t('contextual-create.invalid-context')}
          </p>
          <div className="modal-action mt-4">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={onClose}
            >
              {t('contextual-create.cancel')}
            </button>
          </div>
        </div>
        <form method="dialog" className="modal-backdrop">
          <button type="submit">{t('contextual-create.cancel')}</button>
        </form>
      </dialog>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!displayName.trim()) return;

    const outcome = await createCmd.run({
      root_entry_id: rootEntryId,
      body: {
        entry_type: entryType,
        display_name: displayName.trim(),
        parent_type: parentEntryType,
        parent_id: parentEntryId,
      },
    });

    if (outcome.kind === 'success') {
      const created = outcome.data as Record<string, unknown>;
      const entryId = created.id as string;
      createCmd.reset();
      await invalidate({
        resources: ['entryList', 'internalEntries', 'entryTypes'],
      });
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
      aria-labelledby="create-internal-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-md">
        <h3
          id="create-internal-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('contextual-create.title', {
            type: t(`entry-type.${entryType}`),
          })}
        </h3>

        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          {t('contextual-create.context-label', {
            parent: t(`entry-type.${parentEntryType}`),
          })}
        </p>

        <form onSubmit={handleSubmit} className="mt-4">
          <label className="form-control w-full">
            <div className="label">
              <span className="label-text">
                {t('contextual-create.display-name-label')}
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
              {t('contextual-create.cancel')}
            </button>
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={!displayName.trim() || createCmd.isPending}
            >
              {createCmd.isPending
                ? t('contextual-create.creating')
                : t('contextual-create.create')}
            </button>
          </div>
        </form>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('contextual-create.cancel')}</button>
      </form>
    </dialog>
  );
}
