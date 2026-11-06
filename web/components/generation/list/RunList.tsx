import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { formatDate } from '@/lib/i18n/format';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';
import { RunItemStateBadge } from '@/components/generation/shared/RunItemStateBadge';
import { ProductName } from '@/components/products/ProductName';

/** Raw item shape returned by `list_generation_runs`
 * (`app/generation/run_queries.py::_run_summary`) -- no `title`, only
 * `product_id`, mirroring `list_generation_drafts`'s asymmetry that
 * `DraftList` already works around. */
export interface RunSummaryItem {
  id: string;
  product_id: string;
  status: string;
  done: number;
  total: number;
  created_at: string | null;
}

interface RunListProps {
  runs: RunSummaryItem[];
}

export function RunList({ runs }: RunListProps) {
  const { t, i18n } = useTranslation('common');
  const { buildRunUrl } = useGenerationNav();

  if (runs.length === 0) {
    return (
      <p className="text-sm text-base-content/60">
        {t('generation-list-runs-empty')}
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {runs.map((run) => (
        <li key={run.id} className="card card-compact bg-base-100 shadow-sm">
          <div className="card-body flex-row items-center justify-between">
            <div>
              <Link
                href={buildRunUrl(run.id)}
                className="font-medium hover:underline"
              >
                <ProductName productId={run.product_id} />
              </Link>
              <div className="mt-1 flex items-center gap-2">
                <RunItemStateBadge state={run.status} />
                <span className="text-xs text-base-content/60">
                  {run.done}/{run.total}
                </span>
              </div>
            </div>
            <span className="text-xs text-base-content/60">
              {run.created_at ? formatDate(i18n.language, run.created_at) : ''}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
