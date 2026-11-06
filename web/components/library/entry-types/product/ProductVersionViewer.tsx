import { useTranslation } from 'next-i18next';

interface ProductVersionViewerProps {
  version: Record<string, unknown>;
}

export function ProductVersionViewer({ version }: ProductVersionViewerProps) {
  const { t } = useTranslation('library');
  const points = (version.selling_points as string[]) ?? [];

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.product.name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.product.description')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">
          {(version.description as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.product.audience')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">
          {(version.audience as string) || t('fields.version.no-value')}
        </dd>
      </div>

      {points.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.product.selling-points')}
          </dt>
          <dd className="mt-1">
            <ul className="list-disc pl-4 text-sm text-gray-900 dark:text-gray-100">
              {points.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          </dd>
        </div>
      )}
    </dl>
  );
}
