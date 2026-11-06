import { useCallback } from 'react';
import { useTranslation } from 'next-i18next';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import {
  describeRun,
  type RunListEntry,
  type RunOrigin,
} from '@/lib/assets/run-display';

interface RunSummary extends RunListEntry {
  provider: string;
  model: string;
  status: string;
  total: number;
  done: number;
  failed: number;
  execution_mode: string;
  created_at: string;
}

interface BatchRailProps {
  runs: RunSummary[];
  activeRunId: string | undefined;
  onSelectRun: (runId: string | undefined) => void;
  isLoading: boolean;
  /** run id -> the test plan it came from (`useRunOrigins`). */
  origins?: ReadonlyMap<string, RunOrigin>;
  /** A product's display name (`useProductDirectory().nameFor`):
   * `undefined` while loading, `null` when unknown. */
  productNameFor?: (
    productId: string | null | undefined
  ) => string | null | undefined;
}

/**
 * The Assets page's batch rail (批次). Each Run reads as product / test,
 * origin and time, a Chinese status and done/total (Issue #191); the `gr_`
 * id is only the button's tooltip.
 */
export function BatchRail({
  runs,
  activeRunId,
  onSelectRun,
  isLoading,
  origins,
  productNameFor,
}: BatchRailProps) {
  const { t } = useTranslation('assets');
  const { t: tc } = useTranslation('common');
  const handleClick = useCallback(
    (runId: string) => {
      onSelectRun(activeRunId === runId ? undefined : runId);
    },
    [activeRunId, onSelectRun]
  );

  return (
    <nav className="flex flex-col gap-1 p-2" aria-label={t('runs-nav-label')}>
      <h2 className="text-xs font-semibold uppercase tracking-wide text-base-content/50 px-2 py-1">
        {t('runs-heading')}
      </h2>

      {isLoading &&
        Array.from({ length: 3 }).map((_, i) => (
          <div
            key={i}
            data-testid="rail-skeleton"
            className="h-16 rounded-lg bg-base-200 animate-pulse"
          />
        ))}

      {runs.map((run) => {
        const isActive = activeRunId === run.id;
        const display = describeRun(
          run,
          origins?.get(run.id),
          productNameFor ? productNameFor(run.product_id) : null,
          t,
          tc
        );
        return (
          <button
            key={run.id}
            type="button"
            onClick={() => handleClick(run.id)}
            className={`flex flex-col gap-0.5 px-3 py-2 rounded-lg text-left text-sm transition-colors ${
              isActive ? 'bg-primary text-primary-content' : 'hover:bg-base-200'
            }`}
            aria-label={display.ariaLabel}
            aria-pressed={isActive}
            title={run.id}
            data-testid="batch-rail-run"
            data-run-id={run.id}
          >
            <span className="font-medium line-clamp-2">{display.title}</span>
            <span className="text-xs opacity-70 truncate">
              {display.subtitle}
            </span>
            <div className="flex items-center gap-1.5 text-xs">
              <span
                className={`badge badge-xs ${display.badgeClass}`}
                data-testid="batch-rail-status"
                data-status={run.status}
              >
                {display.statusLabel}
              </span>
              <span className="opacity-70">
                <ExecutionModeTag mode={run.execution_mode} />
              </span>
              <span className="ml-auto opacity-70">
                {display.done}/{display.total}
              </span>
            </div>
          </button>
        );
      })}

      {!isLoading && runs.length === 0 && (
        <p className="text-xs text-base-content/50 px-2 py-4 text-center">
          {t('no-runs')}
        </p>
      )}
    </nav>
  );
}
