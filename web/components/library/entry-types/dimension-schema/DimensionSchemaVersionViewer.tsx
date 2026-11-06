import { useTranslation } from 'next-i18next';

interface DimensionSchemaVersionViewerProps {
  version: Record<string, unknown>;
}

interface DimensionView {
  id: string;
  name: string;
  values: string[];
  constraints: Record<string, unknown>;
}

export function DimensionSchemaVersionViewer({
  version,
}: DimensionSchemaVersionViewerProps) {
  const { t } = useTranslation('library');
  const dims = (version.dimensions as DimensionView[]) ?? [];

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.dimension-schema.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      {dims.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.dimension-schema.dimensions')}
          </dt>
          <dd className="mt-1 space-y-2">
            {dims.map((dim) => (
              <div
                key={dim.id}
                className="rounded border border-gray-200 px-3 py-2 dark:border-gray-700"
              >
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {dim.name}
                </p>
                {dim.values.length > 0 && (
                  <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">
                    {dim.values.join(', ')}
                  </p>
                )}
                {Object.keys(dim.constraints ?? {}).length > 0 && (
                  <pre className="mt-1 rounded bg-gray-50 p-1 text-xs text-gray-600 dark:bg-gray-900 dark:text-gray-400 overflow-x-auto">
                    {JSON.stringify(dim.constraints, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </dd>
        </div>
      )}
    </dl>
  );
}
