import { useTranslation } from 'next-i18next';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';
import { ProductName } from '@/components/products/ProductName';

/** Subset of `GenerationRunDetail` (`get_generation_run`,
 * `lib/domain-client/generated/domain-v1.d.ts`) this card renders. */
interface RunSummaryRun {
  product_id: string;
  provider: string;
  model: string;
  status: string;
  total: number;
  done: number;
  failed: number;
  execution_mode: string;
}

interface RunSummaryProps {
  run: RunSummaryRun;
}

/**
 * Top-of-page card for a `GenerationRunDetail`: product name, Run-level status
 * badge, execution mode/provider/model provenance, and the done/total/failed
 * item counts the Run's `status` is itself derived from (`derive_run_state`,
 * `app/generation/run.py`).
 */
export function RunSummary({ run }: RunSummaryProps) {
  const { t } = useTranslation('common');

  return (
    <div className="card bg-base-100 shadow-sm">
      <div className="card-body">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h2 className="card-title">
              <ProductName productId={run.product_id} />
            </h2>
            <RunItemStateBadge state={run.status} />
          </div>
          <ExecutionModeTag
            mode={run.execution_mode}
            provider={run.provider}
            model={run.model}
          />
        </div>

        <div className="stats stats-horizontal mt-2 shadow-sm">
          <div className="stat">
            <div className="stat-title">{t('generation-run-stat-done')}</div>
            <div className="stat-value text-lg text-success">{run.done}</div>
          </div>
          <div className="stat">
            <div className="stat-title">{t('generation-run-stat-total')}</div>
            <div className="stat-value text-lg">{run.total}</div>
          </div>
          <div className="stat">
            <div className="stat-title">{t('generation-run-stat-failed')}</div>
            <div className="stat-value text-lg text-error">{run.failed}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
