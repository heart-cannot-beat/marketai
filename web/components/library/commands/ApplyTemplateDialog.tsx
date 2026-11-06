import { useTranslation } from 'next-i18next';
import { useEffect, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ApplyTemplateDialogProps {
  templateVersionId: string;
  templateDisplayName: string;
  open: boolean;
  onClose: () => void;
  onApplied: (productId: string, dimensionSchemaId: string) => void;
}

interface ProductOption {
  id: string;
  display_name: string;
}

export function ApplyTemplateDialog({
  templateVersionId,
  templateDisplayName,
  open,
  onClose,
  onApplied,
}: ApplyTemplateDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [selectedProductId, setSelectedProductId] = useState('');
  const applyCmd = useLibraryCommand('apply_template');
  const { invalidate } = useLibraryCache();

  const { data: productsData } = useDomainQuery(
    'list_library_entries',
    { entry_type: 'product', limit: 100 } as any,
    { enabled: open }
  );

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

  const products =
    ((productsData as Record<string, unknown>)?.items as ProductOption[]) ?? [];

  const handleApply = async () => {
    if (!selectedProductId) return;

    const outcome = await applyCmd.run({
      template_version_id: templateVersionId,
      body: {
        target_product_id: selectedProductId,
      },
    });

    if (outcome.kind === 'success') {
      const result = outcome.data as Record<string, unknown>;
      const dimensionSchemaId = result.dimension_schema_id as string;
      applyCmd.reset();
      await invalidate({
        resources: ['entryList', 'internalEntries', 'events', 'entryTypes'],
      });
      onApplied(selectedProductId, dimensionSchemaId);
    }
  };

  const handleClose = () => {
    setSelectedProductId('');
    applyCmd.reset();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="apply-template-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-md">
        <h3
          id="apply-template-title"
          className="text-lg font-bold text-gray-900 dark:text-gray-100"
        >
          {t('commands.apply-template-title')}
        </h3>

        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          {t('commands.apply-template-description', {
            template: templateDisplayName,
          })}
        </p>

        <label className="form-control mt-4 w-full">
          <div className="label">
            <span className="label-text">
              {t('commands.apply-template-target')}
            </span>
          </div>
          <select
            className="select select-bordered w-full"
            value={selectedProductId}
            onChange={(e) => setSelectedProductId(e.target.value)}
          >
            <option value="">
              {t('commands.apply-template-select-product')}
            </option>
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
              </option>
            ))}
          </select>
        </label>

        {applyCmd.problem && (
          <div
            className="mt-3 text-sm text-red-600 dark:text-red-400"
            role="alert"
          >
            {errorMessage(t, applyCmd.problem)}
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
            disabled={!selectedProductId || applyCmd.isPending}
            onClick={handleApply}
          >
            {applyCmd.isPending
              ? t('commands.applying')
              : t('commands.confirm-apply-template')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('commands.cancel')}</button>
      </form>
    </dialog>
  );
}
