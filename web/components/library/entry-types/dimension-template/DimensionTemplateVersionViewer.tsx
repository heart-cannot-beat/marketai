import { useTranslation } from 'next-i18next';

interface DimensionTemplateVersionViewerProps {
  version: Record<string, unknown>;
}

interface DimensionValueView {
  id: string;
  positive_fragments?: string[];
  negative_fragments?: string[];
}

interface DimensionView {
  id: string;
  name: string;
  values: DimensionValueView[];
}

/** Read-only rendering of a published `dimension_template` version in the
 * server's content shape (`_validate_dimension_template`): value objects
 * whose `id` is the value shown in Generation previews. */
export function DimensionTemplateVersionViewer({
  version,
}: DimensionTemplateVersionViewerProps) {
  const { t } = useTranslation('library');
  const dims = (version.dimensions as DimensionView[]) ?? [];

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.dimension-template.dimensions')}
        </dt>
        <dd className="mt-1 space-y-2">
          {dims.length === 0 && (
            <p className="text-sm text-gray-900 dark:text-gray-100">
              {t('fields.version.no-value')}
            </p>
          )}
          {dims.map((dim) => (
            <div
              key={dim.id}
              className="rounded border border-gray-200 px-3 py-2 dark:border-gray-700"
            >
              <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                {dim.name}
              </p>
              {(dim.values ?? []).length > 0 && (
                <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">
                  {(dim.values ?? []).map((value) => value.id).join(', ')}
                </p>
              )}
            </div>
          ))}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.dimension-template.combination-cardinality')}
        </dt>
        <dd className="mt-1 font-mono text-xs text-gray-900 dark:text-gray-100">
          {(version.combination_cardinality as string) ||
            t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
