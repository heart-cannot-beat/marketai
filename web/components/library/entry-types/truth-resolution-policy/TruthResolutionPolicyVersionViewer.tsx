import { useTranslation } from 'next-i18next';

interface TruthResolutionPolicyVersionViewerProps {
  version: Record<string, unknown>;
}

export function TruthResolutionPolicyVersionViewer({
  version,
}: TruthResolutionPolicyVersionViewerProps) {
  const { t } = useTranslation('library');
  const priorities = (version.priority_order as string[]) ?? [];

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.truth-resolution-policy.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      {priorities.length > 0 && (
        <div>
          <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
            {t('fields.truth-resolution-policy.priority-order')}
          </dt>
          <dd className="mt-1">
            <ol className="list-decimal pl-4 text-sm text-gray-900 dark:text-gray-100">
              {priorities.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ol>
          </dd>
        </div>
      )}

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.truth-resolution-policy.conflict-strategy')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.conflict_strategy as string) ||
            t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
