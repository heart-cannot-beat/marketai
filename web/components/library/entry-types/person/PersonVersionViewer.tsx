import { useTranslation } from 'next-i18next';

interface PersonVersionViewerProps {
  version: Record<string, unknown>;
}

export function PersonVersionViewer({ version }: PersonVersionViewerProps) {
  const { t } = useTranslation('library');
  const fragments = (version.prompt_fragments as string[]) ?? [];
  const regions = (version.rights_regions as string[]) ?? [];

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.person.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.person.person-type')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.person_type as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.person.age-expression')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.age_expression as string) || t('fields.version.no-value')}
        </dd>
      </div>

      {fragments.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.person.prompt-fragments')}
          </dt>
          <dd className="mt-1">
            <ul className="list-disc pl-4 text-sm text-gray-900 dark:text-gray-100">
              {fragments.map((f, i) => (
                <li key={i} className="whitespace-pre-wrap">
                  {f}
                </li>
              ))}
            </ul>
          </dd>
        </div>
      )}

      {regions.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.person.rights-regions')}
          </dt>
          <dd className="mt-1">
            <ul className="list-disc pl-4 text-sm text-gray-900 dark:text-gray-100">
              {regions.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </dd>
        </div>
      )}
    </dl>
  );
}
