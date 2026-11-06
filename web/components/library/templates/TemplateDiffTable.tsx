import { useTranslation } from 'next-i18next';
import type { DimensionDiffItem } from '@/hooks/library/useTemplateReapply';

interface TemplateDiffTableProps {
  diff: DimensionDiffItem[];
  onResolve: (
    dimensionId: string,
    resolution: 'accept_template' | 'keep_local'
  ) => void;
}

export function TemplateDiffTable({ diff, onResolve }: TemplateDiffTableProps) {
  const { t } = useTranslation('library');

  if (diff.length === 0) {
    return (
      <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">
        {t('template-reapply.no-changes')}
      </p>
    );
  }

  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-sm" role="table">
        <thead>
          <tr className="border-b border-gray-200 dark:border-gray-700">
            <th className="py-2 text-left font-medium text-gray-700 dark:text-gray-300">
              {t('template-reapply.dimension')}
            </th>
            <th className="py-2 text-left font-medium text-gray-700 dark:text-gray-300">
              {t('template-reapply.status')}
            </th>
            <th className="py-2 text-left font-medium text-gray-700 dark:text-gray-300">
              {t('template-reapply.template-values')}
            </th>
            <th className="py-2 text-left font-medium text-gray-700 dark:text-gray-300">
              {t('template-reapply.local-values')}
            </th>
            <th className="py-2 text-left font-medium text-gray-700 dark:text-gray-300">
              {t('template-reapply.action')}
            </th>
          </tr>
        </thead>
        <tbody>
          {diff.map((item) => (
            <tr
              key={item.dimensionId}
              className="border-b border-gray-100 dark:border-gray-800"
            >
              <td className="py-2 text-gray-900 dark:text-gray-100">
                {item.name}
              </td>
              <td className="py-2">
                <span
                  className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${
                    item.kind === 'unchanged'
                      ? 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
                      : item.kind === 'conflict'
                        ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                        : 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'
                  }`}
                >
                  {t(`template-reapply.kind.${item.kind}`)}
                </span>
              </td>
              <td className="py-2 text-xs text-gray-600 dark:text-gray-400">
                {item.templateValues.join(', ') || '—'}
              </td>
              <td className="py-2 text-xs text-gray-600 dark:text-gray-400">
                {item.localValues.join(', ') || '—'}
              </td>
              <td className="py-2">
                {item.kind === 'conflict' && (
                  <div className="flex gap-1">
                    <button
                      type="button"
                      className={`btn btn-xs ${
                        item.resolution === 'accept_template'
                          ? 'btn-primary'
                          : 'btn-ghost'
                      }`}
                      onClick={() =>
                        onResolve(item.dimensionId, 'accept_template')
                      }
                    >
                      {t('template-reapply.use-template')}
                    </button>
                    <button
                      type="button"
                      className={`btn btn-xs ${
                        item.resolution === 'keep_local'
                          ? 'btn-primary'
                          : 'btn-ghost'
                      }`}
                      onClick={() => onResolve(item.dimensionId, 'keep_local')}
                    >
                      {t('template-reapply.keep-local')}
                    </button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
