import { useTranslation } from 'next-i18next';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import {
  useConnectionCheck,
  type ConnectionCheck,
} from '@/hooks/library/useConnectionCheck';
import { formatDateTime } from '@/lib/i18n/format';

interface ConnectionCheckPanelProps {
  dataSourceEntryId: string;
}

const TONE: Record<ConnectionCheck['health'], string> = {
  healthy:
    'border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-900/20 dark:text-green-300',
  unknown:
    'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300',
  degraded:
    'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300',
  failed:
    'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300',
  revoked:
    'border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300',
};

/**
 * Checks a data source on the server (#383): its published version, whether
 * its connector exists and the bound credential's state. Nothing contacts a
 * provider. The result reads as sentences; a Controlled Mock source carries
 * the Mock badge.
 */
export function ConnectionCheckPanel({
  dataSourceEntryId,
}: ConnectionCheckPanelProps) {
  const { t } = useTranslation('library');
  const { view, check } = useConnectionCheck(dataSourceEntryId);

  if (view.step === 'checking') {
    return (
      <div
        className="rounded-lg border border-gray-200 p-4 dark:border-gray-700"
        aria-busy="true"
      >
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('data-source-ops.checking')}
        </p>
      </div>
    );
  }

  if (view.step === 'error' || view.step === 'uncertain') {
    return (
      <div
        className="rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20"
        role="alert"
      >
        <p className="text-sm text-red-700 dark:text-red-300">
          {view.step === 'error'
            ? view.message
            : t('data-source-ops.outcome-uncertain')}
        </p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={check}
        >
          {t('data-source-ops.retry')}
        </button>
      </div>
    );
  }

  if (view.step === 'done') {
    return <CheckResult check={view.check} onCheckAgain={check} />;
  }

  return (
    <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <p className="text-sm text-gray-700 dark:text-gray-300">
        {t('data-source-ops.connection-description')}
      </p>
      <button
        type="button"
        className="btn btn-primary btn-sm mt-3"
        onClick={check}
      >
        {t('data-source-ops.check-connection')}
      </button>
    </div>
  );
}

function CheckResult({
  check,
  onCheckAgain,
}: {
  check: ConnectionCheck;
  onCheckAgain: () => void;
}) {
  const { t, i18n } = useTranslation('library');
  // A Controlled Mock adapter is Mock whatever its version's mode says.
  const mode = check.adapter_kind === 'mock' ? 'mock' : check.execution_mode;
  return (
    <div className={`rounded-lg border p-4 ${TONE[check.health]}`}>
      <p className="text-sm font-medium">
        {t(`data-source-ops.health.${check.health}`)}
      </p>
      <p className="mt-1 text-sm">
        {t(`data-source-ops.outcome.${check.reason_code}`, {
          defaultValue: t('data-source-ops.outcome-other'),
        })}
      </p>

      <dl className="mt-3 grid grid-cols-2 gap-1 text-xs">
        {check.adapter_kind && (
          <>
            <dt className="text-gray-600 dark:text-gray-400">
              {t('data-source-ops.adapter')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100">
              {t(`data-source-ops.adapter-kind.${check.adapter_kind}`, {
                defaultValue: t('data-source-ops.adapter-kind-other'),
              })}
            </dd>
          </>
        )}
        {mode && (
          <>
            <dt className="text-gray-600 dark:text-gray-400">
              {t('data-source-ops.mode')}
            </dt>
            <dd>
              <ExecutionModeTag mode={mode} />
            </dd>
          </>
        )}
        <dt className="text-gray-600 dark:text-gray-400">
          {t('data-source-ops.checked-at')}
        </dt>
        <dd className="text-gray-900 dark:text-gray-100">
          {formatDateTime(i18n.language, check.checked_at)}
        </dd>
      </dl>

      <button
        type="button"
        className="btn btn-ghost btn-xs mt-3"
        onClick={onCheckAgain}
      >
        {t('data-source-ops.check-again')}
      </button>
    </div>
  );
}
