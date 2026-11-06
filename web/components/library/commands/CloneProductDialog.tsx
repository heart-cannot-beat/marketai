import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface CloneProductDialogProps {
  versionId: string;
  sourceDisplayName: string;
  open: boolean;
  onClose: () => void;
  onCloned: (productId: string) => void;
}

type CloneInclusion = 'offers' | 'references' | 'dimension_schemas';

const INCLUSION_OPTIONS: CloneInclusion[] = [
  'offers',
  'references',
  'dimension_schemas',
];

export function CloneProductDialog({
  versionId,
  sourceDisplayName,
  open,
  onClose,
  onCloned,
}: CloneProductDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [displayName, setDisplayName] = useState('');
  const [inclusions, setInclusions] = useState<Set<CloneInclusion>>(new Set());
  const cloneCmd = useLibraryCommand('clone');
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
      setDisplayName(`${sourceDisplayName} (copy)`);
      nameInputRef.current?.focus();
    }
  }, [open, sourceDisplayName]);

  if (!open) return null;

  const toggleInclusion = (item: CloneInclusion) => {
    setInclusions((prev) => {
      const next = new Set(prev);
      if (next.has(item)) {
        next.delete(item);
      } else {
        next.add(item);
      }
      return next;
    });
  };

  const handleClone = async () => {
    if (!displayName.trim()) return;

    const outcome = await cloneCmd.run({
      version_id: versionId,
      body: {
        display_name: displayName.trim(),
        inclusions: Array.from(inclusions),
      },
    });

    if (outcome.kind === 'success') {
      const result = outcome.data as Record<string, unknown>;
      const productId = result.id as string;
      cloneCmd.reset();
      await invalidate({ resources: ['entryList', 'entryTypes'] });
      onCloned(productId);
    }
  };

  const handleClose = () => {
    setDisplayName('');
    setInclusions(new Set());
    cloneCmd.reset();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="clone-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-md">
        <h3
          id="clone-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('commands.clone-title')}
        </h3>

        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          {t('commands.clone-description', { name: sourceDisplayName })}
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

        <fieldset className="mt-4">
          <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {t('commands.clone-inclusions')}
          </legend>
          <div className="mt-2 space-y-2">
            {INCLUSION_OPTIONS.map((item) => (
              <label
                key={item}
                className="flex items-center gap-2 cursor-pointer"
              >
                <input
                  type="checkbox"
                  className="checkbox checkbox-sm"
                  checked={inclusions.has(item)}
                  onChange={() => toggleInclusion(item)}
                />
                <span className="text-sm text-gray-700 dark:text-gray-300">
                  {t(`commands.clone-include-${item}`)}
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        {cloneCmd.problem && (
          <div
            className="mt-3 text-sm text-red-600 dark:text-red-400"
            role="alert"
          >
            {errorMessage(t, cloneCmd.problem)}
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
            disabled={!displayName.trim() || cloneCmd.isPending}
            onClick={handleClone}
          >
            {cloneCmd.isPending
              ? t('commands.cloning')
              : t('commands.confirm-clone')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('commands.cancel')}</button>
      </form>
    </dialog>
  );
}
