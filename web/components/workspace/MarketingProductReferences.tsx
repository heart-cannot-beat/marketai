import { useState, useCallback } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { usePublishedReferenceAssets } from '@/hooks/library/usePublishedReferenceAssets';
import { ReferenceUploadZone } from '@/components/library/references/ReferenceUploadZone';
import { ReferenceThumbnail } from '@/components/library/references/ReferenceThumbnail';
import { useWorkspace } from './WorkspaceProvider';

export interface ProductReferences {
  reference_version_ids: string[];
  source: string;
  confirms_real_product: boolean;
}

export function MarketingProductReferences({
  value,
  projectId,
  onChange,
  disabled,
}: {
  value: ProductReferences;
  projectId: string;
  onChange: (value: ProductReferences) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const [opened, setOpened] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const references = usePublishedReferenceAssets({ enabled: opened });
  const { refresh } = references;
  const uploaded = useCallback(() => {
    void refresh();
  }, [refresh]);
  return (
    <details
      className="space-y-3 rounded-lg border border-gray-200 p-4 text-sm"
      onToggle={(e) => setOpened(e.currentTarget.open)}
    >
      <summary>{t('marketing-product-photos-title')}</summary>
      <p>{t('marketing-product-photos-description')}</p>
      <Link
        className="underline"
        href={`/w/${current.slug}/library?cat=product`}
      >
        {t('marketing-product-photos-library')}
      </Link>
      {!disabled && (
        <>
          <button
            type="button"
            className="block underline"
            onClick={() => setUploadOpen(!uploadOpen)}
          >
            {t('marketing-upload-product-photo')}
          </button>
          {uploadOpen && (
            <ReferenceUploadZone
              contextEntryId={projectId}
              ownerKind="marketing_project"
              bindToContext={false}
              onComplete={uploaded}
            />
          )}
        </>
      )}
      {references.isLoading && (
        <p role="status">{t('marketing-loading-product-photos')}</p>
      )}
      {references.problem && (
        <p role="alert">{t('marketing-product-photos-load-failed')}</p>
      )}
      {!references.isLoading &&
        !references.problem &&
        !references.items.length && (
          <p>{t('marketing-product-photos-empty')}</p>
        )}
      <fieldset
        disabled={disabled}
        className="grid grid-cols-2 gap-3 sm:grid-cols-3"
      >
        <legend className="mb-2">{t('marketing-select-product-photos')}</legend>
        {references.items.map((item) => (
          <label key={item.versionId} className="min-w-0 rounded border p-2">
            <div className="aspect-square overflow-hidden">
              <ReferenceThumbnail
                versionId={item.versionId}
                alt={item.displayName}
              />
            </div>
            <span className="mt-2 flex items-start gap-2">
              <input
                type="checkbox"
                aria-label={item.displayName}
                checked={value.reference_version_ids.includes(item.versionId)}
                disabled={
                  !value.reference_version_ids.includes(item.versionId) &&
                  value.reference_version_ids.length >= 3
                }
                onChange={(e) =>
                  onChange({
                    ...value,
                    confirms_real_product: false,
                    reference_version_ids: e.target.checked
                      ? [...value.reference_version_ids, item.versionId]
                      : value.reference_version_ids.filter(
                          (id) => id !== item.versionId
                        ),
                  })
                }
              />
              <span className="break-words">{item.displayName}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {references.hasMore && (
        <button
          type="button"
          disabled={references.isLoadingMore}
          className="underline"
          onClick={references.loadMore}
        >
          {t('marketing-load-more-product-photos')}
        </button>
      )}
      {!!value.reference_version_ids.length && (
        <>
          <label className="block space-y-2">
            {t('marketing-product-photo-source')}
            <textarea
              disabled={disabled}
              value={value.source}
              maxLength={1200}
              className="w-full rounded border p-2"
              onChange={(e) =>
                onChange({
                  ...value,
                  source: e.target.value,
                  confirms_real_product: false,
                })
              }
            />
          </label>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              disabled={disabled}
              checked={value.confirms_real_product}
              onChange={(e) =>
                onChange({ ...value, confirms_real_product: e.target.checked })
              }
            />
            {t('marketing-product-photo-confirmation')}
          </label>
        </>
      )}
      {disabled && <p>{t('marketing-product-photos-frozen')}</p>}
    </details>
  );
}
