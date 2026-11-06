import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { usePublishedReferenceAssets } from '@/hooks/library/usePublishedReferenceAssets';
import { ReferenceThumbnail } from '../references/ReferenceThumbnail';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface BindReferenceDialogProps {
  entryId: string;
  draftEtag: string;
  open: boolean;
  onClose: () => void;
  onBound: () => void;
}

/**
 * Bind a published reference to this entry's Draft. The user picks a card
 * (name + controlled-derivative thumbnail) from the Workspace's published
 * references -- the `libver_` id is never typed by hand (Issue #158).
 */
export function BindReferenceDialog({
  entryId,
  draftEtag,
  open,
  onClose,
  onBound,
}: BindReferenceDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [referenceVersionId, setReferenceVersionId] = useState('');
  const [purpose, setPurpose] = useState('');
  const [viewpoint, setViewpoint] = useState('');
  const [priority, setPriority] = useState(0);
  const [conditions, setConditions] = useState('');
  const bindCmd = useLibraryCommand('bind_reference');
  const { invalidate } = useLibraryCache();
  const references = usePublishedReferenceAssets({ enabled: open });

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

  if (!open) return null;

  const canSubmit =
    referenceVersionId.trim().length > 0 && purpose.trim().length > 0;

  const handleBind = async () => {
    if (!canSubmit) return;

    const outcome = await bindCmd.run({
      entry_id: entryId,
      body: {
        reference_version_id: referenceVersionId.trim(),
        purpose: purpose.trim(),
        viewpoint: viewpoint.trim() || null,
        priority,
        conditions: conditions.trim() || null,
        draft_etag: draftEtag,
      },
    });

    if (outcome.kind === 'success') {
      bindCmd.reset();
      await invalidate({ resources: ['entry', 'bindings', 'events'] });
      resetForm();
      onBound();
    }
  };

  const resetForm = () => {
    setReferenceVersionId('');
    setPurpose('');
    setViewpoint('');
    setPriority(0);
    setConditions('');
  };

  const handleClose = () => {
    resetForm();
    bindCmd.reset();
    onClose();
  };

  const selected = references.items.find(
    (item) => item.versionId === referenceVersionId
  );

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="bind-ref-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-2xl">
        <h3
          id="bind-ref-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('commands.bind-reference-title')}
        </h3>

        <div className="mt-4 space-y-4">
          <fieldset>
            <legend className="label-text mb-2 block">
              {t('commands.bind-reference-pick')}
            </legend>
            {references.isLoading && (
              <p
                className="text-sm text-gray-500 dark:text-gray-400"
                aria-busy="true"
              >
                {t('commands.bind-reference-loading')}
              </p>
            )}
            {references.problem && (
              <p
                className="text-sm text-red-600 dark:text-red-400"
                role="alert"
              >
                {t('commands.bind-reference-load-failed')}
              </p>
            )}
            {!references.isLoading &&
              !references.problem &&
              references.items.length === 0 && (
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {t('commands.bind-reference-empty')}
                </p>
              )}
            {references.items.length > 0 && (
              <div
                role="radiogroup"
                aria-label={t('commands.bind-reference-pick')}
                className="grid max-h-72 grid-cols-2 gap-3 overflow-y-auto sm:grid-cols-3"
              >
                {references.items.map((item) => {
                  const isSelected = item.versionId === referenceVersionId;
                  return (
                    <button
                      key={item.versionId}
                      type="button"
                      role="radio"
                      aria-checked={isSelected}
                      aria-label={item.displayName}
                      data-testid="reference-picker-card"
                      onClick={() => setReferenceVersionId(item.versionId)}
                      className={`flex flex-col overflow-hidden rounded-lg border text-left transition-colors ${
                        isSelected
                          ? 'border-blue-500 ring-2 ring-blue-200 dark:ring-blue-900'
                          : 'border-gray-200 hover:border-gray-400 dark:border-gray-700'
                      }`}
                    >
                      <div className="aspect-square w-full">
                        <ReferenceThumbnail
                          versionId={item.versionId}
                          alt={item.displayName}
                        />
                      </div>
                      <span className="truncate px-2 py-1 text-xs text-gray-900 dark:text-gray-100">
                        {item.displayName}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            {references.hasMore && (
              <button
                type="button"
                className="btn btn-ghost btn-xs mt-2"
                disabled={references.isLoadingMore}
                onClick={references.loadMore}
              >
                {t('commands.bind-reference-load-more')}
              </button>
            )}
            {selected && (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                {t('commands.bind-reference-selected', {
                  name: selected.displayName,
                })}
              </p>
            )}
          </fieldset>

          <label className="form-control w-full">
            <div className="label">
              <span className="label-text">{t('commands.bind-purpose')}</span>
            </div>
            <input
              type="text"
              className="input input-bordered w-full"
              value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
              required
              autoComplete="off"
            />
          </label>

          <label className="form-control w-full">
            <div className="label">
              <span className="label-text">{t('commands.bind-viewpoint')}</span>
            </div>
            <input
              type="text"
              className="input input-bordered w-full"
              value={viewpoint}
              onChange={(e) => setViewpoint(e.target.value)}
              autoComplete="off"
            />
          </label>

          <label className="form-control w-full">
            <div className="label">
              <span className="label-text">{t('commands.bind-priority')}</span>
            </div>
            <input
              type="number"
              className="input input-bordered w-full"
              value={priority}
              onChange={(e) => setPriority(Math.max(0, Number(e.target.value)))}
              min={0}
            />
          </label>

          <label className="form-control w-full">
            <div className="label">
              <span className="label-text">
                {t('commands.bind-conditions')}
              </span>
            </div>
            <textarea
              className="textarea textarea-bordered w-full"
              rows={2}
              value={conditions}
              onChange={(e) => setConditions(e.target.value)}
            />
          </label>
        </div>

        {bindCmd.problem && (
          <div
            className="mt-3 text-sm text-red-600 dark:text-red-400"
            role="alert"
          >
            {errorMessage(t, bindCmd.problem)}
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
            disabled={!canSubmit || bindCmd.isPending}
            onClick={handleBind}
          >
            {bindCmd.isPending
              ? t('commands.binding')
              : t('commands.confirm-bind')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('commands.cancel')}</button>
      </form>
    </dialog>
  );
}
