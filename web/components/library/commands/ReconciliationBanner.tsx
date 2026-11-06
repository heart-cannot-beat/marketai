import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';

export type ReconciliationDisplayState =
  | { kind: 'retry_safe'; message: string }
  | { kind: 'lookup_in_progress' }
  | { kind: 'unresolved_ambiguity'; staleSince: number }
  | { kind: 'terminal_problem'; problem: ProblemDetails }
  | { kind: 'not_applied' };

interface ReconciliationBannerProps {
  state: ReconciliationDisplayState;
  onDismiss: () => void;
  onRetry?: () => void;
}

const BANNER_STYLES: Record<ReconciliationDisplayState['kind'], string> = {
  retry_safe:
    'bg-blue-50 border-blue-200 text-blue-800 dark:bg-blue-900/20 dark:border-blue-800 dark:text-blue-300',
  lookup_in_progress:
    'bg-gray-50 border-gray-200 text-gray-700 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-300',
  unresolved_ambiguity:
    'bg-amber-50 border-amber-200 text-amber-800 dark:bg-amber-900/20 dark:border-amber-800 dark:text-amber-300',
  terminal_problem:
    'bg-red-50 border-red-200 text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-300',
  not_applied:
    'bg-gray-50 border-gray-200 text-gray-700 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-300',
};

export function ReconciliationBanner({
  state,
  onDismiss,
  onRetry,
}: ReconciliationBannerProps) {
  const { t } = useTranslation('library');

  return (
    <div
      className={`rounded-lg border p-3 ${BANNER_STYLES[state.kind]}`}
      role="alert"
      aria-live="polite"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 text-sm">
          <p className="font-medium">
            {t(`commands.reconciliation.${state.kind}-title`)}
          </p>
          <p className="mt-1">
            <ReconciliationMessage state={state} />
          </p>
        </div>

        <div className="flex items-center gap-2">
          {state.kind === 'retry_safe' && onRetry && (
            <button
              type="button"
              className="btn btn-ghost btn-xs"
              onClick={onRetry}
            >
              {t('commands.reconciliation.retry')}
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={onDismiss}
            aria-label={t('commands.reconciliation.dismiss')}
          >
            {t('commands.reconciliation.dismiss')}
          </button>
        </div>
      </div>
    </div>
  );
}

function ReconciliationMessage({
  state,
}: {
  state: ReconciliationDisplayState;
}) {
  const { t, i18n } = useTranslation('library');

  switch (state.kind) {
    case 'retry_safe':
      return <>{state.message}</>;
    case 'lookup_in_progress':
      return <>{t('commands.reconciliation.lookup-in-progress')}</>;
    case 'unresolved_ambiguity':
      return (
        <>
          {t('commands.reconciliation.unresolved-ambiguity', {
            date: formatDateTime(i18n.language, state.staleSince),
          })}
        </>
      );
    case 'terminal_problem':
      return <>{errorMessage(t, state.problem)}</>;
    case 'not_applied':
      return <>{t('commands.reconciliation.not-applied')}</>;
  }
}
