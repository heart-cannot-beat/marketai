import { useTranslation } from 'next-i18next';
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import { CostDisplay } from '@/components/generation/shared/CostDisplay';
import { qcStatus } from '@/lib/assets/status-labels';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';

interface AttemptCost {
  amount: string;
  currency: string;
}

/** `AttemptSummary` (`lib/domain-client/generated/domain-v1.d.ts`) -- the
 * latest provider attempt state for a RunItem. */
interface AttemptSummary {
  dispatch_certainty: string;
  outcome: string;
  provider: string;
  model?: string | null;
  version: string;
  cost?: AttemptCost | null;
}

/** `QcResultSummary` -- `state` is one of `pending`/`running`/`passed`/
 * `failed_blocking`/`failed_nonblocking`/`uncertain`
 * (`ck_asset_qc_attempt_state`, `app/generation/models.py`). */
interface QcResultSummary {
  state: string;
}

/** Subset of `GenerationRunItemDetail` (`get_generation_run_item`) this
 * panel renders. */
interface RunItemDetailItem {
  ordinal: number;
  state: string;
  execution_mode: string;
  error_summary?: string;
  /** The code leading `error_summary`; the summary is a diagnostic (#253). */
  error_code?: string | null;
  latest_attempt?: AttemptSummary | null;
  qc_result?: QcResultSummary | null;
  bound_asset_version_id?: string | null;
}

interface RunItemDetailProps {
  item: RunItemDetailItem;
}

/**
 * Full detail for the RunItem currently selected in `RunItemGrid`: state,
 * execution mode, the latest provider attempt (dispatch certainty, outcome,
 * provider/model/version, cost), the latest QC result, the bound asset
 * version (once one exists), and an error summary for terminal-failed items.
 */
export function RunItemDetail({ item }: RunItemDetailProps) {
  const { t } = useTranslation('common');
  const qcPassed = item.qc_result?.state === 'passed';
  // The QC state reads as its Chinese label (Issue #191), never the enum.
  const qc = qcStatus(item.qc_result?.state);

  return (
    <div className="card bg-base-100 shadow-sm">
      <div className="card-body">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">
            {t('generation-run-item-heading', { ordinal: item.ordinal + 1 })}
          </h3>
          <RunItemStateBadge state={item.state} />
        </div>

        <ExecutionModeTag mode={item.execution_mode} />

        {item.latest_attempt && (
          <div className="mt-3 space-y-1 text-sm">
            <p>
              <strong>{t('generation-run-item-dispatch-label')}</strong>{' '}
              {item.latest_attempt.dispatch_certainty}
            </p>
            <p>
              <strong>{t('generation-run-item-outcome-label')}</strong>{' '}
              {item.latest_attempt.outcome}
            </p>
            <p>
              <strong>{t('generation-run-item-provider-label')}</strong>{' '}
              {item.latest_attempt.provider} / {item.latest_attempt.model} /{' '}
              {item.latest_attempt.version}
            </p>
            {item.latest_attempt.cost && (
              <p>
                <strong>{t('generation-run-item-cost-label')}</strong>{' '}
                <CostDisplay
                  amount={item.latest_attempt.cost.amount}
                  currency={item.latest_attempt.cost.currency}
                />
              </p>
            )}
          </div>
        )}

        {item.qc_result && (
          <div className="mt-3 text-sm">
            <strong>{t('generation-run-item-qc-label')}</strong>{' '}
            <span
              className={qcPassed ? 'text-success' : 'text-error'}
              data-testid="run-item-qc-state"
              data-state={item.qc_result.state}
            >
              {t(qc.labelKey)}
            </span>
          </div>
        )}

        {item.bound_asset_version_id && (
          <div className="mt-2 text-sm">
            <strong>{t('generation-run-item-asset-label')}</strong>{' '}
            <code className="text-xs">{item.bound_asset_version_id}</code>
          </div>
        )}

        {item.error_summary && (
          <div className="mt-3 alert alert-error text-sm" role="alert">
            {errorCodeMessage(t, item.error_code)}
          </div>
        )}
      </div>
    </div>
  );
}
