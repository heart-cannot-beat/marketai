import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { useTranslation } from 'next-i18next';

interface OperationStatusProps {
  operation: { status: string; result_ref?: string | null } | undefined;
  pollingProblem?: ProblemDetails;
  outcomeSummary?: React.ReactNode;
  actions?: React.ReactNode;
}

/**
 * `status` keys here are the terminal/non-terminal vocabulary a caller
 * normalizes an Operation's `state` into before handing it to this
 * component (spec: `queued`, not `pending`). Terminal states are
 * `succeeded`, `failed`, `cancelled`, `partially_succeeded`. Each has
 * `common:operation-status-<status>` copy (`state-codes.spec.ts`).
 */
const STATUS_CONFIG: Record<string, { icon: string; variant: string }> = {
  admitted: { icon: '⏳', variant: 'text-gray-500' },
  queued: { icon: '⏳', variant: 'text-gray-500' },
  running: { icon: '⚙️', variant: 'text-blue-600' },
  succeeded: { icon: '✓', variant: 'text-green-600' },
  partially_succeeded: { icon: '⚠', variant: 'text-amber-600' },
  failed: { icon: '✕', variant: 'text-red-600' },
  cancelled: { icon: '—', variant: 'text-gray-500' },
  // `uncertain` (e.g. polling lost track of the Operation) must never claim
  // success or failure — it only says the client is checking.
  uncertain: { icon: '?', variant: 'text-amber-500' },
};

/**
 * Pure display of an Operation's lifecycle. Never fetches or polls — the
 * caller owns that via `useOperation()` and hands in the normalized
 * `operation`, an optional `pollingProblem` (a transport/poll failure,
 * distinct from the Operation itself failing), and caller-specific
 * `outcomeSummary`/`actions` for the current state.
 */
export function OperationStatus({
  operation,
  pollingProblem,
  outcomeSummary,
  actions,
}: OperationStatusProps) {
  const { t } = useTranslation('common');
  if (!operation) return null;

  const status =
    operation.status in STATUS_CONFIG ? operation.status : 'running';
  const config = STATUS_CONFIG[status];
  const isFailed = operation.status === 'failed';

  const wrapperProps = isFailed
    ? { role: 'alert' as const }
    : { 'aria-live': 'polite' as const };

  return (
    <div {...wrapperProps} className="rounded-lg border p-4">
      <div className={`flex items-center gap-2 font-medium ${config.variant}`}>
        <span aria-hidden="true">{config.icon}</span>
        <span>{t(`operation-status-${status}`)}</span>
      </div>

      {outcomeSummary && <div className="mt-2 text-sm">{outcomeSummary}</div>}

      {pollingProblem && (
        <div className="mt-2 text-sm text-amber-600">
          {/* A poll-transport failure; its title is a diagnostic, not copy. */}
          <span>{t('workspace-connection-issue')} </span>
          <span>{t('operation-status-polling-problem')}</span>
        </div>
      )}

      {actions && <div className="mt-3">{actions}</div>}
    </div>
  );
}
