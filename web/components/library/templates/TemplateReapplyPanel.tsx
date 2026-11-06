import { useTranslation } from 'next-i18next';
import { useTemplateReapply } from '@/hooks/library/useTemplateReapply';
import { TemplateDiffTable } from './TemplateDiffTable';

interface TemplateReapplyPanelProps {
  templateVersionId: string;
  schemaEntryId: string;
}

export function TemplateReapplyPanel({
  templateVersionId,
  schemaEntryId,
}: TemplateReapplyPanelProps) {
  const { t } = useTranslation('library');
  const { state, diff, error, loadDiff, setResolution, apply, reset } =
    useTemplateReapply();

  if (state === 'idle') {
    return (
      <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {t('template-reapply.description')}
        </p>
        <button
          type="button"
          className="btn btn-primary btn-sm mt-3"
          onClick={() => loadDiff(templateVersionId, schemaEntryId)}
        >
          {t('template-reapply.load-diff')}
        </button>
      </div>
    );
  }

  if (state === 'loading') {
    return (
      <div
        className="rounded-lg border border-gray-200 p-4 dark:border-gray-700"
        aria-busy="true"
      >
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('template-reapply.loading')}
        </p>
      </div>
    );
  }

  if (state === 'complete') {
    return (
      <div className="rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-900/20">
        <p className="text-sm text-green-700 dark:text-green-300">
          {t('template-reapply.applied')}
        </p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={reset}
        >
          {t('template-reapply.done')}
        </button>
      </div>
    );
  }

  if (state === 'error' || state === 'stale') {
    return (
      <div
        className="rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20"
        role="alert"
      >
        <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={reset}
        >
          {t('template-reapply.retry')}
        </button>
      </div>
    );
  }

  const hasUnresolvedConflicts = diff.some(
    (d) => d.kind === 'conflict' && d.resolution === null
  );

  return (
    <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">
        {t('template-reapply.diff-title')}
      </h3>

      <TemplateDiffTable diff={diff} onResolve={setResolution} />

      <div className="mt-4 flex gap-2">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={hasUnresolvedConflicts || state === 'applying'}
          onClick={() => apply(schemaEntryId)}
        >
          {state === 'applying'
            ? t('template-reapply.applying')
            : t('template-reapply.apply')}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={reset}>
          {t('template-reapply.cancel')}
        </button>
      </div>
    </div>
  );
}
