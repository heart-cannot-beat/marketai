import { useTranslation } from 'next-i18next';
import { useLibraryBindings } from '@/hooks/library/useLibraryBindings';
import { useLibraryReferenceDownload } from '@/hooks/library/useLibraryReferenceDownload';
import { ReferenceThumbnail } from './ReferenceThumbnail';

interface ReferenceBindingListProps {
  entryId: string;
  versionId: string | null;
}

export function ReferenceBindingList({ versionId }: ReferenceBindingListProps) {
  const { t } = useTranslation('library');
  const { items: bindings, isLoading, problem } = useLibraryBindings(versionId);
  const {
    download,
    state: downloadState,
    error: downloadError,
    reset: resetDownload,
  } = useLibraryReferenceDownload();

  if (!versionId) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('tabs.bindings.no-version')}
      </p>
    );
  }

  if (isLoading) {
    return (
      <div aria-busy="true">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('references.loading-bindings')}
        </p>
      </div>
    );
  }

  if (problem) {
    return (
      <div role="alert">
        <p className="text-sm text-red-600 dark:text-red-400">
          {t('references.bindings-error')}
        </p>
      </div>
    );
  }

  if (!bindings || bindings.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('tabs.bindings.empty')}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {downloadError && (
        <div
          className="rounded bg-red-50 p-2 text-xs text-red-600 dark:bg-red-900/20 dark:text-red-400"
          role="alert"
        >
          {downloadError}
          <button
            type="button"
            className="ml-2 underline"
            onClick={resetDownload}
          >
            {t('references.dismiss-error')}
          </button>
        </div>
      )}

      <ul className="divide-y divide-gray-100 dark:divide-gray-800">
        {bindings.map((binding) => {
          const bindingId = binding.id;
          const refVersionId = binding.reference_version_id;
          const refPurpose = binding.purpose;
          const refName = binding.reference_display_name;
          const isReleased = binding.gate_status === 'released';

          return (
            <li
              key={bindingId}
              className="flex items-center justify-between py-2"
            >
              <div className="flex items-center gap-3">
                {isReleased && (
                  <div className="h-12 w-12 shrink-0 overflow-hidden rounded border border-gray-200 dark:border-gray-700">
                    <ReferenceThumbnail
                      versionId={refVersionId}
                      alt={refName || t('references.unnamed-binding')}
                    />
                  </div>
                )}
                <div>
                  <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                    {refName || t('references.unnamed-binding')}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {refPurpose}
                  </p>
                </div>
              </div>
              {isReleased && (
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  disabled={
                    downloadState === 'requesting' ||
                    downloadState === 'downloading'
                  }
                  onClick={() => download(refVersionId)}
                  aria-label={t('references.download-label', { name: refName })}
                >
                  {downloadState === 'requesting'
                    ? t('references.downloading')
                    : t('references.download')}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
