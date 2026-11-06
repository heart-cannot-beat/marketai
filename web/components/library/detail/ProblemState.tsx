import { useTranslation } from 'next-i18next';

interface ProblemStateProps {
  onRetry?: () => void;
}

export function ProblemState({ onRetry }: ProblemStateProps) {
  const { t } = useTranslation('library');

  return (
    <div
      className="flex flex-col items-center justify-center py-12 text-center"
      role="alert"
    >
      <p className="text-lg font-medium text-gray-900 dark:text-gray-100">
        {t('problem.title')}
      </p>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        {t('problem.description')}
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="btn btn-sm btn-outline mt-4"
        >
          {t('problem.retry')}
        </button>
      )}
    </div>
  );
}
