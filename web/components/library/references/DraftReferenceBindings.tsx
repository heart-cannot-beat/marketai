import { useTranslation } from 'next-i18next';
import { ReferenceThumbnail } from './ReferenceThumbnail';

export interface DraftReferenceBinding {
  reference_version_id: string;
  purpose?: string;
  viewpoint?: string;
  priority?: number;
  note?: string;
}

interface DraftReferenceBindingsProps {
  bindings: DraftReferenceBinding[];
}

/**
 * Reference bindings sitting in the entry's open Draft -- what the automatic
 * post-upload promote/bind produces and what the next publish will freeze.
 * Published-version bindings live in `ReferenceBindingList`; this list makes
 * the "uploaded → already bound" step visible before that publish.
 */
export function DraftReferenceBindings({
  bindings,
}: DraftReferenceBindingsProps) {
  const { t } = useTranslation('library');

  if (bindings.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('references.draft-bindings-empty')}
      </p>
    );
  }

  return (
    <div>
      <ul
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
        data-testid="draft-reference-bindings"
      >
        {bindings.map((binding, index) => (
          <li
            key={`${binding.reference_version_id}:${binding.purpose ?? ''}:${binding.viewpoint ?? ''}:${index}`}
            className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700"
          >
            <div className="aspect-square w-full">
              <ReferenceThumbnail
                versionId={binding.reference_version_id}
                alt={binding.purpose ?? t('references.unnamed-binding')}
              />
            </div>
            <div className="px-2 py-1">
              <p className="truncate text-xs font-medium text-gray-900 dark:text-gray-100">
                {binding.purpose || t('references.unnamed-binding')}
              </p>
              {binding.viewpoint && (
                <p className="truncate text-xs text-gray-500 dark:text-gray-400">
                  {binding.viewpoint}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        {t('references.draft-bindings-hint')}
      </p>
    </div>
  );
}
